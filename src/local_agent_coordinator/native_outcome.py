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


# A collaborating partner outside a native project only replies: nothing is
# applied until its reply is parsed. If its process dies first, the call changed
# nothing and can simply be made again. A plain chat has no board to resume, so
# its failed call stays visible for the user to judge.
CALL_DIED = frozenset({"cli_nonzero_exit", "protocol_turn_incomplete",
                       "native_protocol_invalid", "native_process_or_protocol_failure"})


def reply_call_died(job, result):
    """A reply-only call whose process ended without a reply and ran no tool."""
    if not job.get("team") or job.get("mode") == "native_project" or not isinstance(result, dict):
        return False
    events = result.get("events")
    return (result.get("status") != "completed" and result.get("failure_code") in CALL_DIED
            and not (isinstance(events, list) and any(
                isinstance(event, dict) and event.get("type") == "tool_observed" for event in events)))


# A call that ended before its prompt could reach the model. The partner is
# retried, then leaves the objective; nothing here is an unknown outcome.
NATIVE_PREFLIGHT_FAILED = "native_preflight_failed"
_IDENTITY_OR_QUOTA_FACTS = frozenset({
    "native_session_identity_mismatch", "subscription_limit_reached", "native_cleanup_unconfirmed",
})


def prompt_never_sent(observation):
    """The adapter's own receipt: no prompt left the host and nothing is running.

    Kimi's ACP reports `prompt_sent` from a flag set just before the send; the
    CLI adapters report `never_started` before spawning a model. Anything
    weaker keeps the conservative path, which must never replay real work.
    """
    if (not isinstance(observation, dict) or observation.get("cleanup_confirmed") is not True
            or observation.get("side_effects_possible") is not False
            or type(observation.get("tool_calls")) is not int or observation["tool_calls"] != 0
            or type(observation.get("tool_results")) is not int or observation["tool_results"] != 0):
        return False
    diagnostic = observation.get("failure_diagnostic")
    return observation.get("never_started") is True or (isinstance(diagnostic, dict) and diagnostic.get("prompt_sent") is False)


def preflight_failed(session_id, *, result=None, error=None):
    """Is this finished call a retryable failure that never reached the model?"""
    if error is not None:
        return prompt_never_sent(getattr(error, "native_execution", None))
    if (not isinstance(result, dict) or result.get("status") == "completed"
            or result.get("failure_code") in _IDENTITY_OR_QUOTA_FACTS
            or result.get("cleanup_confirmed") is False
            # A returned identity other than the one this call resumed is a
            # real identity question, even when the prompt never left.
            or session_id is not None and result.get("session_id") not in {None, session_id}):
        return False
    return prompt_never_sent(result.get("native_execution"))


def completed_execution(observation):
    """Require native terminal, process exit, tool closure and process cleanup."""
    return (isinstance(observation, dict) and observation.get("status") == "completed"
            and observation.get("cleanup_confirmed") is True
            and type(observation.get("exit_code")) is int and observation["exit_code"] == 0
            and all(type(observation.get(key)) is int and observation[key] >= 0
                    for key in ("tool_calls", "tool_results"))
            and observation["tool_calls"] == observation["tool_results"])
