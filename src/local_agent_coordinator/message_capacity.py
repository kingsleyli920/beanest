"""Shared message text validation. Beanest sets no size limit on a message,
a request body or a prompt: an agent or the user may send any amount of text.

These validators do not parse JSON, rewrite fingerprints, or truncate content.
"""


class MessageCapacityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def checked_message_text(value: object) -> str:
    """Reject only empty or malformed text, then preserve the existing strip semantics."""
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise MessageCapacityError("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise MessageCapacityError("invalid_text") from None
    return value.strip()
