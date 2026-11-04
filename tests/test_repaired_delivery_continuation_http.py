"""Continue cancelled native work after real accepted repairs of the same paths."""
import asyncio
import hashlib
import json
import threading
import unittest
from urllib.parse import urlencode
from uuid import uuid4

import test_native_help_repair_http as repairs
import test_native_review_submission_http as review_fixture


class RepairedDeliveryContinuationHTTPTests(unittest.TestCase):
    request = repairs.NativeHelpRepairHTTPTests.request
    settled = repairs.NativeHelpRepairHTTPTests.settled
    job_rows = review_fixture.NativeReviewSubmissionHTTPTests.job_rows

    def setUp(self):
        self.cancel_started = threading.Event()
        self.resume_started = threading.Event()
        self.resume_release = threading.Event()
        self.hold_continuation = True
        self.cancelled_once = False
        repairs.NativeHelpRepairHTTPTests.setUp(self)

    def tearDown(self):
        self.resume_release.set()
        repairs.NativeHelpRepairHTTPTests.tearDown(self)

    async def handler(self, agent, context, cwd):
        task = context.get("task")
        phase = context["job"]["job_phase"]
        if phase == "work" and task["id"] == "M3" and self.help_requested:
            self.assertTrue(all(item["status"] == "accepted" for item in context["team"]["tasks"]
                                if item["id"].startswith("R")))
            if not self.cancelled_once:
                call = next(call for call in reversed(self.native.calls)
                            if call["context"]["job"]["id"] == context["job"]["id"])
                self.assertIsNotNone(call["session_id"])
                room = self.service.store.list_rooms()["rooms"][0]["id"]
                directory = self.root / "native-receipts" / room
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                receipt = directory / (str(uuid4()) + ".jsonl")
                receipt.write_text(json.dumps({"channel": "stdout", "data": {
                    "type": "thread.started", "thread_id": call["session_id"]}}) + "\n")
                self.cancel_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    self.cancelled_once = True
                    error.native_execution = {"permission": call["permission"], "tool_calls": 1,
                        "tool_results": 1, "cleanup_confirmed": True, "side_effects_possible": False,
                        "exit_code": -15, "receipt_path": str(receipt)}
                    raise
            if self.hold_continuation:
                self.resume_started.set()
                while not self.resume_release.is_set():
                    await asyncio.sleep(0.005)
        return await repairs.NativeHelpRepairHTTPTests.handler(self, agent, context, cwd)

    @staticmethod
    def continue_body(snapshot, text="继续"):
        return {"text": text, "target": "all", "request_id": str(uuid4()), "intent": "continue",
                "binding": snapshot["continuation"]["binding"]}

    def bootstrap_stopped(self):
        """Real accepted repairs plus a cancelled dependent task; reusable for CUA."""
        self.phases, self.inspections, self.resumed = [], [], 0
        self.help_requested, self.legacy_help = False, False
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Implement two source files, repair their integration and review the report.",
            "target": "claude", "request_id": str(uuid4()), "mode": "native_project",
            "project_id": self.project_id, "participants": ["claude", "codex"], "work_mode": "build",
            "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, {"error": created.get("error")})
        self.room_id = created["room"]["id"]
        self.assertTrue(self.cancel_started.wait(5), "Dependent M3 did not start after accepted repairs")
        code, cancelled = self.request("POST", f"/api/rooms/{self.room_id}/cancel", {})
        self.assertEqual(code, 200, {"error": cancelled.get("error")})
        stopped = self.settled(self.room_id)
        self.assertTrue(stopped["cancellation_confirmed"])
        self.assertTrue(self.service.store.stage_cleanup_confirmed(self.room_id))
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in stopped["jobs"]))
        tasks = {task["id"]: task for task in stopped["team"]["tasks"]}
        self.assertEqual(tasks["M3"]["status"], "cancelled")
        self.assertTrue(all(task["status"] == "accepted" for task in tasks.values() if task["id"] != "M3"))
        for path, original in (("src/a.txt", "M1"), ("src/b.txt", "M2")):
            old_sha = next(file["sha256"] for file in tasks[original]["result_files"] if file["path"] == path)
            repair = next(task for task in tasks.values() if task["id"].startswith("R") and path in task["write_paths"])
            new_sha = next(file["sha256"] for file in repair["result_files"] if file["path"] == path)
            self.assertNotEqual(old_sha, new_sha)
        return stopped

    def resume_and_complete(self, stopped):
        previous_calls = len(self.native.calls)
        old_ids = [job["id"] for job in stopped["jobs"]]
        old_rows = self.job_rows(old_ids)
        code, response = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.continue_body(stopped))
        self.assertEqual(code, 200, {"error": response.get("error")})
        try:
            self.assertTrue(self.resume_started.wait(5), "Fresh M3 work did not start")
            code, current = self.request("GET", f"/api/rooms/{self.room_id}")
            self.assertEqual(code, 200)
            self.assertNotEqual(current["current_run"]["id"], stopped["current_run"]["id"])
            previous = {task["id"]: task for task in stopped["team"]["tasks"] if task["id"] != "M3"}
            for task in current["team"]["tasks"]:
                if task["id"] in previous:
                    for key in ("status", "result", "result_version", "result_files", "review_kind"):
                        self.assertEqual(task[key], previous[task["id"]][key], (task["id"], key))
                    self.assertEqual(task["assignment_epoch"], previous[task["id"]]["assignment_epoch"] + 1)
                    self.assertEqual(task["inherited_from_run"], stopped["current_run"]["id"])
            baseline = current["objective"]["baseline"]["files"]
            for path in ("src/a.txt", "src/b.txt"):
                bound = [file for file in baseline if file["path"] == path]
                self.assertEqual(len(bound), 1)
                self.assertEqual(bound[0]["sha256"], hashlib.sha256(("repaired " + path + "\n").encode()).hexdigest())
            new_calls = self.native.calls[previous_calls:]
            self.assertTrue(new_calls)
            self.assertTrue(all(call["context"]["job"]["job_phase"] == "work"
                                and call["context"]["task"]["id"] == "M3" for call in new_calls))
        finally:
            self.resume_release.set()
        final = self.settled(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"]["stop_reason"])
        self.assertEqual(self.job_rows(old_ids), old_rows)
        self.assertEqual(sum(phase == "work" and name in {"M1", "M2"} for phase, name, *_ in self.phases), 2)
        self.assertEqual(self.resumed, 1)
        return final

    def delivery_file(self, snapshot, sha256):
        run = snapshot["current_run"]
        return self.request("GET", f"/api/rooms/{self.room_id}/file?" + urlencode({
            "run_id": run["id"], "revision": run["revision"], "digest": run["digest"],
            "path": "src/a.txt", "sha256": sha256}))

    def test_cancelled_after_accepted_same_path_repairs_continues_without_replaying_work(self):
        self.resume_and_complete(self.bootstrap_stopped())

    def test_real_canonical_drift_still_rejects_continue_without_dispatch(self):
        stopped = self.bootstrap_stopped()
        project = self.service._project(self.room_id)
        original = project.files_for_paths(["src/a.txt"])[0]
        project.apply("claude", "real-canonical-drift", project.snapshot()["revision"], [{
            "path": "src/a.txt", "previous_sha256": original["sha256"], "content": "Later canonical bytes.\n"}])
        code, current = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200)
        calls = len(self.native.calls)
        code, rejected = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.continue_body(current))
        self.assertEqual((code, rejected.get("error")), (409, "objective_binding_changed"))
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(current["current_run"]["id"], stopped["current_run"]["id"])

    def test_old_run_file_binding_stays_on_its_repaired_bytes_after_a_later_canonical_commit(self):
        first = self.resume_and_complete(self.bootstrap_stopped())
        code, response = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.continue_body(
            first, "开始一个独立文字说明目标，保留此前修复成果。"))
        self.assertEqual(code, 200, {"error": response.get("error")})
        followup = self.settled(self.room_id)
        self.assertEqual(followup["objective"]["status"], "complete", followup["team"]["stop_reason"])
        self.assertNotEqual(followup["current_run"]["id"], first["current_run"]["id"])
        # _snapshot follows new canonical bindings for the current run. Make
        # first genuinely historical before checking its immutable file version.
        original_sha = hashlib.sha256(b"original src/a.txt\n").hexdigest()
        repaired_sha = hashlib.sha256(b"repaired src/a.txt\n").hexdigest()
        project = self.service._project(self.room_id)
        original = project.files_for_paths(["src/a.txt"])[0]
        self.assertEqual(original["sha256"], repaired_sha)
        later = project.apply("claude", "later-canonical-commit", project.snapshot()["revision"], [{
            "path": "src/a.txt", "previous_sha256": original["sha256"], "content": "Later canonical bytes.\n"}])
        self.assertGreater(later["revision"], first["current_run"]["revision"])
        newer_sha = hashlib.sha256(b"Later canonical bytes.\n").hexdigest()
        self.assertEqual(project.files_for_paths(["src/a.txt"])[0]["sha256"], newer_sha)
        code, old = self.delivery_file(first, repaired_sha)
        self.assertEqual((code, old.get("content")), (200, "repaired src/a.txt\n"))
        self.assertEqual(self.delivery_file(first, original_sha)[0], 409)
        self.assertEqual(self.delivery_file(first, newer_sha)[0], 409)


if __name__ == "__main__":
    unittest.main()
