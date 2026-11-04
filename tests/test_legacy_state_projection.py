"""Legacy claim events remain readable through actual HTTP/SQLite projection."""
import json
import unittest
from uuid import uuid4

import test_room_paging as paging


class LegacyStateProjectionHTTPTests(unittest.TestCase):
    setUp = paging.RoomPagingHTTPTests.setUp
    tearDown = paging.RoomPagingHTTPTests.tearDown
    request = paging.RoomPagingHTTPTests.request

    def completed_room(self, *, legacy=False, lifecycle="active"):
        settings = {"permission": "workspace_write", "members": {"claude": {"model": "recorded-model", "effort": "high"}}}
        room_id, _ = self.service.store.create_room("Read this historical request.", "claude", str(uuid4()),
                                                   work_mode="build", runtime_settings=settings)
        job = self.service.store.claim(room_id, "claude")
        self.service.store.finish_job(job["id"], message="Historical public result.", session_id=str(uuid4()))
        self.service.store.record_job_cleanup(job["id"], confirmed=True)
        with self.service.store._connection(write=True) as db:
            row = db.execute("SELECT seq,value_json FROM events WHERE room_id=? "
                             "AND json_extract(value_json,'$.kind')='job_claimed'", (room_id,)).fetchone()
            claim = json.loads(row["value_json"])
            if legacy:
                claim.pop("work_mode")
                claim.pop("runtime_settings")
                db.execute("UPDATE events SET value_json=? WHERE seq=?", (json.dumps(claim), row["seq"]))
            # Current choices must never be attributed to an older invocation.
            current = {"permission": "read_only", "effective_permission": "read_only",
                       "members": {"claude": {"model": "current-model", "effort": "low"}}}
            db.execute("UPDATE rooms SET work_mode='plan',runtime_settings_json=? WHERE id=?",
                       (json.dumps(current), room_id))
        if lifecycle != "active":
            self.service.store.set_lifecycle(room_id, "archive" if lifecycle == "archived" else "remove")
        return room_id, job["id"], claim, row["seq"]

    def test_old_plain_active_archived_and_removed_claims_read_without_live_configuration_backfill(self):
        for lifecycle in ("active", "archived", "removed"):
            with self.subTest(lifecycle=lifecycle):
                room_id, job_id, claim, _ = self.completed_room(legacy=True, lifecycle=lifecycle)
                before = self.service.store.snapshot(room_id)
                self.assertIsNone(before["current_run"])
                self.assertEqual(self.request("GET", f"/api/rooms/{room_id}")[0], 200)
                code, state, _ = self.request("GET", f"/api/rooms/{room_id}/state")
                self.assertEqual(code, 200, state)
                self.assertEqual(state["room"]["lifecycle_state"], lifecycle)
                job = next(item for item in state["jobs"] if item["id"] == job_id)
                self.assertEqual(job["work_mode"], "legacy")
                self.assertIsNone(job["runtime_settings"])
                self.assertFalse(job["permission_observed"])
                self.assertTrue(job["cleanup_confirmed"])
                code, page, _ = self.request("GET", f"/api/rooms/{room_id}/events?after_seq=0")
                self.assertEqual(code, 200, page)
                actual = next(event for event in page["events"] if event["kind"] == "job_claimed")
                self.assertNotIn("work_mode", actual)
                self.assertNotIn("runtime_settings", actual)
                code, text, _ = self.request("GET", paging.text_route(room_id, job["text_text_ref"]))
                self.assertEqual(code, 200, text)
                self.assertEqual(text["text"], "Read this historical request.")
                self.assertEqual(self.service.store.snapshot(room_id), before)
        self.assertEqual(self.calls, [])

    def test_modern_claim_keeps_its_recorded_mode_models_and_permission(self):
        room_id, job_id, claim, seq = self.completed_room()
        before = self.service.store.snapshot(room_id)
        code, state, _ = self.request("GET", f"/api/rooms/{room_id}/state")
        self.assertEqual(code, 200, state)
        job = next(item for item in state["jobs"] if item["id"] == job_id)
        self.assertEqual(job["work_mode"], "build")
        self.assertEqual(job["runtime_settings"], claim["runtime_settings"])
        self.assertEqual(job["runtime_settings"]["members"]["claude"]["model"], "recorded-model")
        self.assertEqual(job["permission"], "workspace_write")
        self.assertEqual(job["claim_seq"], seq)
        self.assertEqual(self.service.store.snapshot(room_id), before)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
