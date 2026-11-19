"""How much each account has done in a round, in one unit every provider reports into.

Scheduling sees only accounts, shares and points. Everything specific to a
provider is one line in SCALES, so adding a provider means adding that line.
"""
from __future__ import annotations

from dataclasses import dataclass

LEVELS = {"low": 1, "normal": 2, "high": 3}
DEFAULT_LEVEL = "normal"
IDLE = "idle"  # takes no share of the work being handed out (its role does not build)
# Who decided a member's part of the round: the user (a level, or a ceiling that
# is holding it down), the partners (their latest allocate), or nobody.
USER, PARTNERS, DEFAULT = "user", "partners", "default"
MAX_PERCENT = 100


def user_limit(value):
    """A member's workload setting as ("level", name), ("percent", n) or None."""
    if type(value) is int and 1 <= value <= MAX_PERCENT:
        return ("percent", value)
    return ("level", value) if isinstance(value, str) and value in LEVELS else None


def _kind(limits, member):
    return limits[member][0] if limits.get(member) else None


def level_percents(members, limits):
    """What each level the user fixed is worth, in percent of the round.

    A level keeps its old meaning: 1, 2 or 3 parts, where everyone the user
    left alone counts as 2.
    """
    units = {member: LEVELS[limits[member][1]] if _kind(limits, member) == "level" else LEVELS[DEFAULT_LEVEL]
             for member in members}
    total = sum(units.values())
    return {member: units[member] * 100 / total for member in members if _kind(limits, member) == "level"}


def clamp_allocation(members, limits, shares):
    """The partners' shares with the user's settings applied, and what that changed.

    A level the user chose is fixed, so the member gets exactly that part. A
    percent is a ceiling: less is the partners' call, more is cut down to it.
    """
    fixed = level_percents(members, limits)
    result, limited = {}, []
    for member, asked in shares.items():
        kind, applied = _kind(limits, member), asked
        if kind == "level":
            applied = round(fixed[member])
        elif kind == "percent" and asked > limits[member][1]:
            applied = limits[member][1]
        result[member] = applied
        if applied != asked:
            limited.append({"member": member, "asked": asked, "applied": applied,
                            "by": "user_level" if kind == "level" else "user_percent"})
    return result, limited


def targets(members, limits=None, allocation=None):
    """Each member's intended part of the round: {member: (share, source)}.

    Without a ceiling or an allocation the shares are the plain level parts, so
    a room nobody touched hands work out exactly as before. Otherwise shares
    are percents: fixed levels first, the rest split by the partners'
    allocation (evenly without one), no member above the user's ceiling.
    """
    members, limits = list(members), limits or {}
    if not members:
        # Nobody is available: every call ended unknown or the partners retired. There is no round
        # to split, and the room must still be readable so the user can see that and continue.
        return {}
    ceilings = {member: limits[member][1] for member in members if _kind(limits, member) == "percent"}
    fixed = level_percents(members, limits)
    if not allocation and not ceilings:
        return {member: (LEVELS[limits[member][1]], USER) if member in fixed else (LEVELS[DEFAULT_LEVEL], DEFAULT)
                for member in members}
    even = MAX_PERCENT / len(members)
    raw = {member: float(allocation[member]) if allocation and member in allocation else even
           for member in members if member not in fixed}
    result = {member: (percent, USER) for member, percent in fixed.items()}
    rest, open_members = MAX_PERCENT - sum(fixed.values()), set(raw)
    while open_members:
        total = sum(raw[member] for member in open_members)
        if total <= 0:
            result.update({member: (0.0, PARTNERS) for member in open_members})
            break
        over = [member for member in open_members if member in ceilings and rest * raw[member] / total > ceilings[member]]
        if not over:
            result.update({member: (rest * raw[member] / total, PARTNERS if allocation and member in allocation else DEFAULT)
                           for member in open_members})
            break
        for member in over:
            result[member] = (float(ceilings[member]), USER)
            rest -= ceilings[member]
            open_members.discard(member)
    if not any(share for share, _ in result.values()):
        return {member: (LEVELS[DEFAULT_LEVEL], DEFAULT) for member in members}
    # Rounded so shares that are equal on paper compare equal.
    return {member: (round(share, 6), source) for member, (share, source) in result.items()}


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
    # dsh's session log reports each model call: uncached input, output and cache reads apart.
    "dsh": DEFAULT_SCALE,
}
_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _counts(usage):
    values = usage.get("values") if isinstance(usage, dict) else None
    if not isinstance(values, dict) or not any(type(values.get(field)) is int for field in _FIELDS):
        return None
    return {field: values.get(field) if type(values.get(field)) is int and values[field] >= 0 else 0 for field in _FIELDS}


def token_counts(adapter, usage, *, previous=None, resumed=False):
    """This call's fresh input, output, cache-read and cache-write tokens, or None.

    One place knows how each tool reports tokens, so workload points and an
    API partner's spend count the same call the same way.
    """
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
    return {"input": fresh, "output": counts["output_tokens"],
            "cache_read": cached + counts["cache_read_input_tokens"], "cache_write": counts["cache_creation_input_tokens"]}


def usage_points(adapter, usage, *, previous=None, resumed=False):
    """Points this call cost, or None when it cannot be told from what was reported."""
    counts = token_counts(adapter, usage, previous=previous, resumed=resumed)
    if counts is None:
        return None
    scale = SCALES.get(adapter, DEFAULT_SCALE)
    return (counts["input"] * scale.input + counts["output"] * scale.output
            + counts["cache_read"] * scale.cache_read + counts["cache_write"] * scale.cache_write)


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
        # A profile gives (account, level) or, when shares were worked out, (account, level, share, source).
        entry = profile(member)
        scope, level = entry[0], entry[1]
        share = 0 if level == IDLE else entry[2] if len(entry) > 2 else LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
        source = entry[3] if len(entry) > 3 else DEFAULT
        row = rows.setdefault(scope, {"scope": scope, "members": [], "share": 0, "level": level, "source": source,
                                      "points": 0.0, "calls": 0})
        row["members"].append(member)
        if share > row["share"]:
            row["share"], row["level"], row["source"] = share, level, source
        load = loads.get(member)
        if load:
            row["points"] += load["points"] + (load["calls"] - load["reported"]) * average
            row["calls"] += load["calls"]
    return list(rows.values())


def fair_order(candidates, members, loads, profile, *, when_equal=None):
    """Candidates, least used for their share first.

    Equal shares keep the given order, so a room nobody configured behaves as
    before. when_equal, a key per member, orders them in that case instead
    (ties keep the given order): the task pool passes how many tasks each has
    taken, so new work reaches a free member who has had less of it.
    """
    by_member = {member: row for row in accounts(members, loads, profile) for member in row["members"]}
    if len({row["share"] for row in by_member.values()}) <= 1:
        return sorted(candidates, key=when_equal) if when_equal else list(candidates)
    # An account with no share goes last: it takes work only when nobody else can.
    return sorted(candidates, key=lambda member: (by_member[member]["share"] == 0,
                                                  by_member[member]["points"] / (by_member[member]["share"] or 1), -by_member[member]["share"]))


def summary(members, loads, profile):
    """What the room shows: each account's actual and intended part of this round."""
    rows = accounts(members, loads, profile)
    if len(rows) < 2:
        return None
    points, shares = sum(row["points"] for row in rows), sum(row["share"] for row in rows)
    if not shares:
        return None
    return {"configured": len({row["share"] for row in rows}) > 1, "accounts": [
        {**row, "points": round(row["points"]), "target_percent": round(row["share"] * 100 / shares),
         "actual_percent": round(row["points"] * 100 / points) if points else None} for row in rows]}
