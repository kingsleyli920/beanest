"""Artifact-mode integration: real SQLite/FS, fake providers, no code execution."""
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.room_service import RoomService, _response
from local_agent_coordinator.room_store import RoomError


DRAFT = "def doubled(value):\n    return value * 2\n"
REVISED = DRAFT + "\n# Reviewed as text; no execution was performed.\n"
PEER_TEST = (
    "raise AssertionError('artifact data must not execute in this suite')\n\n"
    "def test_doubled():\n    from toy import doubled\n    assert doubled(2) == 4\n"
)


def change(path, content, previous=None):
    return {"path": path, "previous_sha256": previous, "content": content}


def response(context, *, files=(), action=None, message="Reviewed as text only."):
    value = {"message": message, "state": "done", "actions": [action] if action else []}
    if "project" in context:
        value.update(base_revision=context["proposal_base_revision"], files=list(files))
    return value


def collaboration(agent, context):
    kind = context["job"]["kind"]
    if "project" not in context:
        return response(context)
    if kind == "task":
        peer = next(member["id"] for member in context["members"] if member["id"] != agent)
        return response(context, files=[change("toy.py", DRAFT)],
                        action={"type": "request", "to": peer, "kind": "review",
                                "text": "Review the bound artifact and propose an independent test."})
    if kind == "review":
        return response(context, files=[change("test_toy.py", PEER_TEST)])
    previous = next(file for file in context["project"]["files"] if file["path"] == "toy.py")
    return response(context, files=[change("toy.py", REVISED, previous["sha256"])])


class FakeProvider:
    def __init__(self, handler=collaboration):
        self.handler = handler
        self.calls = []

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        self.calls.append({"agent": agent, "prompt": prompt, "context": context,
                           "session_id": session_id, "cwd": cwd})
        value = self.handler(agent, context)
        if inspect.isawaitable(value):
            value = await value
        identity = session_id or str(uuid5(NAMESPACE_URL, str(cwd)))
        return {"session_id": identity, "status": "completed", "usage": None,
                "events": [], "text": json.dumps(value)}


class ArtifactProtocolTests(unittest.TestCase):
    def test_project_response_rejects_extra_keys_duplicates_and_invalid_revision(self):
        valid = {"message": "A visible reply", "state": "done", "actions": [],
                 "base_revision": 0, "files": []}
        invalid = [
            json.dumps({**valid, "command": "/bin/sh"}),
            json.dumps({**valid, "base_revision": True}),
            json.dumps({**valid, "files": {}}),
            json.dumps({**valid, "files": [None] * 17}),
            json.dumps(valid)[:-1] + ', "base_revision": 1}',
        ]
        for raw in invalid:
            with self.subTest(response=raw[:80]):
                with self.assertRaises(RoomError):
                    _response(raw, "claude", ("claude", "codex"), project=True)
        self.assertEqual(_response(json.dumps(valid), "claude", project=True)["files"], [])
        with self.assertRaises(RoomError):
            _response(json.dumps(valid), "claude", project=False)


class RoomArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        # macOS aliases /var to /private/var; the repository rejects symlinked
        # ancestors, so fixtures use the same canonical root as production.
        self.root = Path(self.directory.name).resolve()
        self.database = self.root / "rooms.sqlite3"
        self.workspace = self.root / "workspaces"
        self.fake = FakeProvider()
        self.release_events = []
        self.service = self.make_service()

    def make_service(self):
        return RoomService(self.database, self.workspace, invoke=self.fake)

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        try:
            await self.service.shutdown()
            if self.service._cleanup_task:
                await self.service._cleanup_task
        finally:
            self.directory.cleanup()

    async def settle(self):
        async def drain():
            while True:
                active = [task for task in self.service._workers.values() if not task.done()]
                if not active:
                    return
                await asyncio.gather(*active)
        await asyncio.wait_for(drain(), 3)

    async def create(self, *, target="claude", request_id="artifact-create"):
        return await self.service.create_room("A new fictional Python artifact", target,
                                              request_id, mode="python_artifacts")

    async def restart(self):
        await self.service.shutdown()
        self.service = self.make_service()

    def repository(self, room_id):
        return self.root / "project-artifacts" / room_id

    async def test_both_initial_authors_save_review_and_feedback_as_three_versions(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(initial=initial):
                room = await self.create(target=initial, request_id="start-" + initial)
                room_id = room["room"]["id"]
                await self.settle()
                saved = await self.service.read_room(room_id)
                self.assertEqual(saved["room"]["mode"], "python_artifacts")
                self.assertEqual(saved["room"]["status"], "completed")
                self.assertEqual(saved["project"]["execution"], "disabled")
                self.assertEqual(saved["project"]["revision"], 3)
                files = {file["path"]: file for file in saved["project"]["files"]}
                self.assertEqual(files["toy.py"]["owner"], initial)
                self.assertEqual(files["test_toy.py"]["owner"], peer)
                self.assertEqual(files["toy.py"]["content"], REVISED)
                self.assertEqual(files["test_toy.py"]["content"], PEER_TEST)
                self.assertEqual(files["toy.py"]["sha256"], hashlib.sha256(REVISED.encode()).hexdigest())
                self.assertEqual([job["kind"] for job in saved["jobs"]], ["task", "review", "feedback"])
                calls = [call for call in self.fake.calls if call["cwd"].parent.name == room_id]
                self.assertEqual([call["agent"] for call in calls], [initial, peer, initial])
                self.assertEqual([call["context"]["project"]["revision"] for call in calls], [0, 1, 2])
                self.assertEqual([call["context"]["proposal_base_revision"] for call in calls], [0, 1, 2])
                self.assertEqual(calls[1]["context"]["parent_messages"][0]["artifact_revision"], 1)
                self.assertEqual(calls[2]["context"]["parent_messages"][0]["artifact_revision"], 2)
                artifacts = [event["revision"] for event in saved["events"] if event["kind"] == "artifact"]
                self.assertEqual(artifacts, [1, 2, 3])
                self.assertFalse(list(self.repository(room_id).rglob("*.py")),
                                 "generated Python stays data inside JSON revisions")

    async def test_invalid_artifact_envelope_is_not_dumped_into_normal_chat(self):
        def invalid(agent, context):
            return {"message": "Not a valid response", "state": "invalid-state", "actions": [],
                    "base_revision": 0, "files": [change("toy.py", "RAW_ENVELOPE_MUST_STAY_OUT_OF_CHAT")]}
        self.fake.handler = invalid
        first = await self.create()
        await self.settle()
        saved = await self.service.read_room(first['room']['id'])
        self.assertEqual(saved['room']['status'], 'failed')
        self.assertEqual(saved['project']['revision'], 0)
        self.assertNotIn('RAW_ENVELOPE_MUST_STAY_OUT_OF_CHAT', json.dumps(saved['events']))
        self.assertEqual([event['sender'] for event in saved['events'] if event['kind'] == 'message'], ['user'])

    async def test_mode_is_idempotent_but_changing_it_conflicts(self):
        first = await self.create()
        second = await self.create()
        self.assertEqual(first["room"]["id"], second["room"]["id"])
        with self.assertRaises(RoomError) as error:
            await self.service.create_room("A new fictional Python artifact", "claude", "artifact-create")
        self.assertEqual(error.exception.code, "request_conflict")
        with self.assertRaises(RoomError) as error:
            await self.create(target="all", request_id="all-not-an-author")
        self.assertEqual(error.exception.code, "project_requires_first_author")
        await self.settle()
        self.assertEqual(len(self.fake.calls), 3)

    async def test_old_rooms_migrate_to_text_without_dispatch_or_artifact_initialization(self):
        room = await self.service.create_room("Legacy text", "claude", "legacy-create")
        await self.settle()
        with self.service.store._connection(write=True) as database:
            database.execute("ALTER TABLE rooms DROP COLUMN mode")
        await self.restart()
        count = len(self.fake.calls)
        saved = await self.service.read_room(room["room"]["id"])
        repeated = await self.service.create_room("Legacy text", "claude", "legacy-create")
        self.assertEqual(saved["room"]["mode"], "text")
        self.assertEqual(repeated["room"]["id"], room["room"]["id"])
        self.assertNotIn("project", saved)
        self.assertFalse((self.root / "project-artifacts").exists())
        self.assertEqual(len(self.fake.calls), count)

    async def test_peer_cannot_overwrite_the_authors_file(self):
        def overwrite(agent, context):
            if context["job"]["kind"] != "review":
                return collaboration(agent, context)
            original = context["project"]["files"][0]
            return response(context, files=[change("toy.py", "VALUE = 99\n", original["sha256"])])
        self.fake.handler = overwrite
        room = await self.create()
        await self.settle()
        saved = await self.service.read_room(room["room"]["id"])
        self.assertEqual(saved["jobs"][-1]["error"], "project_owner_conflict")
        self.assertEqual(saved["project"]["revision"], 1)
        self.assertEqual(saved["project"]["files"][0]["content"], DRAFT)
        self.assertEqual(saved["project"]["files"][0]["owner"], "claude")

    async def test_changed_head_rejects_a_stale_provider_proposal(self):
        started, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)

        async def delayed_review(agent, context):
            if context["job"]["kind"] == "review":
                started.set()
                await release.wait()
            return collaboration(agent, context)
        self.fake.handler = delayed_review
        room = await self.create()
        room_id = room["room"]["id"]
        await asyncio.wait_for(started.wait(), 1)
        self.service._project(room_id).apply("claude", "concurrent-fixture", 1,
                                            [change("notes.py", "NOTE = 'fictional'\n")])
        release.set()
        await self.settle()
        saved = await self.service.read_room(room_id)
        self.assertEqual(saved["project"]["revision"], 2)
        self.assertEqual(saved["jobs"][-1]["error"], "project_revision_conflict")
        self.assertEqual({file["path"] for file in saved["project"]["files"]}, {"toy.py", "notes.py"})

    async def test_an_old_parent_is_inspected_without_claiming_a_newer_revision(self):
        original_finish = self.service.store.finish_job
        advanced = False

        def advance_after_author(job_id, **kwargs):
            nonlocal advanced
            original_finish(job_id, **kwargs)
            if kwargs.get("artifact_revision") == 1 and not advanced:
                advanced = True
                room_id = self.service.store.list_rooms()["rooms"][0]["id"]
                project = self.service._project(room_id)
                previous = project.snapshot()["files"][0]
                project.apply("claude", "newer-fixture", 1,
                              [change("toy.py", REVISED, previous["sha256"])])

        def review_only(agent, context):
            if context["job"]["kind"] == "task":
                return collaboration(agent, context)
            return response(context, message="I inspected the supplied version only; no tests ran.")
        self.fake.handler = review_only
        with patch.object(self.service.store, "finish_job", advance_after_author):
            room = await self.create()
            await self.settle()
        saved = await self.service.read_room(room["room"]["id"])
        review = self.fake.calls[1]["context"]
        self.assertEqual(review["project"]["revision"], 1)
        self.assertEqual(review["project"]["files"][0]["content"], DRAFT)
        self.assertEqual(review["proposal_base_revision"], 2)
        self.assertEqual(review["current_project_files"][0]["sha256"], hashlib.sha256(REVISED.encode()).hexdigest())
        review_id = saved["jobs"][1]["id"]
        message = next(event for event in saved["events"] if event["kind"] == "message" and event["job_id"] == review_id)
        self.assertEqual(message["artifact_revision"], 1)
        self.assertEqual(saved["project"]["revision"], 2)

    async def test_late_result_after_stop_cannot_publish_files(self):
        started, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)

        async def late(agent, context):
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return response(context, files=[change("toy.py", DRAFT)])

        async def do_not_wait_for_stubborn(tasks, timeout):
            return set(), set(tasks)

        self.fake.handler = late
        room = await self.create()
        await asyncio.wait_for(started.wait(), 1)
        with patch("local_agent_coordinator.room_service.asyncio.wait", do_not_wait_for_stubborn):
            stopped = await self.service.cancel_room(room["room"]["id"])
        self.assertFalse(stopped["cancellation_confirmed"])
        release.set()
        await self.settle()
        saved = await self.service.read_room(room["room"]["id"])
        self.assertEqual(saved["project"]["revision"], 0)
        self.assertEqual(saved["project"]["files"], [])
        self.assertEqual(saved["room"]["status"], "unknown")
        self.assertFalse(any(event["kind"] == "artifact" for event in saved["events"]))

    async def test_old_default_deadline_does_not_reject_current_delivery(self):
        now = [1000.0]
        self.service.clock = self.service.store.clock = lambda: now[0]

        def expires_during_provider(agent, context):
            now[0] += self.service.store.room_timeout
            return response(context, files=[change("toy.py", DRAFT)])

        self.fake.handler = expires_during_provider
        room = await self.create()
        await self.settle()
        saved = await self.service.read_room(room["room"]["id"])
        self.assertIsNone(saved["jobs"][0]["error"])
        self.assertEqual(saved["project"]["revision"], 1)
        self.assertEqual(saved["project"]["files"][0]["content"], DRAFT)
        self.assertTrue(any(event["kind"] in {"artifact", "artifact_pending"}
                             for event in saved["events"]))
        self.assertEqual(len(self.fake.calls), 1)

    async def test_session_already_owned_by_another_room_cannot_publish_files(self):
        self.fake.handler = lambda agent, context: response(context, files=[change("toy.py", DRAFT)])
        first = await self.create(request_id="first-room")
        await self.settle()
        identity = next(member["session_id"] for member in (await self.service.read_room(first["room"]["id"]))["members"] if member["id"] == "claude")
        original = self.fake

        async def collision(*args, **kwargs):
            result = await original(*args, **kwargs)
            return {**result, "session_id": identity}

        self.service.invoke = collision
        second = await self.create(request_id="second-room")
        await self.settle()
        saved = await self.service.read_room(second["room"]["id"])
        self.assertEqual(saved["jobs"][0]["error"], "session_identity_changed")
        self.assertEqual(saved["project"]["revision"], 0)
        self.assertEqual(saved["project"]["files"], [])
        self.assertFalse(any(event["kind"] == "artifact" for event in saved["events"]))

    async def test_missing_historical_repository_is_reported_without_initializing_it(self):
        room = await self.create()
        await self.settle()
        room_id = room["room"]["id"]
        await self.restart()
        repository = self.repository(room_id)
        shutil.rmtree(repository)
        count = len(self.fake.calls)
        saved = await self.service.read_room(room_id)
        self.assertFalse(saved["project"]["available"])
        self.assertEqual(saved["project"]["execution"], "disabled")
        self.assertFalse(repository.exists())
        await self.service.read_room(room_id)
        self.assertFalse(repository.exists())
        self.assertEqual(len(self.fake.calls), count)

    async def test_missing_manifest_is_not_replaced_with_an_empty_version(self):
        room = await self.create()
        await self.settle()
        room_id = room["room"]["id"]
        await self.restart()
        manifest = self.repository(room_id) / "manifest.json"
        manifest.unlink()
        saved = await self.service.read_room(room_id)
        self.assertFalse(saved["project"]["available"])
        self.assertFalse(manifest.exists())

    async def test_database_failure_after_apply_preserves_unknown_and_does_not_rewrite(self):
        original_finish = self.service.store.finish_job
        failed = False

        def fail_once(job_id, **kwargs):
            nonlocal failed
            if kwargs.get("artifact_revision") == 1 and not failed:
                failed = True
                raise RoomError("storage_unavailable")
            return original_finish(job_id, **kwargs)

        with patch.object(self.service.store, "finish_job", fail_once):
            room = await self.create()
            await self.settle()
        room_id = room["room"]["id"]
        saved = await self.service.read_room(room_id)
        self.assertTrue(failed)
        self.assertEqual(saved["project"]["revision"], 1)
        self.assertEqual(saved["project"]["files"][0]["content"], DRAFT)
        self.assertEqual(saved["jobs"][0]["error"], "project_commit_unconfirmed")
        self.assertEqual(saved["jobs"][0]["status"], "unknown")
        self.assertEqual(saved["room"]["status"], "unknown")
        self.assertTrue(next(member for member in saved["members"] if member["id"] == "claude")["identity_uncertain"])
        self.assertEqual((await self.service.status())["background_error"], "project_commit_unconfirmed")
        self.assertEqual(len(self.fake.calls), 1)
        await self.restart()
        again = await self.service.read_room(room_id)
        self.assertEqual(again["project"]["revision"], 1)
        self.assertEqual(again["jobs"][0]["error"], "project_commit_unconfirmed")
        self.assertEqual(len(self.fake.calls), 1)

    async def test_sensitive_file_proposal_is_rejected_without_persisting_the_content(self):
        marker = "FICTIONAL_VALUE_NOT_A_REAL_CREDENTIAL"
        self.fake.handler = lambda agent, context: response(
            context, files=[change("toy.py", f'API_KEY = "{marker}"\n')])
        room = await self.create()
        await self.settle()
        saved = await self.service.read_room(room["room"]["id"])
        self.assertEqual(saved["jobs"][0]["error"], "artifact_sensitive_content")
        self.assertEqual(saved["project"]["revision"], 0)
        self.assertEqual(saved["project"]["files"], [])
        self.assertNotIn(marker, json.dumps(saved))
        for artifact in self.repository(room["room"]["id"]).rglob("*.json"):
            self.assertNotIn(marker, artifact.read_text())


if __name__ == "__main__":
    unittest.main()
