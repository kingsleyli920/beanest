"""The short record one round leaves for a fresh native session.

The model writes it, so it stays data: its shape is checked here and it never
grants permission or scope. A long resumed session carries its whole context
into every call, which is the cost this record exists to cut.
"""
from __future__ import annotations

# Below this the resumed context is still cheap enough that keeping the model's
# own memory is worth more than swapping it for a short written record.
ROTATION_MIN_CONTEXT_TOKENS = 200_000
# Between tasks of one round the session still holds decisions the board does
# not, so it takes a larger context before a written record is the better deal.
TASK_ROTATION_MIN_CONTEXT_TOKENS = 400_000
# A task can grow a session past that limit, and with a peer reviewing there is
# no later call by this partner to ask, so the record is requested well before.
TASK_HANDOFF_REQUEST_MIN_CONTEXT_TOKENS = 200_000

# The partner decides how long its own record is: no length or count bound
# drops it. The prompt asks for a short record; that is guidance, not a gate.
_LISTS = ("done", "decisions", "unverified", "key_files")
_FIELDS = frozenset({"goal", *_LISTS})


def handoff_schema() -> dict:
    """Transport shape. Constant, because Claude's cached schema must not vary."""
    properties = {"goal": {"type": "string", "minLength": 1}}
    for name in _LISTS:
        properties[name] = {"type": "array", "items": {"type": "string", "minLength": 1}}
    return {"type": ["object", "null"], "properties": properties, "required": list(properties),
            "additionalProperties": False,
            "description": ("Used by a checkpoint command, and by a complete or review command when "
                            "context.handoff_requested is true; null otherwise. Short record for a later fresh "
                            "session: facts and locations, not the story of how you worked.")}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() and "\0" not in value else None


def normalized_handoff(value) -> dict | None:
    """The stored form, or None when any part breaks the shape."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        return None
    goal = _text(value["goal"])
    if goal is None:
        return None
    result = {"goal": goal}
    for name in _LISTS:
        entries = value[name]
        if not isinstance(entries, list):
            return None
        cleaned = [_text(entry) for entry in entries]
        if any(entry is None for entry in cleaned):
            return None
        result[name] = cleaned
    return result


def context_tokens(usage) -> int | None:
    """Tokens this call sent as context; None when the provider did not say.

    A Claude result adds up every API request of the turn, so a call with
    several tool rounds reads several times its real context. The last
    request's own count is the context size; the total is only the fallback.
    """
    if not isinstance(usage, dict):
        return None
    values = usage.get("last_iteration") or usage.get("values")
    if not isinstance(values, dict):
        return None
    parts = [values.get(key) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    return sum(parts) if all(type(part) is int for part in parts) else None
