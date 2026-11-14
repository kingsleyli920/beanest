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

    def restarted(self, snapshot, candidate):
        """Continue through HTTP and return the settled room after the generic restart."""
        old_rows, calls = self.job_rows([candidate["job_id"]]), len(self.native.calls)
        self.review_release.set()  # The fresh run's T2 review must not wait for this fixture.
        code, accepted = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.continuation(snapshot))
        self.assertEqual(code, 200, {"error": accepted.get("error")})
        final = self.settled(self.room_id)
        kinds = [event["kind"] for event in final["events"]]
        self.assertEqual(kinds.count("native_unconfirmed_calls_abandoned"), 1)
        # The audited review recovery never ran: its evidence did not hold.
        self.assertEqual(kinds.count("native_review_recovery_attestation"), 0)
        self.assertEqual(kinds.count("native_review_retry_queued"), 0)
        self.assertNotEqual(final["current_run"]["id"], snapshot["current_run"]["id"])
        # The failed review is abandoned, never replayed or rewritten.
        self.assertEqual(self.job_rows([candidate["job_id"]]), old_rows)
        later = self.native.calls[calls:]
        self.assertTrue(later)
        self.assertNotIn(candidate["job_id"], [call["context"]["job"]["id"] for call in later])
        return final, later

    def test_unpublished_private_changes_block_the_review_recovery_and_are_reported_not_published(self):
        # Catches two defects: a stray private file being imported (or thrown
        # away) by a recovery it was never part of, and the room refusing every
        # way to continue because of that file.
        snapshot, candidate = self.failed()
        state = self.service.store.native_workspace(self.room_id, candidate["provider"])
        retained = Path(state["project"]) / "unauthorized.txt"
        retained.write_text("Retain this evidence; recovery must not import or discard it.\n")
        code, changed = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200)
        self.assertFalse(changed["current_run"]["can_recover_native"])
        self.assertEqual(changed["current_run"]["native_recovery_reason"], "native_recovery_unpublished_changes")
        self.assertTrue(changed["continuation"]["can_send"], changed["continuation"])
        final, later = self.restarted(snapshot, candidate)
        abandoned = next(event for event in final["events"] if event["kind"] == "native_unconfirmed_calls_abandoned")
        self.assertEqual(abandoned["job_ids"], [candidate["job_id"]])
        self.assertEqual(abandoned["unowned_private_changes"], [{"provider": "codex", "paths": ["unauthorized.txt"]}])
        self.assertEqual(later[0]["context"]["objective"]["continuation"]["previous_stop"]["unowned_private_changes"],
                         [{"provider": "codex", "paths": ["unauthorized.txt"]}])
        # The stray file stays where it was and never reaches the shared files.
        self.assertEqual(retained.read_text(), "Retain this evidence; recovery must not import or discard it.\n")
        self.assertNotIn("unauthorized.txt", [file["path"] for file in final["project"]["files"]])
        # Delivered work is not redone: only the unfinished summary is written.
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 1})

    def changed_reviewed_file(self, contents):
        """The reviewed path changes after the failed review; the review recovery must refuse each time."""
        _, candidate = self.failed()
        project, path = self.service._project(self.room_id), "results/second.md"
        calls = len(self.native.calls)
        for index, content in enumerate(contents):
            current = project.files_for_paths([path])[0]
            project.apply("claude", f"path-CAS-negative-{index}", project.snapshot()["revision"], [{
                "path": path, "previous_sha256": current["sha256"], "content": content}])
            code, changed = self.request("GET", f"/api/rooms/{self.room_id}")
            self.assertEqual(code, 200)
            self.assertFalse(changed["current_run"]["can_recover_native"])
            with self.assertRaises(RoomError) as failure:
                self.service._audit_native_review_recovery(changed, candidate)
            self.assertEqual(failure.exception.code, "native_recovery_binding_changed")
            self.assertTrue(changed["continuation"]["can_send"], changed["continuation"])
        self.assertEqual(len(self.native.calls), calls, "reading the room dispatched nothing")
        final, later = self.restarted(changed, candidate)
        return final, later, project.files_for_paths([path])[0]["content"]

    def test_a_changed_reviewed_path_cannot_authorize_the_review_recovery_but_the_room_restarts(self):
        # Catches a review recovery being authorized for bytes that are no
        # longer the reviewed ones, and the room locking because of it.
        final, later, content = self.changed_reviewed_file(["A new unreviewed version.\n"])
        self.assert_fresh_review(final, later)
        self.assertEqual(content, "A new unreviewed version.\n", "the restart did not put the old bytes back")

    def test_a_reverted_reviewed_path_cannot_authorize_the_review_recovery_but_the_room_restarts(self):
        # Catches ABA: the path changed and went back to the same bytes, which
        # a hash comparison alone would accept as "unchanged since the review".
        final, later, content = self.changed_reviewed_file(
            ["A new unreviewed version.\n", "Synthetic independently owned T2 delivery.\n"])
        self.assert_fresh_review(final, later)
        self.assertEqual(content, "Synthetic independently owned T2 delivery.\n")

    def assert_fresh_review(self, final, later):
        # The delivered T2 is judged again by a new review call; nothing is
        # redone and the old review's verdict is not what accepted it.
        self.assertEqual([(call["agent"], call["context"]["job"]["job_phase"], (call["context"].get("task") or {}).get("id"))
                          for call in later[:2]], [("codex", "review", "T2"), ("codex", "work", "T3")])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 1})

if __name__ == "__main__":
    unittest.main()
