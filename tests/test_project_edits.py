"""Literal edits through actual parsing, SQLite, versioned files and CAS."""
import asyncio
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import room_service
from local_agent_coordinator.collaboration_view import collaboration_view
from local_agent_coordinator.project_edits import ProjectEditError, expand_edits
from local_agent_coordinator.room_response_schema import response_schema
from local_agent_coordinator.team_store import FORMAT_ERRORS
from tests import test_collaboration_view as views
from tests import test_team_runs as runs
from tests import test_team_service as fixtures
from tests.test_team_protocol import HAS_JSONSCHEMA

if HAS_JSONSCHEMA:
    from jsonschema import Draft202012Validator


def file(path, content, owner="claude"):
    return {"path": path, "content": content, "owner": owner,
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


def edit(source, old, new):
    return {"path": source["path"], "base_sha256": source["sha256"], "old_text": old, "new_text": new}


class LiteralEditTests(unittest.TestCase):
    def assert_code(self, code, snapshot, edits):
        before = copy.deepcopy(snapshot)
        with self.assertRaises(ProjectEditError) as raised:
            expand_edits(snapshot, edits)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(snapshot, before)
        self.assertNotIn(code, FORMAT_ERRORS)

    def test_simultaneous_literal_multifile_replacements_preserve_utf8_and_crlf(self):
        a, b = file("a.py", "alpha|beta\r\n汉\r\n"), file("b.py", "DELETE = 1\n")
        snapshot = {"revision": 4, "files": [a, b]}
        before = copy.deepcopy(snapshot)
        files = expand_edits(snapshot, [edit(a, "alpha", "beta"), edit(a, "beta", "gamma"),
            edit(a, "汉\r\n", "🙂\r\n"), edit(b, "DELETE = 1\n", "")])
        self.assertEqual(files, [{"path": "a.py", "previous_sha256": a["sha256"], "content": "beta|gamma\r\n🙂\r\n"},
                                 {"path": "b.py", "previous_sha256": b["sha256"], "content": ""}])
        self.assertEqual(snapshot, before)

    def test_nonunique_including_overlapping_occurrences_and_overlapping_edits_are_rejected(self):
        source = file("a.py", "aaa")
        self.assert_code("project_edit_ambiguous", {"files": [source]}, [edit(source, "aa", "b")])
        source = file("a.py", "abcdef")
        self.assert_code("project_edit_overlap", {"files": [source]}, [edit(source, "abc", "x"), edit(source, "bcd", "y")])
        self.assert_code("project_edit_conflict", {"files": [source]}, [edit(source, "missing", "x")])
        self.assert_code("project_edit_conflict", {"files": [source]}, [{**edit(source, "abc", "x"), "base_sha256": "0" * 64}])

    def test_closed_shapes_count_and_utf8_byte_limit_are_enforced(self):
        source = file("a.py", "one")
        for invalid in ([], [edit(source, "one", "two")] * 17, [edit(source, "", "x")],
                        [edit(source, "one", "one")], [{**edit(source, "one", "two"), "extra": True}],
                        [edit(source, "one", None)], [edit(source, "one", "\0")]):
            self.assert_code("invalid_project_edits", {"files": [source]}, invalid)
        self.assert_code("project_edit_limit", {"files": [source]}, [edit(source, "one", "界" * 11000)])

    @unittest.skipUnless(HAS_JSONSCHEMA, "JSON Schema validator available with MCP extra")
    def test_schema_requires_nullable_edits_for_native_team_project_only_and_parser_keeps_custom_compatibility(self):
        base = {"message": "Concrete result", "state": "done", "commands": [], "base_revision": 0, "files": []}
        options = {"project": True, "member_ids": ("claude", "codex"), "provider": "claude", "collaboration": True}
        for phase in ("work", "review", "plan", "coordinate", "help", "user"):
            schema = response_schema(**options, team_phase=phase)
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            self.assertTrue(validator.is_valid({**base, "edits": None}))
            self.assertTrue(validator.is_valid({**base, "edits": []}))
            payload = {**base, "edits": [edit(file("a.py", "old"), "old", "new")]}
            self.assertEqual(validator.is_valid(payload), phase == "work")
            self.assertFalse(validator.is_valid(base))
        parsed = room_service._response(json.dumps(base), "claude", ("claude", "codex"), team=True, project=True)
        self.assertEqual(parsed["edits"], [])
        self.assertNotIn("edits", response_schema(**{**options, "collaboration": False})["properties"])
        self.assertNotIn("edits", response_schema(**{**options, "project": False})["properties"])
        for invalid in ({**base, "edits": {}}, {**base, "edits": [edit(file("a.py", "old"), "old", "new")],
                        "files": [{"path": "a.py", "previous_sha256": None, "content": "new"}]}):
            with self.assertRaises(room_service.RoomError) as raised:
                room_service._response(json.dumps(invalid), "claude", ("claude", "codex"), team=True, project=True)
            self.assertEqual(raised.exception.code, "invalid_project_edits")

    def test_edit_errors_have_natural_delivery_reasons(self):
        for code in ("invalid_project_edits", "project_edit_conflict", "project_edit_ambiguous", "project_edit_overlap", "project_edit_limit"):
            value = views.snapshot(phase="stopped", status="blocked")
            value["team"]["stop_reason"] = code
            reason = collaboration_view(value)["outcome"]["reason"]
            self.assertIn("未接受", reason)
            self.assertNotIn(code, reason)


class TeamEditPipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runs.TeamRunTests.asyncSetUp
    asyncTearDown = runs.TeamRunTests.asyncTearDown
    settle = runs.TeamRunTests.settle
    scenario = runs.TeamRunTests.scenario
    create = runs.TeamRunTests.create
    next_run = runs.TeamRunTests.next_run

    def edited_reply(self, context, old="1", new="2", *, path=None):
        path = path or context["task"]["write_paths"][0]
        source = next(item for item in context["project"]["files"] if item["path"] == path)
        return {**fixtures.reply(context, commands=[{"type": "complete", "task_id": context["task"]["id"],
            "result": "Applied exact literal change and await independent review."}]), "edits": [edit(source, old, new)]}

    async def test_disjoint_parallel_edits_and_duplicate_receipt_use_existing_atomic_pipeline(self):
        source = await self.create()
        both, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        started, observed, receipts = set(), [], []
        async def work(agent, context, attempt):
            started.add(agent)
            observed.append(context["proposal_base_revision"])
            if len(started) == 2:
                both.set()
            await release.wait()
            return self.edited_reply(context)
        scenario = self.scenario(2)
        scenario.work = work
        original_finish = self.service._finish
        def finish(job, result):
            if json.loads(result["text"]).get("edits"):
                receipts.append((copy.deepcopy(job), copy.deepcopy(result)))
            original_finish(job, result)
        with patch.object(self.service, "_finish", side_effect=finish):
            await self.next_run(source, 2)
            await asyncio.wait_for(both.wait(), 3)
            release.set()
            await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(observed, [source["project"]["revision"]] * 2)
        self.assertEqual(final["team"]["phase"], "ready_for_user")
        self.assertEqual(final["project"]["revision"], source["project"]["revision"] + 2)
        self.assertEqual({item["path"]: item["content"] for item in final["project"]["files"]},
                         {"main.py": "VALUE = 2\n", "checks.py": "EXPECTED = 2\n"})
        before = self.service._project(source["room"]["id"]).snapshot()
        self.assertEqual(len(receipts), 2)
        for job, result in receipts:
            original_finish(job, result)
        self.assertEqual(self.service._project(source["room"]["id"]).snapshot(), before)
        self.assertEqual(len(scenario.reviews), 2)

    async def rejected_edit(self, factory, expected, *, source=None):
        source = source or await self.create()
        original = copy.deepcopy(source["project"]["files"])
        peer = asyncio.Event()
        async def work(agent, context, attempt):
            if agent == "codex":
                await peer.wait()
            return factory(context)
        self.scenario(2).work = work
        await self.next_run(source, 2)
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["project"]["files"], original)
        self.assertEqual(final["project"]["revision"], source["project"]["revision"])
        self.assertEqual(final["team"]["stop_reason"], expected)
        self.assertFalse(any(event["kind"].startswith("format_correction") for event in final["events"]))
        return final

    async def test_one_bad_edit_rejects_entire_batch_without_pending_artifact_or_format_retry(self):
        def bad(context):
            result = self.edited_reply(context)
            result["edits"].append({**result["edits"][0], "old_text": "not present"})
            return result
        final = await self.rejected_edit(bad, "project_edit_conflict")
        self.assertFalse(any(event["kind"] == "artifact_pending" and event.get("run_id") == final["current_run"]["id"]
                             for event in final["events"]))

    async def test_cross_owner_task_scope_is_rejected_before_publish(self):
        await self.rejected_edit(lambda context: self.edited_reply(context, path="checks.py"), "team_scope_conflict")

    async def test_reconstructed_whole_file_secret_is_checked_across_edit_boundary(self):
        self.scenario(1).contents["main"] = "# " + "sk" + "-short\n"
        created = await self.service.create_room("Secret boundary", "all", str(uuid4()), collaboration=True, mode="python_artifacts")
        await self.settle()
        source = await self.service.read_room(created["room"]["id"])
        await self.rejected_edit(lambda context: self.edited_reply(context, "short", "abcdefghijklmnop"),
                                 "artifact_sensitive_content", source=source)

    async def test_small_edit_cannot_expand_existing_project_past_64kib(self):
        scenario = self.scenario(1)
        content = "VALUE = 1\n#" + "x" * 64900 + "\n"
        scenario.contents["main"] = content
        async def work(agent, context, attempt):
            if agent == "claude":
                return fixtures.reply(context, commands=[{"type": "complete", "task_id": "main", "result": "Delivered owned large file"}],
                    files=[fixtures.changed_file(context, "main.py", content)])
        scenario.work = work
        created = await self.service.create_room("Size boundary", "all", str(uuid4()), collaboration=True, mode="python_artifacts")
        await self.settle()
        source = await self.service.read_room(created["room"]["id"])
        self.assertEqual(source["team"]["phase"], "ready_for_user")
        await self.rejected_edit(lambda context: self.edited_reply(context, "1", "2" * 2000),
                                 "project_limit_exceeded", source=source)

    async def test_custom_review_response_cannot_bypass_nonwork_edit_restriction(self):
        source = await self.create()
        scenario = self.scenario(2)
        async def handler(agent, context):
            result = await scenario(agent, context)
            if context["job"]["job_phase"] == "review":
                path = context["task"]["write_paths"][0]
                original = next(item for item in context["project"]["files"] if item["path"] == path)
                result["edits"] = [edit(original, "2", "999")]
            return result
        self.provider.handler = handler
        await self.next_run(source, 2)
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["team"]["stop_reason"], "team_files_forbidden")
        self.assertTrue(all("999" not in item["content"] for item in final["project"]["files"]))

    async def test_stale_same_file_aba_is_not_treated_as_a_clean_base(self):
        source = await self.create()
        entered, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        async def work(agent, context, attempt):
            if agent == "claude":
                entered.set()
            await release.wait()
            if agent == "codex":
                await asyncio.Event().wait()
            return self.edited_reply(context)
        self.scenario(2).work = work
        await self.next_run(source, 2)
        await asyncio.wait_for(entered.wait(), 3)
        project = self.service._project(source["room"]["id"])
        base = project.snapshot()
        original = next(item for item in base["files"] if item["path"] == "main.py")
        changed = project.apply("claude", str(uuid4()), base["revision"],
            [{"path": "main.py", "previous_sha256": original["sha256"], "content": "VALUE = 8\n"}])
        project.apply("claude", str(uuid4()), changed["revision"], [{"path": "main.py", "content": original["content"],
            "previous_sha256": next(item for item in changed["files"] if item["path"] == "main.py")["sha256"]}])
        release.set()
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["team"]["stop_reason"], "project_file_conflict")
        self.assertEqual(final["project"]["files"], base["files"])
        self.assertEqual(final["project"]["revision"], base["revision"] + 2)

    async def test_unknown_then_cancelled_peer_returning_late_edits_cannot_publish(self):
        source = await self.create()
        entered, returned = asyncio.Event(), asyncio.Event()
        async def work(agent, context, attempt):
            if agent == "codex":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    returned.set()
                    return self.edited_reply(context)
            await entered.wait()
            return {"adapter_failure": "subscription_limit_reached"}
        self.scenario(2).work = work
        await self.next_run(source, 2)
        await self.settle()
        self.assertTrue(returned.is_set())
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["team"]["phase"], "unknown")
        self.assertEqual(final["project"]["files"], source["project"]["files"])
        self.assertEqual(final["project"]["revision"], source["project"]["revision"])
