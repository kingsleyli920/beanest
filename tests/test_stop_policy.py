"""Every stop code the source can produce is classified on purpose."""
from pathlib import Path
import re
import unittest

from local_agent_coordinator import native_outcome, room_service, stop_policy

SOURCE = Path(__file__).resolve().parent.parent / "src" / "local_agent_coordinator"

# Hand-written expectations. A new stop code fails the test until it is added
# here, so nobody adds a way to stop without deciding whether the user must act.
EXPECTED = {
    "user": {"budget_exhausted", "cancel_requested", "deadline_exceeded", "explicit_new_attempt", "lifecycle_requested",
             "objective_calls_exhausted", "objective_deadline_exceeded", "objective_no_progress"},
    "keep": {"execution_unknown", "native_cleanup_unconfirmed", "native_recovery_unpublished_changes",
             "native_side_effects_unconfirmed", "team_cleanup_unconfirmed"},
    "own": {"all_partners_quota_exhausted"},
    "auto": {"cli_nonzero_exit", "empty_response", "execution_failed", "execution_repair_no_change",
             "execution_version_stale", "invalid_agent_response", "invalid_project_files", "invalid_response_json",
             "invalid_target", "invalid_team_commands", "invalid_team_dependencies", "invalid_team_task", "invalid_text",
             "message_too_large", "native_auth_unconfirmed", "native_json_files_forbidden", "native_no_progress", "native_preflight_failed",
             "native_permission_required", "native_process_or_protocol_failure", "native_progress_unconfirmed",
             "native_protocol_invalid", "native_protocol_unknown", "native_provider_error", "native_tool_unpaired",
             "native_turn_incomplete", "native_turn_unconfirmed", "native_unchanged_reason_required", "output_limit",
             "process_or_output_failure", "protocol_turn_incomplete", "service_restarted", "service_shutdown",
             "subscription_limit_reached", "team_capability_unavailable", "team_files_forbidden", "team_plan_locked",
             "team_plan_missing", "team_plan_stalled", "team_result_missing", "team_task_not_found",
             "team_task_permission", "team_write_path_conflict", "unexpected_tool_event", "unknown_provider_error"},
}


def classify(code):
    if code in stop_policy.USER_STOPS:
        return "user"
    if code in stop_policy.KEEP_SCENE:
        return "keep"
    if code in stop_policy.OWN_RECOVERY:
        return "own"
    return "auto" if stop_policy.continues_automatically(code) else "unclassified"


def produced_codes():
    text = "\n".join(path.read_text() for path in SOURCE.glob("*.py"))
    literal = set(re.findall(r'\bstop\(db, [a-z_]+, "([a-z_]+)"', text))
    literal |= set(re.findall(r'_stop\([^)]*code="([a-z_]+)"', text))
    return literal | room_service._FAILURES | native_outcome.NATIVE_RECOVERY_ERRORS | native_outcome.FORMAT_ERRORS


class StopPolicyTests(unittest.TestCase):
    def test_every_stop_code_in_the_source_has_a_deliberate_class(self):
        # Catches: a new stop path added without deciding who resolves it.
        expected = {code: kind for kind, codes in EXPECTED.items() for code in codes}
        unlisted = sorted(produced_codes() - set(expected))
        self.assertEqual(unlisted, [], "Add these codes to EXPECTED with the class you intend.")
        self.assertEqual({code: classify(code) for code in expected}, expected)

    def test_a_model_question_is_never_reopened_as_a_host_block(self):
        # Catches: treating the model's own block (with a cause) as recoverable host noise.
        for cause in ("user_decision", "permission_required", "external_dependency", "technical", "unclassified"):
            self.assertFalse(stop_policy.host_block_recoverable({"block_cause": cause, "blocked_reason": "cli_nonzero_exit"}))
        self.assertTrue(stop_policy.host_block_recoverable({"block_cause": None, "blocked_reason": "invalid_team_commands"}))
        self.assertFalse(stop_policy.host_block_recoverable({"block_cause": None, "blocked_reason": "native_cleanup_unconfirmed"}))

    def test_an_audited_kept_scene_never_reopens_a_board_in_place(self):
        # Catches: a session of unknown identity resumed by the generic continue or
        # an in-place block reopen, instead of only the audited fresh-session recovery.
        self.assertEqual(stop_policy.AUDITED_RECOVERY, {"session_identity_unknown"})
        for code in stop_policy.AUDITED_RECOVERY:
            self.assertIn(code, stop_policy.KEEP_SCENE)
            self.assertFalse(stop_policy.continues_automatically(code))
            self.assertTrue(stop_policy.continues_after_recovery_audit(code))
            self.assertFalse(stop_policy.host_block_recoverable({"block_cause": None, "blocked_reason": code}))
            self.assertEqual(room_service.RoomService._auto_continue_actions(code), {"recover-native"})
        self.assertFalse(stop_policy.continues_after_recovery_audit("native_side_effects_unconfirmed"))


class AutoContinueGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_quitting_the_app_is_continued_but_the_users_own_stop_is_not(self):
        # Catches: a room left cancelled by quitting the app never resuming, and
        # the same phase from the user's 停止 being resumed against their wish.
        import tempfile
        from local_agent_coordinator.room_service import RoomService
        with tempfile.TemporaryDirectory() as root:
            service = RoomService(Path(root) / "rooms.sqlite3", Path(root) / "workspaces", invoke=lambda *a, **k: None)
            try:
                def snapshot(code):
                    return {"room": {"lifecycle_state": "active"}, "jobs": [], "executions": [],
                            "objective": {"status": "stopped", "execution_policy": {"mode": "autonomous"}},
                            "team": {"phase": "cancelled", "stop_reason": code}, "current_run": {"id": "run"}}
                service._schedule_auto_continue("quit", snapshot("service_shutdown"))
                service._schedule_auto_continue("user", snapshot("cancel_requested"))
                self.assertEqual(service._auto_continues["quit"]["key"], ("run", "service_shutdown"))
                self.assertNotIn("user", service._auto_continues)
            finally:
                await service.shutdown()


if __name__ == "__main__":
    unittest.main()
