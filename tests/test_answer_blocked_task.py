"""The user answers one task that waits on their decision or permission, and only that task goes on.

Real SQLite task boards for the rule; the real job loop on a fictional project
for the whole path (see test_task_pool).
"""
import unittest
from uuid import uuid4

from local_agent_coordinator.room_service import RoomError
from tests import test_partner_resolved_blocks as blocks
from tests import test_task_pool as pool
from tests.test_native_room_service import native_reply

ASK = "Sending this needs one paid request of at most 0.25 dollars. Do you allow it?"
YES = "Yes, one request, at most 0.25 dollars."


class AnswerStoreTests(blocks.PartnerResolvedBlockStoreTests):
    # The block cases run in test_partner_resolved_blocks; here only the cases below.
    locals().update({name: None for name in dir(blocks.PartnerResolvedBlockStoreTests) if name.startswith("test_")})

    def waiting_on_permission(self):
        """Claude's task asks for permission while codex's task is still being worked on."""
        room = self.plan(paths=True)
        peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
        self.finish(main, [{"type": "block", "task_id": "main", "cause": "permission_required", "reason": ASK}])
        self.assertEqual((self.task(room, "main")["status"], self.task(room, "main")["block_cause"]), ("blocked", "permission_required"))
        return room, peer

    def run_id(self, room):
        return self.store.snapshot(room)["current_run"]["id"]

    def test_the_answer_reopens_that_task_at_once_while_other_work_is_still_running(self):
        # Catches the real gap: the card said "needs your approval" and offered no way to give it, because
        # the question was only put to the user once nothing else in the room could run.
        room, peer = self.waiting_on_permission()
        self.assertEqual(self.store.snapshot(room)["objective"]["status"], "running")
        self.assertTrue(self.store.answer_task(room, "approve-1", run_id=self.run_id(room), task_id="main", text=YES))
        self.store.reconcile_team(room)
        task = self.task(room, "main")
        self.assertEqual((task["status"], task["owner"], task["block_cause"], task["blocked_reason"]), ("assigned", "claude", None, None))
        self.assertEqual(task["help_result"], "The user answered what this task was waiting for, in the app. Their words: " + YES)
        again = self.store.claim(room, "claude")
        self.assertEqual((again["kind"], again["team"]["task_id"]), ("team_work", "main"))
        # The other task goes on being worked on, untouched.
        self.assertEqual((self.task(room, "tests")["status"], self.task(room, "tests")["owner"]), ("working", "codex"))
        said = [(event["sender"], event["text"]) for event in self.events(room, "message") if event["sender"] == "user"]
        self.assertEqual(said[-1], ("user", YES))
        answered = self.events(room, "task_block_answered")
        self.assertEqual([(event["task_id"], event["cause"], event["question"], event["text"]) for event in answered],
                         [("main", "permission_required", ASK, YES)])
        self.assertEqual(self.events(room, "objective_decision_required"), [])

    def test_the_same_request_twice_answers_once(self):
        # Catches a double click reopening the task twice or saying the user's words twice.
        room, _ = self.waiting_on_permission()
        self.assertTrue(self.store.answer_task(room, "approve-1", run_id=self.run_id(room), task_id="main", text=YES))
        self.assertFalse(self.store.answer_task(room, "approve-1", run_id=self.run_id(room), task_id="main", text=YES))
        self.assertEqual(len(self.events(room, "task_block_answered")), 1)

    def test_only_a_task_that_waits_for_the_user_can_be_answered(self):
        # Catches the answer being used to reopen a task that is running, finished, blocked for the
        # partners to sort out, or in another round.
        room = self.plan(paths=True)
        peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
        with self.assertRaises(RoomError) as running:
            self.store.answer_task(room, "a", run_id=self.run_id(room), task_id="main", text=YES)
        self.assertEqual(running.exception.code, "task_answer_unavailable")
        self.finish(main, [{"type": "block", "task_id": "main", "cause": "external_dependency", "reason": "The staging host is down."}])
        if self.task(room, "main")["status"] == "blocked":
            with self.assertRaises(RoomError) as partners:
                self.store.answer_task(room, "b", run_id=self.run_id(room), task_id="main", text=YES)
            self.assertEqual(partners.exception.code, "task_answer_unavailable")
        with self.assertRaises(RoomError) as missing:
            self.store.answer_task(room, "c", run_id=self.run_id(room), task_id="nope", text=YES)
        self.assertEqual(missing.exception.code, "team_task_not_found")
        with self.assertRaises(RoomError) as other_round:
            self.store.answer_task(room, "d", run_id=str(uuid4()), task_id="main", text=YES)
        self.assertEqual(other_round.exception.code, "team_run_conflict")
        self.assertEqual(self.events(room, "task_block_answered"), [])


class AnswerService(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the cases below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.asked = False
        self.resumed_with = None

    async def handler(self, agent, context, cwd):
        task = context["task"]
        if context["job"]["job_phase"] == "work" and task["id"] == "T1":
            if not self.asked:
                self.asked = True
                return native_reply(context, [{"type": "block", "task_id": "T1", "cause": "permission_required", "reason": ASK}])
            self.resumed_with = task.get("help_result")
        return await super().handler(agent, context, cwd)

    async def test_through_the_job_loop_the_owner_resumes_with_the_users_words_and_the_room_finishes(self):
        # Catches the answer being recorded but never reaching the partner that asked.
        self.plan = [pool.plan("T1"), pool.plan("T2")]
        release = {"go": False}
        async def until_answered():
            return release["go"]
        self.holds["T2"] = until_answered          # the other task keeps running while the user answers
        await self.create(["claude", "codex"])
        for _ in range(500):
            if (await self.statuses()).get("T1") == "blocked":
                break
            await pool.asyncio.sleep(0.01)
        state = await self.state()
        self.assertEqual((state["objective"]["status"], (await self.statuses())["T1"]), ("running", "blocked"))
        await self.service.answer_task(self.room_id, "approve-1", run_id=state["current_run"]["id"], task_id="T1", text=YES)
        release["go"] = True
        final = await self.finished_tasks()
        self.assertEqual(self.resumed_with, "The user answered what this task was waiting for, in the app. Their words: " + YES)
        self.assertEqual({task["id"]: task["status"] for task in final["team"]["tasks"]}, {"T1": "accepted", "T2": "accepted"})

    async def finished_tasks(self):
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        return state

    async def test_the_entry_from_outside_cannot_answer_for_the_user(self):
        # Catches permission being granted by something other than the user in the app.
        self.plan = [pool.plan("T1")]
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        with self.assertRaises(RoomError) as refused:
            await self.service.answer_task(self.room_id, "x", run_id=state["current_run"]["id"], task_id="T1", text=YES, source="mcp")
        self.assertEqual(refused.exception.code, "user_task_answer_required")


if __name__ == "__main__":
    unittest.main()
