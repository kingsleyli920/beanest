"""Reject unsupported fresh-review recovery using an actual failed HTTP run."""
import json
import os
from pathlib import Path
import unittest
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError
import test_native_diagnostic_review_http as fixture


class NativeDiagnosticReviewGateTests(unittest.TestCase):
    setUp, tearDown = fixture.NativeDiagnosticReviewHTTPTests.setUp, fixture.NativeDiagnosticReviewHTTPTests.tearDown
    request = fixture.NativeDiagnosticReviewHTTPTests.request
    settled = fixture.NativeDiagnosticReviewHTTPTests.settled
    handler = fixture.NativeDiagnosticReviewHTTPTests.handler
    create = fixture.NativeDiagnosticReviewHTTPTests.create
    job_rows = fixture.NativeDiagnosticReviewHTTPTests.job_rows
    save_metadata = fixture.NativeDiagnosticReviewHTTPTests.save_metadata

    def failed(self):
        snapshot = self.settled(self.create()["room"]["id"])
        self.assertTrue(snapshot["current_run"]["can_recover_native"])
        candidate = self.service.store.native_review_recovery_candidate(self.room_id)
        self.assertIsNotNone(candidate)
        return snapshot, candidate

    @staticmethod
    def continuation(snapshot):
        run = snapshot["current_run"]
        return {"text": "继续", "target": "all", "request_id": str(uuid4()), "intent": "continue",
                "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}}

    def test_candidate_requires_actual_claim_identity_cleanup_and_exact_task(self):
        snapshot, candidate = self.failed()
        room, job = self.room_id, candidate["job_id"]
        with self.service.store._connection(write=True) as db:
            cases = {
                "native cleanup absent": ("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.cleanup_confirmed',json('false')) WHERE job_id=?", (job,)),
                "host cleanup absent": ("UPDATE events SET value_json=json_set(value_json,'$.cleanup_confirmed',json('false')) WHERE room_id=? AND json_extract(value_json,'$.kind')='job_cleanup' AND json_extract(value_json,'$.job_id')=?", (room, job)),
                "claim absent": ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_claimed' AND json_extract(value_json,'$.job_id')=?", (room, job)),
                "confirmed identity differs": ("UPDATE events SET value_json=json_set(value_json,'$.session_id',?) WHERE room_id=? AND json_extract(value_json,'$.kind')='native_session_cleanup_identity' AND json_extract(value_json,'$.job_id')=?", (str(uuid4()), room, job)),
                "history absent": ("DELETE FROM native_session_history WHERE room_id=? AND provider=?", (room, candidate["provider"])),
                "wrong adapter": ("UPDATE members SET adapter='claude' WHERE room_id=? AND provider=?", (room, candidate["provider"])),
                "changed result version": ("UPDATE shared_tasks SET result_version=result_version+1 WHERE room_id=? AND run_id=? AND id=?", (room, candidate["run_id"], candidate["task_id"])),
                "changed assignment epoch": ("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND run_id=? AND id=?", (room, candidate["run_id"], candidate["task_id"])),
                "true permission block": ("UPDATE shared_tasks SET block_cause='permission_required' WHERE room_id=? AND run_id=? AND id=?", (room, candidate["run_id"], candidate["task_id"])),
            }
            for label, (sql, args) in cases.items():
                with self.subTest(label=label):
                    db.execute("SAVEPOINT reject_review")
                    try:
                        db.execute(sql, args)
                        self.assertIsNone(self.service.store._native_review_recovery_candidate(db, room))
                    finally:
                        db.execute("ROLLBACK TO reject_review")
                        db.execute("RELEASE reject_review")
        self.assertEqual(self.service.store.native_review_recovery_candidate(room), candidate)
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 0})

    def test_real_legacy_block_provenance_is_not_released_by_same_error_text(self):
        _, candidate = self.failed()
        room, run = self.room_id, candidate["run_id"]
        with self.service.store._connection(write=True) as db:
            # A legacy block command may have NULL cause and the exact same
            # reason. Preserve that provenance; the host error is not authority.
            db.execute("SAVEPOINT legacy_block")
            try:
                other = db.execute("SELECT id FROM jobs WHERE room_id=? AND id<>? LIMIT 1", (room, candidate["job_id"])).fetchone()[0]
                db.execute("UPDATE team_jobs SET task_id='T3',assignment_epoch=0,result_version=0 WHERE job_id=?", (other,))
                self.service.store._event(db, room, "team_command", sender="codex", job_id=other, command="block")
                held = self.service.store._native_review_recovery_candidate(db, room)
                self.assertEqual(held["recoverable_task_ids"], ["T2"])
                self.assertEqual(db.execute("SELECT block_cause FROM shared_tasks WHERE room_id=? AND run_id=? AND id='T3'", (room, run)).fetchone()[0], None)
            finally:
                db.execute("ROLLBACK TO legacy_block")
                db.execute("RELEASE legacy_block")

    def test_private_receipt_must_have_one_closed_turn_and_matching_session(self):
        _, candidate = self.failed()
        directory = self.service.workspace.parent / "native-receipts" / self.room_id
        receipt = next(directory.glob("*.jsonl"))
        original, metadata = receipt.read_bytes(), receipt.stat()
        rows = [json.loads(line) for line in original.splitlines()]
        cases = {
            "post terminal error": rows + [{"channel": "stdout", "data": {"type": "error"}}],
            "failed terminal": rows[:-1] + [{"channel": "stdout", "data": {"type": "turn.failed"}}],
            "missing terminal": rows[:-1],
            "missing tool result": rows[:-2] + rows[-1:],
            "second terminal": rows + rows[-1:],
            "wrong session": [{"channel": "stdout", "data": {"type": "thread.started", "thread_id": str(uuid4())}}] + rows[1:],
            "malformed row type": [None] + rows[1:],
        }
        try:
            for label, changed in cases.items():
                with self.subTest(label=label):
                    receipt.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in changed))
                    os.utime(receipt, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                    with self.assertRaises(RoomError) as failure:
                        self.service._native_review_receipt_attestation(self.room_id, candidate)
                    self.assertEqual(failure.exception.code, "native_recovery_diagnostic_unconfirmed")
        finally:
            receipt.write_bytes(original)
            os.utime(receipt, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        self.assertEqual(self.service._native_review_receipt_attestation(self.room_id, candidate)["sha256"], self.failed_receipt_sha256)

    def test_unpublished_private_changes_refuse_public_continue_without_new_calls(self):
        snapshot, candidate = self.failed()
        state = self.service.store.native_workspace(self.room_id, candidate["provider"])
        retained = Path(state["project"]) / "unauthorized.txt"
        retained.write_text("Retain this evidence; recovery must not import or discard it.\n")
        calls = len(self.native.calls)
        code, changed = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200)
        self.assertFalse(changed["current_run"]["can_recover_native"])
        code, failure = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.continuation(snapshot))
        self.assertEqual(code, 409, {"error": failure.get("error")})
        self.assertEqual(len(self.native.calls), calls)
        self.assertTrue(retained.is_file())
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 0})

    def test_same_path_changes_and_ABA_refuse_a_fresh_review(self):
        snapshot, candidate = self.failed()
        project, path = self.service._project(self.room_id), "results/second.md"
        old = project.files_for_paths([path])[0]
        calls = len(self.native.calls)
        for label, content in (("changed", "A new unreviewed version.\n"), ("ABA", old["content"])):
            with self.subTest(label=label):
                current = project.files_for_paths([path])[0]
                project.apply("claude", "path-CAS-negative-" + label, project.snapshot()["revision"], [{
                    "path": path, "previous_sha256": current["sha256"], "content": content}])
                code, changed = self.request("GET", f"/api/rooms/{self.room_id}")
                self.assertEqual(code, 200)
                self.assertFalse(changed["current_run"]["can_recover_native"])
                with self.assertRaises(RoomError) as failure:
                    self.service._audit_native_review_recovery(changed, candidate)
                self.assertEqual(failure.exception.code, "native_recovery_binding_changed")
                code, response = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.continuation(changed))
                self.assertEqual(code, 409, {"error": response.get("error")})
                self.assertEqual(len(self.native.calls), calls)


if __name__ == "__main__":
    unittest.main()
