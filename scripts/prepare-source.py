#!/usr/bin/env python3
"""Inspect an explicit source allowlist or prepare a private directory candidate.

This command never publishes, initializes Git, installs dependencies or runs an
agent. Additional private terms belong in local --deny-text arguments, not here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys


MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
TREE_TYPES = {"src": {".py", ".html", ".js", ".css"}, "tests": {".py", ".cjs"}}
EXCLUDED = frozenset({"__pycache__", "node_modules", ".git", ".venv", "runtime",
                      "logs", "artifacts", "dist", "build", "target"})
# Documentation and desktop files are reviewed individually; new files do not
# silently become public. Protocol JSON fixtures are likewise explicit inputs.
FIXED = (
    "README.md", "README.zh-CN.md", "pyproject.toml", ".gitignore", "LICENSE",
    "CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", ".github/workflows/ci.yml",
    "desktop/main.js", "desktop/lifecycle.js", "desktop/lifecycle.test.js", "desktop/build-local.js",
    "desktop/menu.js", "desktop/menu.test.js", "desktop/power-guard.js", "desktop/power-guard.test.js",
    "desktop/project-picker.js", "desktop/project-picker.test.js", "desktop/preload.js", "desktop/build-local.test.js",
    "desktop/chat-location.js", "desktop/chat-location.test.js", "desktop/external-pages.js", "desktop/external-pages.test.js",
    "desktop/locale.js", "desktop/locale.test.js", "desktop/build-icon.swift", "desktop/assets/beanest.svg", "desktop/assets/beanest.png", "desktop/assets/beanest.icns",
    "desktop/package.json", "desktop/package-lock.json", "scripts/start-coordinator.command", "scripts/prepare-source.py",
    "docs/extensions.md", "docs/projects.md", "docs/room-contract.md", "docs/public-readme.md",
    "docs/assets/beanest-en.jpg", "docs/assets/beanest-zh-CN.jpg",
    "tests/fixtures/native_plan_response_v1.json", "tests/fixtures/native_protocol_v1.json",
    "tests/fixtures/complete_command_v1.json",
)
BINARY_FILES = frozenset({"desktop/assets/beanest.png", "desktop/assets/beanest.icns",
                          "docs/assets/beanest-en.jpg", "docs/assets/beanest-zh-CN.jpg"})
MIT_BODY = '''Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.'''
# Match credential values with recognizable shapes, not field names or short
# examples used to exercise redaction. Private application terms are caller data.
CREDENTIALS = re.compile(
    r"\bsk-(?:proj-|ant-api[0-9]+-)?[A-Za-z0-9_-]{24,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b|"
    r"\bAIza[A-Za-z0-9_-]{35}\b|\bAKIA[A-Z0-9]{16}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{30,}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{64,}"
    r"-----END (?:[A-Z]+ )?PRIVATE KEY-----"
)


class ExportError(ValueError):
    def __init__(self, code: str, path: str | None = None):
        self.code, self.path = code, path
        super().__init__(code)


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: bytes

    def manifest(self) -> dict:
        return {"path": self.path, "sha256": hashlib.sha256(self.content).hexdigest()}


def _root(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ExportError("invalid_source_root")
    return path.resolve(strict=True)


def _read(root: Path, relative: str) -> bytes:
    """Open each component without following links, including changed ancestors."""
    descriptors = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptors.append(os.open(root, directory_flags))
        parts = Path(relative).parts
        if not parts or any(part in (".", "..") for part in parts):
            raise ExportError("invalid_source_path", relative)
        for part in parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptors[-1])
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE:
            raise ExportError("invalid_source_file", relative)
        chunks, size = [], 0
        while chunk := os.read(descriptor, min(65536, MAX_FILE + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_FILE:
                raise ExportError("source_too_large", relative)
        return b"".join(chunks)
    except OSError:
        raise ExportError("source_missing_or_unsafe", relative) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _tree(root: Path, name: str):
    directory = root / name
    if directory.is_symlink() or not directory.is_dir():
        raise ExportError("source_missing_or_unsafe", name)
    pending = [directory]
    while pending:
        current = pending.pop()
        for entry in sorted(current.iterdir()):
            relative = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                raise ExportError("source_missing_or_unsafe", relative)
            if entry.name in EXCLUDED or entry.name.startswith("."):
                continue
            if entry.is_dir():
                pending.append(entry)
            elif entry.suffix in TREE_TYPES[name]:
                yield relative


def _scan(text: str, relative: str, deny_text: tuple[str, ...]):
    home = str(Path.home())
    if len(home) > 1 and home in text:
        raise ExportError("personal_path_found", relative)
    folded = text.casefold()
    if any(term.casefold() in folded for term in deny_text):
        raise ExportError("denied_text_found", relative)
    if CREDENTIALS.search(text):
        raise ExportError("credential_shape_found", relative)


def _license(text: str):
    lines = text.strip().splitlines()
    copyrights = [line for line in lines if re.fullmatch(r"Copyright \(c\) [0-9]{4}(?:-[0-9]{4})? .+", line)]
    remainder = " ".join(line for line in lines if line not in copyrights and line != "MIT License")
    if not copyrights or "MIT License" not in lines or " ".join(remainder.split()) != " ".join(MIT_BODY.split()):
        raise ExportError("license_incomplete", "LICENSE")


def collect_source(root: Path, *, deny_text=()) -> list[SourceFile]:
    root = _root(root)
    deny_text = tuple(deny_text)
    if any(not isinstance(term, str) or not term.strip() for term in deny_text):
        raise ExportError("invalid_deny_text")
    selected = {relative: relative for relative in FIXED}
    for name in TREE_TYPES:
        for relative in _tree(root, name):
            selected[relative] = relative
    files, total = [], 0
    for destination, source in sorted(selected.items()):
        content = _read(root, source)
        if source in BINARY_FILES:
            if source.endswith(".png"):
                valid = content.startswith(b"\x89PNG\r\n\x1a\n")
            elif source.endswith(".jpg"):
                valid = content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
            else:
                valid = (content.startswith(b"icns") and len(content) >= 8
                         and int.from_bytes(content[4:8], "big") == len(content))
            if not valid:
                raise ExportError("source_invalid_binary_asset", source)
            # Binary assets are an exact allowlist, not a broad binary exemption;
            # still inspect embedded ASCII paths and credential-like values.
            text = content.decode("latin-1")
        else:
            try:
                text = content.decode("utf-8")
            except UnicodeError:
                raise ExportError("source_not_utf8", source) from None
        _scan(text, source, deny_text)
        _scan(destination, source, deny_text)
        if destination == "LICENSE":
            _license(text)
        total += len(content)
        if total > MAX_TOTAL:
            raise ExportError("source_too_large")
        files.append(SourceFile(destination, content))
    return files


def write_candidate(root: Path, output: Path, files: list[SourceFile]) -> dict:
    root = _root(root)
    names = set()
    for item in files:
        path = Path(item.path)
        if (not item.path or path.is_absolute() or ".." in path.parts or "\\" in item.path
                or path.as_posix() != item.path or item.path == "manifest.json" or item.path in names):
            raise ExportError("invalid_candidate_path")
        names.add(item.path)
    output = Path(output)
    if not output.is_absolute() or ".." in output.parts:
        raise ExportError("invalid_output_path")
    try:
        relative = output.relative_to(root / "runtime")
    except ValueError:
        raise ExportError("output_outside_runtime") from None
    if not relative.parts:
        raise ExportError("invalid_output_path")
    # Refuse links and existing candidates before writing anything. No overwrite
    # option exists; every candidate has its own reviewable manifest.
    current = root
    for part in output.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ExportError("unsafe_output_path")
        if current != output and current.exists() and not current.is_dir():
            raise ExportError("unsafe_output_path")
    if output.exists():
        raise ExportError("output_exists")
    manifest = {"files": [item.manifest() for item in files]}
    created = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.mkdir(mode=0o700)
        created = True
        for item in files:
            destination = output / item.path
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            mode = 0o700 if item.path.startswith("scripts/") else 0o600
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(item.content)
        descriptor = os.open(output / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
    except OSError:
        if created:
            shutil.rmtree(output)
        raise ExportError("candidate_write_failed") from None
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect without creating a candidate (default)")
    mode.add_argument("--output", type=Path, help="New absolute candidate directory below this project's runtime")
    parser.add_argument("--deny-text", action="append", default=[], help="Additional private text to reject")
    args = parser.parse_args(argv)
    try:
        root = Path(__file__).absolute().parent.parent
        files = collect_source(root, deny_text=args.deny_text)
        manifest = (write_candidate(root, args.output, files) if args.output is not None
                    else {"files": [item.manifest() for item in files]})
    except (ExportError, OSError) as error:
        code = error.code if isinstance(error, ExportError) else "source_unavailable"
        relative = " (" + error.path + ")" if isinstance(error, ExportError) and error.path else ""
        print("prepare-source: " + code + relative, file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
