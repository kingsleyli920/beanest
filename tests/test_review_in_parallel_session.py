"""A finished result is reviewed in a parallel session when the only partner allowed to review it is in a long call.

Fictional project, real SQLite and the real job loop; only the native transport
is synthetic (see test_task_pool).
"""
import unittest

from tests import test_parallel_sessions as parallel
from tests import test_partner_resolved_blocks as blocks
from tests import test_task_pool as pool


class ReviewInParallelSession(parallel.ParallelSessions):
    # The parallel-session cases run in test_parallel_sessions; here only the cases below.
    locals().update({name: None for name in dir(parallel.ParallelSessions) if name.startswith("test_")})

    def raw(self, sql):
        with self.service.store._connection() as db:
            return [tuple(row) for row in db.execute(sql, (self.room_id,))]

    async def test_a_result_is_reviewed_at_once_while_its_only_reviewer_is_in_a_long_call(self):
        # Catches the real queue: with two partners, what one delivered could only be reviewed by the
        # other, whose main session was in a long call, so the result and everything behind it waited
        # although that partner had sessions to spare.
        self.plan = [pool.plan("T1"), pool.plan("T2")]
        self.holds["T1"] = self.until_statuses(["T2"], "accepted")   # T1 runs until T2 has been reviewed
        await self.create(["claude", "codex"])
        state = await self.finished()
        owners = self.workers()
        self.assertNotEqual(owners["T1"], owners["T2"])
        self.assertEqual(self.raw("SELECT reviewer FROM shared_tasks WHERE room_id=? AND id='T2'"), [(owners["T1"] + "~x2",)])
        # To the user and to the partners it is that partner who reviewed.
        self.assertEqual(self.reviews["T2"], owners["T1"])
        public = {task["id"]: task["reviewer"] for task in self.public(state)["team"]["tasks"]}
        self.assertEqual(public["T2"], owners["T1"])
        self.assertEqual([(event["task_id"], event["parallel_session"]) for event in state["events"]
                          if event["kind"] == "review_taken_in_parallel"], [("T2", 2)])

    async def test_a_partner_kept_to_one_session_reviews_when_its_call_ends(self):
        # Catches the review going to a session the user's limit forbids.
        self.limits = {"claude": 1, "codex": 1}
        self.plan = [pool.plan("T1"), pool.plan("T2")]
        self.holds["T1"] = self.until_statuses(["T2"], "review")
        await self.create(["claude", "codex"])
        state = await self.finished()
        owners = self.workers()
        self.assertEqual(self.raw("SELECT reviewer FROM shared_tasks WHERE room_id=? AND id='T2'"), [(owners["T1"],)])
        self.assertEqual(self.raw("SELECT count(*) FROM members WHERE room_id=? AND provider LIKE '%~x%'"), [(0,)])
        self.assertEqual([event for event in state["events"] if event["kind"] == "review_taken_in_parallel"], [])

    async def test_a_partner_never_reviews_its_own_result_in_another_session(self):
        # Catches the spare session being used to let a partner approve what it made itself.
        self.limits = {"claude": 3, "codex": 1}
        self.plan = [pool.plan("T1"), pool.plan("T2"), pool.plan("T3")]
        self.hold_until_started(3)
        await self.create(["claude", "codex"])
        await self.finished()
        for task_id, member in self.workers().items():
            self.assertNotEqual(self.reviews[task_id], member, task_id)


class ShortCalls(blocks.PartnerResolvedBlockStoreTests):
    # The block cases run in test_partner_resolved_blocks; here only the case below.
    locals().update({name: None for name in dir(blocks.PartnerResolvedBlockStoreTests) if name.startswith("test_")})

    def lanes(self, room):
        with self.store._connection() as db:
            return [row[0] for row in db.execute("SELECT provider FROM members WHERE room_id=? AND provider LIKE '%~x%'", (room,))]

    def test_a_reviewer_busy_with_a_reply_is_waited_for_and_one_on_a_task_is_not(self):
        # Catches a session being opened for every review whose reviewer is merely answering the
        # user: that ends soon, and a session of its own costs more than it saves.
        self.store.parallel_limits = lambda: {"claude": 3, "codex": 3}
        room = self.plan(paths=True)
        peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
        self.finish(main, [{"type": "complete", "task_id": "main", "result": "Concrete checked result."}])
        # Codex is on its task, so claude's result is reviewed in a session of codex's at once.
        self.assertEqual((self.task(room, "main")["status"], self.lanes(room)), ("review", ["codex~x2"]))
        self.assertEqual([event["task_id"] for event in self.events(room, "review_taken_in_parallel")], ["main"])
        # Claude starts answering the user; codex's result then waits for claude itself.
        self.assertTrue(self.store.send_message(room, "A quick question while you are free.", "claude", "quick-question"))
        self.finish(peer, [{"type": "complete", "task_id": "tests", "result": "Concrete checked result."}])
        self.assertEqual((self.task(room, "tests")["status"], self.task(room, "tests")["reviewer"]), ("review", None))
        self.assertEqual(self.lanes(room), ["codex~x2"])
        self.assertEqual([event["task_id"] for event in self.events(room, "review_taken_in_parallel")], ["main"])


if __name__ == "__main__":
    unittest.main()
