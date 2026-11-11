"""A user's message survives an interruption: a killed reply call, or the
host's automatic continue landing at the moment the user sends.

Real private trees, SQLite and job loop on a fictional project; only the
native transport is synthetic.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import room_service
from local_agent_coordinator.room_service import RoomError
import test_native_room_service as native
from test_native_room_service import native_reply
from wait_support import within

QUESTION = "Codex, why does the value change?"
ANSWER = "The task asks for the new value."
NOTE = "Keep notes.md short."


class InterruptedUserMessages(unittest.IsolatedAsyncioTestCase):
    # Borrow the fixture only; inheriting would rerun every native room test.
    settle = native.NativeRoomServiceTests.settle
    create = native.NativeRoomServiceTests.create
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        await native.NativeRoomServiceTests.asyncSetUp(self)
        native.no_rebalance(self.service)
        self.kill_replies = 0       # user-phase calls whose CLI is killed after reading files
        self.kill_code = "protocol_turn_incomplete"  # what a CLI killed mid-turn reports
        self.kill_tools = 1         # tools the killed reply had started
        self.kill_before_session = False  # killed before the CLI reported its session
        self.crash_work = 0         # work calls whose CLI exits nonzero before writing
        self.wrong_task = 0         # work replies naming a task that does not exist (the host blocks the task)
        self.hold_work = None       # an Event the first call of hold_phase waits on
        self.hold_phase = "work"
        self.held_edit = False      # the held work call edits its own task file before it is cut off
        self.work_started = asyncio.Event()
        invoke = self.service.native_invoke
        async def transport(agent, prompt, cwd, session_id=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            phase = context["job"]["job_phase"]
            if phase == self.hold_phase and self.hold_work is not None and not self.hold_work.is_set():
                if self.held_edit:
                    (Path(cwd) / "main.py").write_text("VALUE = 2\n")
                self.work_started.set()
                try:
                    await self.hold_work.wait()
                except asyncio.CancelledError as error:
                    # As the real adapter: process group killed, the owned receipt names the session.
                    receipt = Path(options["receipt_dir"]) / f"{uuid4()}.jsonl"
                    receipt.parent.mkdir(parents=True, exist_ok=True)
                    receipt.write_text(json.dumps({"channel": "stdout", "data": {
                        "type": "system", "subtype": "init", "session_id": session_id}}) + "\n")
                    error.native_execution = {"permission": options["permission"], "cleanup_confirmed": True, "side_effects_possible": True,
                                              "tool_calls": int(self.held_edit), "tool_results": int(self.held_edit),
                                              "receipt_path": str(receipt)}
                    raise
            result = await invoke(agent, prompt, cwd, session_id=session_id, **options)
            if phase == "user" and self.kill_replies:
                self.kill_replies -= 1
                # Killed in Activity Monitor mid-read: a tool started, none returned.
                result.update(status="failed", failure_code=self.kill_code, text="", native_execution={
                    **result["native_execution"], "exit_code": -9, "tool_calls": self.kill_tools, "tool_results": 0,
                    "side_effects_possible": bool(self.kill_tools)})
                if self.kill_before_session:
                    result["session_id"] = None
            elif phase == "work" and self.crash_work and not context.get("native_partial_inspection"):
                self.crash_work -= 1
                result.update(status="failed", failure_code="cli_nonzero_exit", text="", native_execution={
                    **result["native_execution"], "exit_code": 1})
            return result
        self.service.native_invoke = transport
        delay = patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0)
        delay.start()
        self.addCleanup(delay.stop)

    def handler(self, agent, context, cwd):
        phase, task = context["job"]["job_phase"], context["task"]
        if phase == "user":
            reply = native_reply(context, [])
            reply["message"] = ANSWER
            return reply
        if phase == "work":
            if context.get("native_partial_inspection"):
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Inspected the retained tree."}])
            if self.wrong_task:
                self.wrong_task -= 1
                return native_reply(context, [{"type": "complete", "task_id": "T9", "result": "Wrong task."}])
            if not self.crash_work:
                (cwd / "main.py").write_text("VALUE = 2\n")
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Updated main."}],
                unchanged=[{"path": "notes.md", "reason": "Notes remain correct."}])
        return native.NativeRoomServiceTests.handler(self, agent, context, cwd)

    async def watchdog(self, room_id, passes=4):
        """The host's periodic pass (expire_tasks), with its 15 s pause set to zero."""
        for _ in range(passes):
            await self.service.read_room(room_id)
            await self.service.expire_tasks()
            pending = [state["task"] for state in self.service._auto_continues.values()
                       if state["task"] is not None and not state["task"].done()]
            if pending:
                await within(asyncio.gather(*pending), 10)
            await self.settle()

    def auto_continue_now(self, room_id):
        # The first pass notes the stop, the second presses 继续.
        for _ in range(2):
            self.service._schedule_auto_continue(room_id, self.service.store.snapshot(room_id))
        return self.service._auto_continues[room_id]["task"]

    @staticmethod
    def binding(state):
        run = state["current_run"]
        return {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}

    def answered_by(self, provider, text):
        return [call for call in self.fake.calls
                if call["agent"] == provider and call["context"]["job"]["job_phase"] == "user" and text in call["prompt"]]

    # A reply to the user's question is killed.

    async def test_a_question_whose_reply_was_killed_is_asked_again_and_answered(self):
        # Catches: the killed reply call stopped the room with every action refused
        # (native_recovery_unavailable), and a recovered room never asked the question again.
        self.kill_replies, self.hold_work = 1, asyncio.Event()
        created = await self.create(permission="workspace_write")
        room_id = created["room"]["id"]
        await within(self.work_started.wait(), 5)
        state = await self.service.read_room(room_id)
        await self.service.send_message(room_id, QUESTION, "codex", str(uuid4()), intent="continue", binding=self.binding(state))
        await within(self._first_reply_killed(), 5)
        self.hold_work.set()
        await self.settle()
        stopped = await self.service.read_room(room_id)
        self.assertEqual(stopped["team"]["stop_reason"], "protocol_turn_incomplete")
        self.assertEqual(stopped["stop_kind"], "auto", stopped["current_run"].get("native_recovery_reason"))
        await self.watchdog(room_id)
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(self.answered_by("codex", QUESTION)), 2)
        self.assertTrue(any(event["kind"] == "message" and event["sender"] == "codex" and event.get("text") == ANSWER
                            for event in final["events"]))
        redelivered = [event for event in final["events"] if event["kind"] == "user_message_redelivered"]
        self.assertEqual([event["recipient"] for event in redelivered], ["codex"])

    async def test_a_question_killed_while_only_a_check_was_running_is_answered(self):
        # Catches: the last-resort recovery refusing a reply call that read files
        # (or a peer's check cut off with it), leaving the room stopped with every
        # action refused when no work call was cut off. A CLI that ends its turn
        # but exits nonzero (a crash, or a quit it handles) reports cli_nonzero_exit.
        self.kill_replies, self.hold_work, self.hold_phase = 1, asyncio.Event(), "checkpoint"
        self.kill_code = "cli_nonzero_exit"
        created = await self.create(permission="workspace_write")
        room_id = created["room"]["id"]
        await within(self.work_started.wait(), 5)
        state = await self.service.read_room(room_id)
        await self.service.send_message(room_id, QUESTION, "codex", str(uuid4()), intent="continue", binding=self.binding(state))
        await within(self._first_reply_killed(), 5)
        self.hold_work.set()
        await self.settle()
        stopped = await self.service.read_room(room_id)
        self.assertEqual(stopped["current_run"]["native_recovery_kind"], "unconfirmed_restart",
                         stopped["current_run"]["native_recovery_reason"])
        self.assertEqual(stopped["stop_kind"], "auto")
        await self.watchdog(room_id)
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(self.answered_by("codex", QUESTION)), 2)

    async def test_a_peers_edit_cut_off_by_the_killed_reply_is_kept_and_the_question_answered(self):
        # Catches: the reply call's fault not counting as the peer failure that lets
        # an owner keep the edit it was making, so the room stopped as
        # native_recovery_unpublished_changes; and the question then lost behind
        # the retained work's inspection.
        self.kill_replies, self.hold_work, self.held_edit = 1, asyncio.Event(), True
        created = await self.create(permission="workspace_write")
        room_id = created["room"]["id"]
        await within(self.work_started.wait(), 5)
        state = await self.service.read_room(room_id)
        await self.service.send_message(room_id, QUESTION, "codex", str(uuid4()), intent="continue", binding=self.binding(state))
        await within(self._first_reply_killed(), 5)
        self.hold_work.set()
        await self.settle()
        stopped = await self.service.read_room(room_id)
        self.assertEqual(stopped["team"]["stop_reason"], "protocol_turn_incomplete")
        self.assertTrue(stopped["current_run"]["can_recover_native"], stopped["current_run"]["native_recovery_reason"])
        self.assertEqual(stopped["stop_kind"], "auto")
        await self.watchdog(room_id, passes=6)
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        kinds = [event["kind"] for event in final["events"]]
        self.assertIn("native_partial_inspection_group", kinds)
        self.assertLess(kinds.index("user_message_redelivery_deferred"), kinds.index("user_message_redelivered"))
        self.assertEqual(len(self.answered_by("codex", QUESTION)), 2)
        published = next(file for file in self.service._project(room_id).metadata_for_paths(["main.py"]) if file["path"] == "main.py")
        self.assertEqual(published["sha256"], hashlib.sha256(b"VALUE = 2\n").hexdigest())

    async def kill_reply_during_check(self):
        self.kill_replies, self.hold_work, self.hold_phase = 1, asyncio.Event(), "checkpoint"
        created = await self.create(permission="workspace_write")
        room_id = created["room"]["id"]
        await within(self.work_started.wait(), 5)
        state = await self.service.read_room(room_id)
        await self.service.send_message(room_id, QUESTION, "codex", str(uuid4()), intent="continue", binding=self.binding(state))
        await within(self._first_reply_killed(), 5)
        self.hold_work.set()
        await self.settle()
        return room_id, await self.service.read_room(room_id)

    async def test_a_reply_killed_before_it_reported_its_session_is_recovered_and_answered(self):
        # Catches: a CLI killed in its first seconds (no session id yet) stopping the
        # room as session_identity_unknown until the user pressed 继续, though the
        # host's own audited fresh-session recovery was ready.
        self.kill_tools, self.kill_before_session = 0, True
        room_id, stopped = await self.kill_reply_during_check()
        self.assertEqual(stopped["team"]["stop_reason"], "session_identity_unknown")
        self.assertTrue(stopped["current_run"]["can_recover_native"], stopped["current_run"]["native_recovery_reason"])
        self.assertEqual(stopped["stop_kind"], "auto")
        await self.watchdog(room_id)
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(self.answered_by("codex", QUESTION)), 2)
        self.assertTrue(all(call["session_id"] is None for call in self.answered_by("codex", QUESTION)[1:]))

    async def test_an_unknown_session_that_ran_tools_stays_for_the_user(self):
        # Catches: the host continuing an unknown session past the audit, when a
        # call that may have acted cannot prove whose session it was.
        self.kill_tools, self.kill_before_session = 1, True
        room_id, stopped = await self.kill_reply_during_check()
        self.assertEqual(stopped["team"]["stop_reason"], "session_identity_unknown")
        self.assertFalse(stopped["current_run"]["can_recover_native"])
        self.assertIsNone(stopped["stop_kind"])
        calls = len(self.fake.calls)
        await self.watchdog(room_id)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual((await self.service.read_room(room_id))["current_run"]["id"], stopped["current_run"]["id"])

    async def _first_reply_killed(self):
        while not any(call["context"]["job"]["job_phase"] == "user" for call in self.fake.calls):
            await asyncio.sleep(0.01)

    async def test_a_question_whose_reply_was_killed_twice_is_still_answered(self):
        # Catches: the redelivered question being dropped when its second call dies too.
        self.kill_replies, self.hold_work = 2, asyncio.Event()
        created = await self.create(permission="workspace_write")
        room_id = created["room"]["id"]
        await within(self.work_started.wait(), 5)
        state = await self.service.read_room(room_id)
        await self.service.send_message(room_id, QUESTION, "codex", str(uuid4()), intent="continue", binding=self.binding(state))
        await within(self._first_reply_killed(), 5)
        self.hold_work.set()
        await self.settle()
        await self.watchdog(room_id, passes=6)
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(self.answered_by("codex", QUESTION)), 3)

    async def test_only_the_users_own_unanswered_words_are_sent_again(self):
        # Catches: redelivery replaying host-written "user" phase calls (a partner
        # handoff inspection, a reply-format correction), or a message whose
        # corrected reply already answered it.
        self.hold_work = asyncio.Event()
        room_id = (await self.create(permission="workspace_write"))["room"]["id"]
        await within(self.work_started.wait(), 5)  # The objective runs; codex is idle and could take a call.
        store = self.service.store
        def user_call(db, run_id, text, status="cancelled", correction_of=None):
            job_id = str(uuid4())
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (job_id, room_id, "codex", "team_user", status, text, None, None, None, 0))
            db.execute("INSERT INTO team_jobs(job_id,room_id,phase,task_id,plan_revision,result_version,run_id,correction_of) "
                       "VALUES (?,?,'user',NULL,0,0,?,?)", (job_id, room_id, run_id, correction_of))
            return job_id
        def redelivered(db, run_id):
            store._team.redeliver_unanswered(db, room_id, run_id)
            texts = [row[0] for row in db.execute("SELECT text FROM jobs WHERE room_id=? AND provider='codex' "
                                                  "AND status='queued'", (room_id,))]
            db.execute("UPDATE jobs SET status='cancelled' WHERE room_id=? AND status='queued' AND provider='codex'", (room_id,))
            return texts
        with store._connection(write=True) as db:
            run_id = store._team.team(db, room_id)["current_run_id"]
            inspection = user_call(db, run_id, "Inspect the retained private work.")
            store._event(db, room_id, "partner_handoff_inspection", sender="system", job_id=inspection)
            self.assertEqual(redelivered(db, run_id), [])
            answered = user_call(db, run_id, "An earlier question.", status="failed")
            user_call(db, run_id, "Correct your reply format.", status="completed", correction_of=answered)
            self.assertEqual(redelivered(db, run_id), [])
            # Listed first, the correction would take codex's one queue slot if it were mistaken for the user's words.
            killed, correction = str(uuid4()), str(uuid4())
            for job_id, text, status in ((correction, "Correct your reply format.", "unknown"), (killed, QUESTION, "failed")):
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (job_id, room_id, "codex", "team_user", status, text, None, None, None, 0))
            for job_id, source in ((correction, killed), (killed, None)):
                db.execute("INSERT INTO team_jobs(job_id,room_id,phase,task_id,plan_revision,result_version,run_id,correction_of) "
                           "VALUES (?,?,'user',NULL,0,0,?,?)", (job_id, room_id, run_id, source))
            self.assertEqual(redelivered(db, run_id), [QUESTION])
        self.hold_work.set()
        await self.settle()

    # The host's automatic continue and the user's message at the same moment.

    async def stopped_after_a_crash(self):
        self.crash_work = 1
        created = await self.create(permission="workspace_write")
        await self.settle()
        room_id = created["room"]["id"]
        state = await self.service.read_room(room_id)
        self.assertEqual(state["stop_kind"], "auto", state["team"])
        return room_id, state

    async def send(self, room_id, binding, *, target="all", request_id=None):
        return await self.service.send_message(room_id, NOTE, target, request_id or str(uuid4()),
                                               intent="continue", binding=binding)

    async def delivered_once(self, room_id):
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertEqual(sum(event["kind"] == "message" and event.get("text") == NOTE for event in final["events"]), 1)
        self.assertTrue(any(NOTE in call["prompt"] for call in self.fake.calls))
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        return final

    async def test_a_message_sent_while_the_host_recovers_is_delivered(self):
        # Catches: the host's own recovery held the run while the user sent; the
        # message was refused as team_run_conflict and recorded nowhere.
        room_id, state = await self.stopped_after_a_crash()
        entered, release = threading.Event(), threading.Event()
        audit = self.service._audit_native_recovery
        def held(*args, **kwargs):
            if threading.current_thread() is not threading.main_thread():  # the recovery's own audit, not a page read
                entered.set()
                release.wait(5)
            return audit(*args, **kwargs)
        request_id = str(uuid4())
        with patch.object(self.service, "_audit_native_recovery", side_effect=held):
            auto = self.auto_continue_now(room_id)
            await within(asyncio.to_thread(entered.wait, 5), 6)
            user = asyncio.create_task(self.send(room_id, self.binding(state), request_id=request_id))
            await asyncio.sleep(0.05)
            self.assertFalse(user.done())
            release.set()
            await within(auto, 10)
            await within(user, 10)
        await self.delivered_once(room_id)
        # The client retrying the same request with the binding it saw changes nothing.
        await self.send(room_id, self.binding(state), request_id=request_id)
        await self.delivered_once(room_id)
        self.assertTrue(await self.service.read_message_receipt(room_id, NOTE, "all", request_id,
                                                                intent="continue", binding=self.binding(state)))

    async def test_a_message_from_a_page_that_missed_the_automatic_continue_is_delivered(self):
        # Catches: the page still bound to the stopped run when the host had just
        # continued; the message was refused although the user chose nothing stale.
        room_id, state = await self.stopped_after_a_crash()
        hold = asyncio.Event()
        self.hold_work = hold
        await within(self.auto_continue_now(room_id), 10)
        await self.send(room_id, self.binding(state), target="codex")
        hold.set()
        await self.delivered_once(room_id)

    async def test_a_reply_to_a_host_block_the_host_reopens_meanwhile_is_delivered(self):
        # Catches: the host reopening its own task block while the user's reply
        # waited; the reply was refused as objective_answer_unavailable.
        self.wrong_task = 3
        room_id = (await self.create(permission="workspace_write"))["room"]["id"]
        await self.settle()
        state = await self.service.read_room(room_id)
        self.assertEqual(self.service._natural_continuation(state)["action"], "answer")
        entered, release = asyncio.Event(), asyncio.Event()
        guard = self.service._guard_objective_availability
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await guard(*args, **kwargs)
        with patch.object(self.service, "_guard_objective_availability", side_effect=held):
            user = asyncio.create_task(self.send(room_id, self.binding(state)))
            await within(entered.wait(), 5)
            await within(self.auto_continue_now(room_id), 10)
            release.set()
            await within(user, 10)
        await self.delivered_once(room_id)

    async def test_a_stale_message_after_the_users_own_restart_is_still_refused(self):
        # Catches: re-deciding any stale message, not only one the host's
        # automatic continue overtook; the user's own later action stands.
        room_id, state = await self.stopped_after_a_crash()
        hold = asyncio.Event()
        self.hold_work = hold
        await self.service.recover_native(room_id, str(uuid4()), **self.binding(state))
        with self.assertRaises(RoomError) as refused:
            await self.send(room_id, self.binding(state))
        self.assertEqual(refused.exception.code, "team_run_conflict")
        hold.set()
        await self.settle()


if __name__ == "__main__":
    unittest.main()
