"""Atomic next-call settings: synthetic native calls, real SQLite/private trees."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError
import test_native_room_service as native
from test_team_store import proposal


def settings(model="old", effort="low", permission="full_access"):
    return {"permission": permission, "members": {agent: {"model": model, "effort": effort} for agent in ("claude", "codex")}}


class NaturalRuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler

    def catalogs(self):
        for adapter in ("claude", "codex"):
            self.service._runtime_catalogs[adapter] = {"state": "ready", "models": [
                {"id": model, "efforts": ["low", "high"]} for model in ("old", "new")]}

    async def create(self):
        self.catalogs()
        return await self.service.create_room("Update the value and preserve useful notes.", "claude", str(uuid4()),
            mode="native_project", project_id=self.registration["project"]["id"], participants=["claude", "codex"],
            work_mode="build", runtime_settings=settings(), execution_policy={"mode": "autonomous"})

    async def send(self, room, text, request_id, **update):
        snapshot = await self.service.read_room(room)
        return await self.service.send_message(room, text, "all", request_id, intent="continue",
                                               binding=snapshot["continuation"]["binding"], **update)

    async def wait_for(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_all_old_calls_and_last_cleanup_drain_before_plan_model_activation(self):
        started = {agent: asyncio.Event() for agent in ("claude", "codex")}
        release = {agent: asyncio.Event() for agent in started}
        held_cleanup = asyncio.Event()
        held_job = []
        old_cleanup = self.service.store.record_job_cleanup
        def cleanup(job_id, *, confirmed):
            snapshot = self.service.store.snapshot(room)
            job = next(item for item in snapshot["jobs"] if item["id"] == job_id)
            if job["provider"] == "codex" and job["kind"] == "team_work":
                held_job.append((job_id, confirmed))
                held_cleanup.set()
                return
            old_cleanup(job_id, confirmed=confirmed)
        async def handler(agent, context, cwd):
            phase, task, board = context["job"]["job_phase"], context["task"], context["team"]
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands.extend([proposal("T1", paths=["main.py"]), proposal("T2", paths=["notes.md"])])
                own = "T1" if agent == "claude" else "T2"
                if commands or any(item["id"] == own and item["owner"] is None for item in board["tasks"]):
                    commands.append({"type": "claim", "task_id": own})
                return native.native_reply(context, commands + [{"type": "plan_ready"}])
            if phase == "work":
                self.assertEqual(context["work_mode"], "build")
                self.assertEqual(context["runtime_settings"]["members"][agent]["model"], "old")
                started[agent].set()
                await release[agent].wait()
                (cwd / task["write_paths"][0]).write_text("Delivered " + agent + ".\n")
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Actual old-permission work delivered."}])
            self.assertEqual(context["work_mode"], "plan")
            self.assertEqual(context["native_permission"], "plan")
            self.assertEqual(context["runtime_settings"]["members"][agent]["model"], "new")
            if phase == "user":
                return native.native_reply(context)
            if phase == "review":
                return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact completed result."}])
            return self.handler(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        room = created["room"]["id"]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 4)
        old_count = len(self.fake.calls)
        updated = await self.send(room, "Review the completed work in plan mode.", "settings-drain", work_mode="plan", runtime_settings=settings("new", "high"))
        self.assertTrue(updated["runtime_configuration"]["pending"])
        self.assertEqual(updated["room"]["work_mode"], "build")
        self.assertEqual(updated["runtime_configuration"]["desired"]["runtime_settings"]["effective_permission"], "plan")
        with patch.object(self.service.store, "record_job_cleanup", side_effect=cleanup):
            release["claude"].set()
            await self.wait_for(lambda: any(event["kind"] == "job_cleanup" and event.get("provider") == "claude"
                and event.get("job_id") in {job["id"] for job in self.service.store.snapshot(room)["jobs"] if job["kind"] == "team_work"}
                for event in self.service.store.snapshot(room)["events"]))
            self.assertEqual(len(self.fake.calls), old_count)
            self.assertEqual((await self.service.read_room(room))["room"]["work_mode"], "build")
            release["codex"].set()
            await asyncio.wait_for(held_cleanup.wait(), 4)
            await self.settle()
            pending = await self.service.read_room(room)
            self.assertTrue(pending["runtime_configuration"]["pending"])
            self.assertEqual(pending["runtime_configuration"]["pending_reason"], "team_cleanup_unconfirmed")
            self.assertEqual(len(self.fake.calls), old_count)
        for job_id, confirmed in held_job:
            old_cleanup(job_id, confirmed=confirmed)
        self.service._wake(room)
        await self.settle()
        final = await self.service.read_room(room)
        self.assertFalse(final["runtime_configuration"]["pending"])
        self.assertEqual(final["room"]["work_mode"], "plan")
        self.assertEqual(final["current_run"]["runtime_settings"]["members"]["claude"]["model"], "old")
        later = self.fake.calls[old_count:]
        self.assertTrue(later)
        self.assertTrue(all(call["permission"] == "plan" and call["runtime_settings"]["model"] == "new" for call in later))
        claims = [event for event in final["events"] if event["kind"] == "job_claimed"]
        self.assertTrue(any(event["work_mode"] == "plan" and event["runtime_settings"]["members"]["claude"]["model"] == "new" for event in claims))

    async def held_work(self, *, before_invoke=False):
        started, release = asyncio.Event(), asyncio.Event()
        original_prepare = self.service._native_workspace_for_job
        if before_invoke:
            async def prepare(job):
                if job["team"]["job_phase"] == "work":
                    started.set()
                    await release.wait()
                return await original_prepare(job)
            self.service._native_workspace_for_job = prepare
        async def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "user":
                return native.native_reply(context)
            if phase == "work" and not before_invoke:
                started.set()
                await release.wait()
            return self.handler(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await asyncio.wait_for(started.wait(), 4)
        return created["room"]["id"], release

    async def test_claimed_before_invoke_keeps_old_frozen_model_and_permission(self):
        room, release = await self.held_work(before_invoke=True)
        self.assertFalse(any(call["context"]["job"]["job_phase"] == "work" for call in self.fake.calls))
        await self.send(room, "Use the next model for later calls.", "pre-invoke-change", runtime_settings=settings("new", "high"))
        release.set()
        await self.settle()
        work = next(call for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work")
        self.assertEqual(work["runtime_settings"], {"model": "old", "effort": "low"})
        self.assertEqual(work["permission"], "full_access")
        self.assertEqual(work["context"]["runtime_settings"]["members"]["claude"]["model"], "old")
        self.assertTrue(any(call.get("runtime_settings", {}).get("model") == "new" for call in self.fake.calls))

    async def test_exact_receipt_retry_and_last_accepted_desired_settings(self):
        room, release = await self.held_work()
        before = await self.service.read_room(room)
        binding = before["continuation"]["binding"]
        body = {"intent": "continue", "binding": binding, "runtime_settings": settings("new", "low")}
        await self.service.send_message(room, "Next model.", "all", "first-settings", **body)
        await self.send(room, "Keep this additional fact.", "without-settings")
        self.assertEqual((await self.service.read_room(room))["runtime_configuration"]["desired"]["runtime_settings"]["members"]["claude"]["effort"], "low")
        await self.send(room, "Use more effort subsequently.", "second-settings", runtime_settings=settings("new", "high"))
        prior = copy.deepcopy(self.service.store.snapshot(room))
        calls = len(self.fake.calls)
        self.service._runtime_catalogs.clear()
        self.assertTrue((await self.service.read_message_receipt(room, "Next model.", "all", "first-settings", **body))["received"])
        self.assertEqual(self.service.store.snapshot(room), prior)
        await self.service.send_message(room, "Next model.", "all", "first-settings", **body)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual((await self.service.read_room(room))["runtime_configuration"]["desired"]["runtime_settings"]["members"]["claude"]["effort"], "high")
        with self.assertRaisesRegex(RoomError, "request_conflict"):
            await self.service.read_message_receipt(room, "Next model.", "all", "first-settings", **(body | {"runtime_settings": settings("new", "high")}))
        release.set()
        await self.settle()

    async def test_bad_model_and_binding_race_commit_neither_message_nor_preferences(self):
        room, release = await self.held_work()
        before = self.service.store.snapshot(room)
        with self.assertRaisesRegex(RoomError, "runtime_model_unsupported"):
            await self.send(room, "Invalid selection.", "invalid-selection", runtime_settings=settings("missing"))
        self.assertEqual(self.service.store.snapshot(room), before)
        entered, resume = asyncio.Event(), asyncio.Event()
        async def availability(*args):
            entered.set()
            await resume.wait()
            return []
        with patch.object(self.service, "_guard_objective_availability", side_effect=availability):
            pending = asyncio.create_task(self.send(room, "Late settings.", "changed-head", runtime_settings=settings("new")))
            await entered.wait()
            release.set()
            await self.settle()
            resume.set()
            with self.assertRaisesRegex(RoomError, "team_run_conflict"):
                await pending
        final = self.service.store.snapshot(room)
        self.assertFalse(final["runtime_configuration"]["pending"])
        self.assertFalse(any(event.get("text") == "Late settings." for event in final["events"]))

    async def test_pause_keeps_desired_preferences_without_activation_or_work(self):
        room, release = await self.held_work()
        accepted = await self.send(room, "Apply later settings.", "settings-before-pause", runtime_settings=settings("new"))
        objective = accepted["objective"]["id"]
        await self.service.objective_action(room, "pause", "pause-settings", objective_id=objective)
        count = len(self.fake.calls)
        release.set()
        await self.settle()
        paused = await self.service.read_room(room)
        self.assertEqual(paused["objective"]["status"], "paused")
        self.assertTrue(paused["runtime_configuration"]["pending"])
        self.assertEqual(paused["runtime_configuration"]["pending_reason"], "objective_paused")
        self.assertEqual(paused["room"]["runtime_settings"]["members"]["claude"]["model"], "old")
        self.assertEqual(len(self.fake.calls), count)
        await self.service.objective_action(room, "resume", "resume-settings", objective_id=objective)
        await self.settle()
        final = await self.service.read_room(room)
        self.assertFalse(final["runtime_configuration"]["pending"])
        self.assertEqual(final["room"]["runtime_settings"]["members"]["claude"]["model"], "new")

    async def retained_plan(self, *, limited_mode="plan"):
        from test_native_multi_partial_recovery import NativeMultiPartialRecoveryTests
        before, _ = await NativeMultiPartialRecoveryTests.interrupted_pair(self)
        self.catalogs()
        self.calls_after_recovery = []
        def handler(agent, context, cwd):
            self.calls_after_recovery.append(context)
            task, phase = context["task"], context["job"]["job_phase"]
            if context.get("native_partial_inspection"):
                self.assertEqual(context["native_permission"], "read_only")
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Read the retained SHA and existing evidence; no checks replayed."}])
            if phase == "user":
                self.assertEqual(context["native_permission"], "plan" if limited_mode == "plan" else "read_only")
                self.assertTrue(context.get("native_readonly_discussion"))
                return native.native_reply(context)
            if phase == "work":
                self.assertEqual(context["native_permission"], "full_access")
                self.assertIn("Do not replay the interrupted work or engine commands", context["job"]["text"])
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Publish the inspected retained bytes without replaying checks."}])
            if phase == "review":
                return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Reviewed the exact newly published SHA."}])
            return self.handler(agent, context, cwd)
        self.fake.handler = handler
        await self.send(self.room_id, "Discuss the retained changes before continuing implementation.", "recover-with-plan",
                        work_mode=limited_mode, runtime_settings=settings("new", "high", "full_access" if limited_mode == "plan" else "read_only"))
        await self.settle()
        limited = await self.service.read_room(self.room_id)
        self.assertEqual(limited["objective"]["status"], "running")
        self.assertEqual(limited["room"]["work_mode"], limited_mode)
        self.assertEqual(limited["objective"]["delivery"]["total"], 0)
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        self.assertTrue(all(task["status"] == "assigned" and task["result_version"] == 0 for task in limited["team"]["tasks"]))
        self.assertEqual(len(self.calls_after_recovery), 2)
        for old, current in zip(before["team"]["tasks"], limited["team"]["tasks"]):
            self.assertEqual((old["id"], old["owner"], old["result_version"]), (current["id"], current["owner"], current["result_version"]))
        return limited

    async def test_recover_plan_discussion_then_build_reuses_frozen_d_without_reinspection_or_publication(self):
        limited = await self.retained_plan()
        await self.send(self.room_id, "Explain the retained evidence and remaining work.", "discuss-retained")
        await self.settle()
        discussed = await self.service.read_room(self.room_id)
        self.assertEqual(discussed["team"]["tasks"], limited["team"]["tasks"])
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        discussions = [event for event in discussed["events"] if event["kind"] == "native_readonly_discussion"]
        self.assertEqual(len(discussions), 2)
        self.assertEqual(len(self.calls_after_recovery), 4)
        await self.send(self.room_id, "Continue implementation from those retained files.", "return-build",
                        work_mode="build", runtime_settings=settings("new", "high"))
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertTrue(all(task["status"] == "accepted" and task["result_version"] == 1 for task in final["team"]["tasks"]))
        self.assertEqual(sum(bool(context.get("native_partial_inspection")) for context in self.calls_after_recovery), 2)
        self.assertEqual(sum(context["job"]["job_phase"] == "work" and not context.get("native_partial_inspection")
                             for context in self.calls_after_recovery), 2)

    async def test_readonly_permission_holds_implementation_and_frozen_d_drift_prevents_return_work(self):
        await self.retained_plan(limited_mode="build")
        from pathlib import Path
        state = self.service.store.native_workspace(self.room_id, "claude")
        (Path(state["project"]) / "main.py").write_text("Uninspected external edit.\n")
        before_calls = len(self.calls_after_recovery)
        await self.send(self.room_id, "Resume the retained implementation.", "build-after-drift", runtime_settings=settings("new"))
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["team"]["stop_reason"], "native_recovery_unpublished_changes")
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        self.assertFalse(any(context["self"] == "claude" and context["job"]["job_phase"] == "work"
                             for context in self.calls_after_recovery[before_calls:]))

    async def test_stop_after_pending_settings_never_activates_or_dispatches_late_work(self):
        room, release = await self.held_work()
        await self.send(room, "Use the next preferences.", "pending-before-stop", work_mode="plan", runtime_settings=settings("new"))
        count = len(self.fake.calls)
        await self.service.cancel_room(room)
        release.set()
        await self.settle()
        stopped = await self.service.read_room(room)
        self.assertTrue(stopped["runtime_configuration"]["pending"])
        self.assertEqual(stopped["room"]["work_mode"], "build")
        self.assertEqual(len(self.fake.calls), count)
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in stopped["jobs"]))

    async def test_unclaimed_write_queue_is_deferred_by_plan_then_requeued_under_build(self):
        limited = await self.retained_plan()
        wake = self.service._wake
        with patch.object(self.service, "_wake", return_value=None):
            await self.send(self.room_id, "Continue with implementation next.", "queue-build", work_mode="build")
            self.service.store.reconcile_team(self.room_id)
            queued = self.service.store.snapshot(self.room_id)
            write_jobs = [job for job in queued["jobs"] if job["status"] == "queued" and job["kind"] == "team_work"]
            self.assertEqual(len(write_jobs), 2)
            await self.send(self.room_id, "Discuss the plan first instead.", "replace-queued-with-plan", work_mode="plan")
        wake(self.room_id)
        await self.settle()
        discussed = await self.service.read_room(self.room_id)
        for job in write_jobs:
            actual = next(item for item in discussed["jobs"] if item["id"] == job["id"])
            self.assertEqual((actual["status"], actual["error"]), ("cancelled", "runtime_configuration_changed"))
        self.assertEqual(discussed["team"]["tasks"], limited["team"]["tasks"])
        self.assertFalse(discussed["runtime_configuration"]["pending"])
        await self.send(self.room_id, "Proceed with the retained implementation.", "build-after-queue-replacement", work_mode="build")
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])

    async def test_pending_preferences_and_receipt_survive_a_clean_paused_service_restart(self):
        room, release = await self.held_work()
        before = await self.service.read_room(room)
        body = {"intent": "continue", "binding": before["continuation"]["binding"],
                "work_mode": "plan", "runtime_settings": settings("new", "high")}
        await self.service.send_message(room, "Use this selection when work is continued.", "all", "persist-settings", **body)
        await self.service.objective_action(room, "pause", "pause-durable-settings", objective_id=before["objective"]["id"])
        release.set()
        await self.settle()
        expected = self.service.store.snapshot(room)["runtime_configuration"]
        calls = len(self.fake.calls)
        await self.service.shutdown()
        from local_agent_coordinator.room_service import RoomService
        from local_agent_coordinator.agent_registry import default_registry
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", native_invoke=self.fake,
                                   registry=default_registry())
        restored = self.service.store.snapshot(room)["runtime_configuration"]
        self.assertEqual(restored["desired"], expected["desired"])
        self.assertTrue(restored["pending"])
        self.assertEqual(self.service.store.snapshot(room)["room"]["work_mode"], "build")
        self.assertTrue((await self.service.read_message_receipt(room, "Use this selection when work is continued.", "all", "persist-settings", **body))["received"])
        self.assertEqual(len(self.fake.calls), calls)

    async def test_mode_only_inherits_accepted_models_without_requiring_catalog_refresh(self):
        room, release = await self.held_work()
        self.service._runtime_catalogs.clear()
        accepted = await self.send(room, "Discuss first using the existing model selections.", "mode-only-after-restart", work_mode="plan")
        desired = accepted["runtime_configuration"]["desired"]
        self.assertEqual(desired["work_mode"], "plan")
        self.assertEqual(desired["runtime_settings"]["members"], settings()["members"])
        release.set()
        await self.settle()

    async def test_interrupted_retained_discussion_reenters_only_fresh_readonly_with_original_frozen_d(self):
        await self.retained_plan()
        old = self.fake.handler
        seen = []
        preparing = asyncio.Event()
        original_prepare = self.service._native_workspace_for_job
        async def held_prepare(job):
            if job["team"]["job_phase"] == "user" and job["provider"] == "codex":
                preparing.set()
                await asyncio.Event().wait()
            return await original_prepare(job)
        self.service._native_workspace_for_job = held_prepare
        async def failed_discussion(agent, context, cwd):
            if context["job"]["job_phase"] == "user":
                await preparing.wait()
                seen.append(agent)
                self.fake.failure = {"status": "failed", "failure_code": "native_protocol_unknown"}
            return old(agent, context, cwd)
        self.fake.handler = failed_discussion
        await self.send(self.room_id, "Explain the retained changes.", "interrupted-plan-discussion")
        await self.settle()
        failed = await self.service.read_room(self.room_id)
        self.assertEqual(failed["team"]["stop_reason"], "native_protocol_unknown")
        self.assertTrue(seen)
        candidates = self.service.store.native_partial_recovery_candidates(self.room_id)
        self.assertEqual(len(candidates), 2, candidates)
        source = next(item for item in candidates if item["provider"] == "claude")
        # Plan permission is eligible only for a uniquely claimed discussion,
        # never an ordinary work call or a differently frozen permission.
        for fault in ('work_phase', 'build_claim', 'wrong_permission'):
            with self.service.store._connection(write=True) as db:
                db.execute('SAVEPOINT invalid_plan_source')
                try:
                    if fault == 'work_phase':
                        db.execute("UPDATE team_jobs SET phase='work' WHERE job_id=?", (source['job_id'],))
                    elif fault == 'build_claim':
                        db.execute("UPDATE events SET value_json=json_set(value_json,'$.work_mode','build') "
                            "WHERE room_id=? AND json_extract(value_json,'$.kind')='job_claimed' "
                            "AND json_extract(value_json,'$.job_id')=?", (self.room_id, source['job_id']))
                    else:
                        db.execute("UPDATE events SET value_json=json_set(value_json,'$.runtime_settings.effective_permission','full_access') "
                            "WHERE room_id=? AND json_extract(value_json,'$.kind')='job_claimed' "
                            "AND json_extract(value_json,'$.job_id')=?", (self.room_id, source['job_id']))
                    job = db.execute("SELECT j.*,t.phase,t.run_id,t.task_id,t.result_version,t.assignment_epoch,n.metadata_json "
                        "FROM jobs j JOIN team_jobs t ON t.job_id=j.id JOIN native_executions n ON n.job_id=j.id "
                        "WHERE j.id=?", (source['job_id'],)).fetchone()
                    self.assertIsNone(self.service.store._native_retained_inspection_candidate(db, self.room_id, job), fault)
                finally:
                    db.execute('ROLLBACK TO invalid_plan_source')
                    db.execute('RELEASE invalid_plan_source')
        fallback = next(item for item in candidates if item["provider"] == "codex")
        self.assertIn("not_started_discussion_job_id", fallback)
        with self.service.store._connection(write=True) as db:
            value = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (fallback["not_started_discussion_job_id"],)).fetchone()[0]
            invalid = json.loads(value) | {"tool_calls": False, "tool_results": False}
            db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(invalid), fallback["not_started_discussion_job_id"]))
        self.assertFalse(any(item["provider"] == "codex" for item in self.service.store.native_partial_recovery_candidates(self.room_id)))
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (value, fallback["not_started_discussion_job_id"]))
        self.service._native_workspace_for_job = original_prepare
        self.fake.failure = None
        self.fake.handler = old
        count = len(self.fake.calls)
        await self.send(self.room_id, "Continue the read-only discussion from those same retained files.", "recover-plan-discussion")
        await self.settle()
        recovered = await self.service.read_room(self.room_id)
        new = self.fake.calls[count:]
        self.assertTrue(new)
        self.assertTrue(all(call["permission"] == "read_only" and call["session_id"] is None for call in new))
        self.assertTrue(all(call["context"].get("native_partial_inspection") for call in new))
        self.assertTrue(all(task["result_version"] == 0 and task["status"] == "assigned" for task in recovered["team"]["tasks"]))
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        old_jobs = {job["id"]: (job["status"], job["error"]) for job in failed["jobs"]}
        self.assertEqual({job["id"]: (job["status"], job["error"]) for job in recovered["jobs"] if job["id"] in old_jobs}, old_jobs)

    async def test_known_metadata_rejection_can_recover_as_plan_without_publishing_dirty_files(self):
        before, _ = await native.NativeRoomServiceTests.metadata_failure(self)
        room = before["room"]["id"]
        def handler(agent, context, cwd):
            task = context["task"]
            if context.get("native_partial_inspection"):
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Discussed the retained candidate, no publication or repeated checks."}])
            if context["job"]["job_phase"] == "user":
                return native.native_reply(context)
            return self.handler(agent, context, cwd)
        self.fake.handler = handler
        count = len(self.fake.calls)
        await self.send(room, "Discuss the candidate first with the existing permission.", "metadata-to-plan", work_mode="plan")
        await self.settle()
        final = await self.service.read_room(room)
        self.assertEqual(final["objective"]["status"], "running", final["team"])
        self.assertEqual(final["team"]["tasks"][0]["status"], "assigned")
        self.assertEqual(final["team"]["tasks"][0]["result_version"], 0)
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        later = self.fake.calls[count:]
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["permission"], "read_only")
        self.assertIsNotNone(later[0]["context"].get("native_partial_inspection"))

    async def test_completed_goal_followup_in_plan_replies_once_without_assessment_or_work_loop(self):
        self.fake.handler = self.handler
        created = await self.create()
        room = created["room"]["id"]
        await self.settle()
        before = await self.service.read_room(room)
        self.assertEqual(before["objective"]["status"], "complete")
        original = self.fake.handler
        def discuss(agent, context, cwd):
            self.assertEqual(context["job"]["job_phase"], "user")
            self.assertEqual(context["native_permission"], "plan")
            self.assertEqual(context["objective"]["original_goal"], before["objective"]["original_goal"])
            return native.native_reply(context)
        self.fake.handler = discuss
        count = len(self.fake.calls)
        await self.send(room, "Explain another possible approach before making changes.", "complete-to-plan", work_mode="plan")
        await self.settle()
        final = await self.service.read_room(room)
        self.assertNotEqual(final["objective"]["id"], before["objective"]["id"])
        # A delivered read-only discussion ends its objective; it no longer stays running.
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(len(self.fake.calls) - count, 2)
        self.assertEqual(final["project"]["revision"], before["project"]["revision"])
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in final["jobs"]))
        self.service._wake(room)
        await self.settle()
        self.assertEqual(len(self.fake.calls) - count, 2)
        self.fake.handler = original

    async def test_natural_resume_can_queue_discussion_while_prior_call_cleanup_is_pending(self):
        room, release = await self.held_work()
        before = await self.service.read_room(room)
        await self.service.objective_action(room, "pause", "pause-held-settings", objective_id=before["objective"]["id"])
        resumed = await self.send(room, "Resume with discussion first.", "resume-while-held", work_mode="plan", runtime_settings=settings("new"))
        self.assertEqual(resumed["objective"]["status"], "paused")
        self.assertTrue(resumed["runtime_configuration"]["pending"])
        count = len(self.fake.calls)
        release.set()
        await self.settle()
        final = await self.service.read_room(room)
        # The resumed plan discussion was delivered, which ends its objective.
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertFalse(final["runtime_configuration"]["pending"])
        user_calls = [call for call in self.fake.calls[count:] if call["context"]["job"]["job_phase"] == "user"]
        self.assertEqual(len(user_calls), 2)
        self.assertTrue(all(call["permission"] == "plan" and call["runtime_settings"]["model"] == "new" for call in user_calls))

    async def test_return_to_build_replaces_unclaimed_plan_replies_without_losing_posted_requests(self):
        await self.retained_plan()
        wake = self.service._wake
        with patch.object(self.service, "_wake", return_value=None):
            await self.send(self.room_id, "Explain the remaining evidence first.", "queued-plan-reply")
            queued = [job["id"] for job in self.service.store.snapshot(self.room_id)["jobs"]
                      if job["status"] == "queued" and job["kind"] == "team_user"]
            self.assertEqual(len(queued), 2)
            await self.send(self.room_id, "Proceed directly with the retained implementation now.", "build-before-discussion-claimed", work_mode="build")
        start = len(self.fake.calls)
        wake(self.room_id)
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertTrue(all(job["status"] == "cancelled" for job in final["jobs"] if job["id"] in queued))
        self.assertFalse(any(call["context"]["job"]["job_phase"] == "user" for call in self.fake.calls[start:]))
        first = self.fake.calls[start]["context"]
        self.assertIn("Explain the remaining evidence first.", [event["text"] for event in first["current_user_updates"]])
        self.assertIn("Proceed directly with the retained implementation now.", [event["text"] for event in first["current_user_updates"]])
