"""Incomplete read-only inspections may be retried, never counted as work."""
import asyncio
import json
from pathlib import Path
import unittest
from uuid import uuid4

import test_native_multi_partial_recovery as grouped
from test_native_room_service import native_reply


class NativeInspectionReentryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = grouped.NativeMultiPartialRecoveryTests.asyncSetUp
    asyncTearDown = grouped.NativeMultiPartialRecoveryTests.asyncTearDown
    settle = grouped.NativeMultiPartialRecoveryTests.settle
    handler = grouped.NativeMultiPartialRecoveryTests.handler
    create = grouped.NativeMultiPartialRecoveryTests.create
    interrupted_pair = grouped.NativeMultiPartialRecoveryTests.interrupted_pair
    refused = grouped.NativeMultiPartialRecoveryTests.refused
    finish = grouped.NativeMultiPartialRecoveryTests.finish
    restarted = grouped.NativeMultiPartialRecoveryTests.restarted

    async def interrupted_inspections(self):
        _, binding = await self.interrupted_pair()
        peer_started = asyncio.Event()
        async def handler(agent, context, cwd):
            self.assertTrue(context.get("native_partial_inspection"))
            self.assertEqual(self.fake.calls[-1]["permission"], "read_only")
            if agent == "claude":
                await peer_started.wait()
                self.fake.failure = {"status": "failed", "failure_code": "native_protocol_unknown", "native_execution": {
                    "permission": "read_only", "tool_calls": 7, "tool_results": 6, "cleanup_confirmed": True,
                    "side_effects_possible": False, "exit_code": None}}
                return native_reply(context)
            peer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                identity = str(uuid4())
                receipt = self.service.workspace.parent / "native-receipts" / self.room_id / (str(uuid4()) + ".jsonl")
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text(json.dumps({"channel": "stdout", "data": {"type": "thread.started", "thread_id": identity}}) + "\n")
                error.native_execution = {"permission": "read_only", "tool_calls": 6, "tool_results": 6,
                    "cleanup_confirmed": True, "side_effects_possible": False, "exit_code": -15,
                    "receipt_path": str(receipt)}
                raise
        self.fake.handler = handler
        await self.service.recover_native(self.room_id, "incomplete-inspection", **binding)
        await self.settle()
        self.fake.failure = None
        before = await self.service.read_room(self.room_id)
        self.assertEqual(before["team"]["stop_reason"], "native_protocol_unknown")
        current_jobs = [entry for entry in before["native_executions"] if entry["run_id"] == before["current_run"]["id"]]
        self.assertEqual({(entry["tool_calls"], entry["tool_results"]) for entry in current_jobs}, {(7, 6), (6, 6)})
        self.assertTrue(all(entry["permission"] == "read_only" for entry in current_jobs))
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)
        self.assertFalse(any(event["kind"] == "native_partial_work_queued" for event in before["events"]))
        peer = next(member for member in before["members"] if member["id"] == "codex")
        self.assertIsNone(peer["session_id"])
        # The owned init receipt confirms the canceled process identity;
        # incomplete execution still requires a fresh read-only inspection.
        self.assertFalse(peer["identity_uncertain"])
        binding = {key: before["current_run"][key] for key in ("revision", "digest")}
        binding["run_id"] = before["current_run"]["id"]
        return before, binding, current_jobs

    async def test_incomplete_readonly_group_restarts_fresh_inspection_without_fake_tool_results(self):
        before, binding, observations = await self.interrupted_inspections()
        self.assertTrue(before["current_run"]["can_recover_native"])
        inspected = set()
        original_jobs = {job["id"]: (job["status"], job["error"]) for job in before["jobs"]}
        def handler(agent, context, cwd):
            task, phase = context["task"], context["job"]["job_phase"]
            if context.get("native_partial_inspection"):
                self.assertEqual(self.fake.calls[-1]["permission"], "read_only")
                self.assertIsNone(self.fake.calls[-1]["session_id"])
                inspected.add(agent)
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Retained SHA inspected anew; no old result was reconstructed."}])
            if phase == "work":
                self.assertEqual(inspected, {"claude", "codex"})
                self.assertIn("Do not replay the interrupted work or engine commands", context["job"]["text"])
                return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Only remaining original work completed."}])
            if phase == "review":
                return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Checked exact published SHA."}])
            return self.handler(agent, context, cwd)
        self.fake.handler = handler
        await self.service.recover_native(self.room_id, "fresh-inspection-after-gap", **binding)
        await self.settle()
        final = await self.service.read_room(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual({job["id"]: (job["status"], job["error"]) for job in final["jobs"] if job["id"] in original_jobs}, original_jobs)
        for old in observations:
            self.assertEqual(next(entry for entry in final["native_executions"] if entry["job_id"] == old["job_id"]), old)

    async def test_permission_or_cleanup_or_drift_cannot_use_inspection_reentry_but_the_room_restarts(self):
        # Catches a changed permission, an unconfirmed exit or a drifted file
        # still passing as an incomplete read-only inspection, and the room
        # refusing to continue once that re-inspection is unavailable.
        before, binding, observations = await self.interrupted_inspections()
        failed = next(entry for entry in observations if entry["provider"] == "claude")
        with self.service.store._connection() as db:
            original = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (failed["job_id"],)).fetchone()[0]
        count = len(self.fake.calls)
        for change, reason in (({"permission": "full_access"}, "native_recovery_unpublished_changes"),
                               ({"cleanup_confirmed": False}, "native_recovery_cleanup_unconfirmed")):
            with self.subTest(change=change):
                altered = json.loads(original) | change
                with self.service.store._connection(write=True) as db:
                    db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(altered), failed["job_id"]))
                self.refused(reason)
                with self.service.store._connection(write=True) as db:
                    db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (original, failed["job_id"]))
        state = self.service.store.native_workspace(self.room_id, "claude")
        path = Path(state["project"]) / "main.py"
        path.write_text("Changed since the frozen inspection intent.\n")
        self.refused("native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        final, later = await self.restarted("readonly-drift", binding)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        # The drifted bytes were not imported by the restart; their owner's new
        # work call is what delivered its own task's file.
        self.assertEqual(sorted((call["agent"], call["context"]["task"]["id"]) for call in later
                                if call["context"]["job"]["job_phase"] == "work"), [("claude", "T1"), ("codex", "T2")])
        self.assertEqual(self.service._project(self.room_id).files_for_paths(["main.py"])[0]["owner"], "claude")

    async def test_identity_needs_this_job_and_same_room_provider_adapter_history(self):
        # Catches a session identity proven by another adapter, another room or
        # another job admitting the re-inspection, and that unproven session
        # being continued by the restart that follows.
        before, binding, observations = await self.interrupted_inspections()
        peer = next(entry for entry in observations if entry["provider"] == "codex")
        identity = next(event for event in before["events"] if event["kind"] == "native_session_cleanup_identity" and event.get("job_id") == peer["job_id"])
        sid, count = identity["session_id"], len(self.fake.calls)
        with self.service.store._connection(write=True) as db:
            db.execute("DELETE FROM native_session_history WHERE room_id=? AND provider='codex' AND session_id=?", (self.room_id, sid))
        for alternate in (None, (self.room_id, "codex", "claude", sid), (str(uuid4()), "codex", "codex", sid)):
            with self.subTest(history=alternate):
                with self.service.store._connection(write=True) as db:
                    if alternate:
                        db.execute("INSERT INTO native_session_history VALUES (?,?,?,?)", alternate)
                self.refused("native_recovery_unpublished_changes")
                with self.service.store._connection(write=True) as db:
                    if alternate:
                        db.execute("DELETE FROM native_session_history WHERE room_id=? AND provider=? AND adapter=? AND session_id=?", alternate)
        with self.service.store._connection(write=True) as db:
            db.execute("INSERT INTO native_session_history VALUES (?,?,?,?)", (self.room_id, "codex", "codex", sid))
            db.execute("DELETE FROM events WHERE seq=?", (identity["seq"],))
            # A known UUID / old-job event alone does not identify this attempt.
            self.service.store._event(db, self.room_id, "native_session_cleanup_identity", provider="codex", confirmed=True,
                                     session_id=sid, source="owned_receipt", job_id=before["jobs"][0]["id"])
        self.refused("native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        final, later = await self.restarted("readonly-old-identity", binding)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        first = next(call for call in later if call["agent"] == "codex")
        self.assertIsNone(first["session_id"])
        self.assertNotIn(sid, [call["session_id"] for call in later])

    async def test_released_or_wrong_group_or_user_stop_cannot_borrow_readonly_gate(self):
        # Catches a group that names other jobs, a group already released to
        # work, or a stop the user asked for being re-inspected read-only as
        # if it were this incomplete inspection.
        before, binding, observations = await self.interrupted_inspections()
        group = next(event for event in reversed(before["events"]) if event["kind"] == "native_partial_inspection_group")
        with self.service.store._connection() as db:
            original = db.execute("SELECT value_json FROM events WHERE seq=?", (group["seq"],)).fetchone()[0]
        changed = json.loads(original)
        changed["inspection_job_ids"] = [before["jobs"][0]["id"]]
        count = len(self.fake.calls)
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE events SET value_json=? WHERE seq=?", (json.dumps(changed), group["seq"]))
        self.refused("native_recovery_unpublished_changes")
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE events SET value_json=? WHERE seq=?", (original, group["seq"]))
            self.service.store._event(db, self.room_id, "native_partial_work_queued", inspection_job_id=observations[0]["job_id"], job_id=str(uuid4()))
            released = db.execute("SELECT max(seq) FROM events WHERE room_id=?", (self.room_id,)).fetchone()[0]
        self.refused("native_recovery_unpublished_changes")
        with self.service.store._connection(write=True) as db:
            db.execute("DELETE FROM events WHERE seq=?", (released,))
        self.assertTrue(self.service._snapshot(self.room_id)["current_run"]["can_recover_native"], "the untouched evidence holds again")
        self.service.store.request_stop(self.room_id, "cancelled")
        self.refused("native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        # The user's own continue still works after their stop, as a plain restart.
        final, later = await self.restarted("readonly-user-stop", binding)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])

    async def test_an_ordinary_full_access_work_gap_cannot_claim_a_readonly_retry(self):
        # Catches an ordinary work call that ended mid-tool being treated as an
        # incomplete read-only inspection (and so retried with its work kept
        # as inspected) just because its record shows a tool gap.
        before, binding = await self.interrupted_pair()
        failed = next(entry for entry in before["native_executions"] if entry["run_id"] == binding["run_id"] and entry["provider"] == "claude" and entry["phase"] == "work")
        with self.service.store._connection() as db:
            original = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (failed["job_id"],)).fetchone()[0]
        self.assertTrue(self.service._snapshot(self.room_id)["current_run"]["can_recover_native"])
        count = len(self.fake.calls)
        for permission in ("full_access", "read_only"):
            with self.subTest(permission=permission):
                altered = json.loads(original) | {"tool_calls": 7, "tool_results": 6, "permission": permission}
                with self.service.store._connection(write=True) as db:
                    db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(altered), failed["job_id"]))
                self.refused("native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        final, later = await self.restarted("ordinary-gap", binding)
        abandoned = next(event for event in final["events"] if event["kind"] == "native_unconfirmed_calls_abandoned")
        # The call that ended mid-tool is told to the next run as unverified.
        self.assertIn(failed["job_id"], abandoned["unverified_job_ids"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])

    async def test_foreign_history_or_live_identity_and_nonfresh_registration_are_rejected(self):
        # Catches a session that another room or owner also holds, or whose
        # registration is not this call's first confirmed one, admitting the
        # re-inspection, and then being continued by the restart that follows.
        before, binding, observations = await self.interrupted_inspections()
        peer = next(entry for entry in observations if entry["provider"] == "codex")
        identity = next(event for event in before["events"] if event["kind"] == "native_session_cleanup_identity" and event.get("job_id") == peer["job_id"])
        sid = identity["session_id"]
        count = len(self.fake.calls)
        for foreign in ((str(uuid4()), "codex", "codex", sid), (self.room_id, "another-owner", "codex", sid)):
            with self.subTest(foreign_history=foreign[1:3]):
                with self.service.store._connection(write=True) as db:
                    db.execute("INSERT INTO native_session_history VALUES (?,?,?,?)", foreign)
                self.refused("native_recovery_unpublished_changes")
                with self.service.store._connection(write=True) as db:
                    db.execute("DELETE FROM native_session_history WHERE room_id=? AND provider=? AND adapter=? AND session_id=?", foreign)
        neighbor, _ = self.service.store.create_room("Neighbor remains independent.", "codex", str(uuid4()), participants=["codex"])
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=? WHERE room_id=? AND provider='codex'", (sid, neighbor))
        self.refused("native_recovery_unpublished_changes")
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=NULL WHERE room_id=? AND provider='codex'", (neighbor,))
            original = db.execute("SELECT value_json FROM events WHERE seq=?", (identity["seq"],)).fetchone()[0]
        for change in ({"first_registration": False}, {"confirmed": False}):
            with self.subTest(identity=change):
                with self.service.store._connection(write=True) as db:
                    db.execute("UPDATE events SET value_json=? WHERE seq=?", (json.dumps(json.loads(original) | change), identity["seq"]))
                self.refused("native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        # Still unconfirmed when the room is continued: that session is not used.
        final, later = await self.restarted("readonly-unconfirmed-identity", binding)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertIsNone(next(call for call in later if call["agent"] == "codex")["session_id"])
        self.assertNotIn(sid, [call["session_id"] for call in later])
