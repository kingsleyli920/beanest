"""A work reply that also settles another task is corrected, not fatal."""
import unittest

import test_native_room_service as native
from test_native_room_service import native_reply


class WorkTerminalCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create

    async def test_extra_complete_for_another_task_requests_a_correction_and_finishes(self):
        # Catches: Claude finished D1b and also sent complete for its blocked
        # parent D1; team_task_permission failed the call and stopped the
        # whole objective instead of asking for one terminal command.
        work, original = [], self.handler
        def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "work":
                work.append(context["job"]["text"])
                (cwd / "main.py").write_text("VALUE = 2\n")
                commands = [{"type": "complete", "task_id": context["task"]["id"], "result": "Updated main."}]
                if len(work) == 1:
                    commands.append({"type": "complete", "task_id": "T9", "result": "Also finishing another task."})
                return native_reply(context, commands, unchanged=[{"path": "notes.md", "reason": "Notes remain correct."}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(work), 2)
        self.assertIn("other tasks, including a parent", work[1])
        self.assertFalse(any(job["error"] == "team_task_permission" for job in final["jobs"]))
        self.assertTrue(any(event["kind"] == "format_correction_queued" for event in final["events"]))


if __name__ == "__main__":
    unittest.main()
