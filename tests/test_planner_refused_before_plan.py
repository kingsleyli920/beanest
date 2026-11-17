"""A planner whose reply was refused before the plan existed is asked again once the plan is written.

Fictional project, real SQLite and the real job loop; only the native transport
is synthetic (see test_task_pool).
"""
import unittest

from tests import test_native_room_service as native
from tests import test_task_pool as pool
from tests.test_native_room_service import native_reply
from tests.wait_support import poll_async


class PlannerRefusedBeforePlan(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the case below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.refused = 0

    def blocked(self):
        return [event["provider"] for event in self.service.store.snapshot(self.room_id)["events"]
                if event["kind"] == "native_response_blocked"]

    async def handler(self, agent, context, cwd):
        member, phase, board = context["self"], context["job"]["job_phase"], context["team"]
        if phase == "plan" and member != board["plan_author"] and not board["tasks"] and self.refused < 2:
            # Not the author, yet it proposes: the host refuses the reply, and the corrected one too.
            self.refused += 1
            reply = native_reply(context, [])
            reply["commands"] = [{"type": "propose", "id": "X1"}]
            return reply
        if phase == "plan" and member == board["plan_author"] and not board["tasks"]:
            # The author is still writing while the other planner is refused twice and left without a call.
            await poll_async(self.blocked, lambda providers: providers == ["codex"], timeout=5)
        return await super().handler(agent, context, cwd)

    async def test_a_planner_refused_twice_before_the_plan_was_written_is_asked_again_and_the_room_finishes(self):
        # Catches a room left planning for ever with no call queued: the refusal stopped applying
        # when the author's plan changed the board, and nobody asked the refused planner again.
        self.plan = [pool.plan("T1")]
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertEqual(self.refused, 2)
        self.assertEqual([event["provider"] for event in state["events"] if event["kind"] == "planner_asked_again"], ["codex"])
        self.assertEqual(state["team"]["tasks"][0]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
