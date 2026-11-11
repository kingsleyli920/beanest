"""Continuous objective policy, separate from file/tool permissions.

Autonomous work is unlimited by default. Explicit autonomous limits remain
user choices; retired manual-mode defaults are historical observations only.
"""
from __future__ import annotations


class ExecutionPolicyError(ValueError):
    code = "invalid_execution_policy"


def normalized_policy(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or "mode" not in value
            or set(value) - {"mode", "max_seconds", "max_calls", "quota_failover"}
            or not isinstance(value["mode"], str)
            or value["mode"] not in {"manual", "autonomous"}):
        raise ExecutionPolicyError()
    autonomous = value["mode"] == "autonomous"
    seconds = value.get("max_seconds", None if autonomous else 7200)
    calls = value.get("max_calls", None if autonomous else 64)
    failover = value.get("quota_failover", True)
    if (not (seconds is None and autonomous or type(seconds) is int and seconds in {7200, 14400, 28800})
            or not (calls is None and autonomous or type(calls) is int and 1 <= calls <= 256)
            or type(failover) is not bool):
        raise ExecutionPolicyError()
    return {"mode": "autonomous", "max_seconds": seconds if autonomous else None,
            "max_calls": calls if autonomous else None, "quota_failover": failover}


def effective_policy(value):
    """Read current explicit limits without reviving retired manual defaults.

    Absence stays absent so a simple text request does not become an objective.
    A new value is returned without mutating the historical policy.
    """
    return normalized_policy(value)


def public_call_limit(stored_limit):
    """SQLite's existing NOT NULL column uses zero for an omitted call cap.

    This decoder preserves historical positive values. Active scheduling uses
    effective_policy instead of enforcing those historical caps.
    """
    return stored_limit if stored_limit else None


def calls_available(remaining, minimum=1):
    return remaining is None or remaining >= minimum


def stage_available(policy, stage):
    """Stage counts are observations, never a reason to stop useful work."""
    return True


def public_deadline(stored_deadline):
    """Zero stores an explicitly omitted autonomous deadline in legacy columns."""
    return stored_deadline if stored_deadline else None


def deadline_reached(deadline, now):
    return deadline is not None and deadline != 0 and now >= deadline


def seconds_available(deadline, now):
    return max(0, deadline - now) if deadline else None


DELIVERED = ("accepted", "self_checked")
# The user removed this task from the round. It ends the task for scheduling,
# but it is never a delivery: no files, preview, review count or acceptance.
WITHDRAWN = "withdrawn"


def task_finished(task):
    """Self-check can unblock work, but is never an independent acceptance."""
    return task["status"] in DELIVERED


def task_settled(task):
    """Nothing more will happen to this task in its round: delivered or withdrawn.

    Use this for "is the round done" and "may a dependent start"; use
    task_finished for anything that counts or ships a result.
    """
    return task["status"] in DELIVERED or task["status"] == WITHDRAWN
