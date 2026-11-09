"""How much each account has done in a round, in one unit every provider reports into.

Scheduling sees only accounts, shares and points. Everything specific to a
provider is one line in SCALES, so adding a provider means adding that line.
"""
from __future__ import annotations

from dataclasses import dataclass

LEVELS = {"low": 1, "normal": 2, "high": 3}
DEFAULT_LEVEL = "normal"


@dataclass(frozen=True)
class UsageScale:
    """What one reported token is worth, and how this provider counts them."""
    input: float = 1.0
    output: float = 5.0
    cache_read: float = 0.1
    cache_write: float = 1.25
    cached_input_is_part_of_input: bool = False  # the input count already includes cached tokens
    cumulative_per_session: bool = False         # each report is the session's running total


DEFAULT_SCALE = UsageScale()
SCALES = {
    "claude": DEFAULT_SCALE,
    "codex": UsageScale(cached_input_is_part_of_input=True, cumulative_per_session=True),
    "kimi": DEFAULT_SCALE,
}
_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _counts(usage):
    values = usage.get("values") if isinstance(usage, dict) else None
    if not isinstance(values, dict) or not any(type(values.get(field)) is int for field in _FIELDS):
        return None
    return {field: values.get(field) if type(values.get(field)) is int and values[field] >= 0 else 0 for field in _FIELDS}


def usage_points(adapter, usage, *, previous=None, resumed=False):
    """Points this call cost, or None when it cannot be told from what was reported."""
    counts = _counts(usage)
    if counts is None:
        return None
    scale = SCALES.get(adapter, DEFAULT_SCALE)
    if scale.cumulative_per_session and resumed:
        before = _counts(previous)
        if before is None or any(counts[field] < before[field] for field in _FIELDS):
            return None  # a resumed running total without its starting point says nothing about this call
        counts = {field: counts[field] - before[field] for field in _FIELDS}
    cached = counts["cached_input_tokens"]
    fresh = max(counts["input_tokens"] - cached, 0) if scale.cached_input_is_part_of_input else counts["input_tokens"]
    return (fresh * scale.input + counts["output_tokens"] * scale.output
            + (cached + counts["cache_read_input_tokens"]) * scale.cache_read
            + counts["cache_creation_input_tokens"] * scale.cache_write)


def accounts(members, loads, profile):
    """One row per account, however many partners share it.

    A call that reported nothing counts as an average call of this round, so a
    provider without usage data still takes its turn instead of looking free.
    """
    reported_calls = sum(load["reported"] for load in loads.values())
    reported_points = sum(load["points"] for load in loads.values())
    average = reported_points / reported_calls if reported_calls else 1.0
    rows = {}
    for member in members:
        scope, level = profile(member)
        share = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
        row = rows.setdefault(scope, {"scope": scope, "members": [], "share": 0, "level": level, "points": 0.0, "calls": 0})
        row["members"].append(member)
        if share > row["share"]:
            row["share"], row["level"] = share, level
        load = loads.get(member)
        if load:
            row["points"] += load["points"] + (load["calls"] - load["reported"]) * average
            row["calls"] += load["calls"]
    return list(rows.values())


def fair_order(candidates, members, loads, profile):
    """Candidates, least used for their share first.

    Equal shares keep the given order, so a room nobody configured behaves as before.
    """
    by_member = {member: row for row in accounts(members, loads, profile) for member in row["members"]}
    if len({by_member[member]["share"] for member in candidates}) <= 1:
        return list(candidates)
    return sorted(candidates, key=lambda member: (by_member[member]["points"] / by_member[member]["share"], -by_member[member]["share"]))


def summary(members, loads, profile):
    """What the room shows: each account's actual and intended part of this round."""
    rows = accounts(members, loads, profile)
    if len(rows) < 2:
        return None
    points, shares = sum(row["points"] for row in rows), sum(row["share"] for row in rows)
    return {"configured": len({row["share"] for row in rows}) > 1, "accounts": [
        {**row, "points": round(row["points"]), "target_percent": round(row["share"] * 100 / shares),
         "actual_percent": round(row["points"] * 100 / points) if points else None} for row in rows]}
