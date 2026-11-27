"""A stale pause drain is reconciled from cleanup, never from UI state."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError, RoomStore


class ShutdownPauseResumeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RoomStore(Path(directory.name).resolve() / "rooms.sqlite3")
        self.addCleanup(self.store.close)

    def paused_call(self, cleanup=True, *, shutdown=True, defer_resume=False):
        room = self.store.create_room("Synthetic paused native call", "codex", str(uuid4()),
            mode="native_project", project_id=str(uuid4()), participants=["codex"], work_mode="build",
            execution_policy={"mode": "autonomous"})[0]
        job = self.store.claim(room, "codex")
        objective = self.store.snapshot(room)["objective"]["id"]
        self.store.objective_action(room, "pause", str(uuid4()), [objective], objective_id=objective)
        if defer_resume:
            self.store.objective_action(room, "resume", str(uuid4()), [objective], objective_id=objective,
                                        request_identity=["explicit deferred resume", room])
        if cleanup is not None:
            # The receipt can precede the final shutdown transaction, so this
            # still sees a working call and leaves pause_requested set.
            self.store.record_job_cleanup(job["id"], confirmed=cleanup)
        if shutdown:
            self.store.request_stop(room, "service_shutdown")
            self.store.finish_stop(room, confirmed=False, final_status="interrupted")
        return room, objective, job

    def resume(self, room, objective):
        self.store.objective_action(room, "resume", str(uuid4()), [objective], objective_id=objective)

    def test_clean_shutdown_resume_leaves_pause_without_clearing_identity_or_run_recovery(self):
        room, objective, job = self.paused_call()
        before = self.store.snapshot(room)
        self.assertTrue(before["objective"]["pause_requested"])
        self.resume(room, objective)
        after = self.store.snapshot(room)
        self.assertEqual(after["objective"]["status"], "stopped")
        self.assertFalse(after["objective"]["pause_requested"])
        self.assertEqual(after["objective"]["stop_reason"], "service_shutdown")
        self.assertEqual(after["current_run"]["id"], before["current_run"]["id"])
        self.assertEqual(after["current_run"]["status"], "unknown")
        self.assertEqual(after["jobs"], before["jobs"])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT identity_uncertain FROM members WHERE room_id=?", (room,)).fetchone()[0], 1)
        self.assertIsNone(self.store.claim(room, "codex"), "identity recovery must still prevent dispatch")

    def test_working_call_is_not_released_even_with_cleanup_receipt(self):
        room, objective, job = self.paused_call(shutdown=False)
        with self.assertRaises(RoomError) as raised:
            self.resume(room, objective)
        self.assertEqual(raised.exception.code, "objective_not_paused")
        self.assertTrue(self.store.snapshot(room)["objective"]["pause_requested"])

    def test_missing_or_false_cleanup_stays_blocked_until_a_real_confirmation(self):
        for cleanup in (None, False):
            with self.subTest(cleanup=cleanup):
                room, objective, job = self.paused_call(cleanup)
                with self.assertRaises(RoomError) as raised:
                    self.resume(room, objective)
                self.assertEqual(raised.exception.code, "objective_not_paused")
                self.assertEqual(self.store.snapshot(room)["objective"]["status"], "paused")
                self.store.record_job_cleanup(job["id"], confirmed=True)
                self.resume(room, objective)
                self.assertEqual(self.store.snapshot(room)["objective"]["status"], "stopped")

    def test_an_already_deferred_resume_is_settled_once_without_rolling_back_cleanup(self):
        room, objective, job = self.paused_call(defer_resume=True)
        self.resume(room, objective)
        after = self.store.snapshot(room)
        self.assertEqual(after["objective"]["status"], "stopped")
        resumed = [event for event in after["events"] if event["kind"] == "objective_resumed"]
        self.assertEqual(len(resumed), 1)
        self.assertTrue(resumed[0]["after_cleanup"])


if __name__ == "__main__":
    unittest.main()
