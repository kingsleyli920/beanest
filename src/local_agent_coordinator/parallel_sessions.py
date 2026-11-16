"""One partner doing several tasks at once.

A partner runs one call at a time per session. When ready tasks outnumber the
free partners, the host lets a partner with room left work on another task in
a parallel session of its own. To the user and to the other partners that is
still the one partner: same name, same account, same quota and workload.

Inside the host each parallel session is a lane: a hidden identity
"<partner id>~x<n>" with its own row, native session and private workspace, so
everything that keeps two calls apart keeps two lanes apart. A lane only ever
does task work. It never plans, reviews, checks a round or receives a message,
and no user or partner can name one: the "~" is outside what a partner id may
contain. Everything that leaves the host (a prompt, an HTTP response) is
folded first, so a lane's id reads as its partner's.
"""
from __future__ import annotations

import re

SEPARATOR = "~x"
DEFAULT_LIMIT = 3   # tasks one partner may work on at once, lanes included; the user changes it per partner
MIN_LIMIT, MAX_LIMIT = 1, 8
_BASE = r"[a-z][a-z0-9_-]{0,31}"
_LANE = re.compile(r"(" + _BASE + r")~x([2-" + str(MAX_LIMIT) + r"])\Z")
_LANE_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_~-])(" + _BASE + r")~x[2-" + str(MAX_LIMIT) + r"](?![A-Za-z0-9_~])")


def lane_id(partner: str, number: int) -> str:
    """The identity of a partner's nth session; the first is the partner itself."""
    if not MIN_LIMIT <= number <= MAX_LIMIT:
        raise ValueError("invalid_parallel_session")
    return partner if number == 1 else partner + SEPARATOR + str(number)


def is_lane(value) -> bool:
    return isinstance(value, str) and SEPARATOR in value and _LANE.match(value) is not None


def partner_of(value):
    """The partner an identity belongs to: itself, unless it is a lane."""
    if isinstance(value, str) and SEPARATOR in value:
        match = _LANE.match(value)
        if match:
            return match[1]
    return value


def lane_number(value) -> int:
    match = _LANE.match(value) if isinstance(value, str) else None
    return int(match[2]) if match else 1


def checked_limit(value) -> int:
    if type(value) is not int or not MIN_LIMIT <= value <= MAX_LIMIT:
        raise ValueError("invalid_parallel_limit")
    return value


def fold_text(text: str) -> str:
    return _LANE_IN_TEXT.sub(r"\1", text) if SEPARATOR in text else text


def fold(value):
    """The same data with every lane read as its partner.

    A list entry that describes a lane itself (its "id" is a lane) is dropped,
    as is a mapping entry keyed by a lane: the partner's own entry stands for
    both. Ids listed side by side are named once.
    """
    if isinstance(value, str):
        return fold_text(value)
    if isinstance(value, dict):
        return {key: fold(item) for key, item in value.items() if not is_lane(key) and key != "parallel_sessions"}
    if isinstance(value, (list, tuple)):
        plain = {item for item in value if isinstance(item, str) and not is_lane(item)}
        result, added = [], set()
        for item in value:
            if isinstance(item, dict) and is_lane(item.get("id")):
                continue
            if is_lane(item):
                partner = partner_of(item)
                if partner not in plain and partner not in added:
                    added.add(partner)
                    result.append(partner)
                continue
            result.append(fold(item))
        return result
    return value
