"""A message that arrives while the goal waits on the user's decision is answered once work goes on.

The store cases use real SQLite task boards; the service case runs the real job
loop on a fictional project with a synthetic native transport (see test_task_pool).
"""
import unittest
from uuid import uuid4

from tests import test_partner_resolved_blocks as blocks
from tests import test_task_pool as pool
from tests.test_native_room_service import native_reply

NOTE = "The courier moved the handover to Thursday; plan around that."


class WaitingMessageStoreTests(blocks.PartnerResolvedBlockStoreTests):
    # The block cases run in test_partner_resolved_blocks; here only the cases below.
    locals().update({name: None for name in dir(blocks.PartnerResolvedBlockStoreTests) if name.startswith("test_")})

    def waiting_on_a_decision(self):
        """One task asks the user, the other is blocked by the host: the goal waits, nobody has a call."""
        room = self.plan(paths=True)
        peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
        self.finish(main, [{"type": "block", "task_id": "main", "cause": "user_decision", "reason": "Pick A or B."}])
        for _ in range(3):
            self.finish(peer, [])
            peer = self.store.claim(room, "codex") or peer
        self.assertTrue(self.store.require_task_decision(room, self.binding(room)))
        self.assertEqual(self.store.snapshot(room)["objective"]["status"], "needs_user")
        return room

    def queued(self, room):
        """Every call now waiting to be claimed, as (partner, kind, text)."""
        with self.store._connection() as db:
            return sorted(tuple(row) for row in db.execute(
                "SELECT provider,kind,text FROM jobs WHERE room_id=? AND status='queued'", (room,)))

    def test_a_message_sent_while_the_goal_waits_is_answered_when_the_host_reopens_the_blocked_work(self):
        # Catches the real gap: a note relayed while the room waited on a decision was shown in the
        # conversation, the host then reopened the blocked task by itself for one partner, and the
        # partners with nothing to do were never asked to answer the note.
        room = self.waiting_on_a_decision()
        self.assertTrue(self.store.send_message(room, NOTE, "all", "relayed-note", source="mcp"))
        self.assertEqual(self.queued(room), [], "nothing is dispatched while the goal waits on the user")
        self.assertEqual([event["recipients"] for event in self.events(room, "user_message_waiting")], [["claude", "codex"]])
        self.assertTrue(self.store.resume_recoverable_block(room))
        self.store.reconcile_team(room)
        self.assertEqual([entry for entry in self.queued(room) if entry[1] == "team_user"],
                         [("claude", "team_user", NOTE), ("codex", "team_user", NOTE)])
        self.assertEqual([event["recipients"] for event in self.events(room, "user_message_handed_out")], [["claude", "codex"]])

    def test_a_waiting_message_goes_only_to_the_partner_it_named_and_only_once(self):
        # Catches handing a directed message to everyone, and handing the same message out again
        # each time the scheduler runs or the same request is repeated.
        room = self.waiting_on_a_decision()
        self.assertTrue(self.store.send_message(room, NOTE, "claude", "relayed-note", source="mcp"))
        self.assertFalse(self.store.send_message(room, NOTE, "claude", "relayed-note", source="mcp"))
        self.assertTrue(self.store.resume_recoverable_block(room))
        for _ in range(3):
            self.store.reconcile_team(room)
        self.assertEqual([entry for entry in self.queued(room) if entry[1] == "team_user"], [("claude", "team_user", NOTE)])
        job = self.store.claim(room, "claude")
        self.assertEqual((job["kind"], job["text"]), ("team_user", NOTE))
        self.finish(job, [])
        self.store.reconcile_team(room)
        self.assertEqual([entry for entry in self.queued(room) if entry[1] == "team_user"], [])
        self.assertEqual(len(self.events(room, "user_message_handed_out")), 1)

    def test_a_message_sent_while_work_is_going_on_is_not_kept_for_later(self):
        # Catches treating every message as waiting: one sent to a running room is dispatched at once
        # (or read by the busy partner) and must not be dispatched again later.
        room = self.plan(paths=True)
        self.store.claim(room, "codex")
        self.assertTrue(self.store.send_message(room, NOTE, "all", "relayed-note", source="mcp"))
        self.store.reconcile_team(room)
        self.assertEqual(self.events(room, "user_message_waiting"), [])
        self.assertEqual(self.events(room, "user_message_handed_out"), [])


class WaitingMessageService(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the case below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.answered = False

    async def handler(self, agent, context, cwd):
        if context["job"]["job_phase"] == "checkpoint" and not self.answered:
            return native_reply(context, [{"type": "checkpoint", "decision": "needs_user", "summary": "A layout choice is open.",
                                           "next_goal": None, "scope_reference": context["objective"]["scope_reference"],
                                           "question": "Which layout do you choose?"}])
        return await super().handler(agent, context, cwd)

    async def test_through_the_job_loop_the_note_waits_for_the_answer_and_then_reaches_both_partners(self):
        # Catches the note being dropped, or dispatched while the goal still waits, on the path where
        # the user's answer starts a new round: both partners are then planning and read it there.
        self.plan = [pool.plan("T1")]
        await self.create(["claude", "codex"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "needs_user")
        before = len(self.fake.calls)
        await self.service.send_message(self.room_id, NOTE, "all", "relayed-note", source="mcp")
        await self.settle()
        self.assertEqual(len(self.fake.calls), before, "nothing is dispatched while the goal waits on the user")
        self.answered = True
        run = state["current_run"]
        await self.service.objective_action(self.room_id, "answer", str(uuid4()), run_id=run["id"], revision=run["revision"],
                                            digest=run["digest"], objective_id=state["objective"]["id"], text="Use the compact layout.")
        await self.settle()
        final = await self.state()
        self.assertEqual(final["objective"]["status"], "complete")
        readers = {call["context"]["self"] for call in self.fake.calls[before:]
                   if any(update["text"] == NOTE for update in call["context"]["current_user_updates"])}
        self.assertEqual(readers, {"claude", "codex"})
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "user_message_handed_out"]), 1)


if __name__ == "__main__":
    unittest.main()
