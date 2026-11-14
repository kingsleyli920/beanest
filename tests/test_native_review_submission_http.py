"""Native review submission through real HTTP; synthetic transport, no models."""
import asyncio
import hashlib
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError
import test_long_team_result_http as http
import test_native_room_service as native
from test_team_store import proposal


class NativeReviewSubmissionHTTPTests(unittest.TestCase):
    request = http.LongTeamResultHTTPTests.request
    tearDown = http.LongTeamResultHTTPTests.tearDown
    create = http.LongTeamResultHTTPTests.create
    settled = http.LongTeamResultHTTPTests.settled

    def setUp(self):
        http.LongTeamResultHTTPTests.setUp(self)
        self.review_reason = "This is a submitted peer opinion, without a claim of file inspection."
        self.review_tools = 0
        self.four_tasks = self.legacy_host = self.model_block = self.hold_review = False
        self.long_checkpoint_reason = False
        self.checkpoint_reviews = 0
        self.work_counts = {task: 0 for task in ("T1", "T2", "T3", "T4")}
        self.review_started, self.review_release = threading.Event(), threading.Event()
        self.addCleanup(self.review_release.set)
        transport = self.native

        async def invoke(agent, prompt, cwd, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            review = context["job"]["job_phase"] == "review"
            if review:
                options = {**options, "on_activity": lambda event: None}
            result = await transport(agent, prompt, cwd, **options)
            if review:
                result["events"] = []
                count = self.review_tools
                if self.four_tasks and self.legacy_host:
                    count = (0 if context["job"]["format_attempt"] else 7) if context["task"]["id"] == "T3" else 1
                result["native_execution"].update(tool_calls=count, tool_results=count)
            return result

        self.service.native_invoke = invoke

    def handler(self, agent, context, cwd):
        if self.four_tasks:
            return self.four_task_handler(agent, context, cwd)
        if context["job"]["job_phase"] == "checkpoint_review" and self.long_checkpoint_reason:
            self.checkpoint_reviews += 1
            return native.native_reply(context, [{"type": "checkpoint_review",
                "verdict": "changes_requested" if self.checkpoint_reviews == 1 else "accept",
                "reason": self.review_reason,
                "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        if context["job"]["job_phase"] == "review":
            return native.native_reply(context, [{"type": "review", "task_id": context["task"]["id"],
                "verdict": "accept", "reason": self.review_reason}])
        return http.LongTeamResultHTTPTests.handler(self, agent, context, cwd)

    async def four_task_handler(self, agent, context, cwd):
        phase, task = context["job"]["job_phase"], context.get("task")
        if phase in {"plan", "coordinate"}:
            commands = []
            if not context["team"]["tasks"] and agent == context["team"]["plan_author"]:
                commands = [proposal("T1", paths=["results/t1.md"]), proposal("T2", paths=["results/t2.md"]),
                    proposal("T3", paths=["results/t3.md"], dependencies=["T1", "T2"]),
                    proposal("T4", paths=["results/t4.md"], dependencies=["T3"]),
                    {"type": "claim", "task_id": "T1"}, {"type": "claim", "task_id": "T3"}]
            elif context["team"]["tasks"] and agent == "codex":
                commands = [{"type": "claim", "task_id": key} for key in ("T2", "T4")
                            if next(item for item in context["team"]["tasks"] if item["id"] == key)["owner"] is None]
            return native.native_reply(context, [*commands, {"type": "plan_ready"}])
        if phase == "work":
            self.work_counts[task["id"]] += 1
            (cwd / "results").mkdir(exist_ok=True)
            (cwd / task["write_paths"][0]).write_text(f"Synthetic {task['id']} delivery.\n")
            return native.native_reply(context, [{"type": "complete", "task_id": task["id"],
                "result": f"The {task['id']} file is ready for ordinary peer review."}])
        if phase == "review":
            if task["id"] == "T3" and self.model_block:
                return native.native_reply(context, [{"type": "block", "task_id": "T3",
                    "reason": "native_review_unobserved", "cause": None}])
            if task["id"] == "T3" and self.hold_review and not self.legacy_host:
                self.review_started.set()
                while not self.review_release.is_set():
                    await asyncio.sleep(0.005)
            reason = self.review_reason if task["id"] == "T3" else "Ordinary peer opinion without an inspection claim."
            return native.native_reply(context, [{"type": "review", "task_id": task["id"],
                "verdict": "accept", "reason": reason}])
        return http.LongTeamResultHTTPTests.handler(self, agent, context, cwd)

    @staticmethod
    def continue_body(snapshot):
        current = snapshot["current_run"]
        return {"text": "继续", "target": "all", "intent": "continue", "request_id": str(uuid4()),
                "binding": {"run_id": current["id"], "revision": current["revision"], "digest": current["digest"]}}

    def job_rows(self, ids):
        placeholders = ",".join("?" for _ in ids)
        with self.service.store._connection() as db:
            return {table: [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}", ids)]
                for table, key in (("jobs", "id"), ("team_jobs", "job_id"), ("native_executions", "job_id"))}

    def assert_mixed_rejection_is_not_a_collateral_stop(self, failed):
        """Negative-only old-row reconstruction, rolled back after classification."""
        room_id, run_id = failed["room"]["id"], failed["current_run"]["id"]
        calls = len(self.native.calls)
        with self.service.store._connection(write=True) as db:
            source = self.service.store._team._host_text_failure_source(db, room_id, run_id)
            self.assertEqual(source["task_id"], "T3")  # A is the real zero-tool legacy host refusal.
            db.execute("SAVEPOINT second_independent_failure")
            try:
                # B starts from an actually completed T1 peer review. Model an
                # independently refused old review, rather than a sibling merely
                # stopped by A: its zero-tool record has a genuinely wrong S digest.
                review = db.execute("SELECT j.id FROM jobs j JOIN team_jobs t ON t.job_id=j.id "
                    "WHERE j.room_id=? AND t.run_id=? AND t.task_id='T1' AND t.phase='review'", (room_id, run_id)).fetchone()
                native_row = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (review[0],)).fetchone()
                metadata = json.loads(native_row[0])
                metadata.update(tool_calls=0, tool_results=0)
                metadata["binding"]["digest"] = "0" * 64
                db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(metadata), review[0]))
                db.execute("UPDATE jobs SET status='failed',error='native_review_unobserved' WHERE id=?", (review[0],))
                db.execute("UPDATE shared_tasks SET status='blocked',blocked_reason='native_review_unobserved',"
                           "review_reason=NULL WHERE room_id=? AND run_id=? AND id='T1'", (room_id, run_id))
                db.execute("UPDATE team_jobs SET result_version=0 WHERE job_id=?", (review[0],))
                stale_version = self.service.store._team._host_text_failure_source(db, room_id, run_id)
                self.assertEqual(stale_version["task_id"], "T3")
                db.execute("UPDATE team_jobs SET result_version=1,assignment_epoch=assignment_epoch+1 WHERE job_id=?", (review[0],))
                stale_epoch = self.service.store._team._host_text_failure_source(db, room_id, run_id)
                self.assertEqual(stale_epoch["task_id"], "T3")
                db.execute("UPDATE team_jobs SET assignment_epoch=assignment_epoch-1 WHERE job_id=?", (review[0],))
                self.assertIsNone(self.service.store._team._host_text_failure_source(db, room_id, run_id))
            finally:
                db.execute("ROLLBACK TO second_independent_failure")
                db.execute("RELEASE second_independent_failure")
        self.assertEqual(len(self.native.calls), calls)

    def legacy_failed_and_carried(self):
        """Only this temporary service reenacts the two retired host refusals."""
        self.four_tasks = self.legacy_host = True
        self.review_reason = ("A detailed ordinary review opinion. " * 100)[:3000]
        validate = self.service._validate_team_result

        def legacy_validation(job, parsed):
            if job["team"]["job_phase"] == "review" and job["team"]["task_id"] == "T3":
                raise RoomError("native_review_unobserved" if job["team"]["format_attempt"] else "invalid_text")
            return validate(job, parsed)

        with patch.object(self.service, "_validate_team_result", side_effect=legacy_validation):
            failed = self.settled(self.create()["room"]["id"])
        room_id = failed["room"]["id"]
        self.assertEqual(failed["team"]["stop_reason"], "native_review_unobserved")
        tasks = {task["id"]: task for task in failed["team"]["tasks"]}
        self.assertEqual([(tasks[key]["status"], tasks[key]["result_version"]) for key in ("T1", "T2")],
                         [("accepted", 1), ("accepted", 1)])
        self.assertEqual((tasks["T3"]["status"], tasks["T3"]["result_version"]), ("blocked", 1))
        failed_reviews = [job for job in failed["jobs"] if job["error"] in {"invalid_text", "native_review_unobserved"}]
        self.assertEqual([job["error"] for job in failed_reviews], ["invalid_text", "native_review_unobserved"])
        observations = {row["job_id"]: row for row in failed["native_executions"]}
        self.assertEqual([(observations[job["id"]]["tool_calls"], observations[job["id"]]["tool_results"])
                          for job in failed_reviews], [(7, 7), (0, 0)])
        calls = len(self.native.calls)
        # The deployed legacy host lacked this provenance bridge. Its genuine
        # public restart carried the board but could not dispatch any task.
        with patch.object(self.service.store._team, "_host_text_failure_source", return_value=None):
            code, response = self.request("POST", f"/api/rooms/{room_id}/messages", self.continue_body(failed))
            self.assertEqual(code, 200, {"error": response.get("error")})
            carried = self.settled(room_id)
        self.assertEqual(carried["objective"]["status"], "needs_user")
        self.assertEqual(carried["objective"]["budget"]["used"], 0)
        self.assertEqual(len(self.native.calls), calls)
        self.legacy_host = False
        code, current = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(code, 200, {"error": current.get("error")})
        return failed, current

    def test_completed_zero_tool_review_is_accepted(self):
        final = self.settled(self.create()["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"]["stop_reason"])
        self.assertEqual(final["team"]["tasks"][0]["status"], "accepted")
        self.assertEqual(final["team"]["tasks"][0]["review_reason"], self.review_reason)
        reviews = [row for row in final["native_executions"] if row["phase"] == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual((reviews[0]["tool_calls"], reviews[0]["tool_results"]), (0, 0))
        self.assertEqual(reviews[0]["status"], "completed")
        self.assertTrue(reviews[0]["cleanup_confirmed"])

    def test_long_review_reason_is_persisted_and_read_in_full_run(self):
        self.review_tools = 1
        self.long_checkpoint_reason = True
        self.review_reason = ("审核意见与尚未验证的边界。" * 300)[:3000]
        final = self.settled(self.create()["room"]["id"])
        room_id = final["room"]["id"]
        self.assertEqual(final["objective"]["status"], "complete", final["team"]["stop_reason"])
        self.assertEqual(final["team"]["tasks"][0]["review_reason"], self.review_reason)
        checkpoint_reason = next(event["feedback"]["reason"] for event in final["events"]
                                 if event["kind"] == "objective_checkpoint_revision_requested")
        self.assertEqual(checkpoint_reason, self.review_reason)
        self.assertEqual(self.checkpoint_reviews, 2)
        route = f"/api/rooms/{room_id}/runs?" + urlencode({"run_id": final["current_run"]["id"]})
        code, run = self.request("GET", route)
        self.assertEqual(code, 200, {"error": run.get("error")})
        reason = run["team"]["tasks"][0]["review_reason"]
        self.assertEqual(reason, self.review_reason)
        self.assertEqual((len(reason.encode()), hashlib.sha256(reason.encode()).hexdigest()),
                         (len(self.review_reason.encode()), hashlib.sha256(self.review_reason.encode()).hexdigest()))
        self.assertEqual((len(checkpoint_reason.encode()), hashlib.sha256(checkpoint_reason.encode()).hexdigest()),
                         (len(self.review_reason.encode()), hashlib.sha256(self.review_reason.encode()).hexdigest()))

    def test_continue_requeues_only_the_failed_review_without_replaying_accepted_work(self):
        failed, carried = self.legacy_failed_and_carried()
        room_id = carried["room"]["id"]
        self.assertTrue(carried["current_run"]["can_recover_native"] or
                        self.service.store.host_text_failure(room_id) is not None)
        old_ids = [job["id"] for job in carried["jobs"]]
        old_rows = self.job_rows(old_ids)
        old_calls = len(self.native.calls)
        confirmed_session = next(member["session_id"] for member in carried["members"] if member["id"] == "codex")
        self.assertIsNotNone(confirmed_session)
        self.hold_review = True
        try:
            code, response = self.request("POST", f"/api/rooms/{room_id}/messages", self.continue_body(carried))
            self.assertEqual(code, 200, {"error": response.get("error")})
            self.assertTrue(self.review_started.wait(3), "fresh T3 review did not start")
            calls = self.native.calls[old_calls:]
            self.assertEqual(len(calls), 1)
            first = calls[0]
            self.assertEqual((first["agent"], first["permission"], first["session_id"]), ("codex", "full_access", confirmed_session))
            self.assertEqual((first["context"]["job"]["job_phase"], first["context"]["task"]["id"]), ("review", "T3"))
            self.assertNotIn(first["context"]["job"]["id"], old_ids)
            self.assertEqual(first["context"]["job"]["format_attempt"], 0)
            self.assertEqual(first["context"]["task"]["result_version"], 1)
            held = self.service.store.snapshot(room_id)
            tasks = {task["id"]: task for task in held["team"]["tasks"]}
            self.assertEqual([(tasks[key]["status"], tasks[key]["result_version"]) for key in ("T1", "T2")],
                             [("accepted", 1), ("accepted", 1)])
            self.assertEqual(tasks["T4"]["dependencies"], ["T3"])
            self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 1, "T4": 0})
        finally:
            self.review_release.set()
        final = self.settled(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"]["stop_reason"])
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 1, "T4": 1})
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(self.job_rows(old_ids), old_rows)

    def test_changed_binding_and_a_model_block_never_use_the_review_recovery(self):
        failed, carried = self.legacy_failed_and_carried()
        self.assert_mixed_rejection_is_not_a_collateral_stop(failed)
        room_id = carried["room"]["id"]
        project = self.service._project(room_id)
        original = project.files_for_paths(["results/t3.md"])[0]
        project.apply("claude", "test-current-binding-drift", project.snapshot()["revision"], [{
            "path": original["path"], "previous_sha256": original["sha256"], "content": "A later real canonical change.\n"}])
        code, current = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(code, 200, {"error": current.get("error")})
        calls = len(self.native.calls)
        self.assertFalse(current["current_run"]["can_recover_native"])
        code, response = self.request("POST", f"/api/rooms/{room_id}/messages", self.continue_body(current))
        self.assertEqual(code, 409, {"error": response.get("error")})
        self.assertEqual(len(self.native.calls), calls)
        self.model_block = True
        blocked = self.settled(self.create()["room"]["id"])
        blocked_room = blocked["room"]["id"]
        # The real command/event already exists. Only model the historical NULL
        # column used before cause normalization; this never fabricates success.
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET block_cause=NULL WHERE room_id=? AND run_id=? AND id='T3'",
                       (blocked_room, blocked["current_run"]["id"]))
        code, blocked = self.request("GET", f"/api/rooms/{blocked_room}")
        self.assertEqual(code, 200, {"error": blocked.get("error")})
        self.assertIsNone(self.service.store.host_text_failure(blocked_room))
        task = next(task for task in blocked["team"]["tasks"] if task["id"] == "T3")
        self.assertEqual((task["blocked_reason"], task["block_cause"]), ("native_review_unobserved", None))
        self.assertTrue(any(event["kind"] == "team_command" and event.get("command") == "block"
                            for event in blocked["events"]))
        self.assertFalse(blocked["current_run"]["can_recover_native"])
        calls = len(self.native.calls)
        current = blocked["current_run"]
        code, response = self.request("POST", f"/api/rooms/{blocked_room}/recover-native", {
            "request_id": str(uuid4()), "run_id": current["id"], "revision": current["revision"], "digest": current["digest"]})
        # The review recovery does not apply to a block the model raised. The
        # room is still not locked: recover-native abandons the ended calls
        # and restarts, without retrying or accepting the old review.
        self.assertEqual(code, 200, {"error": response.get("error")})
        kinds = [event["kind"] for event in self.settled(blocked_room)["events"]]
        self.assertEqual(kinds.count("native_unconfirmed_calls_abandoned"), 1)
        self.assertEqual(kinds.count("native_review_retry_queued"), 0)


if __name__ == "__main__":
    unittest.main()
