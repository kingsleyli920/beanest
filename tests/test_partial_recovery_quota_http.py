"""Partial recovery installs inspection before quota retirement can dispatch work."""
import asyncio
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_long_team_result_http as http
import test_native_room_service as native
from test_team_store import proposal


class PartialRecoveryQuotaHTTPTests(unittest.TestCase):
    request = http.LongTeamResultHTTPTests.request
    setUp = http.LongTeamResultHTTPTests.setUp
    settled = http.LongTeamResultHTTPTests.settled

    def tearDown(self):
        if hasattr(self, "release_inspection"):
            self.release_inspection.set()
        http.LongTeamResultHTTPTests.tearDown(self)

    def handler(self, agent, context, cwd):
        async def reply():
            phase, task = context["job"]["job_phase"], context.get("task")
            if phase in {"plan", "coordinate"}:
                commands = []
                if not context["team"]["tasks"] and agent == context["team"]["plan_author"]:
                    commands = [proposal("T1", paths=["main.py"]), proposal("T2", paths=["notes.md"]),
                                {"type": "claim", "task_id": "T1"}, {"type": "claim", "task_id": "T2"}]
                return native.native_reply(context, [*commands, {"type": "plan_ready"}])
            if context.get("native_partial_inspection"):
                self.assertEqual(agent, "codex")
                self.assertEqual(context["native_permission"], "read_only")
                self.assertIsNone(self.native.calls[-1]["session_id"])
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                self.inspection_started.set()
                while not self.release_inspection.is_set():
                    await asyncio.sleep(0.005)
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"],
                    "result": "Read the retained bytes; no implementation or test was replayed."}])
            if phase == "work":
                if getattr(self, "fail_first", False):
                    self.assertEqual(task["id"], "T1")
                    (cwd / "main.py").write_text("VALUE = 2\n")
                    self.native.failure = {"status": "failed", "failure_code": "cli_nonzero_exit", "native_execution": {
                        "permission": "full_access", "tool_calls": 1, "tool_results": 1, "cleanup_confirmed": True,
                        "side_effects_possible": True, "exit_code": 1, "duration_seconds": 0.01}}
                    return native.native_reply(context)
                self.assertEqual(context["native_permission"], "full_access")
                if task["id"] == "T1":
                    self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                else:
                    (cwd / "notes.md").write_text("Remaining independent notes.\n")
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"],
                    "result": "Submitted the remaining current result."}])
            if phase == "review":
                return native.native_reply(context, [{"type": "review", "task_id": task["id"],
                    "verdict": "accept", "reason": "Checked this exact current result."}])
            return http.LongTeamResultHTTPTests.handler(self, agent, context, cwd)
        return reply()

    def create_partial_failure(self):
        (self.source / "main.py").write_text("VALUE = 1\n")
        self.fail_first = True
        self.inspection_started, self.release_inspection = threading.Event(), threading.Event()
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Update the value and remaining notes.", "target": "codex", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        before = self.settled(created["room"]["id"])
        self.fail_first, self.native.failure = False, None
        self.assertEqual(before["team"]["stop_reason"], "cli_nonzero_exit")
        self.assertTrue(before["current_run"]["can_recover_native"])
        return before

    def recover_with_unavailable_peer(self, before):
        room, current = before["room"]["id"], before["current_run"]
        self.recovery_start = len(self.native.calls)
        body = {"request_id": str(uuid4()), "run_id": current["id"],
                "revision": current["revision"], "digest": current["digest"]}
        with patch.object(self.service, "_guard_objective_availability", AsyncMock(return_value=["claude"])):
            code, recovered = self.request("POST", f"/api/rooms/{room}/recover-native", body)
        self.assertEqual(code, 200, recovered)
        self.assertTrue(self.inspection_started.wait(3))
        code, during = self.request("GET", f"/api/rooms/{room}")
        self.assertEqual(code, 200, during)
        group = next(event for event in during["events"] if event["kind"] == "native_partial_inspection_group"
                     and event["run_id"] == during["current_run"]["id"])
        self.assertEqual(len(group["inspection_job_ids"]), 1)
        calls = self.native.calls[self.recovery_start:]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["context"]["native_partial_inspection"])
        self.assertFalse(any(event["kind"] == "native_partial_work_queued" and event["run_id"] == group["run_id"]
                             for event in during["events"]))
        self.assertFalse(any(job["status"] == "queued" for job in during["jobs"]))
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        return during

    def test_partial_owner_inspects_before_other_owned_work_when_peer_quota_is_blocked(self):
        before = self.create_partial_failure()
        during = self.recover_with_unavailable_peer(before)
        self.release_inspection.set()
        final = self.settled(before["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual({task["id"]: task["status"] for task in final["team"]["tasks"]},
                         {"T1": "self_checked", "T2": "self_checked"})
        calls = self.native.calls[self.recovery_start:]
        self.assertTrue(all(call["agent"] == "codex" for call in calls))
        ordinary = [call for call in calls if call["context"]["job"]["job_phase"] == "work"
                    and not call["context"].get("native_partial_inspection")]
        self.assertEqual([call["context"]["task"]["id"] for call in ordinary], ["T1", "T2"])
        old = {job["id"]: (job["status"], job["error"]) for job in before["jobs"]}
        self.assertEqual({job["id"]: (job["status"], job["error"]) for job in final["jobs"] if job["id"] in old}, old)
        self.assertNotEqual(during["current_run"]["id"], before["current_run"]["id"])
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")

    def test_pause_after_quota_peer_recovery_does_not_release_any_ordinary_work(self):
        before = self.create_partial_failure()
        during = self.recover_with_unavailable_peer(before)
        room = before["room"]["id"]
        code, paused = self.request("POST", f"/api/rooms/{room}/pause", {
            "request_id": str(uuid4()), "objective_id": during["objective"]["id"]})
        self.assertEqual(code, 200, paused)
        self.release_inspection.set()
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "paused")
        self.assertEqual(len(self.native.calls[self.recovery_start:]), 1)
        self.assertFalse(any(event["kind"] == "native_partial_work_queued"
                             and event["run_id"] == final["current_run"]["id"] for event in final["events"]))
        self.assertTrue(all(task["result_version"] == 0 for task in final["team"]["tasks"]))
        state = self.service.store.native_workspace(room, "codex")
        self.assertEqual((Path(state["project"]) / "main.py").read_text(), "VALUE = 2\n")


if __name__ == "__main__":
    unittest.main()
