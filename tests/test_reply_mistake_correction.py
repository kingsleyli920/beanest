"""A correctable mistake in a reply is corrected; it never stops the objective."""
import unittest

import test_native_room_service as native
from test_native_room_service import native_reply


class ReplyMistakeCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create

    def mistake_once(self, phase, mistake):
        seen, original = [], self.handler
        def handler(agent, context, cwd):
            if context["job"]["job_phase"] == phase:
                seen.append(context["job"]["text"])
                if len(seen) == 1:
                    # The mistaken turn does no work; the fixture's own work
                    # handler asserts the untouched starting bytes.
                    return mistake(context, None if phase == "work" else original(agent, context, cwd))
            return original(agent, context, cwd)
        self.fake.handler = handler
        return seen

    async def assert_corrected(self, phase, code, mistake, hint):
        seen = self.mistake_once(phase, mistake)
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", (code, final["team"]))
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn(hint, seen[1])
        corrections = [event for event in final["events"] if event["kind"] == "format_correction_queued"]
        self.assertEqual([event["code"] for event in corrections], [code])
        self.assertNotIn(code, {event.get("code") for event in final["events"] if event["kind"] == "objective_stopped"})

    async def test_review_of_another_task_is_corrected(self):
        # Catches: a review command naming a task this call is not reviewing
        # raised team_task_permission and stopped the objective.
        def mistake(context, reply):
            return native_reply(context, [{"type": "review", "task_id": "T9", "verdict": "accept", "reason": "Wrong task."}])
        await self.assert_corrected("review", "team_task_permission", mistake, "does not hold")

    async def test_help_request_to_an_unknown_peer_is_corrected(self):
        # Catches: request_help.to naming a peer that is not in the room raised
        # invalid_target, failed the call and stopped the objective.
        def mistake(context, reply):
            return native_reply(context, [{"type": "request_help", "task_id": context["task"]["id"], "to": "ghost",
                                           "capability": "text", "text": "Please advise."}],
                                unchanged=[{"path": "notes.md", "reason": "Notes remain correct."}])
        await self.assert_corrected("work", "invalid_target", mistake, "different available peer")

    async def test_review_reply_with_file_edits_is_corrected(self):
        # Catches: a native reply that carried JSON edits instead of editing the
        # private tree raised native_json_files_forbidden and stopped.
        def mistake(context, reply):
            return {**reply, "edits": [{"path": "main.py", "old": "VALUE = 2\n", "new": "VALUE = 3\n"}]}
        await self.assert_corrected("review", "native_json_files_forbidden", mistake, "write files directly")

    async def test_planner_repeating_a_corrected_mistake_stops_instead_of_hanging(self):
        # Catches: after the one correction was refused as repeated evidence,
        # the plan phase stayed blocked and the room sat in planning forever
        # with no call queued and no stop.
        original = self.handler
        def handler(agent, context, cwd):
            reply = original(agent, context, cwd)
            if context["job"]["job_phase"] == "plan":
                return {**reply, "files": [{"path": "plan.md", "previous_sha256": None, "content": "Plan.\n"}]}
            return reply
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["team"]["phase"], "stopped", final["team"])
        self.assertEqual(final["team"]["stop_reason"], "native_json_files_forbidden")
        # The stop still offers a way forward: the user can continue the objective.
        self.assertTrue(final["current_run"]["can_restart_objective"], final["current_run"].get("restart_objective_reason"))
        corrections = [event for event in final["events"] if event["kind"] == "format_correction_queued"]
        # One correction per planner, never a second one for the same evidence.
        providers = {job["id"]: job["provider"] for job in final["jobs"]}
        self.assertEqual(sorted((providers[event["job_id"]], event["code"]) for event in corrections),
                         [("claude", "native_json_files_forbidden"), ("codex", "native_json_files_forbidden")])


if __name__ == "__main__":
    unittest.main()
