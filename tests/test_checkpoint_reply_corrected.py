"""A checkpoint reply whose fields do not fit its decision is corrected, not answered by stopping the goal.

Fictional project, real SQLite and the real job loop; only the native transport
is synthetic (see test_task_pool).
"""
import json
import unittest

from local_agent_coordinator.room_service import RoomError
from tests import test_partner_resolved_blocks as blocks
from tests import test_task_pool as pool
from tests.test_native_room_service import native_reply


class CheckpointReplyCorrected(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the cases below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.slips = []          # the broken checkpoint replies still to be given, in order
        self.checkpoints = 0

    async def handler(self, agent, context, cwd):
        if context["job"]["job_phase"] == "checkpoint":
            self.checkpoints += 1
            if self.slips:
                return native_reply(context, [self.slips.pop(0)(context)])
        return await super().handler(agent, context, cwd)

    @staticmethod
    def go_on_without_a_next_goal(context):
        # What a partner really sent: the next step written into summary, next_goal left empty.
        return {"type": "checkpoint", "decision": "continue", "summary": "The probe passed; the four candidates are next.",
                "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}

    async def kinds(self, kind):
        return [event for event in (await self.state())["events"] if event["kind"] == kind]

    async def test_a_continue_without_a_next_goal_is_sent_back_once_and_the_goal_goes_on(self):
        # Catches the real stall: the goal stopped on this slip, the automatic continue asked the same
        # partner again, it slipped the same way, and the room stayed stopped until the user came back.
        self.plan = [pool.plan("T1")]
        self.slips = [self.go_on_without_a_next_goal]
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertEqual(self.checkpoints, 2, "the broken reply and its correction")
        self.assertEqual([event["code"] for event in await self.kinds("format_correction_queued")], ["invalid_objective_checkpoint"])
        self.assertEqual(await self.kinds("objective_stopped"), [])
        self.assertEqual(state["objective"]["status"], "complete")

    async def test_the_correction_is_told_what_a_checkpoint_needs(self):
        # Catches a correction that only repeats the error code, so the partner cannot tell what was wrong.
        self.plan = [pool.plan("T1")]
        self.slips = [self.go_on_without_a_next_goal]
        await self.create(["claude", "codex"])
        await self.finished()
        correction = [call["prompt"] for call in self.fake.calls if call["context"]["job"]["job_phase"] == "checkpoint"][1]
        self.assertIn("continue must give next_goal as non-empty text", correction)

    async def test_a_partner_that_slips_again_stops_the_goal_instead_of_looping(self):
        # Catches the correction turning into an endless exchange with a partner that keeps slipping.
        self.plan = [pool.plan("T1")]
        self.slips = [self.go_on_without_a_next_goal] * 6
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertNotEqual(state["objective"]["status"], "complete")
        self.assertLessEqual(self.checkpoints, 4, "a bounded number of attempts")


class AllocationNamingAnAbsentPartner(blocks.PartnerResolvedBlockStoreTests):
    # The block cases run in test_partner_resolved_blocks; here only the cases below.
    locals().update({name: None for name in dir(blocks.PartnerResolvedBlockStoreTests) if name.startswith("test_")})

    def out_of_quota(self, room, provider):
        """Mark a partner as out of quota for this goal, the way a quota stop leaves it."""
        with self.store._connection(write=True) as db:
            objective = self.store._team.objectives.current(db, room)
            db.execute("UPDATE objectives SET retired_json=? WHERE id=?", (json.dumps(
                {provider: {"reason": "agent_quota_exhausted", "at": 0, "cleanup_confirmed": True}}), objective["id"]))
            self.assertNotIn(provider, self.store._team.objectives.available_members(db, room))

    def allocation(self, room):
        with self.store._connection() as db:
            return self.store._team._allocation(db, self.store.snapshot(room)["current_run"]["id"])

    def test_a_zero_beside_a_partner_out_of_quota_is_accepted_and_not_kept(self):
        # Catches the reply refused in the real room at the start of every round: a partner wrote
        # "kimi: 0" beside a partner that was out of quota, which is how it says "not this one".
        room = self.plan(paths=True)
        peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
        self.out_of_quota(room, "codex")
        self.finish(main, [{"type": "allocate", "shares": {"claude": 100, "codex": 0}, "reason": "Codex is out of quota."},
                           {"type": "complete", "task_id": "main", "result": "Concrete checked result."}])
        self.assertEqual(self.task(room, "main")["status"], "review")
        self.assertEqual(self.allocation(room), {"claude": 100})
        self.assertEqual([event for event in self.events(room, "warning") if event.get("code") == "invalid_team_allocation"], [])

    def test_naming_only_partners_that_cannot_work_or_a_stranger_is_still_refused(self):
        # Catches the check being dropped altogether along with the over-strict part.
        room = self.plan(paths=True)
        peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
        self.out_of_quota(room, "codex")
        for shares in ({"codex": 0}, {"claude": 100, "nobody": 0}):
            with self.assertRaises(RoomError) as refused:
                self.store.prevalidate_team_result(main["id"], [
                    {"type": "allocate", "shares": shares, "reason": "Split."},
                    {"type": "complete", "task_id": "main", "result": "Concrete checked result."}])
            self.assertEqual(refused.exception.code, "invalid_team_allocation", shares)


if __name__ == "__main__":
    unittest.main()
