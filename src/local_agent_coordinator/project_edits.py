"""Bounded literal replacements, expanded before ordinary artifact validation.

This pure transform grants no write authority. The caller must still enforce
task scope, owner, cancellation, file limits, secret checks and workspace CAS.
"""
from __future__ import annotations

import re


MAX_EDITS = 16
MAX_EDIT_BYTES = 32 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"path", "base_sha256", "old_text", "new_text"}


class ProjectEditError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def expand_edits(snapshot, edits):
    """Resolve disjoint replacements against one immutable original snapshot.

    Every match is located in the original, including overlapping occurrences.
    Reverse application preserves those positions; edits never match text
    introduced by an earlier edit in the same response.
    """
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise ProjectEditError("invalid_project_edits")
    indexed = {file["path"]: file for file in snapshot["files"]}
    groups, size = {}, 0
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != _FIELDS
                or any(not isinstance(edit[key], str) for key in _FIELDS)
                or not edit["old_text"] or edit["old_text"] == edit["new_text"]
                or not _HASH.fullmatch(edit["base_sha256"])
                or "\0" in edit["old_text"] or "\0" in edit["new_text"]):
            raise ProjectEditError("invalid_project_edits")
        try:
            size += len(edit["old_text"].encode("utf-8")) + len(edit["new_text"].encode("utf-8"))
        except UnicodeError:
            raise ProjectEditError("invalid_project_edits") from None
        if size > MAX_EDIT_BYTES:
            raise ProjectEditError("project_edit_limit")
        file = indexed.get(edit["path"])
        if file is None or file["sha256"] != edit["base_sha256"]:
            raise ProjectEditError("project_edit_conflict")
        start = file["content"].find(edit["old_text"])
        if start < 0:
            raise ProjectEditError("project_edit_conflict")
        if file["content"].find(edit["old_text"], start + 1) >= 0:
            raise ProjectEditError("project_edit_ambiguous")
        groups.setdefault(edit["path"], []).append((start, start + len(edit["old_text"]), edit["new_text"]))
    files = []
    for path, replacements in sorted(groups.items()):
        replacements.sort()
        if any(right[0] < left[1] for left, right in zip(replacements, replacements[1:])):
            raise ProjectEditError("project_edit_overlap")
        content = indexed[path]["content"]
        for start, end, replacement in reversed(replacements):
            content = content[:start] + replacement + content[end:]
        files.append({"path": path, "previous_sha256": indexed[path]["sha256"], "content": content})
    return files
