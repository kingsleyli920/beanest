"""Facts shared by native execution, response submission and recovery gates.

A completed native call is evidence of execution, not acceptance of its reply.
Historical recovery codes remain separate from current response validation.
"""
NATIVE_METADATA_ERROR = "native_unchanged_reason_required"
NATIVE_RECOVERY_ERRORS = frozenset({
    "native_protocol_invalid", "native_protocol_unknown", "native_process_or_protocol_failure",
    "protocol_turn_incomplete", "invalid_response_json",
})
# Mistakes in the reply itself: a command for a task, phase or role this call
# does not hold, a proposal outside planning, a wrong help target, files from
# a non-work call, JSON file bodies from a native call, an unavailable
# capability, a proposal writing another task's path without depending on it.
# Nothing was applied, so the caller corrects its reply instead of the whole
# objective stopping. State conflicts (reassignment, changed results, scope
# trespass) stay terminal.
REPLY_MISTAKES = frozenset({
    "team_task_permission", "team_plan_locked", "invalid_target", "team_files_forbidden",
    "team_capability_unavailable", "native_json_files_forbidden", "team_write_path_conflict",
})
FORMAT_ERRORS = frozenset({
    "invalid_agent_response", "invalid_team_commands", "invalid_team_task",
    "invalid_team_dependencies", "invalid_project_files", "invalid_text", "team_task_not_found",
    "team_result_missing", "message_too_large", NATIVE_METADATA_ERROR,
}) | REPLY_MISTAKES


def completed_execution(observation):
    """Require native terminal, process exit, tool closure and process cleanup."""
    return (isinstance(observation, dict) and observation.get("status") == "completed"
            and observation.get("cleanup_confirmed") is True
            and type(observation.get("exit_code")) is int and observation["exit_code"] == 0
            and all(type(observation.get(key)) is int and observation[key] >= 0
                    for key in ("tool_calls", "tool_results"))
            and observation["tool_calls"] == observation["tool_results"])
