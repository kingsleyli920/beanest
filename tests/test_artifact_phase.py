"""Real room/storage stage handoff with fake providers and a bounded fake runner."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.agent_registry import default_registry, AgentRegistry
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError
from local_agent_coordinator.project_workspace import ProjectWorkspaceError


class FakeRunner:
    cleanup = True

    async def ready(self):
        return {"available": True}

    async def run(self, project, *, timeout, run_id):
        digest = hashlib.sha256(json.dumps(project, sort_keys=True, ensure_ascii=True,
                                          separators=(",", ":")).encode()).hexdigest()
        return {"status": "completed", "exit_code": 0, "cleanup_confirmed": self.cleanup,
                "run_id": run_id, "revision": project["revision"],
                "snapshot_sha256": digest, "stdout": "fake observation", "stderr": ""}


class ArtifactPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.calls = []
        self.gate = None

        async def invoke(adapter, prompt, cwd, session_id=None, timeout=120):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            self.calls.append(context)
            if self.gate is not None:
                await self.gate.wait()
            files, actions = [], []
            if not context["project"]["files"]:
                files = [{"path": "main.py", "previous_sha256": None, "content": "VALUE = '豆'\n"}]
            if context["job"]["kind"] == "task":
                peer = next(member["id"] for member in context["members"] if member["id"] != context["self"])
                actions = [{"type": "request", "to": peer, "kind": "review", "text": "Review this version."}]
            value = {"message": "A static review only.", "state": "done", "actions": actions,
                     "base_revision": context["proposal_base_revision"], "files": files}
            return {"session_id": session_id or str(uuid5(NAMESPACE_URL, str(cwd))),
                    "status": "completed", "events": [], "usage": None, "text": json.dumps(value)}

        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   registry=default_registry(invoke), runner=FakeRunner())

    async def asyncTearDown(self):
        if self.gate is not None:
            self.gate.set()
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_explicit_long_call_limit_preserves_default_and_hard_ceiling(self):
        self.assertEqual(self.service.call_timeout, 120)
        for invalid in (True, False, 0, -1, 600.1, float('inf'), float('nan'), '600'):
            with self.subTest(timeout=invalid), self.assertRaises(RoomError):
                RoomService(self.root / 'invalid.sqlite3', self.root / 'invalid',
                            registry=default_registry(), call_timeout=invalid)
        extended = RoomService(self.root / 'long.sqlite3', self.root / 'long',
                               registry=default_registry(), call_timeout=600)
        try:
            self.assertEqual(extended.call_timeout, 600)
        finally:
            await extended.shutdown()

    async def settle(self, room_id):
        for _ in range(100):
            observed = await self.service.phase_checkpoint(room_id)
            if observed["settled"]:
                return observed
            if observed["snapshot"]["room"]["status"] in {"failed", "unknown", "budget_exhausted"}:
                self.fail(str(observed["snapshot"]["room"]))
            await asyncio.sleep(.005)
        self.fail("Fake stage did not settle")

    async def first(self):
        room = await self.service.create_artifact_phase("Create a small module.", "claude", "first", ["claude", "codex"])
        return await self.settle(room["room"]["id"])

    async def test_source_is_seeded_before_first_call_with_owner_and_new_sessions(self):
        first = await self.first()
        previous = first["snapshot"]
        room_id = previous["room"]["id"]
        calls_before = len(self.calls)
        second = await self.service.create_artifact_phase("Review and extend the previous module.", "codex", "second",
            ["claude", "codex"], source_room_id=room_id.upper(),
            source_revision=previous["project"]["revision"], source_digest=first["project_digest"])
        observed = await self.settle(second["room"]["id"])
        self.assertEqual(self.calls[calls_before]["project"]["files"], previous["project"]["files"])
        self.assertEqual(self.calls[calls_before]["proposal_base_revision"], 1)
        self.assertEqual(self.calls[calls_before]["current_project_capacity"], {
            "source_bytes": 14, "source_byte_limit": 65536,
            "file_count": 1, "file_limit": 16, "unit": "UTF-8 bytes",
        })
        self.assertEqual(observed["snapshot"]["project"]["files"], previous["project"]["files"])
        old_ids = {member["session_id"] for member in previous["members"]}
        self.assertTrue(old_ids.isdisjoint(member["session_id"] for member in observed["snapshot"]["members"]))
        self.assertEqual((await self.service.phase_checkpoint(room_id))["project_digest"], first["project_digest"])
        count = len(self.calls)
        replay = await self.service.create_artifact_phase("Review and extend the previous module.", "codex", "second",
            ["claude", "codex"], source_room_id=room_id,
            source_revision=previous["project"]["revision"], source_digest=first["project_digest"])
        self.assertEqual(replay["room"]["id"], second["room"]["id"])
        await asyncio.sleep(.01)
        self.assertEqual(len(self.calls), count)

    async def test_source_digest_revision_and_members_are_fail_closed(self):
        first = await self.first()
        args = {"source_room_id": first["snapshot"]["room"]["id"],
                "source_revision": first["snapshot"]["project"]["revision"],
                "source_digest": first["project_digest"]}
        count = len(self.calls)
        cases = [({**args, "source_digest": "0" * 64}, ["claude", "codex"]),
                 ({**args, "source_revision": 99}, ["claude", "codex"]),
                 ({**args, "source_digest": None}, ["claude", "codex"]),
                 (args, ["claude", "missing"]), (args, ["claude", {}])]
        for number, (source, participants) in enumerate(cases):
            with self.subTest(number=number), self.assertRaises(RoomError):
                await self.service.create_artifact_phase("Continue.", "claude", f"bad-{number}", participants, **source)
        self.assertEqual(len(self.service.store.list_rooms()["rooms"]), 1)
        self.assertEqual(len(self.calls), count)

    async def test_checkpoint_waits_for_actual_worker_even_with_canonical_uuid_alias(self):
        self.gate = asyncio.Event()
        room = await self.service.create_artifact_phase("Create a module.", "claude", "waiting", ["claude", "codex"])
        await asyncio.sleep(.005)
        observed = await self.service.phase_checkpoint(room["room"]["id"].upper())
        self.assertFalse(observed["settled"])
        self.assertIsNone(observed["project_digest"])
        self.gate.set()
        await self.settle(room["room"]["id"])

    async def test_adapter_change_prevents_handoff_without_new_call(self):
        first = await self.first()
        self.service.registry = AgentRegistry(replace(spec, adapter="changed")
            if spec.id == "claude" else spec for spec in
            (self.service.registry.get(key) for key in self.service.registry.ids()))
        with self.assertRaisesRegex(RoomError, "phase_source_unconfirmed"):
            await self.service.create_artifact_phase("Continue.", "codex", "changed", ["claude", "codex"],
                source_room_id=first["snapshot"]["room"]["id"],
                source_revision=first["snapshot"]["project"]["revision"], source_digest=first["project_digest"])
        self.assertEqual(len(self.service.store.list_rooms()["rooms"]), 1)

    async def test_origin_is_in_store_idempotency_fingerprint(self):
        origin = {"room_id": str(uuid5(NAMESPACE_URL, "origin")), "revision": 1, "sha256": "a" * 64}
        args = dict(mode="python_artifacts", participants=["claude", "codex"], artifact_origin=origin)
        created, fresh = self.service.store.create_room("Goal.", "claude", "origin-key", **args)
        self.assertTrue(fresh)
        replay, fresh = self.service.store.create_room("Goal.", "claude", "origin-key", **args)
        self.assertEqual(replay, created)
        self.assertFalse(fresh)
        with self.assertRaisesRegex(RoomError, "request_conflict"):
            self.service.store.create_room("Goal.", "claude", "origin-key", **{**args, "artifact_origin": {**origin, "revision": 2}})

    async def test_unconfirmed_seed_never_dispatches_a_model(self):
        first = await self.first()
        count = len(self.calls)
        with patch("local_agent_coordinator.room_service.ProjectWorkspace.seed_python",
                   side_effect=ProjectWorkspaceError("project_commit_unconfirmed")):
            with self.assertRaisesRegex(RoomError, "project_initialization_unconfirmed"):
                await self.service.create_artifact_phase("Continue.", "codex", "uncertain", ["claude", "codex"],
                    source_room_id=first["snapshot"]["room"]["id"],
                    source_revision=first["snapshot"]["project"]["revision"], source_digest=first["project_digest"])
        await asyncio.sleep(.01)
        self.assertEqual(len(self.calls), count)
        child = next(room for room in self.service.store.list_rooms()["rooms"] if room["id"] != first["snapshot"]["room"]["id"])
        observed = await self.service.phase_checkpoint(child["id"])
        self.assertEqual(observed["snapshot"]["room"]["status"], "unknown")
        self.assertFalse(observed["settled"])

    async def test_changed_source_read_is_rejected_before_child_creation(self):
        first = await self.first()
        source_id = first["snapshot"]["room"]["id"]
        project = self.service._project(source_id)
        original = project.snapshot

        def changed_read(revision=None):
            value = original(revision=revision)
            if revision is not None:
                value["files"][0]["content"] = "VALUE = 2\n"
                value["files"][0]["sha256"] = hashlib.sha256(b"VALUE = 2\n").hexdigest()
            return value

        with patch.object(project, "snapshot", side_effect=changed_read):
            with self.assertRaisesRegex(RoomError, "phase_source_unconfirmed"):
                await self.service.create_artifact_phase("Continue.", "codex", "changed-read", ["claude", "codex"],
                    source_room_id=source_id, source_revision=first["snapshot"]["project"]["revision"],
                    source_digest=first["project_digest"])
        self.assertEqual(len(self.service.store.list_rooms()["rooms"]), 1)

    async def test_shutdown_reports_confirmed_cleanup_for_a_settled_stage(self):
        await self.first()
        await self.service.shutdown()
        self.assertIs(self.service.shutdown_cleanup_confirmed, True)

    async def test_shutdown_keeps_unknown_execution_cleanup_false_after_tasks_end(self):
        self.service._runner_instance.cleanup = False
        room = await self.service.create_artifact_phase("Create a module.", "claude", "bad-cleanup", ["claude", "codex"])
        for _ in range(100):
            snapshot = await self.service.read_room(room["room"]["id"])
            if snapshot["room"]["status"] == "unknown":
                break
            await asyncio.sleep(.005)
        self.assertEqual(snapshot["room"]["status"], "unknown")
        await self.service.shutdown()
        self.assertIs(self.service.shutdown_cleanup_confirmed, False)
