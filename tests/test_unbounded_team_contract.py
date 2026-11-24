"""Beanest sets no count or size cap on a plan, a task, a reply or a room.

The agents have none of these limits, so the room must not add them. Each
test drives the real store or service with work just past an old cap.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_response_schema import response_schema
from local_agent_coordinator.room_service import _response
from local_agent_coordinator.room_store import RoomStore
import test_native_room_service as native
import test_room_service as plain
from test_team_store import proposal
import test_team_store as store_fixtures

HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
if HAS_JSONSCHEMA:
    from jsonschema import Draft202012Validator


class UnboundedPlanTests(unittest.TestCase):
    finish = store_fixtures.TeamStoreTests.finish
    task = store_fixtures.TeamStoreTests.task

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = RoomStore(Path(self.directory.name) / "rooms.sqlite3", clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def plan_author_job(self):
        room_id = self.store.create_room("A large shared deliverable", "claude", str(uuid4()), collaboration=True)[0]
        return room_id, self.store.claim(room_id, "claude")

    def test_a_plan_of_150_tasks_with_a_70_dependency_task_is_recorded_whole(self):
        # Catches the 100-command reply cap and the 64-dependency task cap refusing a large plan.
        room_id, author = self.plan_author_job()
        tasks = [proposal("T%03d" % number) for number in range(1, 150)]
        tasks.append(proposal("T150", dependencies=["T%03d" % number for number in range(1, 71)]))
        self.finish(author, tasks + [{"type": "claim", "task_id": "T001"}, {"type": "plan_ready"}])
        board = self.store.snapshot(room_id)["team"]["tasks"]
        self.assertEqual(len(board), 150)
        self.assertEqual(len(self.task(room_id, "T150")["dependencies"]), 70)
        self.assertEqual(self.task(room_id, "T150")["dependencies"][-1], "T070")

    def test_a_task_with_1500_write_paths_is_recorded_with_every_path(self):
        # Catches the 1,000-path cap on one task's write_paths.
        room_id, author = self.plan_author_job()
        paths = ["src/module_%04d.py" % number for number in range(1500)]
        self.finish(author, [proposal("wide", paths=paths), {"type": "plan_ready"}])
        recorded = self.task(room_id, "wide")["write_paths"]
        self.assertEqual(len(recorded), 1500)
        self.assertEqual((recorded[0], recorded[-1]), ("src/module_0000.py", "src/module_1499.py"))

    def test_250000_character_task_text_and_block_reason_are_kept_in_full(self):
        # Catches the 100,000-character cap on what a partner writes in a task or a block reason.
        room_id, author = self.plan_author_job()
        long_description = "设计说明。" * 50_000
        task = {**proposal("long"), "description": long_description, "acceptance": "a" * 250_000}
        self.finish(author, [task, {"type": "claim", "task_id": "long"}, {"type": "plan_ready"}])
        peer = self.store.claim(room_id, "codex")
        self.finish(peer, [{"type": "plan_ready"}])
        confirmation = self.store.claim(room_id, "codex")
        self.finish(confirmation, [{"type": "plan_ready"}])
        recorded = self.task(room_id, "long")
        self.assertEqual((len(recorded["description"]), len(recorded["acceptance"])), (250_000, 250_000))
        work = self.store.claim(room_id, "claude")
        reason = "被这个外部依赖挡住了。" * 25_000
        self.assertEqual(len(reason), 275_000)
        self.finish(work, [{"type": "block", "task_id": "long", "reason": reason, "cause": "technical"}])
        self.assertEqual(self.task(room_id, "long")["blocked_reason"], reason)


class UnboundedReplyTests(unittest.TestCase):
    MEMBERS = tuple("partner_%02d" % number for number in range(40))

    def test_a_250000_character_message_is_parsed_in_full(self):
        # Catches the 100,000-character cap on a partner's visible message.
        message = "这一轮做了什么、为什么这样做。" * 16_667
        self.assertGreater(len(message), 250_000)
        raw = json.dumps({"message": message, "state": "done", "commands": []}, ensure_ascii=False)
        self.assertEqual(_response(raw, "claude", ("claude", "codex"), team=True)["message"], message)

    def test_a_forty_member_room_gets_a_schema_and_its_replies_parse(self):
        # Catches the 32-member cap in the schema handed to partners and in reply parsing.
        schema = response_schema(project=False, member_ids=self.MEMBERS, provider="partner_00")
        self.assertEqual(len(schema["properties"]["actions"]["items"]["properties"]["to"]["enum"]), 39)
        team = response_schema(project=False, member_ids=self.MEMBERS, provider="partner_00", collaboration=True)
        self.assertIn("commands", team["properties"])
        raw = json.dumps({"message": "Asking the last partner.", "state": "working", "actions": [
            {"type": "request", "to": "partner_39", "kind": "help", "text": "Please check the parser."}]})
        parsed = _response(raw, "partner_00", self.MEMBERS)
        self.assertEqual(parsed["action"]["to"], "partner_39")

    @unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is optional")
    def test_the_native_schema_accepts_1200_unchanged_paths_and_150_commands(self):
        # Catches maxItems derived from the old path and command caps in the native schema.
        schema = response_schema(project=True, native=True, collaboration=True, member_ids=("claude", "codex"),
                                 provider="claude", stable=True)
        validator = Draft202012Validator(schema)
        null_command = {field: None for field in schema["properties"]["commands"]["items"]["properties"]}
        reply = {"message": "Many.", "state": "done", "base_revision": 0, "files": [], "edits": None,
                 "commands": [{**null_command, "type": "plan_ready"}] * 150,
                 "unchanged_paths": [{"path": "f%04d.txt" % number, "reason": "Left as found."} for number in range(1200)],
                 "resolved_conflicts": ["f%04d.txt" % number for number in range(1200)]}
        self.assertEqual([error.message for error in validator.iter_errors(reply)], [])


class NativeManyChangedFilesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    create = native.NativeRoomServiceTests.create
    handler = native.NativeRoomServiceTests.handler

    async def test_a_work_call_that_changed_1200_new_files_publishes_all_of_them(self):
        # Catches the host trimming free files at the 1,000-path cap or giving up at 100 commands,
        # which left changed files blocked and failed the call with native_changes_not_imported.
        original = self.handler

        def handler(agent, context, cwd):
            value = original(agent, context, cwd)
            if context["job"]["job_phase"] == "work":
                generated = cwd / "generated"
                generated.mkdir(exist_ok=True)
                for number in range(1200):
                    (generated / ("part_%04d.txt" % number)).write_text("part %d\n" % number)
            return value

        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete", result["team"])
        self.assertIsNone(result["team"]["stop_reason"])
        paths = result["team"]["tasks"][0]["write_paths"]
        self.assertEqual(len(paths), 1202)
        self.assertEqual(paths[:3], ["main.py", "notes.md", "generated/part_0000.txt"])
        self.assertEqual([item for state in result["native_workspaces"] for item in state["blocked"]], [])
        self.assertNotIn("native_changes_not_imported", json.dumps([job.get("error") for job in result["jobs"]]))
        project = self.service._project(created["room"]["id"])
        last = project.files_for_paths(["generated/part_1199.txt"])
        self.assertEqual(last[0]["content"], "part 1199\n")


class PlainTextRoomLongReplyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = plain.RoomServiceTests.asyncSetUp
    asyncTearDown = plain.RoomServiceTests.asyncTearDown
    settle = plain.RoomServiceTests.settle
    create = plain.RoomServiceTests.create

    async def test_a_50000_character_reply_in_a_plain_text_room_is_kept_whole(self):
        # Catches the plain text room cutting a partner's reply to 8,000 characters.
        prose = ("第一部分先说结论，再给出每一步的理由。" * 2_700)[:50_000]
        self.assertEqual(len(prose), 50_000)
        self.fake.responses["claude"] = [plain.answer(prose)]
        room = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual(snapshot["jobs"][0]["error"], None)
        self.assertTrue(any(event.get("text") == prose for event in snapshot["events"]))

    async def test_a_50000_character_reply_that_is_not_json_is_still_shown_whole(self):
        # Catches the raw reply of a plain text room being cut to 8,000 characters when it cannot be parsed.
        prose = "这是一段没有按格式写的长回答。" * 3_334
        self.assertGreater(len(prose), 50_000)
        self.fake.responses["claude"] = [plain.answer(text=prose)]
        room = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual(snapshot["jobs"][0]["error"], "invalid_agent_response")
        self.assertTrue(any(event.get("text") == prose for event in snapshot["events"]))


if __name__ == "__main__":
    unittest.main()
