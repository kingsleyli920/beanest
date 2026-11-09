"""One-line progress file a long foreground command can write for the user.

The file is written by whatever the partner's command chooses to write, so its
content is untrusted display text. It is never tool activity, progress evidence,
model context or proof that anything succeeded; see docs/room-contract.md.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import unicodedata
import uuid

ENVIRONMENT_VARIABLE = "BEANEST_PROGRESS_FILE"
MAX_READ_BYTES = 4096
MAX_CHARACTERS = 200
MIN_EVENT_SECONDS = 30

_TERMINAL_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def create_progress_file(directory):
    """Create an empty private file and return its absolute path."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("progress directory must be private")
    path = directory.resolve() / f"{uuid.uuid4()}.progress"
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    return path


def remove_progress_file(path):
    """Delete the file; the command may already have removed or replaced it."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _display_text(line):
    without_sequences = _TERMINAL_SEQUENCE.sub("", line).replace("\t", " ")
    return "".join(character for character in without_sequences
                   if unicodedata.category(character) not in {"Cc", "Cf"}).strip()


def read_progress_line(path, *, redact=lambda text: text):
    """Return the last displayable line from the end of the file, or None.

    Anything that is not a plain readable file yields None: a missing file, a
    symlink, or a FIFO opened by a command (O_NONBLOCK keeps that from hanging).
    Redaction runs before the length cap so a cut cannot expose half a secret.
    """
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - MAX_READ_BYTES)
        data = os.pread(descriptor, MAX_READ_BYTES, start)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if start:
        # The window can open in the middle of a character; that half is ours.
        data = data[next((index for index, byte in enumerate(data) if byte & 0xC0 != 0x80), len(data)):]
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        text = _display_text(line)
        if text:
            return redact(text)[:MAX_CHARACTERS]
    return None


class ProgressThrottle:
    """Decide whether a read line deserves a new event for this job."""

    def __init__(self, minimum_seconds=MIN_EVENT_SECONDS):
        self.minimum_seconds = minimum_seconds
        self.text = None
        self.recorded_at = None

    def due(self, text, now):
        if not text or text == self.text:
            return False
        # A clock stepped backwards must not silence the channel.
        return self.recorded_at is None or not 0 <= now - self.recorded_at < self.minimum_seconds

    def mark(self, text, now):
        self.text, self.recorded_at = text, now
