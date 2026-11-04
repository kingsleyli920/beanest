"""Shared message and inbound JSON byte budgets, not model or task budgets.

Text is measured before the existing whitespace normalization. Transport callers
measure actual wire bytes before reading/decoding; JSON escaping is not text size.
These validators do not parse JSON, rewrite fingerprints, or truncate content.
"""

MAX_MESSAGE_UTF8_BYTES = 1024 * 1024
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024
# Actual serialized stdin input, independent of output and work budgets.
MAX_PROMPT_BYTES = 16 * 1024 * 1024


class MessageCapacityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def checked_message_text(value: object) -> str:
    """Validate raw UTF-8 bytes, then preserve the existing strip semantics."""
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise MessageCapacityError("invalid_text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise MessageCapacityError("invalid_text") from None
    if size > MAX_MESSAGE_UTF8_BYTES:
        raise MessageCapacityError("message_too_large")
    return value.strip()


def checked_json_body_size(size: object) -> int:
    """Check a parsed byte count; HTTP/MCP retain their own JSON validation."""
    if type(size) is not int or size < 0:
        raise MessageCapacityError("invalid_request")
    if size > MAX_JSON_BODY_BYTES:
        raise MessageCapacityError("body_too_large")
    return size
