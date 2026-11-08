"""Which stops of an autonomous objective wait for the user.

Anything the user's 继续 would fix is continued by the host, once per state of
the board: a stop that repeats with no new progress is left for the user.
Only the two explicit lists below wait for the user; every other code,
including ones added later, continues automatically.
"""

# The user chose to stop, or a limit the user set was reached, or the
# objective itself asked a question.
USER_STOPS = frozenset({
    "cancel_requested", "lifecycle_requested", "explicit_new_attempt",
    "budget_exhausted", "deadline_exceeded", "objective_calls_exhausted",
    "objective_deadline_exceeded", "objective_stage_limit", "objective_no_progress",
    "objective_decision_required", "objective_scope_confirmation", "objective_checkpoint_disagreed",
    # The partner switched login source; its session stays in the other home.
    "partner_account_home_changed",
})

# The outcome itself is unknown or private work cannot be reconciled safely;
# continuing could hide a live process, a commit or unpublished edits.
KEEP_SCENE = frozenset({
    "project_commit_unconfirmed", "native_cleanup_unconfirmed", "team_cleanup_unconfirmed",
    "native_recovery_cleanup_unconfirmed", "session_identity_changed", "session_identity_unknown",
    "native_recovery_unpublished_changes", "native_side_effects_unconfirmed", "execution_unknown",
})

# Stops with their own waiting path; the generic continuation leaves them alone.
OWN_RECOVERY = frozenset({"all_partners_quota_exhausted"})


def continues_automatically(code):
    return isinstance(code, str) and code not in USER_STOPS | KEEP_SCENE | OWN_RECOVERY


def host_block_recoverable(task):
    """A task the host blocked on a call's failure, not a question the model asked."""
    return task["block_cause"] is None and continues_automatically(task["blocked_reason"])
