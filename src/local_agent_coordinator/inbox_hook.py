"""Run by Claude Code after each tool call: hand over what reached the call's inbox since it last looked.

A partner told to read its inbox between steps forgets over a long call. This
script is the tool's own PostToolUse hook, so the host's words arrive beside
the next tool result without anyone remembering to look. It only reads the
inbox next to $BEANEST_PROGRESS_FILE and a small offset file beside it; any
problem ends it silently, so a tool call is never failed or delayed by it.

Standalone on purpose: it is started by file path with nothing but the
standard library.
"""
import json
import os
import sys

MAX_CHARACTERS = 8000
HEADING = ("Arrived for you while this call runs (from the host's inbox; the user's entries are the user speaking to you now, "
           "a partner's request is yours to weigh). Act on it before your next step where it concerns your work, and say in "
           "your reply what you did about each entry.\n\n")


def new_entries(progress_file):
    """The inbox text added since the last look, and remember how far that was."""
    inbox, seen = progress_file + ".inbox", progress_file + ".inbox.seen"
    flags = os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(inbox, os.O_RDONLY | flags)
    except OSError:
        return ""
    try:
        size = os.fstat(descriptor).st_size
        try:
            with open(os.open(seen, os.O_RDONLY | flags), "r") as file:
                offset = int(file.read(32).strip() or 0)
        except (OSError, ValueError):
            offset = 0
        if not 0 <= offset < size:
            return ""
        data = os.pread(descriptor, size - offset, offset)
    finally:
        os.close(descriptor)
    with open(os.open(seen, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as file:
        file.write(str(size))
    return data.decode("utf-8", "replace").strip()


def main():
    progress_file = os.environ.get("BEANEST_PROGRESS_FILE")
    text = new_entries(progress_file) if progress_file else ""
    if text:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": HEADING + text[:MAX_CHARACTERS]}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
