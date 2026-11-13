"""Delivery projection must distinguish recorded work from final acceptance."""
import copy
import unittest

from local_agent_coordinator.collaboration_view import collaboration_view


FILE_HASH = "a" * 64
SNAPSHOT_HASH = "b" * 64


def snapshot(*, mode="python_artifacts", status="accepted", phase="ready_for_user"):
    return {
        "room": {"mode": mode, "status": "ready_for_user" if phase == "ready_for_user" else "running"},
        "project": {"available": True, "revision": 3, "snapshot_sha256": SNAPSHOT_HASH,
                    "files": [{"path": "result.py", "sha256": FILE_HASH}]},
        "jobs": [], "executions": [],
        "team": {"phase": phase, "stop_reason": None, "tasks": [
            {"id": "result", "title": "Deliver result", "owner": "claude", "status": status,
             "dependencies": [], "result": "A concrete file was submitted.",
             "result_files": [{"path": "result.py", "sha256": FILE_HASH}]}]},
    }


def successful_execution():
    return {"revision": 3, "snapshot_sha256": SNAPSHOT_HASH, "status": "completed",
            "result": {"status": "completed", "exit_code": 0, "cleanup_confirmed": True}}


class CollaborationViewTests(unittest.TestCase):
    def test_execution_repair_remains_in_progress_with_failed_evidence(self):
        value = snapshot(mode="python_sandbox", status="working", phase="working")
        value["team"]["execution_repair_id"] = "failed-execution"
        value["executions"] = [{**successful_execution(), "status": "failed",
            "result": {"status": "failed", "exit_code": 1, "cleanup_confirmed": True}}]
        outcome = collaboration_view(value)["outcome"]
        self.assertEqual(outcome["state"], "in_progress")
        self.assertIn("重新互审", outcome["reason"])
        self.assertNotEqual(outcome["validation"]["scope"], "sandbox_execution")

    def test_previous_run_execution_cannot_certify_the_current_run(self):
        value = snapshot(mode="python_sandbox")
        value["current_run"] = {"id": "run-current", "accepted_at": None}
        value["executions"] = [{**successful_execution(), "team_run_id": "run-previous"}]
        outcome = collaboration_view(value)["outcome"]
        self.assertEqual(outcome["state"], "needs_user")
        self.assertNotEqual(outcome["validation"]["scope"], "sandbox_execution")
        value["executions"][0]["team_run_id"] = "run-current"
        self.assertEqual(collaboration_view(value)["outcome"]["state"], "ready_for_user")

    def test_explicit_acceptance_is_distinct_from_peer_review(self):
        value = snapshot()
        value["current_run"] = {"id": "current", "accepted_at": 123}
        outcome = collaboration_view(value)["outcome"]
        self.assertEqual(outcome["state"], "accepted")
        self.assertIn("你已接受", outcome["reason"])
        value["current_run"]["can_continue"] = False
        self.assertIn("轮数上限", collaboration_view(value)["outcome"]["next_action"])

    def test_legacy_completed_round_never_becomes_goal_acceptance(self):
        legacy = {"room": {"mode": "text", "status": "completed"},
                  "jobs": [{"kind": "review", "status": "completed"}], "executions": []}
        outcome = collaboration_view(legacy)["outcome"]
        self.assertEqual(outcome["state"], "round_finished")
        self.assertIn("尚未记录验收", outcome["reason"])
        self.assertEqual(outcome["validation"]["scope"], "static_review")

    def test_accepted_team_results_are_ready_for_user_acceptance_not_verified_goal(self):
        result = collaboration_view(snapshot())
        self.assertEqual(result["outcome"]["state"], "ready_for_user")
        self.assertIn("等待你验收", result["outcome"]["reason"])
        self.assertEqual(result["outcome"]["validation"]["scope"], "static_review")
        self.assertEqual(result["outcome"]["validation"]["revision"], None)
        self.assertEqual(result["outcome"]["remaining"], [])
        self.assertEqual(collaboration_view(snapshot(), active=True)["outcome"]["state"], "in_progress")

    def test_only_exact_current_revision_and_digest_can_expose_sandbox_execution(self):
        current = snapshot(mode="python_sandbox")
        current["executions"] = [successful_execution()]
        outcome = collaboration_view(current)["outcome"]
        self.assertEqual(outcome["state"], "ready_for_user")
        self.assertEqual(outcome["validation"]["scope"], "sandbox_execution")
        self.assertEqual(outcome["validation"]["revision"], 3)
        self.assertIn("程序输出仍需核对", outcome["validation"]["summary"])
        for field, value in (("revision", 2), ("snapshot_sha256", "c" * 64)):
            with self.subTest(field=field):
                mismatched = copy.deepcopy(current)
                mismatched["executions"][0][field] = value
                outcome = collaboration_view(mismatched)["outcome"]
                self.assertNotEqual(outcome["validation"]["scope"], "sandbox_execution")
                self.assertEqual(outcome["state"], "needs_user")

    def test_fake_success_exit_boolean_failed_status_and_unconfirmed_cleanup_do_not_pass(self):
        mutations = [
            ("execution", "status", "unknown"),
            ("result", "status", "failed"),
            ("result", "exit_code", False),
            ("result", "exit_code", True),
            ("result", "exit_code", 1),
            ("result", "exit_code", "0"),
            ("result", "cleanup_confirmed", False),
            ("result", "cleanup_confirmed", None),
        ]
        for level, field, value in mutations:
            with self.subTest(level=level, field=field, value=value):
                current = snapshot(mode="python_sandbox")
                execution = successful_execution()
                target = execution if level == "execution" else execution["result"]
                target[field] = value
                current["executions"] = [execution]
                outcome = collaboration_view(current)["outcome"]
                self.assertEqual(outcome["state"], "needs_user")
                self.assertNotEqual(outcome["validation"]["scope"], "sandbox_execution")
                self.assertIsNone(outcome["validation"]["revision"])

    def test_accepted_file_changed_or_removed_after_review_requires_user(self):
        for files in ([{"path": "result.py", "sha256": "d" * 64}], []):
            with self.subTest(files=files):
                current = snapshot()
                current["project"]["files"] = files
                outcome = collaboration_view(current)["outcome"]
                self.assertEqual(outcome["state"], "needs_user")
                self.assertTrue(any("后来发生变化" in reason for reason in outcome["remaining"]))

    def test_empty_job_queue_with_undelivered_or_dependency_waiting_tasks_is_not_ready(self):
        for status in ("open", "assigned", "working", "review", "waiting_peer"):
            with self.subTest(status=status):
                current = snapshot(status=status, phase="working")
                bound = current["team"]["tasks"][0]
                bound.update(result=None, result_files=None, dependencies=["prerequisite"], help_to="codex")
                projected = collaboration_view(current)
                self.assertNotEqual(projected["outcome"]["state"], "ready_for_user")
                self.assertTrue(projected["outcome"]["remaining"])
                self.assertEqual(projected["outcome"]["validation"]["scope"], "not_run")

    def test_stale_ready_phase_cannot_override_an_unfinished_task(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                current = snapshot(status="review")
                current["team"]["tasks"][0].update(result=None, result_files=None)
                if empty:
                    current["team"]["tasks"] = []
                projected = collaboration_view(current)
                self.assertNotEqual(projected["outcome"]["state"], "ready_for_user")
                if not empty:
                    self.assertTrue(projected["outcome"]["remaining"])

    def test_a_task_waiting_for_the_team_to_re_split_files_does_not_ask_the_user(self):
        # Catches the real room where Codex blocked R3 for coordination, the room replanned by itself,
        # yet the headline told the user that information or a decision was needed from them.
        value = snapshot(mode="native_project", status="working", phase="working")
        value["team"]["tasks"].append({"id": "R3", "title": "Fix the wait budget", "owner": "codex", "status": "blocked",
                                       "dependencies": [], "result": None, "result_files": None,
                                       "blocked_reason": "Needs ownership of two more files.",
                                       "block_cause": "coordination_required", "internal_replan_revision": -1})
        outcome = collaboration_view(value)["outcome"]
        self.assertEqual(outcome["state"], "in_progress")
        self.assertEqual(outcome["reason"], "Fix the wait budget：需要调整文件分工。手头的工作做完后，伙伴会重新分配，不用你操作。")
        self.assertIn("Fix the wait budget：等伙伴重新分配文件后继续", outcome["remaining"])
        # A block for the user, or a coordination block nobody has asked to replan, still asks the user.
        for cause, replan in (("user_decision", -1), ("coordination_required", None)):
            with self.subTest(cause=cause, replan=replan):
                value["team"]["tasks"][1].update(block_cause=cause, internal_replan_revision=replan)
                self.assertEqual(collaboration_view(value)["outcome"]["state"], "needs_user")

    def test_a_file_reworked_by_a_later_task_is_not_a_changed_delivery(self):
        # Catches the real room that said work needed the user while running: a repair task changed a file
        # an earlier accepted task had delivered, and the earlier delivery was reported as changed afterwards.
        value = snapshot(mode="native_project", status="accepted", phase="working")
        value["project"]["files"] = [{"path": "result.py", "sha256": "b" * 64}]
        value["team"]["tasks"].append({"id": "repair", "title": "Repair result", "owner": "codex", "status": "review",
                                       "dependencies": [], "write_paths": ["result.py"], "result": "Reworked.",
                                       "result_files": [{"path": "result.py", "sha256": "b" * 64}]})
        outcome = collaboration_view(value)["outcome"]
        self.assertEqual(outcome["state"], "in_progress")
        self.assertFalse(any("交付文件后来发生变化" in line for line in outcome["remaining"]))
        # Once the repair is accepted its version is the delivery, and it matches.
        value["team"]["tasks"][1]["status"] = "accepted"
        self.assertFalse(any("交付文件后来发生变化" in line for line in collaboration_view(value)["outcome"]["remaining"]))
        # The latest delivery changed afterwards with nobody holding the file: that still needs a look.
        value["project"]["files"] = [{"path": "result.py", "sha256": "c" * 64}]
        outcome = collaboration_view(value)["outcome"]
        self.assertEqual(outcome["remaining"], ["Repair result：交付文件后来发生变化，需要重新核对。"])

    def test_stop_and_unknown_preserve_actionable_subscription_failure_reason(self):
        for phase, expected in (("stopped", "stopped"), ("unknown", "unknown")):
            with self.subTest(phase=phase):
                current = snapshot(status="blocked", phase=phase)
                current["team"]["stop_reason"] = "subscription_limit_reached"
                current["team"]["tasks"][0]["blocked_reason"] = "subscription_limit_reached"
                outcome = collaboration_view(current)["outcome"]
                self.assertEqual(outcome["state"], expected)
                self.assertIn("订阅额度已用完", outcome["reason"])
                self.assertTrue(any("订阅额度已用完" in reason for reason in outcome["remaining"]))
                self.assertIn("不会自动重跑", outcome["next_action"])


if __name__ == "__main__":
    unittest.main()
