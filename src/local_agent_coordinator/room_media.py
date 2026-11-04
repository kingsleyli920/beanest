"""Verified file handles and local attachment references, without path authorization.

The room service must authorize the source and root before calling this module.
Returned handles pin the verified inode; callers still own streaming and closing.
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO
from urllib.parse import urlsplit

from .folder_workspace import _excluded


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CHUNK_BYTES = 1024 * 1024
_MIME_TYPES = {
    ".png": ("image/png", "image"),
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".gif": ("image/gif", "image"),
    ".webp": ("image/webp", "image"),
    ".avif": ("image/avif", "image"),
    ".bmp": ("image/bmp", "image"),
    ".svg": ("image/svg+xml", "image"),
    ".mp4": ("video/mp4", "video"),
    ".m4v": ("video/mp4", "video"),
    ".webm": ("video/webm", "video"),
    ".mov": ("video/quicktime", "video"),
    ".ogv": ("video/ogg", "video"),
    ".mp3": ("audio/mpeg", "audio"),
    ".wav": ("audio/wav", "audio"),
    ".ogg": ("audio/ogg", "audio"),
    ".opus": ("audio/ogg", "audio"),
    ".m4a": ("audio/mp4", "audio"),
    ".aac": ("audio/aac", "audio"),
    ".flac": ("audio/flac", "audio"),
}
_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)\Z")
_REFERENCES = re.compile(
    r"!?\[[^\]\r\n]*\]\(\s*(?:<(?P<link_angle>[^>\r\n]+)>|"
    r"(?P<link>[^\s)]+))(?:\s+[^)\r\n]*)?\)"
    r"|`(?P<code>[^`\r\n]+)`"
    r"|<(?P<angle>[^<>\r\n]+)>"
    r"|(?P<absolute>(?:file://(?:localhost)?)?/[^\r\n<>`\"'|，；！？。：“”‘’（）【】《》]+?"
    r"\.[A-Za-z0-9]{1,16})(?=$|[\s,;!?，；！？。:：)\]])"
    r"|(?P<plain>[^\s<>\[\](){}`\"'|;,!?，；！？。：“”‘’（）【】《》]+)"
)


class MediaError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def mime_kind(name: str) -> tuple[str, str]:
    """Use an explicit media allowlist; other files remain downloads."""
    return _MIME_TYPES.get(Path(name).suffix.casefold(), ("application/octet-stream", "file"))


def _identity(metadata) -> tuple:
    return tuple(getattr(metadata, key) for key in (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _absolute_path(value) -> Path:
    if not isinstance(value, (str, Path)):
        raise MediaError("media_invalid_path")
    path = Path(value)
    raw = str(path)
    try:
        raw.encode("utf-8")
    except UnicodeError:
        raise MediaError("media_invalid_path") from None
    if (not path.is_absolute() or not path.name or ".." in path.parts
            or "\\" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise MediaError("media_invalid_path")
    if any(_excluded(part) == "sensitive_path" for part in path.parts[1:]):
        raise MediaError("media_unsafe_path")
    return path


def open_verified(path, expected_sha256=None) -> tuple[BinaryIO, dict]:
    """Open without following links, hash in chunks, then rewind a stable file.

    Identity is (device, inode, mode, links, bytes, mtime_ns, ctime_ns). No file
    size ceiling is imposed. The caller owns the returned binary file object.
    """
    path = _absolute_path(path)
    directory = descriptor = None
    stream = None
    try:
        directory = os.open(path.anchor, _DIR_FLAGS)
        for part in path.parts[1:-1]:
            child = os.open(part, _DIR_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, _FILE_FLAGS, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MediaError("media_unsafe_path")
        stream = os.fdopen(descriptor, "rb", buffering=0)
        descriptor = None
        digest = hashlib.sha256()
        length = 0
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
            length += len(chunk)
        after = os.fstat(stream.fileno())
        sha256 = digest.hexdigest()
        if (_identity(before) != _identity(after) or length != after.st_size
                or expected_sha256 is not None and sha256 != expected_sha256):
            raise MediaError("media_changed")
        stream.seek(0)
        mime, kind = mime_kind(path.name)
        return stream, {"sha256": sha256, "bytes": length, "mime": mime,
                        "kind": kind, "name": path.name, "identity": _identity(after)}
    except OSError as exc:
        code = "media_unsafe_path" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "media_unavailable"
        if stream is not None:
            stream.close()
        raise MediaError(code) from None
    except BaseException:
        if stream is not None:
            stream.close()
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def parse_range(value, total: int) -> tuple[int, int, int]:
    """Return an inclusive single HTTP byte range, or raise for HTTP 416."""
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise MediaError("media_range_invalid")
    if value is None:
        return 200, 0, total - 1
    match = _RANGE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None or total == 0:
        raise MediaError("media_range_invalid")
    first, last = match.groups()
    try:
        if first:
            start = int(first)
            end = min(int(last), total - 1) if last else total - 1
            if start >= total or end < start:
                raise ValueError
        else:
            suffix = int(last)
            if suffix <= 0:
                raise ValueError
            start, end = max(0, total - suffix), total - 1
    except ValueError:
        raise MediaError("media_range_invalid") from None
    return 206, start, end


def local_references(text: str) -> list[str]:
    """Extract local file candidates from text without reading or authorizing them."""
    if not isinstance(text, str):
        return []
    result = []
    seen = set()
    for match in _REFERENCES.finditer(text):
        candidate = next(value for value in match.groups() if value is not None).strip()
        candidate = candidate.rstrip("。:：")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if (parsed.scheme and parsed.scheme.casefold() != "file"
                or candidate.startswith(("#", "//"))
                or any(ord(char) < 32 or ord(char) == 127 for char in candidate)):
            continue
        name = Path(parsed.path).name
        if not name or not Path(name).suffix or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result
