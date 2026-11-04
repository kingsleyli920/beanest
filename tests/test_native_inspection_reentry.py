"""Incomplete read-only inspections may be retried, never counted as work."""
import asyncio
import json
from pathlib import Path
import unittest
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError
import test_native_multi_partial_recovery as grouped
from test_native_room_service import native_reply


class NativeInspectionReentryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = grouped.NativeMultiPartialRecoveryTests.asyncSetUp
    asyncTearDown = grouped.NativeMultiPartialRecoveryTests.asyncTearDown
    settle = grouped.NativeMultiPartialRecoveryTests.settle
    handler = grouped.NativeMultiPartialRecoveryTests.handler
    create = grouped.NativeMultiPartialRecoveryTests.create
    interrupted_pair = grouped.NativeMultiPartialRecoveryTests.interrupted_pair

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

    async def test_permission_or_cleanup_or_drift_cannot_use_inspection_reentry(self):
        before, binding, observations = await self.interrupted_inspections()
        failed = next(entry for entry in observations if entry["provider"] == "claude")
        with self.service.store._connection() as db:
            original = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (failed["job_id"],)).fetchone()[0]
        count = len(self.fake.calls)
        for change in ({"permission": "full_access"}, {"cleanup_confirmed": False}):
            with self.subTest(change=change):
                altered = json.loads(original) | change
                with self.service.store._connection(write=True) as db:
                    db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(altered), failed["job_id"]))
                with self.assertRaises(RoomError):
                    await self.service.recover_native(self.room_id, str(uuid4()), **binding)
                with self.service.store._connection(write=True) as db:
                    db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (original, failed["job_id"]))
        state = self.service.store.native_workspace(self.room_id, "claude")
        path = Path(state["project"]) / "main.py"
        path.write_text("Changed since the frozen inspection intent.\n")
        with self.assertRaises(RoomError) as denied:
            await self.service.recover_native(self.room_id, "readonly-drift", **binding)
        self.assertEqual(denied.exception.code, "native_recovery_unpublished_changes")
        self.assertEqual(len(self.fake.calls), count)
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)

    async def test_identity_needs_this_job_and_same_room_provider_adapter_history(self):
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
                with self.assertRaises(RoomError):
                    await self.service.recover_native(self.room_id, str(uuid4()), **binding)
                with self.service.store._connection(write=True) as db:
                    if alternate:
                        db.execute("DELETE FROM native_session_history WHERE room_id=? AND provider=? AND adapter=? AND session_id=?", alternate)
        with self.service.store._connection(write=True) as db:
            db.execute("INSERT INTO native_session_history VALUES (?,?,?,?)", (self.room_id, "codex", "codex", sid))
            db.execute("DELETE FROM events WHERE seq=?", (identity["seq"],))
            # A known UUID / old-job event alone does not identify this attempt.
            self.service.store._event(db, self.room_id, "native_session_cleanup_identity", provider="codex", confirmed=True,
                                     session_id=sid, source="owned_receipt", job_id=before["jobs"][0]["id"])
        with self.assertRaises(RoomError):
            await self.service.recover_native(self.room_id, "readonly-old-identity", **binding)
        self.assertEqual(len(self.fake.calls), count)

    async def test_released_or_wrong_group_or_user_stop_cannot_borrow_readonly_gate(self):
        before, binding, observations = await self.interrupted_inspections()
        group = next(event for event in reversed(before["events"]) if event["kind"] == "native_partial_inspection_group")
        with self.service.store._connection() as db:
            original = db.execute("SELECT value_json FROM events WHERE seq=?", (group["seq"],)).fetchone()[0]
        changed = json.loads(original)
        changed["inspection_job_ids"] = [before["jobs"][0]["id"]]
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE events SET value_json=? WHERE seq=?", (json.dumps(changed), group["seq"]))
        with self.assertRaises(RoomError):
            await self.service.recover_native(self.room_id, "readonly-old-group", **binding)
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE events SET value_json=? WHERE seq=?", (original, group["seq"]))
            self.service.store._event(db, self.room_id, "native_partial_work_queued", inspection_job_id=observations[0]["job_id"], job_id=str(uuid4()))
            released = db.execute("SELECT max(seq) FROM events WHERE room_id=?", (self.room_id,)).fetchone()[0]
        with self.assertRaises(RoomError):
            await self.service.recover_native(self.room_id, "readonly-released-group", **binding)
        with self.service.store._connection(write=True) as db:
            db.execute("DELETE FROM events WHERE seq=?", (released,))
        self.service.store.request_stop(self.room_id, "cancelled")
        with self.assertRaises(RoomError):
            await self.service.recover_native(self.room_id, "readonly-user-stop", **binding)
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)

    async def test_an_ordinary_full_access_work_gap_cannot_claim_a_readonly_retry(self):
        before, binding = await self.interrupted_pair()
        failed = next(entry for entry in before["native_executions"] if entry["run_id"] == binding["run_id"] and entry["provider"] == "claude" and entry["phase"] == "work")
        with self.service.store._connection() as db:
            original = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (failed["job_id"],)).fetchone()[0]
        for permission in ("full_access", "read_only"):
            altered = json.loads(original) | {"tool_calls": 7, "tool_results": 6, "permission": permission}
            with self.service.store._connection(write=True) as db:
                db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(altered), failed["job_id"]))
            with self.assertRaises(RoomError):
                await self.service.recover_native(self.room_id, "ordinary-gap-" + permission, **binding)
        self.assertEqual(self.service._project(self.room_id).snapshot()["revision"], 1)

    async def test_foreign_history_or_live_identity_and_nonfresh_registration_are_rejected(self):
        before, binding, observations = await self.interrupted_inspections()
        peer = next(entry for entry in observations if entry["provider"] == "codex")
        identity = next(event for event in before["events"] if event["kind"] == "native_session_cleanup_identity" and event.get("job_id") == peer["job_id"])
        sid = identity["session_id"]
        count = len(self.fake.calls)
        for foreign in ((str(uuid4()), "codex", "codex", sid), (self.room_id, "another-owner", "codex", sid)):
            with self.subTest(foreign_history=foreign):
                with self.service.store._connection(write=True) as db:
                    db.execute("INSERT INTO native_session_history VALUES (?,?,?,?)", foreign)
                with self.assertRaises(RoomError):
                    await self.service.recover_native(self.room_id, str(uuid4()), **binding)
                with self.service.store._connection(write=True) as db:
                    db.execute("DELETE FROM native_session_history WHERE room_id=? AND provider=? AND adapter=? AND session_id=?", foreign)
        neighbor, _ = self.service.store.create_room("Neighbor remains independent.", "codex", str(uuid4()), participants=["codex"])
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=? WHERE room_id=? AND provider='codex'", (sid, neighbor))
        with self.assertRaises(RoomError):
            await self.service.recover_native(self.room_id, "readonly-live-collision", **binding)
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=NULL WHERE room_id=? AND provider='codex'", (neighbor,))
            original = db.execute("SELECT value_json FROM events WHERE seq=?", (identity["seq"],)).fetchone()[0]
        for change in ({"first_registration": False}, {"confirmed": False}):
            with self.subTest(identity=change):
                with self.service.store._connection(write=True) as db:
                    db.execute("UPDATE events SET value_json=? WHERE seq=?", (json.dumps(json.loads(original) | change), identity["seq"]))
                with self.assertRaises(RoomError):
                    await self.service.recover_native(self.room_id, str(uuid4()), **binding)
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE events SET value_json=? WHERE seq=?", (original, identity["seq"]))
        self.assertEqual(len(self.fake.calls), count)
