import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_response_schema import response_schema
from local_agent_coordinator.room_store import MAX_INSPECTION_CONTEXT_BYTES, RoomError, RoomStore


class ProjectInspectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "rooms.sqlite3"
        self.now = 1000.0
        self.store = RoomStore(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def room(self, **options):
        return self.store.create_room("Inspect the selected project", "claude", str(uuid4()),
                                      mode="project_folder", project_id=str(uuid4()), **options)[0]

    def inspect(self, job, **options):
        return self.store.finish_project_inspection(job["id"], observation={
            "artifact_revision": 3,
            "request": {"kind": "read", "path": "src/app.py"},
            "result": {"content": "print('hello')", "sha256": "a" * 64},
        }, **options)

    def plan(self):
        room_id = self.room(collaboration=True)
        author = self.store.claim(room_id, "claude")
        peer = self.store.claim(room_id, "codex")
        proposal = {"type": "propose", "id": "main", "title": "Update app", "description": "Update the app.",
                    "acceptance": "Peer reviews changes", "dependencies": [], "write_paths": ["src/app.py"],
                    "capability": "text"}
        self.store.finish_job(author["id"], team_commands=[proposal, {"type": "claim", "task_id": "main"},
                                                         {"type": "plan_ready"}])
        self.store.finish_job(peer["id"], team_commands=[{"type": "plan_ready"}])
        confirmation = self.store.claim(room_id, "codex")
        self.store.finish_job(confirmation["id"], team_commands=[{"type": "plan_ready"}])
        return room_id

    def context(self, room_id, provider="claude", run_id=None, task_id=None, phase="task"):
        return self.store.inspection_context(room_id, provider, run_id, task_id, phase)

    def test_folder_schema_only_adds_request_and_single_member_edits(self):
        options = {"project": True, "member_ids": ("claude",), "provider": "claude"}
        original = response_schema(**options)
        folder = response_schema(**options, folder=True)
        self.assertNotIn("project_request", original["properties"])
        self.assertNotIn("edits", original["properties"])
        self.assertIn("edits", folder["required"])
        request = folder["properties"]["project_request"]
        self.assertEqual(request["type"], ["object", "null"])
        self.assertEqual(set(request["required"]), {"kind", "path", "query", "offset", "limit"})
        self.assertEqual(request["properties"]["limit"]["maximum"], 200)
        with self.assertRaises(ValueError):
            response_schema(**options, folder=1)

    def test_single_member_inspection_continues_without_completing_room(self):
        room_id = self.room(participants=["claude"])
        job = self.store.claim(room_id, "claude")
        session = str(uuid4())
        next_id = self.inspect(job, session_id=session, message="Read app", usage={"input_tokens": 9})
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "running")
        self.assertEqual(snapshot["budget"]["used"], 1)
        self.assertEqual([item["status"] for item in snapshot["jobs"]], ["completed", "queued"])
        self.assertTrue(any(event["kind"] == "usage" and event["usage"]["input_tokens"] == 9
                            for event in snapshot["events"]))
        resumed = self.store.claim(room_id, "claude")
        self.assertEqual(resumed["id"], next_id)
        self.assertEqual(resumed["session_id"], session)
        self.assertEqual(self.store.snapshot(room_id)["budget"]["used"], 2)
        self.store.finish_job(next_id, message="Completed")
        self.assertEqual(self.store.snapshot(room_id)["room"]["status"], "completed")

    def test_duplicate_completion_does_not_save_or_queue_twice(self):
        room_id = self.room()
        job = self.store.claim(room_id, "claude")
        first = self.inspect(job)
        self.assertIsNone(self.inspect(job))
        self.assertEqual(len(self.store.snapshot(room_id)["jobs"]), 2)
        context = self.context(room_id)
        self.assertEqual([item["job_id"] for item in context["observations"]], [job["id"]])
        self.assertEqual(self.store.claim(room_id, "claude")["id"], first)

    def test_legacy_artifact_parent_and_reply_target_survive_inspections(self):
        room_id = self.room()
        author = self.store.claim(room_id, "claude")
        self.store.finish_job(author["id"], artifact_revision=1, action={
            "to": "codex", "kind": "review", "text": "Review app"})
        review = self.store.claim(room_id, "codex")
        self.inspect(review)
        resumed = self.store.claim(room_id, "codex")
        self.assertEqual(resumed["parent_id"], author["id"])
        self.assertEqual(resumed["reply_to"], "claude")
        self.assertEqual(resumed["kind"], "review")
        self.store.finish_job(resumed["id"], message="Approved")
        self.assertEqual(self.store.claim(room_id, "claude")["kind"], "feedback")

    def test_historical_constructor_budget_does_not_stop_inspection(self):
        self.store.close()
        self.store = RoomStore(self.path, max_calls=2, clock=lambda: self.now)
        room_id = self.room()
        for _ in range(3):
            self.assertIsNotNone(self.inspect(self.store.claim(room_id, "claude")))
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "running")
        self.assertEqual(snapshot["budget"]["used"], 3)
        self.assertIsNone(snapshot["budget"]["limit"])
        self.assertEqual(len(snapshot["jobs"]), 4)
        self.assertEqual(len(self.context(room_id)["observations"]), 3)
        self.store.request_stop(room_id, "user_stopped")
        self.assertIsNone(self.store.claim(room_id, "claude"))

    def test_historical_deadline_allows_current_inspection(self):
        room_id = self.room()
        job = self.store.claim(room_id, "claude")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET deadline=? WHERE id=?", (self.now + 900, room_id))
        self.now += 901
        self.assertIsNotNone(self.inspect(job))
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "running")
        self.assertEqual(len(snapshot["jobs"]), 2)
        self.assertEqual(len(self.context(room_id)["observations"]), 1)

    def test_stop_prevents_observation_and_followup(self):
        room_id = self.room()
        job = self.store.claim(room_id, "claude")
        self.store.request_stop(room_id, "user_stopped")
        self.assertIsNone(self.inspect(job))
        self.assertEqual(self.context(room_id)["observations"], [])
        self.assertEqual(len(self.store.snapshot(room_id)["jobs"]), 1)

    def test_identity_conflict_and_uncertainty_fail_without_inspection(self):
        for uncertain in (False, True):
            room_id = self.room()
            job = self.store.claim(room_id, "claude")
            session = str(uuid4())
            if not uncertain:
                with self.store._connection(write=True) as db:
                    db.execute("UPDATE members SET session_id=? WHERE room_id=? AND provider='claude'",
                               (str(uuid4()), room_id))
            self.inspect(job, session_id=session, uncertain=uncertain)
            snapshot = self.store.snapshot(room_id)
            self.assertEqual(snapshot["jobs"][0]["status"], "unknown")
            self.assertEqual(len(snapshot["jobs"]), 1)
            self.assertEqual(self.context(room_id)["observations"], [])

    def test_planning_inspection_does_not_reconcile_or_finish_phase(self):
        room_id = self.room(collaboration=True)
        job = self.store.claim(room_id, "claude")
        self.inspect(job)
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["phase"], "planning")
        self.assertEqual(snapshot["team"]["tasks"], [])
        resumed = self.store.claim(room_id, "claude")
        self.assertEqual(resumed["team"], job["team"])

    def test_work_inspection_preserves_task_binding_and_correction_markers(self):
        room_id = self.plan()
        job = self.store.claim(room_id, "claude")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE team_jobs SET format_attempt=1,correction_of=?,capacity_correction_of=?,"
                       "repair_execution_id='exec-proof' WHERE job_id=?", (job["id"], job["id"], job["id"]))
            db.execute("UPDATE shared_tasks SET repair_execution_id='exec-proof' WHERE room_id=?", (room_id,))
        self.inspect(job)
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["tasks"][0]["status"], "assigned")
        self.assertEqual(snapshot["team"]["tasks"][0]["result_version"], 0)
        self.assertEqual(snapshot["team"]["phase"], "working")
        resumed = self.store.claim(room_id, "claude")
        self.assertEqual(resumed["team"]["format_attempt"], 1)
        self.assertEqual(resumed["team"]["correction_of"], job["id"])
        self.assertEqual(resumed["team"]["capacity_correction_of"], job["id"])
        self.assertEqual(resumed["team"]["repair_execution_id"], "exec-proof")
        self.assertEqual(resumed["team"]["revision"], job["team"]["revision"])
        self.assertEqual(resumed["team"]["result_version"], job["team"]["result_version"])
        context = self.context(room_id, run_id=job["team"]["run_id"], task_id="main", phase="work")
        self.assertEqual(len(context["observations"]), 1)

    def test_changed_task_version_rejects_inspection_atomically(self):
        room_id = self.plan()
        job = self.store.claim(room_id, "claude")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET result_version=result_version+1 WHERE room_id=?", (room_id,))
        with self.assertRaises(RoomError) as failure:
            self.inspect(job)
        self.assertEqual(failure.exception.code, "team_result_changed")
        self.assertEqual(self.store.snapshot(room_id)["jobs"][-1]["status"], "working")

    def test_historical_team_budget_does_not_block_current_inspection(self):
        room_id = self.plan()
        job = self.store.claim(room_id, "claude")
        before = self.store.snapshot(room_id)["team"]["tasks"]
        with self.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET call_limit=used WHERE id=?", (room_id,))
        self.assertIsNotNone(self.inspect(job))
        snapshot = self.store.snapshot(room_id)
        # The completed inspection queues the same task's next call, moving
        # working back to assigned without changing its owner or result; the
        # finished call no longer marks the task as being worked on.
        expected = [{**{key: value for key, value in task.items() if key != "call_running"}, "status": "assigned"}
                    for task in before]
        self.assertTrue(all(task.get("call_running") for task in before))
        self.assertEqual(snapshot["team"]["tasks"], expected)
        self.assertEqual(snapshot["room"]["status"], "running")
        self.assertIsNone(snapshot["budget"]["limit"])
        self.assertEqual(snapshot["jobs"][-1]["status"], "queued")
        self.assertEqual(len(self.context(room_id, run_id=job["team"]["run_id"], task_id="main", phase="work")["observations"]), 1)

    def test_historical_team_deadline_does_not_fail_current_inspection(self):
        room_id = self.plan()
        job = self.store.claim(room_id, "claude")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET deadline=? WHERE id=?", (self.now + 7200, room_id))
        self.now += 7201
        self.assertIsNotNone(self.inspect(job))
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "running")
        self.assertEqual(snapshot["jobs"][-1]["status"], "queued")
        self.assertFalse(any(item["error"] == "deadline_exceeded" for item in snapshot["jobs"]))

    def test_review_inspection_preserves_exact_result_until_actual_verdict(self):
        room_id = self.plan()
        work = self.store.claim(room_id, "claude")
        self.store.finish_job(work["id"], artifact_revision=3, artifact_digest="a" * 64,
                              team_commands=[{"type": "complete", "task_id": "main", "result": "Updated app"}])
        review = self.store.claim(room_id, "codex")
        self.assertEqual(review["team"]["job_phase"], "review")
        self.inspect(review)
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["team"]["tasks"][0]["status"], "review")
        self.assertEqual(snapshot["team"]["tasks"][0]["result_version"], 1)
        resumed = self.store.claim(room_id, "codex")
        self.assertEqual(resumed["team"], review["team"])
        self.store.finish_job(resumed["id"], team_commands=[{
            "type": "review", "task_id": "main", "verdict": "accept", "reason": "Checked exact app result"}])
        self.assertEqual(self.store.snapshot(room_id)["team"]["tasks"][0]["status"], "accepted")

    def test_context_is_bounded_isolated_and_survives_reopen(self):
        room_id = self.room()
        for index in range(5):
            job = self.store.claim(room_id, "claude")
            self.store.finish_project_inspection(job["id"], observation={"result": {"index": index}})
        context = self.context(room_id)
        self.assertTrue(context["truncated"])
        self.assertEqual([item["observation"]["result"]["index"] for item in context["observations"]], [1, 2, 3, 4])
        self.assertEqual(self.context(room_id, provider="codex")["observations"], [])
        self.assertEqual(self.context(room_id, run_id="other")["observations"], [])
        self.assertEqual(self.context(room_id, task_id="other")["observations"], [])
        self.assertEqual(self.context(room_id, phase="review")["observations"], [])
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.context(room_id), context)

    def test_context_byte_cap_and_recoverable_read_error(self):
        room_id = self.room()
        for index in range(3):
            job = self.store.claim(room_id, "claude")
            self.store.finish_project_inspection(job["id"], observation={
                "request": {"path": "absent.py"},
                "result": {"error": "file_not_found", "detail": "x" * 20000, "index": index}})
        context = self.context(room_id)
        self.assertTrue(context["truncated"])
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=True).encode()), MAX_INSPECTION_CONTEXT_BYTES)
        self.assertEqual(context["observations"][-1]["observation"]["result"]["error"], "file_not_found")
        self.assertEqual(context["observations"][-1]["observation"]["result"]["index"], 2)

    def test_invalid_or_unbounded_observation_does_not_finish_call(self):
        room_id = self.room()
        job = self.store.claim(room_id, "claude")
        for observation in ([], {"result": float("nan")}, {"result": "x" * MAX_INSPECTION_CONTEXT_BYTES}):
            with self.assertRaises(RoomError):
                self.store.finish_project_inspection(job["id"], observation=observation)
        self.assertEqual(self.store.snapshot(room_id)["jobs"][0]["status"], "working")
        self.assertEqual(self.context(room_id)["observations"], [])

    def test_emoji_observations_use_actual_utf8_budget_and_resume(self):
        room_id = self.room()
        for index in range(4):
            job = self.store.claim(room_id, "claude")
            next_id = self.store.finish_project_inspection(job["id"], observation={
                "result": {"content": "😀" * 4096, "index": index}})
            self.assertIsNotNone(next_id)
            self.assertEqual(self.store.snapshot(room_id)["jobs"][-1]["status"], "queued")
        context = self.context(room_id)
        self.assertTrue(context["truncated"])
        self.assertEqual(len(context["observations"]), 2)
        self.assertEqual(context["observations"][-1]["observation"]["result"]["index"], 3)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode("utf-8")), 48 * 1024)

    def test_review_read_receipts_survive_recent_window_and_never_return_content(self):
        room_id = self.plan()
        work = self.store.claim(room_id, "claude")
        self.store.finish_job(work["id"], artifact_revision=3, artifact_digest="a" * 64,
                              team_commands=[{"type": "complete", "task_id": "main", "result": "Updated app"}])
        review = self.store.claim(room_id, "codex")
        for index in range(5):
            path = f"src/observed-{index}.py"
            self.store.finish_project_inspection(review["id"], observation={
                "request": {"kind": "read", "path": path, "offset": 0},
                "result": {"path": path, "sha256": "a" * 64, "revision": 3, "offset": 0,
                           "content": "😀 observed code", "truncated": False}})
            review = self.store.claim(room_id, "codex")
        run = review["team"]["run_id"]
        context = self.store.inspection_context(room_id, "codex", run, "main", "review")
        self.assertEqual(len(context["observations"]), 4)
        reads = self.store.inspection_reads(room_id, "codex", run, "main")
        self.assertEqual(len(reads), 5)
        self.assertEqual(reads[0]["path"], "src/observed-0.py")
        self.assertEqual(reads[0]["content_bytes"], len("😀 observed code".encode()))
        self.assertNotIn("content", reads[0])
        self.assertNotIn("observed code", json.dumps(reads))
        for provider, run_id, task_id, phase in (("claude", run, "main", "review"),
                ("codex", "other-run", "main", "review"), ("codex", run, "other-task", "review"),
                ("codex", run, "main", "plan")):
            self.assertEqual(self.store.inspection_reads(room_id, provider, run_id, task_id, phase), [])
        self.assertEqual(self.store.inspection_reads(self.room(), "codex", run, "main"), [])

    def test_read_receipts_keep_stale_bindings_explicit_and_reject_invalid_or_past_eof(self):
        room_id = self.room()
        result = {"path": "src/main.py", "sha256": "a" * 64, "revision": 2, "offset": 0,
                  "content": "old code", "truncated": False}
        requests = [({"kind": "read", "path": "src/main.py", "offset": 0}, result),
                    ({"kind": "read", "path": "src/main.py", "offset": 999},
                     {**result, "offset": 999, "content": ""}),
                    ({"kind": "read", "path": "src/main.py", "offset": 0}, {**result, "content": ""}),
                    ({"kind": "read", "path": "src/main.py", "offset": 0}, {**result, "error": "unavailable"}),
                    ({"kind": "search", "path": "src/main.py", "offset": 0}, result),
                    ({"kind": "read", "path": "different.py", "offset": 0}, result),
                    ({"kind": "read", "path": "src/main.py", "offset": 0}, {**result, "sha256": "bad"})]
        for query, value in requests:
            job = self.store.claim(room_id, "claude")
            self.store.finish_project_inspection(job["id"], observation={"request": query, "result": value})
        reads = self.store.inspection_reads(room_id, "claude", None, None, phase="task")
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0]["revision"], 2)
        self.assertNotEqual(reads[0]["revision"], 3, "A stale receipt must never stand in for revision 3")
        self.assertEqual(reads[1]["content_bytes"], 0, "Caller must additionally prove trusted file size is zero")
        self.assertTrue(all(item["offset"] == 0 for item in reads))

    def test_long_unicode_task_paths_are_folder_only(self):
        path = "/".join(["目录" * 30] * 4) + "/说明.txt"
        self.assertGreater(len(path), 240)
        proposal = {"type": "propose", "id": "long", "title": "Edit long path",
                    "description": "Update a discovered project file", "acceptance": "Peer reviews the result",
                    "dependencies": [], "write_paths": [path], "capability": "text"}
        room_id = self.room(collaboration=True)
        job = self.store.claim(room_id, "claude")
        self.assertTrue(self.store.prevalidate_team_result(job["id"], [proposal]))
        self.store.finish_job(job["id"], team_commands=[proposal, {"type": "claim", "task_id": "long"}])
        task = self.store.snapshot(room_id)["team"]["tasks"][0]
        self.assertEqual(task["write_paths"], [path])
        self.assertIsNone(task["owner"], "a planning claim reserves nothing; the task waits in the pool")
        legacy, _ = self.store.create_room("Legacy task", "claude", str(uuid4()), collaboration=True)
        legacy_job = self.store.claim(legacy, "claude")
        with self.assertRaises(RoomError) as failure:
            self.store.prevalidate_team_result(legacy_job["id"], [proposal])
        self.assertEqual(failure.exception.code, "invalid_team_task")


if __name__ == "__main__":
    unittest.main()
