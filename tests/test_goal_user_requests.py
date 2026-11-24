"""Everything the user said to a goal reaches the partners, however many objectives it took.

Each automatic continue and each follow-up opens a new objective under the same
original goal. Only the request just before one used to be carried, so in the
九烛 room the user's 11-22 decisions (keep only main, packaging allowed) dropped
out while the 11-18 goal's "push to the feature branch, do not package" stayed
in view, and the partners recreated the branch and refused to package.
"""
import unittest

from tests import test_partner_resolved_blocks as blocks
from tests import test_task_pool as pool

EARLY = "Keep only main: merge the feature branch and delete it. Packaging is allowed this round."
LATER = "Carry on with what is left."


class GoalUserRequestStoreTests(blocks.PartnerResolvedBlockStoreTests):
    # The block cases run in test_partner_resolved_blocks; here only the cases below.
    locals().update({name: None for name in dir(blocks.PartnerResolvedBlockStoreTests) if name.startswith("test_")})

    def test_requests_from_earlier_objectives_of_the_same_goal_stay_and_another_goal_starts_over(self):
        # Catches the earlier objective's request being dropped once a newer objective began, a repeated
        # send listed twice, a partner's own message counted as the user's, or another goal's messages leaking in.
        room = self.plan(paths=True)
        self.assertTrue(self.store.send_message(room, EARLY, "all", "early", source="user"))
        self.assertTrue(self.store.send_message(room, EARLY, "all", "early-again", source="user"))
        with self.store._connection(write=True) as db:
            current = self.store._team.objectives.current(db, room)
            began = current["created_at"]
            # The objective now running began after the early request, as an automatic continue does...
            db.execute("UPDATE objectives SET created_at=? WHERE id=?", (self.now + 1, current["id"]))
            # ...and the objective that received the early request had the same goal.
            db.execute("INSERT INTO objectives (id,room_id,original_goal,policy_json,created_at,deadline,status,work_mode) "
                       "SELECT 'earlier-same-goal',room_id,original_goal,policy_json,?,deadline,'stopped',work_mode "
                       "FROM objectives WHERE id=?", (began, current["id"]))
        self.now += 5
        self.assertTrue(self.store.send_message(room, LATER, "all", "later", source="user"))
        requests = self.store.objective_user_requests(room)
        texts = [item["text"] for item in requests]
        self.assertEqual(texts[-2:], [EARLY, LATER])
        self.assertEqual(texts.count(EARLY), 1, "a repeated send is listed once")
        self.assertTrue(texts[0].startswith("A shared deliverable"), "the request that created the goal comes first")
        with self.store._connection(write=True) as db:
            # A different goal written later in the same room starts with nothing of the old one.
            db.execute("UPDATE objectives SET original_goal='A different goal entirely.' WHERE id=?",
                       (self.store._team.objectives.current(db, room)["id"],))
        self.assertEqual([item["text"] for item in self.store.objective_user_requests(room)], [LATER])
        # A partner's own message is never counted as the user's.
        self.assertTrue(all(item["text"] != LATER or item["recipient"] == "all" for item in self.store.objective_user_requests(room)))


class GoalUserRequestPromptTests(pool.PoolService):
    for name in [name for name in dir(pool.PoolService) if name.startswith("test_")]:
        locals()[name] = None

    async def test_every_partner_call_reads_the_users_requests_to_the_goal(self):
        # Catches the requests not reaching the partners' context at all.
        self.plan = [pool.plan("T1")]
        await self.create(["claude", "codex"])
        await self.service.send_message(self.room_id, EARLY, "all", "early-note", source="user")
        await self.settle()
        later = [call for call in self.fake.calls
                 if any(item["text"] == EARLY for item in call["context"].get("objective_user_requests", []))]
        self.assertTrue(later, "a call after the request carries it")


if __name__ == "__main__":
    unittest.main()
