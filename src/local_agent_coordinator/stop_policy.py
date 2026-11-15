"""Which stops of an autonomous objective wait for the user.

Anything the user's 继续 would fix is continued by the host. A stop that
comes back with no new progress is continued again after a growing pause; it
is never given up to the user. Only the explicit lists below wait for the
user; every other code, including ones added later, continues automatically.

The host does not judge the partners' progress or scope for them: repeated
identical results and a checkpoint that wants to keep going are theirs to
sort out, so neither is a user stop. A single call that ran out of time
(deadline_exceeded) is an ordinary failed call; the limits the user set stop
as objective_calls_exhausted and objective_deadline_exceeded.
"""
from .block_contract import USER_BLOCK_CAUSES

# The user chose to stop, or a limit the user set was reached, or a partner
# asked the user a question.
USER_STOPS = frozenset({
    "cancel_requested", "lifecycle_requested", "explicit_new_attempt",
    "objective_calls_exhausted", "objective_deadline_exceeded", "objective_stage_limit",
    "objective_decision_required", "objective_checkpoint_disagreed",
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

# Kept scenes are continued too, but never by reopening the board in place:
# only through the native restart that abandons the ended calls, accepts
# nothing from them and tells the next partners what is unknown (a process
# that may still run, a commit that may or may not have landed, edits nobody
# owns). They judge and deal with it; a stopped room nothing can restart
# helps nobody. A room without that restart keeps the scene for the user.
AUDITED_RECOVERY = KEEP_SCENE


def continues_automatically(code):
    return isinstance(code, str) and code not in USER_STOPS | KEEP_SCENE | OWN_RECOVERY


def continues_after_recovery_audit(code):
    return code in AUDITED_RECOVERY


def block_waits_for_user(task):
    """Whether a blocked task is the user's to resolve.

    A block a partner raised waits only when its cause asks for the user's
    decision or permission. A block without a cause is the host's own, added
    when a call failed: it waits only when that failure's stop would.
    """
    if task["block_cause"] is None:
        return not continues_automatically(task["blocked_reason"])
    return task["block_cause"] in USER_BLOCK_CAUSES


def host_block_recoverable(task):
    """A blocked task the host hands back without asking the user: its own
    block is reopened, a partner's goes back to the partners to replan."""
    return not block_waits_for_user(task)
