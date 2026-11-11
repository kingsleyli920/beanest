"""Quitting the app while a native partner is mid-work: synthetic transport,
real SQLite, private trees, job loop and the host's automatic continue."""
import asyncio
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import room_service
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.room_service import RoomService
import test_native_room_service as native_tests
from test_native_room_service import native_reply
from test_team_store import proposal

RETAINED = "Retained codex notes.\n"


class NativeShutdownRetainedWorkTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_tests.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native_tests.NativeRoomServiceTests.asyncTearDown
    settle = native_tests.NativeRoomServiceTests.settle
    handler = native_tests.NativeRoomServiceTests.handler

    def make_service(self):
        return RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", native_invoke=self.fake,
                           registry=default_registry())

    async def host_continue(self, room_id):
        """The host's automatic continue, without its 15-second delay."""
        snapshot = self.service._snapshot(room_id)
        with patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0):
            # The first sighting records the stop; the next one, after the delay, acts.
            self.service._schedule_auto_continue(room_id, snapshot)
            self.service._schedule_auto_continue(room_id, snapshot)
        state = self.service._auto_continues.get(room_id)
        if state and state["task"]:
            await state["task"]
        await self.settle()
        return await self.service.read_room(room_id)

    async def interrupted_room(self):
        """Claude finished T1 and awaits review; codex is mid-T2 with unimported
        edits in its private tree when the service shuts down."""
        self.codex_started = asyncio.Event()
        self.calls = []

        async def handler(agent, context, cwd):
            phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
            inspection = bool(context.get("native_partial_inspection"))
            self.calls.append((agent, "inspection" if inspection else phase, task and task["id"]))
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands.extend([proposal("T1", paths=["main.py"]), proposal("T2", paths=["notes.md"])])
                own = {"claude": "T1", "codex": "T2"}.get(agent)
                if own and (commands or any(item["id"] == own and item["owner"] is None for item in board["tasks"])):
                    commands.append({"type": "claim", "task_id": own})
                return native_reply(context, commands + [{"type": "plan_ready"}])
            if inspection:
                self.assertEqual((cwd / "notes.md").read_text(), RETAINED)
                return native_reply(context, [{"type": "complete", "task_id": task["id"],
                                               "result": "Inspected the retained notes; nothing replayed."}])
            if phase == "work" and agent == "claude":
                (cwd / "main.py").write_text("VALUE = 2\n")
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Updated main."}])
            if phase == "work" and agent == "codex":
                if (cwd / "notes.md").read_text() == RETAINED:
                    return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Finished the notes."}])
                (cwd / "notes.md").write_text(RETAINED)
                self.codex_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    # What a CLI stopped by the service's own shutdown reports:
                    # its tools all returned and its process is gone.
                    identity = next(member["session_id"] for member in self.service.store.snapshot(self.room_id)["members"]
                                    if member["id"] == agent)
                    receipt = self.service.workspace.parent / "native-receipts" / self.room_id / (str(uuid4()) + ".jsonl")
                    receipt.parent.mkdir(parents=True, exist_ok=True)
                    receipt.write_text(json.dumps({"channel": "stdout", "data": {"type": "thread.started", "thread_id": identity}}) + "\n")
                    error.native_execution = {"permission": "workspace_write", "tool_calls": 1, "tool_results": 1,
                        "side_effects_possible": True, "cleanup_confirmed": True, "exit_code": -15,
                        "receipt_path": str(receipt)}
                    raise
            if phase == "review":
                return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept",
                                               "reason": "Read the exact published file."}])
            return self.handler(agent, context, cwd)

        self.fake.handler = handler
        created = await self.service.create_room("Update the value and the notes.", "claude", str(uuid4()),
            mode="native_project", project_id=self.registration["project"]["id"], participants=["claude", "codex"],
            work_mode="build", runtime_settings={"permission": "workspace_write", "members": {}},
            execution_policy={"mode": "autonomous"})
        self.room_id = created["room"]["id"]
        await asyncio.wait_for(self.codex_started.wait(), 10)
        for _ in range(200):
            room = await self.service.read_room(self.room_id)
            if next(task for task in room["team"]["tasks"] if task["id"] == "T1")["status"] == "review":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("T1 never reached review")
        await self.service.shutdown()
        self.service = self.make_service()
        before = await self.service.read_room(self.room_id)
        self.assertEqual(before["team"]["stop_reason"], "service_shutdown")
        codex_tree = self.service.store.native_workspace(self.room_id, "codex")["project"]
        self.assertEqual((self.root / codex_tree / "notes.md").read_text(), RETAINED)
        self.assertEqual((self.source / "notes.md").read_text(), "Original notes.\n")
        return before

    async def test_a_partner_cut_off_by_shutdown_inspects_its_retained_work_before_any_other_call(self):
        # Catches: after the app quits mid-work, the host's continue handing the cut-off
        # partner another task's review while its private tree still holds the unimported
        # edits, so every call fails with native_changes_not_imported and the room stops
        # for the user, who did nothing wrong.
        before = await self.interrupted_room()
        self.assertEqual(before["stop_kind"], "auto")
        mark = len(self.calls)
        # One automatic continue is enough; nothing is left for the user.
        final = await self.host_continue(self.room_id)
        errors = [job["error"] for job in final["jobs"] if job["error"]]
        self.assertNotIn("native_changes_not_imported", errors)
        self.assertEqual(final["objective"]["status"], "complete", (final["team"], errors))
        self.assertEqual(final["team"]["stop_reason"], None)
        # Codex's first call after the restart is the read-only inspection of its retained
        # notes, and it did nothing else until that inspection finished.
        codex_after = [call for call in self.calls[mark:] if call[0] == "codex"]
        self.assertEqual(codex_after[0], ("codex", "inspection", "T2"), codex_after)
        self.assertIn(("codex", "work", "T2"), codex_after)
        inspected = [event for event in final["events"] if event["kind"] == "native_partial_inspected"]
        self.assertEqual([(event["task_id"], event["outcome"]) for event in inspected], [("T2", "complete")])
        # The retained bytes, not a redo, are what got published.
        published = {file["path"]: file["content"]
                     for file in self.service._project(self.room_id).files_for_paths(["main.py", "notes.md"])}
        self.assertEqual(published, {"main.py": "VALUE = 2\n", "notes.md": RETAINED})
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]), final["team"])

    async def test_edits_outside_the_cut_off_task_are_not_retained_for_inspection(self):
        # Catches: widening the restart's retention to bytes the task never owned, which an
        # inspection would then hand to ordinary work as if they were the task's own.
        await self.interrupted_room()
        candidates = self.service.store.native_cut_off_work(self.room_id)
        self.assertEqual([(item["provider"], item["task_id"], item["write_paths"]) for item in candidates],
                         [("codex", "T2", ["notes.md"])])
        self.assertEqual([item["observation"]["changed_paths"] for item in self.service._audit_cut_off_work(self.room_id)],
                         [["notes.md"]])
        tree = self.root / self.service.store.native_workspace(self.room_id, "codex")["project"]
        (tree / "outside.py").write_text("Unowned.\n")
        self.assertEqual(self.service._audit_cut_off_work(self.room_id), [])


if __name__ == "__main__":
    unittest.main()
