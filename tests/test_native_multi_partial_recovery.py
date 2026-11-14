"""Multi-owner retained recovery: synthetic transport, real SQLite/private trees."""
import asyncio
import json
import unittest
from uuid import uuid4

import test_native_room_service as native_tests
from test_native_room_service import native_reply
from test_team_store import proposal


class NativeMultiPartialRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_tests.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native_tests.NativeRoomServiceTests.asyncTearDown
    settle = native_tests.NativeRoomServiceTests.settle
    handler = native_tests.NativeRoomServiceTests.handler
    create = native_tests.NativeRoomServiceTests.create

    async def interrupted_pair(self):
        peer_started = asyncio.Event()
        self.initial_work = []

        async def handler(agent, context, cwd):
            phase, board = context["job"]["job_phase"], context["team"]
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands.extend([proposal("T1", paths=["main.py"]), proposal("T2", paths=["notes.md"])])
                own = "T1" if agent == "claude" else "T2"
                if commands or any(task["id"] == own and task["owner"] is None for task in board["tasks"]):
                    commands.append({"type": "claim", "task_id": own})
                return native_reply(context, commands + [{"type": "plan_ready"}])
            self.assertEqual(phase, "work")
            self.initial_work.append(agent)
            path = "main.py" if agent == "claude" else "notes.md"
            (cwd / path).write_text("Retained " + agent + " work.\n")
            if agent == "claude":
                await peer_started.wait()
                self.fake.failure = {"status": "failed", "failure_code": "protocol_turn_incomplete"}
                return native_reply(context)
            peer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                identity = next(member["session_id"] for member in self.service.store.snapshot(self.room_id)["members"]
                                if member["id"] == agent)
                receipt = self.service.workspace.parent / "native-receipts" / self.room_id / (str(uuid4()) + ".jsonl")
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text(json.dumps({"channel": "stdout", "data": {"type": "thread.started", "thread_id": identity}}) + "\n")
                error.native_execution = {"permission": "full_access", "tool_calls": 1, "tool_results": 1,
                    "side_effects_possible": True, "cleanup_confirmed": True, "exit_code": -15,
                    "receipt_path": str(receipt)}
                raise
        self.fake.handler = handler
        created = await self.create()
        self.room_id = created["room"]["id"]
        await self.settle()
        self.fake.failure = None
        before = await self.service.read_room(self.room_id)
        self.assertEqual(set(self.initial_work), {"claude", "codex"})
        work = [job for job in before["jobs"] if job["kind"] == "team_work"]
        self.assertEqual({job["error"] for job in work}, {"protocol_turn_incomplete", "invoke_interrupted"})
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        binding = {key: before["current_run"][key] for key in ("revision", "digest")}
        binding["run_id"] = before["current_run"]["id"]
        return before, binding

    def refused(self, reason):
        """The retained-work recovery is refused for `reason`, by a fresh audit
        rather than the cached display one; the room itself can still continue."""
        snapshot = self.service._snapshot(self.room_id)
        run = snapshot["current_run"]
        self.assertEqual((run["can_recover_native"], run["native_recovery_reason"]), (False, reason))
        self.assertTrue(snapshot["continuation"]["can_send"], snapshot["continuation"])

    def finish(self, agent, context, cwd):
        task, phase = context["task"], context["job"]["job_phase"]
        if phase == "work":
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Finished from the current private files."}])
        if phase == "review":
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the published bytes."}])
        return self.handler(agent, context, cwd)

    async def restarted(self, request_id, binding):
        """Continue for real. What starts is the generic restart: the ended calls
        are abandoned, nothing is re-inspected read-only and nothing is published
        by the restart itself."""
        since = self.service.store.snapshot(self.room_id)["events"][-1]["seq"]
        count = len(self.fake.calls)
        self.fake.handler = self.finish
        await self.service.recover_native(self.room_id, request_id, **binding)
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        await self.settle()
        final = await self.service.read_room(self.room_id)
        kinds = [event["kind"] for event in final["events"] if event["seq"] > since]
        self.assertEqual(kinds.count("native_unconfirmed_calls_abandoned"), 1)
        for kind in ("native_partial_recovery_attestation", "native_partial_inspection_group", "native_partial_inspected",
                     "native_partial_work_queued", "native_partial_workspace_transferred"):
            self.assertEqual(kinds.count(kind), 0, kind)
        later = self.fake.calls[count:]
        self.assertTrue(later)
        self.assertFalse(any(call["context"].get("native_partial_inspection") or call["permission"] == "read_only" for call in later))
        self.assertNotEqual(final["current_run"]["id"], binding["run_id"])
        return final, later

    async def test_all_retained_owners_are_inspected_before_work_or_import(self):
        before, binding = await self.interrupted_pair()
        old_jobs = {job["id"]: (job["status"], job["error"]) for job in before["jobs"]}
        old_tree = {agent: self.service.store.native_workspace(self.room_id, agent)["project"] for agent in ("claude", "codex")}
        inspections = {agent: asyncio.Event() for agent in old_tree}
        release_peer = asyncio.Event()
        ordinary = []

        async def handler(agent, context, cwd):
            phase, task = context["job"]["job_phase"], context["task"]
            if context.get("native_partial_inspection"):
                self.assertEqual(self.fake.calls[-1]["permission"], "read_only")
                self.assertIsNone(self.fake.calls[-1]["session_id"])
                self.assertEqual(str(cwd), old_tree[agent])
                expected = "Retained " + agent + " work.\n"
                self.assertEqual((cwd / task["write_paths"][0]).read_text(), expected)
                inspections[agent].set()
                if agent == "codex":
                    await release_peer.wait()
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Inspected retained bytes; prior tests are not replayed."}])
            if phase == "work":
                self.assertTrue(all(event.is_set() for event in inspections.values()))
                self.assertIn("Do not replay the interrupted work or engine commands", context["job"]["text"])
                ordinary.append(agent)
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Publish the inspected retained work."}])
            if phase == "review":
                return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Exact published SHA reviewed."}])
            return self.handler(agent, context, cwd)
        self.fake.handler = handler
        start = len(self.fake.calls)
        await self.service.recover_native(self.room_id, "recover-pair", **binding)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in inspections.values())), 3)
        await asyncio.sleep(0.05)
        self.assertEqual(ordinary, [])
        held = await self.service.read_room(self.room_id)
        self.assertEqual(held["objective"]["delivery"]["total"], 0)
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        self.assertTrue(all(task["result_version"] == 0 for task in held["team"]["tasks"]))
        release_peer.set()
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(set(ordinary), {"claude", "codex"})
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        for previous, current in zip(before["team"]["tasks"], final["team"]["tasks"]):
            self.assertEqual((current["id"], current["owner"], current["write_paths"]),
                             (previous["id"], previous["owner"], previous["write_paths"]))
            self.assertEqual(current["assignment_epoch"], previous["assignment_epoch"] + 1)
            self.assertEqual(current["review_kind"], "independent_review")
        archived = await self.service.read_run(self.room_id, binding["run_id"])
        self.assertEqual(archived["team"]["tasks"], before["team"]["tasks"])
        self.assertEqual(final["room"]["runtime_settings"], before["room"]["runtime_settings"])
        self.assertEqual({job["id"]: (job["status"], job["error"]) for job in final["jobs"] if job["id"] in old_jobs}, old_jobs)
        self.assertFalse(any(call["context"]["job"]["job_phase"] in {"plan", "coordinate"} for call in self.fake.calls[start:]))
        count = len(self.fake.calls)
        await self.service.recover_native(self.room_id, "recover-pair", **binding)
        self.assertEqual(len(self.fake.calls), count)
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        self.assertEqual((self.source / "notes.md").read_text(), "Original notes.\n")

    async def test_second_owner_outside_scope_or_unknown_cleanup_cannot_join_the_group_but_the_room_restarts(self):
        # Catches a second owner's file outside its task, or a call whose exit
        # was never confirmed, being inspected and published as retained work;
        # and the room refusing to continue because of either.
        before, binding = await self.interrupted_pair()
        peer = next(job for job in before["jobs"] if job["error"] == "invoke_interrupted")
        state = self.service.store.native_workspace(self.room_id, "codex")
        from pathlib import Path
        unexpected = Path(state["project"]) / "outside.py"
        unexpected.write_text("Unauthorized.\n")
        count = len(self.fake.calls)
        self.refused("native_recovery_unpublished_changes")
        unexpected.unlink()
        self.service.store.record_job_cleanup(peer["id"], confirmed=False)
        self.refused("native_recovery_cleanup_unconfirmed")
        self.assertEqual(len(self.fake.calls), count)
        # Continue with both facts present: they are reported to the next run.
        unexpected.write_text("Unauthorized.\n")
        final, later = await self.restarted("recover-unclean-outside", binding)
        abandoned = next(event for event in final["events"] if event["kind"] == "native_unconfirmed_calls_abandoned")
        self.assertEqual(abandoned["unowned_private_changes"], [{"provider": "codex", "paths": ["outside.py"]}])
        self.assertEqual(abandoned["possibly_running_job_ids"], [peer["id"]])
        told = later[0]["context"]["objective"]["continuation"]["previous_stop"]
        self.assertEqual(sorted((call["provider"], call["process_may_still_be_running"]) for call in told["ended_calls"]),
                         [("claude", False), ("codex", True)])
        # The stray file stays private; a possibly running call's session is not continued.
        self.assertEqual(unexpected.read_text(), "Unauthorized.\n")
        self.assertNotIn("outside.py", [file["path"] for file in final["project"]["files"]])
        self.assertIsNone(next(call for call in later if call["agent"] == "codex")["session_id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])

    async def test_unrelated_interruption_cannot_borrow_a_protocol_fault(self):
        # Catches an interruption that cannot be shown to have been running
        # when the protocol fault happened joining the retained group anyway.
        _, binding = await self.interrupted_pair()
        candidates = self.service.store.native_partial_recovery_candidates(self.room_id)
        self.assertEqual(len(candidates), 2)
        peer = next(candidate for candidate in candidates if candidate["source_error"] == "invoke_interrupted")
        with self.service.store._connection(write=True) as db:
            # Same room/error labels do not prove the peer was already working
            # when the first failure occurred.
            row = db.execute("SELECT seq,value_json FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_claimed' "
                             "AND json_extract(value_json,'$.job_id')=?", (self.room_id, peer["job_id"])).fetchone()
            db.execute("DELETE FROM events WHERE seq=?", (row["seq"],))
            self.service.store._event(db, self.room_id, "job_claimed", sender="codex", job_id=peer["job_id"])
        count = len(self.fake.calls)
        self.refused("native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        final, later = await self.restarted("recover-unrelated-cancel", binding)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        # Each owner's file reached the shared copy through its own new work call.
        self.assertEqual(sorted((call["agent"], call["context"]["task"]["id"]) for call in later
                                if call["context"]["job"]["job_phase"] == "work"), [("claude", "T1"), ("codex", "T2")])

    async def test_group_pause_keeps_both_inspection_notes_and_retained_bytes(self):
        _, binding = await self.interrupted_pair()
        started = {agent: asyncio.Event() for agent in ("claude", "codex")}
        release = asyncio.Event()
        work = []

        async def handler(agent, context, cwd):
            task, phase = context["task"], context["job"]["job_phase"]
            if context.get("native_partial_inspection"):
                started[agent].set()
                await release.wait()
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": agent + " retained evidence inspected."}])
            if phase == "work":
                work.append(agent)
                self.assertIn(agent + " retained evidence inspected.", context["job"]["text"])
                self.assertEqual((cwd / task["write_paths"][0]).read_text(), "Retained " + agent + " work.\n")
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Only publish inspected work."}])
            if phase == "review":
                return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Verified exact published bytes."}])
            return self.handler(agent, context, cwd)
        self.fake.handler = handler
        recovering = await self.service.recover_native(self.room_id, "recover-pair-paused", **binding)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 3)
        objective = recovering["objective"]["id"]
        await self.service.objective_action(self.room_id, "pause", "pause-group", objective_id=objective)
        release.set()
        await self.settle()
        self.assertEqual(work, [])
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        paused = await self.service.read_room(self.room_id)
        self.assertEqual(paused["objective"]["status"], "paused")
        self.assertEqual(len([event for event in paused["events"] if event["kind"] == "native_partial_inspected"]), 2)
        await self.service.objective_action(self.room_id, "resume", "resume-group", objective_id=objective)
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(sorted(work), ["claude", "codex"])

    async def test_second_inspection_modification_cannot_release_any_group_work(self):
        _, binding = await self.interrupted_pair()
        finished_first = asyncio.Event()
        start = len(self.fake.calls)

        async def handler(agent, context, cwd):
            self.assertTrue(context.get("native_partial_inspection"))
            task = context["task"]
            if agent == "codex":
                await finished_first.wait()
                (cwd / "notes.md").write_text("Changed during read-only inspection.\n")
            else:
                finished_first.set()
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Inspection claims success."}])
        self.fake.handler = handler
        await self.service.recover_native(self.room_id, "recover-mutating-inspection", **binding)
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["team"]["stop_reason"], "native_recovery_unpublished_changes")
        self.assertFalse(any(event["kind"] == "native_partial_work_queued" for event in final["events"]))
        self.assertTrue(all(call["context"]["native_partial_inspection"] for call in self.fake.calls[start:]))
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        self.assertTrue(all(task["result_version"] == 0 for task in final["team"]["tasks"]))

    async def test_peer_epoch_or_tool_gap_cannot_join_retained_group(self):
        # Catches a peer whose task was reassigned since, or whose call ended
        # mid-tool, being inspected and published with the retained group.
        _, binding = await self.interrupted_pair()
        candidate = next(item for item in self.service.store.native_partial_recovery_candidates(self.room_id)
                         if item["provider"] == "codex")
        count = len(self.fake.calls)
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND run_id=? AND id=?",
                       (self.room_id, binding["run_id"], candidate["task_id"]))
        self.refused("native_recovery_unpublished_changes")
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch-1 WHERE room_id=? AND run_id=? AND id=?",
                       (self.room_id, binding["run_id"], candidate["task_id"]))
            row = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (candidate["job_id"],)).fetchone()
            metadata = json.loads(row[0])
            metadata["tool_results"] = 0
            db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(metadata), candidate["job_id"]))
        self.refused("native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        final, later = await self.restarted("recover-unpaired-peer", binding)
        abandoned = next(event for event in final["events"] if event["kind"] == "native_unconfirmed_calls_abandoned")
        # The call that ended mid-tool is reported as unverified, not trusted.
        self.assertIn(candidate["job_id"], abandoned["unverified_job_ids"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])

    async def test_protocol_interruption_during_group_inspection_reaudits_successful_unpublished_owner(self):
        _, binding = await self.interrupted_pair()
        first_done = asyncio.Event()

        async def inspect_then_fail(agent, context, cwd):
            self.assertTrue(context.get("native_partial_inspection"))
            task = context["task"]
            if agent == "claude":
                first_done.set()
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Inspected; still unpublished."}])
            await first_done.wait()
            for _ in range(50):
                events = self.service.store.snapshot(self.room_id)["events"]
                if any(event["kind"] == "native_partial_inspected" and event["sender"] == "claude" for event in events):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("First inspection never finished")
            self.fake.failure = {"status": "failed", "failure_code": "protocol_turn_incomplete"}
            return native_reply(context)
        self.fake.handler = inspect_then_fail
        await self.service.recover_native(self.room_id, "recover-before-inspection-fault", **binding)
        await self.settle()
        self.fake.failure = None
        interrupted = await self.service.read_room(self.room_id)
        self.assertEqual(interrupted["team"]["stop_reason"], "protocol_turn_incomplete")
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        self.assertFalse(any(event["kind"] == "native_partial_work_queued" for event in interrupted["events"]))
        candidates = self.service.store.native_partial_recovery_candidates(self.room_id)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(next(candidate for candidate in candidates if candidate["provider"] == "claude")["inspection_only"])
        binding = {key: interrupted["current_run"][key] for key in ("revision", "digest")}
        binding["run_id"] = interrupted["current_run"]["id"]
        state = self.service.store.native_workspace(self.room_id, "claude")
        from pathlib import Path
        path = Path(state["project"]) / "main.py"
        content = path.read_bytes()
        path.write_text("Not the inspected version.\n")
        # The inspected owner is re-audited: changed bytes no longer pass as
        # the inspected ones. (Continuing now would abandon the calls instead;
        # here the bytes are put back and the audited recovery goes on.)
        self.refused("native_recovery_unpublished_changes")
        path.write_bytes(content)
        self.assertTrue(self.service._snapshot(self.room_id)["current_run"]["can_recover_native"])
        inspected = set()
        def finish(agent, context, cwd):
            task, phase = context["task"], context["job"]["job_phase"]
            if context.get("native_partial_inspection"):
                inspected.add(agent)
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Same retained SHA rechecked."}])
            if phase == "work":
                self.assertEqual(inspected, {"claude", "codex"})
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Complete remaining original work from inspected bytes."}])
            if phase == "review":
                return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Exact result reviewed."}])
            return self.handler(agent, context, cwd)
        self.fake.handler = finish
        await self.service.recover_native(self.room_id, "recover-after-inspection-fault", **binding)
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
