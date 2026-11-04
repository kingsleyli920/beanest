"""Sidebar activity comes from real current jobs, not an objective label."""
import copy
import time
import unittest
from uuid import uuid4

import test_room_paging as paging


class RoomListActivityTests(unittest.TestCase):
    setUp = paging.RoomPagingHTTPTests.setUp
    tearDown = paging.RoomPagingHTTPTests.tearDown
    request = paging.RoomPagingHTTPTests.request

    def test_http_two_working_rooms_queued_and_completed_keep_activity_in_compact_list(self):
        working_ids = []
        for goal in ("First held request", "Second held request"):
            code, created, _ = self.request("POST", "/api/rooms?response=ack",
                {"goal": goal, "target": "claude", "request_id": str(uuid4())})
            self.assertEqual(code, 200, created)
            working_ids.append(created["room_id"])
        deadline = time.monotonic() + 2
        while len(self.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(self.calls), 2)
        queued_id, _ = self.service.store.create_room("A queued request", "claude", str(uuid4()))
        completed_id, _ = self.service.store.create_room("An already completed request", "codex", str(uuid4()))
        job = self.service.store.claim(completed_id, "codex")
        self.service.store.finish_job(job["id"], message="Completed without additional work.")
        self.service.store.record_job_cleanup(job["id"], confirmed=True)

        for suffix in ("", "?view=compact"):
            code, listed, _ = self.request("GET", "/api/rooms" + suffix)
            self.assertEqual(code, 200, listed)
            rooms = {room["id"]: room for room in listed["rooms"]}
            for room_id in working_ids:
                self.assertEqual(rooms[room_id]["work_activity"],
                    {"state": "working", "working_jobs": 1, "queued_jobs": 0, "draining": False})
            self.assertEqual(rooms[queued_id]["work_activity"],
                {"state": "queued", "working_jobs": 0, "queued_jobs": 1, "draining": False})
            self.assertEqual(rooms[completed_id]["work_activity"],
                {"state": "idle", "working_jobs": 0, "queued_jobs": 0, "draining": False})
            if suffix:
                self.assertEqual(listed["projection"], "room_list_v1")
                self.assertIn("goal_text_ref", rooms[working_ids[0]])
            for room_id in (*working_ids, queued_id, completed_id):
                code, state, _ = self.request("GET", f"/api/rooms/{room_id}/state")
                self.assertEqual(code, 200, state)
                self.assertEqual(state["room"]["work_activity"], rooms[room_id]["work_activity"])
        self.assertEqual(len(self.calls), 2, "Listing must not dispatch the retained queued room.")

    def test_current_run_filter_and_pause_terminal_lifecycle_gates_preserve_truthful_counts(self):
        room_id, _ = self.service.store.create_room("A current team run", "both", str(uuid4()),
            collaboration=True, execution_policy={"mode": "autonomous"})
        snapshot = self.service.store.snapshot(room_id)
        roles = self.service.store.job_roles(room_id)
        summarize = self.service._room_work_activity
        queued = summarize(snapshot, roles)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["queued_jobs"], 2)
        stale = copy.deepcopy(snapshot)
        stale["current_run"]["id"] = str(uuid4())
        self.assertEqual(summarize(stale, roles),
            {"state": "idle", "working_jobs": 0, "queued_jobs": 0, "draining": False})

        paused = copy.deepcopy(snapshot)
        paused["objective"].update(status="paused", pause_requested=True)
        self.assertEqual(summarize(paused, roles)["state"], "idle")
        paused["jobs"][0]["status"] = "working"
        self.assertEqual(summarize(paused, roles),
            {"state": "working", "working_jobs": 1, "queued_jobs": 1, "draining": True})
        for status in ("needs_user", "complete", "stopped", "interrupted"):
            stopped = copy.deepcopy(paused)
            stopped["objective"]["status"] = status
            activity = summarize(stopped, roles)
            self.assertEqual(activity["state"], "idle")
            self.assertEqual(activity["working_jobs"], 1, "An inactive display must not falsify historical job state.")
        for lifecycle in ("archived", "removed"):
            inactive = copy.deepcopy(paused)
            inactive["room"]["lifecycle_state"] = lifecycle
            self.assertEqual(summarize(inactive, roles)["state"], "idle")
        unknown = copy.deepcopy(paused)
        unknown["room"]["status"] = "unknown"
        self.assertEqual(summarize(unknown, roles)["state"], "idle")
        executing = copy.deepcopy(snapshot)
        executing["jobs"] = []
        executing["executions"] = [{"team_run_id": snapshot["current_run"]["id"], "status": "working"}]
        self.assertEqual(summarize(executing, roles)["state"], "working")
        self.assertEqual(summarize(executing, roles)["working_jobs"], 0)
        executing["executions"][0]["team_run_id"] = str(uuid4())
        self.assertEqual(summarize(executing, roles)["state"], "idle")
        legacy_paused = copy.deepcopy(snapshot)
        legacy_paused.pop("objective")
        legacy_paused.pop("current_run")
        legacy_paused["room"]["status"] = "paused"
        self.assertEqual(summarize(legacy_paused, {})["state"], "idle")
        legacy_paused["jobs"][0]["status"] = "working"
        self.assertEqual(summarize(legacy_paused, {}),
            {"state": "working", "working_jobs": 1, "queued_jobs": 1, "draining": True})
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
