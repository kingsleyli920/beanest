"""Objective integration uses real SQLite/private artifacts and fake transport.

No model, external project, native account query or Docker process is started.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_service import RoomService, RoomError
from local_agent_coordinator.room_store import RoomStore
from test_team_service import FakeTeamProvider, reply, changed_file
from test_team_store import proposal


POLICY = {"mode": "autonomous", "quota_failover": True}


class ObjectiveIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.fake = FakeTeamProvider()
        self.now = 1000.0
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   invoke=self.fake, clock=lambda: self.now)
        self.rounds = 1
        self.rejected = False

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def settle(self):
        async def wait():
            for _ in range(100):
                tasks = [task for task in self.service._workers.values() if not task.done()]
                tasks += [task for task in self.service._executions.values() if not task.done()]
                if not tasks:
                    return
                await asyncio.gather(*tasks)
            self.fail("fake scheduler did not settle")
        # A hang guard, not a speed assertion: 32 calls and 8 stages take
        # about 2.5 s locally and exceeded 5 s on slower CI runners.
        await asyncio.wait_for(wait(), 30)
        self.assertIsNone(self.service._background_error)
        self.assertFalse(self.fake.assertion_failures)

    def handler(self, agent, context):
        phase, board = context["job"]["job_phase"], context["team"]
        objective = context.get("objective")
        stage = objective["stage"] if objective else 1
        if phase == "checkpoint":
            continuing = stage < self.rounds
            return reply(context, commands=[{
                "type": "checkpoint", "decision": "continue" if continuing else "complete",
                "summary": "There is one remaining acceptance item." if continuing else "All requested items are delivered.",
                "next_goal": "Finish the remaining requested acceptance item." if continuing else None,
                "scope_reference": objective["original_goal"][:16], "question": None}])
        if phase == "checkpoint_review":
            return reply(context, commands=[{"type": "checkpoint_review", "verdict": "accept",
                "reason": "Checked the original goal and exact delivery evidence.",
                "proposal_digest": objective["checkpoint_proposal"]["proposal_digest"]}])
        if phase in {"plan", "coordinate"}:
            commands = []
            tasks = {task["id"]: task for task in board["tasks"]}
            if not tasks and agent == board["plan_author"]:
                proposals = [proposal("main", paths=[f"stage{stage}.py"], capability="python")]
                commands += proposals
                tasks = {task["id"]: {"owner": None} for task in proposals}
            if "main" in tasks and tasks["main"]["owner"] is None and agent == board["plan_author"]:
                commands.append({"type": "claim", "task_id": "main"})
            if tasks:
                commands.append({"type": "plan_ready"})
            return reply(context, commands=commands)
        task = context["task"]
        if phase == "work":
            return reply(context, commands=[{"type": "complete", "task_id": task["id"], "result": "Exact requested file saved."}],
                         files=[changed_file(context, task["write_paths"][0], f"VALUE = {stage}\n")])
        if phase == "review":
            if task["owner"] == agent:
                self.assertEqual(task["review_kind"], "self_check")
            else:
                self.assertEqual(task["review_kind"], "independent_review")
            return reply(context, commands=[{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Checked exact saved file."}])
        self.fail("unexpected phase " + phase)

    async def create(self, **kwargs):
        self.fake.handler = self.handler
        return await self.service.create_room("Build the two requested acceptance items.", "claude", str(uuid4()),
                                             mode="python_artifacts", work_mode="build", execution_policy=POLICY, **kwargs)

    async def test_two_stages_keep_one_total_ledger_without_accepting_or_resetting_deadline(self):
        self.rounds = 2
        created = await self.create()
        initial_deadline = created["objective"]["deadline"]
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(result["objective"]["stage"], 2)
        self.assertEqual(result["objective"]["deadline"], initial_deadline)
        self.assertEqual(result["objective"]["budget"]["used"], len(self.fake.calls))
        self.assertEqual(sum(run["budget"]["used"] for run in result["runs"]), len(self.fake.calls))
        self.assertTrue(all(run["accepted_at"] is None for run in result["runs"]))
        self.assertEqual(result["objective"]["delivery"]["total"], 2)
        self.assertTrue(result["objective"]["delivery"]["verified"])
        self.assertFalse((await self.service.status())["autonomous_work_active"])

    async def test_useful_automatic_work_passes_eight_stages_and_thirty_two_calls(self):
        self.rounds = 10
        created = await self.create()
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete", result["team"])
        self.assertEqual(result["objective"]["stage"], 10)
        self.assertGreater(result["objective"]["budget"]["used"], 32)
        self.assertIsNone(result["objective"]["budget"]["limit"])
        self.assertIsNone(result["objective"]["deadline"])
        self.assertEqual(result["objective"]["budget"]["used"], len(self.fake.calls))
        self.assertTrue(all(run["accepted_at"] is None for run in result["runs"]))
        self.assertEqual(result["objective"]["delivery"]["total"], 10)

    async def test_checkpoint_transition_waits_for_final_cleanup_receipt_then_wakes_once(self):
        self.rounds = 2
        record = self.service.store.record_job_cleanup
        held = []
        def cleanup(job_id, *, confirmed):
            context = self.service.store._team
            with self.service.store._connection() as db:
                job = context.job_context(db, job_id)
            if job and job["job_phase"] == "checkpoint_review" and not held:
                held.append((job_id, confirmed))
                return
            record(job_id, confirmed=confirmed)
        self.service.store.record_job_cleanup = cleanup
        created = await self.create()
        await self.settle()
        room_id = created["room"]["id"]
        paused_boundary = await self.service.read_room(room_id)
        self.assertEqual(paused_boundary["objective"]["stage"], 1)
        self.assertEqual(paused_boundary["objective"]["status"], "awaiting_continue")
        self.assertFalse(self.service.store.stage_cleanup_confirmed(room_id))
        self.assertEqual(len(held), 1)
        record(held[0][0], confirmed=held[0][1])
        self.service._wake(room_id)
        self.service._wake(room_id)
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["stage"], 2)
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "run_started"]), 1)

    async def test_explicit_single_member_has_honest_self_check_and_checkpoint(self):
        created = await self.create(participants=["claude"])
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(result["team"]["tasks"][0]["status"], "self_checked")
        self.assertEqual(result["collaboration"]["outcome"]["validation"]["scope"], "self_check")
        self.assertEqual(result["objective"]["next_step"]["review_kind"], "self_check")
        self.assertIn("自检", result["collaboration"]["outcome"]["reason"])
        self.assertNotIn("交付并互审", result["collaboration"]["outcome"]["reason"])

    async def test_former_manual_policy_continues_without_a_new_approval_step(self):
        self.rounds = 2
        self.fake.handler = self.handler
        created = await self.service.create_room("Build the two requested acceptance items.", "claude", "manual",
             mode="python_artifacts", work_mode="build", execution_policy={**POLICY, "mode": "manual"})
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(result["objective"]["execution_policy"]["mode"], "autonomous")
        self.assertEqual(result["objective"]["stage"], 2)
        self.assertTrue(all(run["accepted_at"] is None for run in result["runs"]))
        summary = next(room for room in (await self.service.list_rooms())["rooms"] if room["id"] == created["room"]["id"])
        self.assertEqual(summary["objective_status"], "complete")
        self.assertEqual(summary["objective_goal"], result["objective"]["original_goal"])

    async def test_out_of_scope_suggestion_never_auto_starts(self):
        original = self.handler
        def handler(agent, context):
            result = original(agent, context)
            if context["job"]["job_phase"] == "checkpoint":
                result["commands"][0].update(decision="continue", next_goal="Build an unrelated product.", scope_reference="unrelated")
            return result
        self.fake.handler = handler
        created = await self.service.create_room("Build a bounded original item.", "claude", "scope", mode="python_artifacts", execution_policy=POLICY)
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "needs_user")
        self.assertFalse(result["objective"]["next_step"]["can_start"])
        self.assertEqual(result["objective"]["stage"], 1)
        self.assertTrue(result["current_run"]["can_start_suggested_goal"])

    async def test_user_decision_continues_same_objective_and_total_budget(self):
        original = self.handler
        def handler(agent, context):
            result = original(agent, context)
            if context["job"]["job_phase"] == "checkpoint" and context["objective"]["stage"] == 1:
                result["commands"][0].update(decision="needs_user", question="Which value should the last item use?",
                                              next_goal="Finish the remaining acceptance item.")
            if context["objective"]["stage"] == 2:
                self.assertEqual(context["objective"]["user_decision"]["text"], "Use the agreed value 2.")
            return result
        self.fake.handler = handler
        created = await self.service.create_room("Build two bounded acceptance items.", "claude", "decision",
            mode="python_artifacts", execution_policy=POLICY)
        await self.settle()
        before = await self.service.read_room(created["room"]["id"])
        self.assertTrue(before["objective"]["can_answer"])
        binding = before["objective"]["next_step"]["binding"]
        objective_id, deadline, used = before["objective"]["id"], before["objective"]["deadline"], before["objective"]["budget"]["used"]
        with self.assertRaisesRegex(RoomError, "objective_answer_required"):
            await self.service.start_run(created["room"]["id"], "Use 2", "all", "wrong-reset", **binding, execution_policy=POLICY)
        await self.service.objective_action(created["room"]["id"], "answer", "answer", objective_id=objective_id,
                                            text="Use the agreed value 2.", **binding)
        await self.settle()
        result = await self.service.objective_action(created["room"]["id"], "answer", "answer", objective_id=objective_id,
                                                    text="Use the agreed value 2.", **binding)
        self.assertEqual(result["objective"]["id"], objective_id)
        self.assertEqual(result["objective"]["deadline"], deadline)
        self.assertEqual(result["objective"]["stage"], 2)
        self.assertGreater(result["objective"]["budget"]["used"], used)
        self.assertEqual(result["objective"]["budget"]["used"], len(self.fake.calls))

    async def test_natural_reply_answers_a_non_native_decision_in_the_same_objective(self):
        original = self.handler
        update = "Use the agreed value two and continue."
        def handler(agent, context):
            value = original(agent, context)
            if context["job"]["job_phase"] == "checkpoint" and context["objective"]["stage"] == 1:
                value["commands"][0].update(decision="needs_user", question="Which value is required?",
                                            next_goal="Finish the remaining acceptance item.")
            if context["objective"]["stage"] == 2:
                self.assertEqual(context["objective"]["user_decision"]["text"], update)
            return value
        self.fake.handler = handler
        created = await self.service.create_room("Build the agreed acceptance items.", "claude", "natural-decision",
            mode="python_artifacts", execution_policy=POLICY)
        await self.settle()
        before = await self.service.read_room(created["room"]["id"])
        self.assertTrue(before["continuation"]["can_send"])
        await self.service.send_message(created["room"]["id"], update, "all", "natural-answer",
                                        intent="continue", binding=before["continuation"]["binding"])
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["id"], before["objective"]["id"])
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(final["objective"]["deadline"], before["objective"]["deadline"])
        self.assertGreater(final["objective"]["budget"]["used"], before["objective"]["budget"]["used"])

    async def test_task_block_stops_autonomy_and_quota_does_not_erase_question(self):
        original = self.handler
        def handler(agent, context):
            if context["job"]["job_phase"] == "work" and context["objective"]["stage"] == 1:
                return reply(context, commands=[{"type": "block", "task_id": "main", "reason": "Choose the required output value."}])
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room("Build the agreed bounded item.", "claude", "blocked-decision",
            mode="python_artifacts", execution_policy=POLICY)
        await self.settle()
        room_id = created["room"]["id"]
        before = await self.service.read_room(room_id)
        self.assertEqual(before["objective"]["status"], "needs_user")
        self.assertEqual(before["team"]["phase"], "working")
        self.assertEqual(before["team"]["tasks"][0]["status"], "blocked")
        self.assertIn("Choose the required output value", before["objective"]["next_step"]["question"])
        self.assertFalse((await self.service.status())["autonomous_work_active"])
        count = len(self.fake.calls)
        self.service._retire_quota(room_id, "claude", "agent_quota_exhausted")
        self.service._wake(room_id)
        await self.settle()
        blocked = await self.service.read_room(room_id)
        self.assertEqual(len(self.fake.calls), count)
        self.assertEqual(blocked["team"]["tasks"][0]["owner"], "claude")
        self.assertEqual(blocked["team"]["tasks"][0]["status"], "blocked")
        binding = blocked["objective"]["next_step"]["binding"]
        await self.service.objective_action(room_id, "answer", "answer-block", **binding,
            objective_id=blocked["objective"]["id"], text="Use value 2.")
        await self.settle()
        after = await self.service.read_room(room_id)
        self.assertEqual(after["objective"]["id"], before["objective"]["id"])
        self.assertEqual(after["objective"]["deadline"], before["objective"]["deadline"])
        self.assertGreater(after["objective"]["budget"]["used"], before["objective"]["budget"]["used"])
        self.assertEqual(after["objective"]["status"], "complete")
        old = self.service.store.read_run(room_id, before["current_run"]["id"])
        self.assertEqual(old["team"]["tasks"][0]["status"], "blocked")

    async def test_quota_refusal_keeps_started_call_and_peer_takes_unfinished_work(self):
        original = self.handler
        failed = False
        def handler(agent, context):
            nonlocal failed
            if context["job"]["job_phase"] == "work" and agent == "claude" and not failed:
                failed = True
                return {"adapter_failure": "subscription_limit_reached"}
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room("Build one acceptance item.", "claude", "failover", mode="python_artifacts", execution_policy=POLICY)
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertTrue(failed)
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(result["team"]["tasks"][0]["owner"], "codex")
        self.assertEqual(result["team"]["tasks"][0]["status"], "self_checked")
        self.assertEqual(result["objective"]["budget"]["used"], len(self.fake.calls))
        self.assertEqual(sum(job["status"] == "failed" for job in result["jobs"]), 1)

    async def test_every_partner_out_of_quota_waits_then_continues_the_same_board_when_one_is_back(self):
        # Catches: once every partner was out of quota the room stayed stopped
        # after the quota came back, until the user pressed continue by hand.
        original, exhausted = self.handler, {"claude", "codex"}
        def handler(agent, context):
            if agent in exhausted and context["job"]["job_phase"] == "work":
                return {"adapter_failure": "subscription_limit_reached"}
            return original(agent, context)
        async def guard(members):
            if any(member in exhausted for member in members):
                raise RoomError("agent_quota_exhausted")
        created = await self.create()
        self.fake.handler = handler
        await self.settle()
        room = created["room"]["id"]
        stopped = await self.service.read_room(room)
        self.assertEqual(stopped["team"]["stop_reason"], "all_partners_quota_exhausted")
        self.assertEqual(sorted(stopped["objective"]["retired_partners"]), ["claude", "codex"])
        self.service._guard_availability = guard
        calls = len(self.fake.calls)

        async def tick():
            await self.service.expire_tasks()
            wait = self.service._quota_waits.get(room)
            if wait is not None:
                await wait["task"]
            await self.settle()
            return await self.service.read_room(room)

        waiting = await tick()
        self.assertEqual(waiting["current_run"]["id"], stopped["current_run"]["id"])
        self.assertEqual(waiting["team"]["phase"], "stopped")
        self.assertEqual(len(self.fake.calls), calls)
        exhausted.discard("codex")
        self.now += 60
        # Rechecks are spaced out, so a quota that just returned waits for the next one.
        self.assertEqual((await tick())["current_run"]["id"], stopped["current_run"]["id"])
        self.now += 300
        final = await tick()
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertNotEqual(final["current_run"]["id"], stopped["current_run"]["id"])
        self.assertEqual(final["objective"]["continuation"]["preserved_task_ids"], ["main"])
        self.assertEqual(final["team"]["tasks"][0]["owner"], "codex")
        self.assertFalse(any(call["agent"] == "claude" for call in self.fake.calls[calls:]))
        self.assertTrue(any(event["kind"] == "quota_wait_resumed" for event in final["events"]))

    async def tick(self, room):
        # One watchdog pass of the desktop service, then let what it started finish.
        await self.service.expire_tasks()
        for state in list(self.service._auto_continues.values()):
            if state["task"] is not None:
                await state["task"]
        await self.settle()
        return await self.service.read_room(room)

    async def failing_once(self, failures):
        original = self.handler
        def handler(agent, context):
            if context["job"]["job_phase"] == "work" and failures:
                return {"adapter_failure": failures.pop(0)}
            return original(agent, context)
        created = await self.create()
        self.fake.handler = handler
        await self.settle()
        return created["room"]["id"]

    async def test_a_stop_the_user_asked_for_is_never_continued_automatically(self):
        room = await self.failing_once(["cli_nonzero_exit"])
        stopped = await self.service.read_room(room)
        await self.service.cancel_room(room)
        for _ in range(3):
            self.now += 20
            value = await self.tick(room)
        self.assertEqual(value["current_run"]["id"], stopped["current_run"]["id"])
        self.assertFalse(any(event["kind"] == "objective_auto_continued" for event in value["events"]))

    async def test_preclaim_quota_reassigns_planning_without_charging_retired_member(self):
        async def guard(members):
            if "claude" in members:
                raise RoomError("agent_quota_exhausted")
        self.service._guard_availability = guard
        created = await self.create()
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(result["team"]["plan_author"], "codex")
        self.assertFalse(any(call["agent"] == "claude" for call in self.fake.calls))
        self.assertEqual(result["objective"]["budget"]["used"], len(self.fake.calls))
        self.assertEqual(result["team"]["tasks"][0]["status"], "self_checked")

    async def test_selected_file_seed_assignment_is_not_replayed_on_quota_transfer(self):
        source = self.root / "private-source"
        source.mkdir(mode=0o700)
        (source / "stage1.py").write_text("VALUE = 0\n")
        project = (await self.service.register_project("Private fixture", str(source), ["stage1.py"]))["project"]
        original = self.handler
        def handler(agent, context):
            if context["job"]["job_phase"] == "work" and agent == "claude":
                return {"adapter_failure": "subscription_limit_reached"}
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room("Improve only the selected file.", "claude", "selected-transfer",
            mode="project_files", project_id=project["id"], execution_policy=POLICY)
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(result["project"]["files"][0]["owner"], "codex")
        self.assertEqual((source / "stage1.py").read_text(), "VALUE = 0\n")

    async def test_pause_waits_for_current_cleanup_and_preserves_queued_work(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.handler
        async def handler(agent, context):
            if context["job"]["job_phase"] == "work":
                entered.set()
                await release.wait()
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room("Build one acceptance item.", "claude", "pause", mode="python_artifacts", execution_policy=POLICY)
        await asyncio.wait_for(entered.wait(), 2)
        room_id, objective_id = created["room"]["id"], created["objective"]["id"]
        paused = await self.service.objective_action(room_id, "pause", "pause-1", objective_id=objective_id)
        self.assertTrue(paused["objective"]["pause_requested"])
        release.set()
        await self.settle()
        paused = await self.service.read_room(room_id)
        self.assertEqual(paused["objective"]["status"], "paused")
        self.assertFalse(paused["objective"]["pause_requested"])
        calls = len(self.fake.calls)
        await asyncio.sleep(0)
        self.assertEqual(len(self.fake.calls), calls)
        await self.service.objective_action(room_id, "resume", "resume-1", objective_id=objective_id)
        await self.settle()
        self.assertEqual((await self.service.read_room(room_id))["objective"]["status"], "complete")

    async def test_repeated_retirement_keeps_inflight_reviewer_then_transfers_rejected_file(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.handler
        first_review = True
        async def handler(agent, context):
            nonlocal first_review
            if context["job"]["job_phase"] == "review" and first_review:
                first_review = False
                self.assertEqual(agent, "codex")
                entered.set()
                await release.wait()
                return reply(context, commands=[{"type": "review", "task_id": "main", "verdict": "changes_requested",
                                                   "reason": "The saved value must be 9."}])
            if context["job"]["job_phase"] == "work" and agent == "codex":
                self.assertEqual(context["host_authorized_transfer_paths"], ["stage1.py"])
                return reply(context, commands=[{"type": "complete", "task_id": "main", "result": "Corrected exact transferred file."}],
                             files=[changed_file(context, "stage1.py", "VALUE = 9\n")])
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room("Build one acceptance item.", "claude", "retired-review", mode="python_artifacts", execution_policy=POLICY)
        await asyncio.wait_for(entered.wait(), 2)
        room_id = created["room"]["id"]
        for _ in range(3):
            self.assertTrue(self.service._retire_quota(room_id, "claude", "agent_quota_exhausted"))
            task = self.service.store.snapshot(room_id)["team"]["tasks"][0]
            self.assertEqual(task["reviewer"], "codex")
            self.assertEqual(task["assignment_epoch"], 0)
        release.set()
        await self.settle()
        result = await self.service.read_room(room_id)
        task = result["team"]["tasks"][0]
        self.assertEqual(task["owner"], "codex")
        self.assertEqual(task["assignment_epoch"], 1)
        self.assertEqual(task["result_version"], 2)
        self.assertEqual(task["status"], "self_checked")
        self.assertEqual(result["project"]["files"][0]["content"], "VALUE = 9\n")
        self.assertEqual(result["project"]["files"][0]["owner"], "codex")
        self.assertEqual(result["objective"]["status"], "complete")

    async def test_later_stage_can_modify_retired_accepted_file_with_exact_grant(self):
        self.rounds = 2
        original = self.handler
        refused = False
        def handler(agent, context):
            nonlocal refused
            phase, stage = context["job"]["job_phase"], context["objective"]["stage"]
            if phase == "checkpoint" and agent == "claude" and not refused:
                refused = True
                return {"adapter_failure": "subscription_limit_reached"}
            result = original(agent, context)
            if phase in {"plan", "coordinate"} and stage == 2:
                for command in result["commands"]:
                    if command["type"] == "propose":
                        command["write_paths"] = ["stage1.py"]
            return result
        self.fake.handler = handler
        created = await self.service.create_room("Build the two requested acceptance items.", "claude", "cross-stage", mode="python_artifacts", execution_policy=POLICY)
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertTrue(refused)
        self.assertEqual(result["objective"]["stage"], 2)
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(result["project"]["files"][0]["owner"], "codex")
        self.assertEqual(result["project"]["files"][0]["content"], "VALUE = 2\n")
        old = self.service.store.read_run(created["room"]["id"], result["runs"][0]["id"])
        self.assertEqual(old["team"]["tasks"][0]["owner"], "claude")
        self.assertEqual(old["team"]["tasks"][0]["status"], "accepted")

    async def test_old_accepted_baseline_reads_exact_history_after_new_head_overwrite(self):
        self.fake.handler = self.handler
        created = await self.service.create_room("Build one original item.", "claude", "legacy-baseline", mode="python_artifacts", collaboration=True)
        await self.settle()
        ready = await self.service.read_room(created["room"]["id"])
        source = ready["current_run"]
        binding = {"run_id": source["id"], "revision": source["revision"], "digest": source["digest"]}
        await self.service.accept_run(created["room"]["id"], "user-accepted", **binding)
        assessed = await self.service.objective_action(created["room"]["id"], "objective", "arrange-next",
            execution_policy=POLICY, **binding)
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        baseline = result["objective"]["baseline"]
        self.assertEqual(result["objective"]["delivery"]["total"], 0)
        self.assertEqual(baseline["run_id"], source["id"])
        self.assertTrue(next(run for run in result["runs"] if run["id"] == source["id"])["accepted_at"])
        file = baseline["files"][0]
        original = self.handler
        def changed_handler(agent, context):
            result = original(agent, context)
            if context["job"]["job_phase"] == "work":
                result["files"] = [changed_file(context, "stage1.py", "VALUE = 99\n")]
            return result
        self.fake.handler = changed_handler
        current = result["current_run"]
        await self.service.start_run(created["room"]["id"], "Explicit new goal using the same owned file.", "claude", "new-head",
            run_id=current["id"], revision=current["revision"], digest=current["digest"], execution_policy=POLICY)
        await self.settle()
        head = await self.service.read_room(created["room"]["id"])
        self.assertEqual(head["project"]["files"][0]["content"], "VALUE = 99\n")
        history = await self.service.read_delivery_file(created["room"]["id"], **binding, path=file["path"], sha256=file["sha256"])
        self.assertEqual(history["content"], "VALUE = 1\n")
        with self.assertRaises(RoomError):
            await self.service.read_delivery_file(created["room"]["id"], **{**binding, "digest": "0" * 64}, path=file["path"], sha256=file["sha256"])

    async def test_sandbox_execution_and_bounded_repair_finish_before_checkpoint(self):
        from test_room_execution import FakeRunner
        runner = FakeRunner()
        runner.handler = lambda call: {"status": "completed", "exit_code": 1 if len(runner.calls) == 1 else 0,
            "stdout": "fixture output", "stderr": "", "cleanup_confirmed": True, "reason": None}
        self.service._runner_instance = runner
        original = self.handler
        def handler(agent, context):
            result = original(agent, context)
            if context["job"]["job_phase"] == "work" and context["job"].get("repair_execution_id"):
                result["files"] = [changed_file(context, context["task"]["write_paths"][0], "VALUE = 9\n")]
            if context["job"]["job_phase"] in {"checkpoint", "checkpoint_review"}:
                self.assertEqual(len(runner.calls), 2)
                observation = context["execution_observation"]
                self.assertEqual(observation["team_run_id"], context["job"]["run_id"])
                self.assertEqual(observation["revision"], context["project"]["revision"])
                self.assertEqual(observation["result"]["exit_code"], 0)
                self.assertTrue(observation["result"]["cleanup_confirmed"])
                self.assertEqual(observation["result"]["report_trust"], "untrusted")
            return result
        self.fake.handler = handler
        created = await self.service.create_room("Build a checked acceptance item.", "claude", "sandbox-objective",
            mode="python_sandbox", work_mode="build", execution_policy=POLICY, participants=["claude"])
        await self.settle()
        result = await self.service.read_room(created["room"]["id"])
        self.assertEqual(result["objective"]["status"], "complete")
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(result["team"]["tasks"][0]["status"], "self_checked")
        self.assertEqual(result["team"]["tasks"][0]["result_version"], 2)
        self.assertEqual(result["collaboration"]["outcome"]["validation"]["scope"], "sandbox_execution")


class ObjectiveStoreBoundaryTests(unittest.TestCase):
    def test_checkpoint_commands_are_phase_bound_and_review_reason_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RoomStore(Path(temp) / "rooms.sqlite3")
            try:
                room, _ = store.create_room("Bounded original objective", "claude", "phase-bound", execution_policy=POLICY)
                job = store.claim(room, "claude")
                command = {"type": "checkpoint", "decision": "complete", "summary": "Done", "next_goal": None,
                           "scope_reference": None, "question": None}
                with self.assertRaisesRegex(RoomError, "team_task_permission"):
                    store.prevalidate_team_result(job["id"], [command])
                with store._connection(write=True) as db:
                    db.execute("UPDATE jobs SET status='completed' WHERE room_id=?", (room,))
                    db.execute("UPDATE teams SET phase='ready_for_user' WHERE room_id=?", (room,))
                    binding = {"run_id": store._team.team(db, room)["current_run_id"], "revision": 0, "digest": "0" * 64}
                store.record_job_cleanup(job["id"], confirmed=True)
                self.assertTrue(store.begin_objective_checkpoint(room, binding))
                proposal_job = store.claim(room, "claude")
                store.finish_job(proposal_job["id"], message="Checked goal", team_commands=[command])
                store.record_job_cleanup(proposal_job["id"], confirmed=True)
                reviewer = store.claim(room, "codex")
                value = store.snapshot(room)["objective"]["checkpoint_proposal"]
                review = {"type": "checkpoint_review", "verdict": "changes_requested", "reason": None,
                          "proposal_digest": value["proposal_digest"]}
                with self.assertRaisesRegex(RoomError, "invalid_text"):
                    store.prevalidate_team_result(reviewer["id"], [review])
            finally:
                store.close()

    def test_last_authorized_calls_are_not_invalidated_by_another_scheduler_wake(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RoomStore(Path(temp) / "rooms.sqlite3")
            try:
                room, _ = store.create_room("One bounded item", "claude", "last-call", execution_policy={**POLICY, "max_calls": 2})
                author, peer = store.claim(room, "claude"), store.claim(room, "codex")
                store.reconcile_team(room)
                self.assertEqual(store.snapshot(room)["team"]["phase"], "planning")
                store.finish_job(author["id"], message="Planned actual work", team_commands=[proposal("main"),
                    {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
                store.reconcile_team(room)
                self.assertEqual(store.snapshot(room)["team"]["phase"], "planning")
                self.assertTrue(store.prevalidate_team_result(peer["id"], [{"type": "plan_ready"}]))
                store.finish_job(peer["id"], message="Confirmed exact plan", team_commands=[{"type": "plan_ready"}])
                result = store.snapshot(room)
                self.assertEqual([job["status"] for job in result["jobs"][:2]], ["completed", "completed"])
                # Already leased results stay valid; the explicit total cap
                # prevents a third call once both have settled.
                self.assertFalse(any(job["status"] in {"queued", "working"} for job in result["jobs"]))
                self.assertEqual(result["objective"]["stop_reason"], "objective_calls_exhausted")
                self.assertEqual(result["objective"]["budget"]["used"], 2)
                self.assertIsNone(store.claim(room, "codex"))
            finally:
                store.close()

    def test_policy_normalization_does_not_break_replay_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RoomStore(Path(temp) / "rooms.sqlite3")
            try:
                first = store.create_room("Goal", "claude", "stable", execution_policy={"mode": "autonomous"})
                self.assertEqual(store.creation_request_seen("Goal", "claude", "stable", execution_policy={"mode":"autonomous", "max_seconds":None, "max_calls":None, "quota_failover":True}), first[0])
                self.assertEqual(store.create_room("Goal", "claude", "stable", execution_policy={"mode":"autonomous", "max_seconds":None, "max_calls":None, "quota_failover":True}), (first[0], False))
            finally:
                store.close()

    def test_total_budget_claim_and_restart_do_not_reset_objective(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rooms.sqlite3"
            now = [1000.0]
            store = RoomStore(path, clock=lambda: now[0])
            room, _ = store.create_room("Bounded goal", "claude", "budget", execution_policy={**POLICY, "max_calls": 2})
            first = store.claim(room, "claude")
            second = store.claim(room, "codex")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            before = store.snapshot(room)["objective"]
            self.assertEqual(before["budget"]["used"], 2)
            store.close()
            now[0] += 120
            store = RoomStore(path, clock=lambda: now[0])
            try:
                store.recover()
                after = store.snapshot(room)["objective"]
                self.assertEqual(after["budget"]["used"], 2)
                self.assertEqual(after["deadline"], before["deadline"])
                self.assertEqual(after["status"], "interrupted")
                self.assertIsNone(store.claim(room, "claude"))
            finally:
                store.close()
