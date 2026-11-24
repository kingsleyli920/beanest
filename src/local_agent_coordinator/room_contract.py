"""Error type, text checks and command shapes shared by the room stores.

Kept free of every store so room_store, team_store, objective_store and the
response schema can all depend on it without importing one another.
"""
from __future__ import annotations


# Beanest sets no count or size bound on what partners write or on how many
# partners a room holds: the agents themselves have none, so neither does the
# room. Shape, identity and safety are still checked.


class RoomError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


COMMAND_FIELDS = {
    "propose": {"type", "id", "title", "description", "acceptance", "dependencies", "write_paths", "capability"},
    "claim": {"type", "task_id"},
    "complete": {"type", "task_id", "result"},
    "block": {"type", "task_id", "reason", "cause"},
    "review": {"type", "task_id", "verdict", "reason"},
    "request_help": {"type", "task_id", "to", "text", "capability", "repair_paths"},
    "plan_ready": {"type"},
    "checkpoint": {"type", "decision", "summary", "next_goal", "scope_reference", "question"},
    "checkpoint_review": {"type", "verdict", "reason", "proposal_digest"},
    # The plan author's mid-run edit of a task nobody has started: its full new
    # dependency list and a narrowed write_paths. It reuses existing field names,
    # so the stable union schema keeps its fields and only gains this kind.
    "adjust": {"type", "task_id", "dependencies", "write_paths"},
    # A work call adding files to its own task's write_paths when no unfinished
    # task holds them, applied before its result is imported. Existing field
    # names again, so the stable union schema only gains this kind.
    "extend": {"type", "task_id", "write_paths"},
    # Any partner's split of this round's work among the members: shares maps a
    # member id to 0-100. The one field this adds to the union schema is shares.
    "allocate": {"type", "shares", "reason"},
}


def checked_message(value: object) -> str:
    from .message_capacity import checked_message_text, MessageCapacityError
    try:
        return checked_message_text(value)
    except MessageCapacityError as error:
        raise RoomError(error.code) from None


def checked_text(value: object, maximum: int | None = None) -> str:
    """Non-empty text without NUL. maximum is only for identifiers (request and
    task ids); what a partner writes is never bounded by length."""
    if (not isinstance(value, str) or not value.strip() or maximum is not None and len(value) > maximum
            or "\0" in value):
        raise RoomError("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise RoomError("invalid_text") from None
    return value.strip()
