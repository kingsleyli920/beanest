"""A follow-up to a finished goal, sent through the local MCP entry, goes on like one typed in the app.

Fictional project, real SQLite and the real job loop; only the native transport
is synthetic (see test_task_pool).
"""
import unittest

from local_agent_coordinator.room_service import RoomError
from tests import test_task_pool as pool

NOTE = "Now add the small export step as well."


class FollowUpFromOutside(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the cases below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def from_outside(self, request_id, text=NOTE):
        run = (await self.state())["current_run"]
        await self.service.start_run(self.room_id, text, "all", request_id, run_id=run["id"], revision=run["revision"],
                                     digest=run["digest"], source="mcp")
        await self.settle()
        return await self.state()

    async def test_a_follow_up_from_outside_starts_the_next_goal_under_the_rooms_own_way_of_working(self):
        # Catches the real dead end: the entry could only start a plain round, which stops for the
        # user after one pass and is refused outright while any partner is out of quota, so a relayed
        # follow-up to a finished room started nothing.
        self.plan = [pool.plan("T1")]
        await self.create(["claude", "codex"])
        first = await self.finished()
        self.plan = [pool.plan("U1")]
        calls = len(self.fake.calls)
        second = await self.from_outside("relayed-follow-up")
        self.assertEqual(second["objective"]["status"], "complete", second["team"])
        self.assertNotEqual(second["objective"]["id"], first["objective"]["id"])
        self.assertEqual([(event["sender"], event["text"]) for event in second["events"]
                          if event["kind"] == "message" and event["sender"] in {"user", "mcp"}][-1], ("mcp", NOTE))
        self.assertEqual([event["sender"] for event in second["events"] if event["kind"] == "objective_followup_requested"], ["mcp"])
        self.assertEqual(second["room"]["runtime_settings"]["permission"], "full_access")
        # The partners are asked what the follow-up still needs, with its words in front of them
        # (this fixture's partner answers that nothing is left, so the new goal ends at once).
        asked = self.fake.calls[calls]["context"]
        self.assertEqual((asked["job"]["job_phase"], asked["current_user_updates"][-1]["text"],
                          asked["objective"]["continuation"]["user_request"]), ("checkpoint", NOTE, NOTE))
        # The same request again changes nothing.
        calls = len(self.fake.calls)
        run = second["current_run"]
        first_run = first["current_run"]
        await self.service.start_run(self.room_id, NOTE, "all", "relayed-follow-up", run_id=first_run["id"],
                                     revision=first_run["revision"], digest=first_run["digest"], source="mcp")
        await self.settle()
        self.assertEqual(((await self.state())["current_run"]["id"], len(self.fake.calls)), (run["id"], calls))

    async def test_a_room_that_never_had_a_goal_of_its_own_still_gets_a_plain_round(self):
        # Catches the entry starting a self-driving goal where the user never chose one.
        self.plan = [pool.plan("T1")]
        await self.create(["claude", "codex"], objective=False)
        await self.settle()
        self.plan = [pool.plan("U1")]
        state = await self.from_outside("relayed-plain")
        self.assertIsNone(state.get("objective"))
        self.assertEqual(state["team"]["phase"], "ready_for_user")
        self.assertEqual([event for event in state["events"] if event["kind"] == "objective_followup_requested"], [])

    async def test_nothing_else_the_user_alone_may_do_is_open_to_the_entry(self):
        # Catches the opening being wider than a follow-up: a new goal, an answer or a changed
        # permission from outside stay refused.
        self.plan = [pool.plan("T1")]
        await self.create(["claude", "codex"])
        state = await self.finished()
        run = state["current_run"]
        binding = {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}
        for action in ("objective", "restart-objective", "reassess-objective"):
            with self.assertRaises(RoomError) as refused:
                self.service.store.objective_action(self.room_id, action, "outside-" + action, [binding], binding=binding,
                                                    user_message=NOTE, message_source="mcp")
            self.assertEqual(refused.exception.code, "user_execution_policy_required")
        with self.assertRaises(RoomError) as refused:
            await self.service.objective_action(self.room_id, "followup-objective", "outside-direct", **binding,
                                                user_message=NOTE, source="mcp")
        self.assertEqual(refused.exception.code, "user_execution_policy_required")
        with self.assertRaises(RoomError) as refused:
            await self.service.start_run(self.room_id, NOTE, "all", "outside-settings", **binding, source="mcp",
                                         runtime_settings={"permission": "full_access", "members": {}})
        self.assertEqual(refused.exception.code, "user_native_permission_required")


if __name__ == "__main__":
    unittest.main()
