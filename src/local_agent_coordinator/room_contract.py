"""Error type, text checks and command shapes shared by the room stores.

Kept free of every store so room_store, team_store, objective_store and the
response schema can all depend on it without importing one another.
"""
from __future__ import annotations


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
}


def checked_message(value: object) -> str:
    from .message_capacity import checked_message_text, MessageCapacityError
    try:
        return checked_message_text(value)
    except MessageCapacityError as error:
        raise RoomError(error.code) from None


def checked_text(value: object, maximum: int = 3000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise RoomError("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise RoomError("invalid_text") from None
    return value.strip()
