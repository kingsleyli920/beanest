"""Inert directory snapshots with bounded, versioned access through the coordinator.

Source trees are read only during explicit seeding. Content-addressed private
blobs and immutable indexes avoid putting an entire project into model context.
This module neither executes project code nor writes back to its source.
"""
from __future__ import annotations

import codecs
from collections import Counter
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from uuid import uuid4

from pathspec import GitIgnoreSpec

from .agent_registry import safe_agent_id
from .project_catalog import (_SECRET, _SENSITIVE_NAMES, _absolute_path,
                              _open_absolute, canonical_json, ProjectCatalogError)
from .project_workspace import (ProjectWorkspace, ProjectWorkspaceError, _uuid,
                                _private, _open_directory, _ID, _HASH)


FORMAT = "beanest-folder-workspace-v1"
MAX_SOURCE_FILES = 20_000
MAX_SOURCE_BYTES = 512 * 1024 * 1024
CAPTURE_SECONDS = 30
MAX_FILE_BYTES = 1024 * 1024
MAX_CHANGE_BYTES = 2 * 1024 * 1024
MAX_CHANGED_TOTAL_BYTES = 16 * 1024 * 1024
MAX_CHANGE_FILES = 16
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_SEARCH_BYTES = 16 * 1024 * 1024
MAX_IGNORE_BYTES = 64 * 1024
MAX_IGNORE_PATTERNS = 1000
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", ".venv", "venv", "env",
    "__pycache__", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".next", ".nuxt", "build", "dist", "target", "runtime", ".tox", ".idea", ".godot",
})


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# (index sha256, revision) pairs whose decoded bytes passed every check.
_VALIDATED_INDEXES = set()


def _path(value, *, root=False):
    if value == "" and root:
        return value
    if (not isinstance(value, str) or not value or len(value) > 1024
            or "\\" in value or value.startswith("/")
            or _CONTROL.search(value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ProjectWorkspaceError("invalid_project_files")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ProjectWorkspaceError("invalid_project_files") from None
    return value


def validate_folder_path(value):
    """Validate a portable relative project path without restricting its alphabet."""
    return _path(value)


def _excluded(path, *, captured=False):
    parts = path.split("/")
    for part in parts:
        name = part.casefold()
        if name in _EXCLUDED_DIRS and not (captured and name == ".godot"):
            return "excluded_directory"
        if (name == ".env" or name.startswith(".env.") or name in _SENSITIVE_NAMES
                or name.split(".")[0] in _SENSITIVE_NAMES
                or name.endswith((".pem", ".key", ".p12", ".pfx"))):
            return "sensitive_path"
    return None


def _attributes(metadata):
    return tuple(getattr(metadata, key) for key in
                 ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


class FolderWorkspace(ProjectWorkspace):
    """Room-owned snapshot; inherited locking only touches the private runtime."""

    def __init__(self, root, room_id, *, create=False):
        try:
            self.root = Path(root)
        except TypeError:
            raise ProjectWorkspaceError("invalid_project_root") from None
        self.room_id = _uuid(room_id)
        if (self.room_id is None or not self.root.is_absolute() or ".." in self.root.parts
                or type(create) is not bool):
            raise ProjectWorkspaceError("invalid_project_request")
        self.format = FORMAT
        self.max_bytes = MAX_CHANGE_BYTES
        with self._locked(create=create) as (room, revisions, created):
            if created:
                os.mkdir("blobs", mode=0o700, dir_fd=room)
                manifest = {"format": FORMAT, "room_id": self.room_id, "revision": -1, "commits": []}
                self._publish_index(room, revisions, manifest, [], None, None, None, {})
            else:
                self._manifest(room)

    def _read(self, directory, filename):
        descriptor = os.open(filename, _FILE_FLAGS, dir_fd=directory)
        try:
            _private(descriptor)
            if os.fstat(descriptor).st_size > MAX_INDEX_BYTES:
                raise ProjectWorkspaceError("project_storage_corrupt")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_INDEX_BYTES + 1)
            if len(raw) > MAX_INDEX_BYTES:
                raise ProjectWorkspaceError("project_storage_corrupt")
            return raw
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _decode_json(self, raw):
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            def reject(_value):
                raise ValueError
            return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
        except (ValueError, UnicodeError, RecursionError):
            raise ProjectWorkspaceError("project_storage_corrupt") from None

    def _manifest(self, room):
        value = self._decode_json(self._read(room, "manifest.json"))
        if (not isinstance(value, dict) or set(value) != {"format", "room_id", "revision", "commits"}
                or value["format"] != FORMAT or value["room_id"] != self.room_id
                or type(value["revision"]) is not int or not isinstance(value["commits"], list)
                or value["revision"] != len(value["commits"]) - 1):
            raise ProjectWorkspaceError("project_storage_corrupt")
        seen = set()
        for commit in value["commits"]:
            if (not isinstance(commit, dict)
                    or set(commit) != {"snapshot_id", "sha256", "proposal_id", "fingerprint"}
                    or _uuid(commit["snapshot_id"]) != commit["snapshot_id"]
                    or not isinstance(commit["sha256"], str) or not _HASH.fullmatch(commit["sha256"])
                    or (commit["proposal_id"] is not None and
                        (not isinstance(commit["proposal_id"], str) or not _ID.fullmatch(commit["proposal_id"])
                         or commit["proposal_id"] in seen))
                    or (commit["fingerprint"] is not None and
                        (not isinstance(commit["fingerprint"], str) or not _HASH.fullmatch(commit["fingerprint"])))):
                raise ProjectWorkspaceError("project_storage_corrupt")
            seen.add(commit["proposal_id"])
        return value

    def _index(self, revisions, manifest, revision=None):
        revision = manifest["revision"] if revision is None else revision
        if type(revision) is not int or revision < 0 or revision > manifest["revision"]:
            raise ProjectWorkspaceError("project_revision_conflict")
        commit = manifest["commits"][revision]
        raw = self._read(revisions, commit["snapshot_id"] + ".json")
        if _hash(raw) != commit["sha256"]:
            raise ProjectWorkspaceError("project_storage_corrupt")
        value = self._decode_json(raw)
        # Bytes are re-read and hashed every time; only the per-file checks of
        # identical, already validated bytes are skipped on each UI poll.
        if (commit["sha256"], revision) in _VALIDATED_INDEXES:
            return value
        if (not isinstance(value, dict)
                or set(value) != {"format", "revision", "files", "source", "excluded"}
                or value["format"] != FORMAT or value["revision"] != revision
                or not isinstance(value["files"], list) or len(value["files"]) > MAX_SOURCE_FILES
                or not isinstance(value["excluded"], dict)):
            raise ProjectWorkspaceError("project_storage_corrupt")
        seen, total = set(), 0
        for item in value["files"]:
            if (not isinstance(item, dict)
                    or set(item) - {"path", "sha256", "bytes", "text", "owner", "changed", "executable"}
                    or not {"path", "sha256", "bytes", "text", "owner", "changed"} <= set(item)
                    or "executable" in item and type(item["executable"]) is not bool):
                raise ProjectWorkspaceError("project_storage_corrupt")
            try:
                _path(item["path"])
            except ProjectWorkspaceError:
                raise ProjectWorkspaceError("project_storage_corrupt") from None
            # Older snapshots could capture .godot before it became a standard
            # cache exclusion. Keep those verified inputs readable, not writable.
            if (_excluded(item["path"], captured=True) or item["path"].casefold() in seen
                    or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])
                    or type(item["bytes"]) is not int or item["bytes"] < 0
                    or type(item["text"]) is not bool or type(item["changed"]) is not bool
                    or (item["owner"] is not None and not safe_agent_id(item["owner"]))):
                raise ProjectWorkspaceError("project_storage_corrupt")
            total += item["bytes"]
            seen.add(item["path"].casefold())
        if (total > MAX_SOURCE_BYTES
                or sum(item["bytes"] for item in value["files"] if item["changed"]) > MAX_CHANGED_TOTAL_BYTES):
            raise ProjectWorkspaceError("project_storage_corrupt")
        source = value["source"]
        if source is not None and (not isinstance(source, dict)
                or set(source) != {"id", "name", "path", "selection_kind", "source_hash"}
                or source.get("selection_kind") != "folder" or _uuid(source.get("id")) != source.get("id")
                or not isinstance(source.get("source_hash"), str) or not _HASH.fullmatch(source["source_hash"])
                or not isinstance(source.get("name"), str) or not isinstance(source.get("path"), str)):
            raise ProjectWorkspaceError("project_storage_corrupt")
        if len(_VALIDATED_INDEXES) >= 4096:
            _VALIDATED_INDEXES.clear()
        _VALIDATED_INDEXES.add((commit["sha256"], revision))
        return value

    def _publish_index(self, room, revisions, manifest, files, proposal_id, fingerprint, source, excluded):
        revision = manifest["revision"] + 1
        index = {"format": FORMAT, "revision": revision, "files": files, "source": source, "excluded": excluded}
        raw, identifier = canonical_json(index), str(uuid4())
        commit = {"snapshot_id": identifier, "sha256": _hash(raw),
                  "proposal_id": proposal_id, "fingerprint": fingerprint}
        updated = {**manifest, "revision": revision, "commits": [*manifest["commits"], commit]}
        encoded = canonical_json(updated)
        if max(len(raw), len(encoded)) > MAX_INDEX_BYTES:
            raise ProjectWorkspaceError("project_limit_exceeded")
        temporary = ".manifest-" + str(uuid4())
        attempted = False
        try:
            self._write_new(revisions, identifier + ".json", raw)
            os.fsync(revisions)
            self._write_new(room, temporary, encoded)
            attempted = True
            os.replace(temporary, "manifest.json", src_dir_fd=room, dst_dir_fd=room)
            os.fsync(room)
        except OSError:
            # Never pretend that an attempted publication definitely did not happen.
            code = "project_commit_unconfirmed" if attempted else "project_storage_unavailable"
            raise ProjectWorkspaceError(code) from None
        finally:
            try:
                os.unlink(temporary, dir_fd=room)
            except FileNotFoundError:
                pass
        return index

    def _blob(self, blobs, entry):
        descriptor = os.open(entry["sha256"], _FILE_FLAGS, dir_fd=blobs)
        try:
            _private(descriptor)
            if os.fstat(descriptor).st_size != entry["bytes"]:
                raise ProjectWorkspaceError("project_storage_corrupt")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _verified_blob(self, blobs, entry):
        """Bound verification work before returning any file bytes to a caller."""
        if entry["bytes"] > MAX_SEARCH_BYTES:
            raise ProjectWorkspaceError("project_read_limit_exceeded")
        descriptor = self._blob(blobs, entry)
        try:
            digest = hashlib.sha256()
            deadline = time.monotonic() + 2
            while True:
                raw = os.read(descriptor, 256 * 1024)
                if not raw:
                    break
                digest.update(raw)
                if time.monotonic() >= deadline:
                    raise ProjectWorkspaceError("project_read_limit_exceeded")
            if digest.hexdigest() != entry["sha256"]:
                raise ProjectWorkspaceError("project_storage_corrupt")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _store_bytes(self, blobs, raw):
        digest = _hash(raw)
        try:
            self._write_new(blobs, digest, raw)
            os.fsync(blobs)
        except FileExistsError:
            entry = {"sha256": digest, "bytes": len(raw)}
            with os.fdopen(self._blob(blobs, entry), "rb") as stream:
                if stream.read(len(raw) + 1) != raw:
                    raise ProjectWorkspaceError("project_storage_corrupt")
        return digest

    def _content(self, blobs, entry):
        if not entry["text"]:
            raise ProjectWorkspaceError("project_binary_file")
        if entry["bytes"] > MAX_FILE_BYTES:
            raise ProjectWorkspaceError("project_limit_exceeded")
        with os.fdopen(self._blob(blobs, entry), "rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) != entry["bytes"] or _hash(raw) != entry["sha256"]:
            raise ProjectWorkspaceError("project_storage_corrupt")
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            raise ProjectWorkspaceError("project_storage_corrupt") from None

    def _summary(self, index, blobs):
        changed = [{**{key: item[key] for key in ("path", "sha256", "owner", "bytes")},
                    "content": self._content(blobs, item)} for item in index["files"] if item["changed"]]
        entries = {}
        for item in index["files"]:
            first, _, rest = item["path"].partition("/")
            entries[first] = {"path": first, "type": "directory" if rest else "file"}
        return {"format": FORMAT, "kind": "folder", "revision": index["revision"], "files": changed,
                "source": index["source"], "total_files": len(index["files"]),
                "total_bytes": sum(item["bytes"] for item in index["files"]), "excluded": index["excluded"],
                "root_entries": [entries[key] for key in sorted(entries)[:100]],
                "root_entries_truncated": len(entries) > 100}

    def index_sha256(self, revision):
        """Verify an immutable index before trusting a native workspace binding."""
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            index = self._index(revisions, manifest, revision)
            return manifest["commits"][index["revision"]]["sha256"]

    def snapshot(self, revision=None):
        with self._locked() as (room, revisions, _):
            index = self._index(revisions, self._manifest(room), revision)
            blobs = _open_directory("blobs", parent=room)
            try:
                return self._summary(index, blobs)
            finally:
                os.close(blobs)

    def materialize_snapshot(self, destination, revision=None, *, cancelled=None):
        """Export immutable private bytes into a fresh host-owned directory.

        This host-only operation neither recaptures the source nor executes
        anything. The caller owns destination lifecycle and must not start a
        process there until this method returns successfully. Historical
        executable metadata is unknown when absent, never guessed from names.
        """
        try:
            destination = _absolute_path(destination)
            parent = _open_absolute(destination.parent, private=True)
        except ProjectCatalogError as error:
            raise ProjectWorkspaceError(error.code) from None
        except OSError:
            raise ProjectWorkspaceError("project_unsafe_path") from None
        output = None
        created = False
        identity = None
        try:
            with self._locked() as (room, revisions, _):
                manifest = self._manifest(room)
                index = self._index(revisions, manifest, revision)
                try:
                    os.mkdir(destination.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    raise ProjectWorkspaceError("project_file_conflict") from None
                created = True
                output = os.open(destination.name, _DIR_FLAGS, dir_fd=parent)
                identity = os.fstat(output)
                blobs = _open_directory("blobs", parent=room)
                deadline = time.monotonic() + CAPTURE_SECONDS
                metadata = []
                try:
                    for entry in index["files"]:
                        directories = []
                        directory = output
                        try:
                            parts = entry["path"].split("/")
                            for part in parts[:-1]:
                                try:
                                    os.mkdir(part, mode=0o700, dir_fd=directory)
                                except FileExistsError:
                                    pass
                                directory = os.open(part, _DIR_FLAGS, dir_fd=directory)
                                directories.append(directory)
                            descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                                 getattr(os, "O_NOFOLLOW", 0),
                                                 0o700 if entry.get("executable") else 0o600, dir_fd=directory)
                            digest, written = hashlib.sha256(), 0
                            with os.fdopen(descriptor, "wb") as stream, os.fdopen(self._blob(blobs, entry), "rb") as source:
                                while True:
                                    if cancelled is not None and cancelled():
                                        raise ProjectWorkspaceError("project_export_cancelled")
                                    if time.monotonic() >= deadline:
                                        raise ProjectWorkspaceError("project_read_limit_exceeded")
                                    chunk = source.read(256 * 1024)
                                    if not chunk:
                                        break
                                    digest.update(chunk)
                                    written += len(chunk)
                                    stream.write(chunk)
                                stream.flush()
                                os.fsync(stream.fileno())
                            if written != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                                raise ProjectWorkspaceError("project_storage_corrupt")
                            metadata.append({key: entry[key] for key in ("path", "sha256", "bytes", "text")} |
                                            {"executable": entry.get("executable")})
                        finally:
                            for descriptor in reversed(directories):
                                os.close(descriptor)
                    os.fsync(output)
                    os.fsync(parent)
                    return {"revision": index["revision"],
                            "index_sha256": manifest["commits"][index["revision"]]["sha256"],
                            "destination": str(destination), "files": metadata}
                finally:
                    os.close(blobs)
        except BaseException:
            # Remove only the directory created by this call, never a swapped
            # path or an existing caller directory. rmtree itself uses dirfds.
            if created and identity is not None:
                current = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    shutil.rmtree(destination.name, dir_fd=parent)
            raise
        finally:
            if output is not None:
                os.close(output)
            os.close(parent)

    def files_for_paths(self, paths, revision=None):
        if not isinstance(paths, list) or len(paths) > MAX_CHANGE_FILES:
            raise ProjectWorkspaceError("invalid_project_files")
        names = [_path(path) for path in paths]
        if len(set(names)) != len(names):
            raise ProjectWorkspaceError("invalid_project_files")
        with self._locked() as (room, revisions, _):
            index = self._index(revisions, self._manifest(room), revision)
            files = {item["path"]: item for item in index["files"]}
            if any(path not in files for path in names):
                raise ProjectWorkspaceError("project_file_unavailable")
            blobs = _open_directory("blobs", parent=room)
            try:
                return [{**{key: files[path][key] for key in ("path", "sha256", "owner", "bytes")},
                         "content": self._content(blobs, files[path])} for path in names]
            finally:
                os.close(blobs)

    def metadata_for_paths(self, paths, revision=None):
        """Return exact immutable file metadata without decoding large/binary assets."""
        if not isinstance(paths, list) or len(paths) > MAX_CHANGE_FILES:
            raise ProjectWorkspaceError("invalid_project_files")
        names = [_path(path) for path in paths]
        if len(set(names)) != len(names):
            raise ProjectWorkspaceError("invalid_project_files")
        with self._locked() as (room, revisions, _):
            index = self._index(revisions, self._manifest(room), revision)
            selected = set(names)
            return [{key: entry[key] for key in ("path", "sha256", "owner", "bytes", "text")}
                    for entry in index["files"] if entry["path"] in selected]

    def assign_baseline_owners(self, mapping, *, allowed_owners):
        """Freeze known handoff authors on a fresh capture without changing bytes."""
        if not isinstance(mapping, dict) or not mapping or len(mapping) > MAX_SOURCE_FILES:
            raise ProjectWorkspaceError("invalid_project_request")
        for path, binding in mapping.items():
            _path(path)
            if (not isinstance(binding, dict) or set(binding) != {"sha256", "owner"}
                    or binding["owner"] not in allowed_owners or not safe_agent_id(binding["owner"])
                    or not isinstance(binding["sha256"], str) or not _HASH.fullmatch(binding["sha256"])):
                raise ProjectWorkspaceError("invalid_project_request")
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            if manifest["revision"] != 1:
                raise ProjectWorkspaceError("project_revision_conflict")
            index = self._index(revisions, manifest)
            files = {entry["path"]: entry for entry in index["files"]}
            if any(path not in files or files[path]["sha256"] != binding["sha256"]
                   or files[path]["owner"] is not None for path, binding in mapping.items()):
                raise ProjectWorkspaceError("project_owner_conflict")
            assigned = [{**entry, "owner": mapping.get(entry["path"], {}).get("owner", entry["owner"])}
                        for entry in index["files"]]
            self._publish_index(room, revisions, manifest, assigned, "native-baseline-owners",
                _hash(canonical_json(mapping)), index["source"], index["excluded"])

    def source_comparison(self, path, revision=None):
        """Compare a captured baseline with one exact private revision, never the source tree."""
        path = _path(path)
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            current = self._index(revisions, manifest, revision)
            result = {"available": False, "base_revision": 1, "revision": current["revision"],
                      "path": path, "before_sha256": None, "after_sha256": None,
                      "diff": "", "truncated": False, "scope": "captured_source"}
            if manifest["revision"] < 1 or current["source"] is None:
                return {**result, "reason": "project_not_seeded"}
            try:
                baseline = self._index(revisions, manifest, 1)
            except ProjectWorkspaceError as error:
                return {**result, "reason": error.code}
            except OSError:
                return {**result, "reason": "project_baseline_unavailable"}
            if baseline["source"] is None:
                return {**result, "reason": "project_not_seeded"}
            before = next((item for item in baseline["files"] if item["path"] == path), None)
            after = next((item for item in current["files"] if item["path"] == path), None)
            result.update(before_sha256=before["sha256"] if before else None,
                          after_sha256=after["sha256"] if after else None)
            if after is None:
                return {**result, "reason": "project_file_unavailable"}
            for entry in (before, after):
                if entry is not None and not entry["text"]:
                    return {**result, "reason": "project_binary_file"}
                if entry is not None and entry["bytes"] > MAX_FILE_BYTES:
                    return {**result, "reason": "project_limit_exceeded"}
        # Immutable revision numbers make these separate short reads safe even
        # if another task publishes a newer head between them.
        try:
            before_text = self.files_for_paths([path], revision=1)[0]["content"] if before else ""
            after_text = self.files_for_paths([path], revision=current["revision"])[0]["content"]
        except ProjectWorkspaceError as error:
            return {**result, "reason": error.code}
        lines = difflib.unified_diff(before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
                                     fromfile="captured_source/" + path if before else "/dev/null",
                                     tofile="revision-" + str(current["revision"]) + "/" + path, n=3)
        parts, size, truncated = [], 0, False
        for line in lines:
            if not line.endswith("\n"):
                line += "\n\\ No newline at end of file\n"
            raw = line.encode("utf-8")
            remaining = MAX_RESPONSE_BYTES - size
            if len(raw) > remaining:
                parts.append(raw[:remaining].decode("utf-8", errors="ignore"))
                truncated = True
                break
            parts.append(line)
            size += len(raw)
        return {**result, "available": True, "diff": "".join(parts), "truncated": truncated}

    def seed_folder(self, registration, *, cancelled=None):
        """Explicit-start boundary: copy eligible current working-tree bytes, never Git HEAD."""
        try:
            if (not isinstance(registration, dict) or registration.get("selection_kind") != "folder"
                    or registration.get("files") != [] or _uuid(registration.get("id")) is None
                    or _uuid(registration.get("id")) != registration.get("id")
                    or not isinstance(registration.get("name"), str)):
                raise ProjectWorkspaceError("invalid_project_request")
            source_path = _absolute_path(registration.get("path"))
        except ProjectCatalogError as error:
            raise ProjectWorkspaceError(error.code) from None
        stage_name = ".capture-" + str(uuid4())
        stage = parent = source = None
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            current = self._index(revisions, manifest)
            # A retry must not recapture a source that may now have different bytes.
            if current["source"] is not None:
                identity = {key: registration[key] for key in ("id", "name", "path", "selection_kind")}
                if all(current["source"].get(key) == value for key, value in identity.items()):
                    blobs = _open_directory("blobs", parent=room)
                    try:
                        return {**self._summary(current, blobs), "replayed": True}
                    finally:
                        os.close(blobs)
                raise ProjectWorkspaceError("project_proposal_conflict")
            if manifest["revision"] != 0:
                raise ProjectWorkspaceError("project_not_seeded")
            os.mkdir(stage_name, mode=0o700, dir_fd=room)
            parent = os.dup(room)
            stage = _open_directory(stage_name, parent=room)
        # Source I/O never holds the shared workspace lock. A concurrent status
        # read sees the empty baseline until a complete index is published.
        try:
            deadline = time.monotonic() + CAPTURE_SECONDS
            source = _open_absolute(source_path)
            files, excluded = self._capture_tree(source, stage, cancelled=cancelled)
            if cancelled is not None and cancelled():
                raise ProjectWorkspaceError("project_capture_cancelled")
            identity = {key: registration[key] for key in ("id", "name", "path", "selection_kind")}
            identity["source_hash"] = _hash(canonical_json(files))
            fingerprint = _hash(canonical_json(identity))
            with self._locked() as (room, revisions, _):
                manifest = self._manifest(room)
                current = self._index(revisions, manifest)
                if cancelled is not None and cancelled():
                    raise ProjectWorkspaceError("project_capture_cancelled")
                if current["source"] is not None or manifest["revision"] != 0:
                    raise ProjectWorkspaceError("project_revision_conflict")
                blobs = _open_directory("blobs", parent=room)
                promoted, publication_attempted = [], False
                try:
                    for sha in sorted({entry["sha256"] for entry in files}):
                        if cancelled is not None and cancelled():
                            raise ProjectWorkspaceError("project_capture_cancelled")
                        if time.monotonic() >= deadline:
                            raise ProjectWorkspaceError("project_capture_limit")
                        try:
                            os.link(sha, sha, src_dir_fd=stage, dst_dir_fd=blobs, follow_symlinks=False)
                            promoted.append(sha)
                        except FileExistsError:
                            entry = next(item for item in files if item["sha256"] == sha)
                            with os.fdopen(self._blob(blobs, entry), "rb") as stream:
                                if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                                    raise ProjectWorkspaceError("project_storage_corrupt")
                        os.unlink(sha, dir_fd=stage)
                    os.fsync(blobs)
                    if cancelled is not None and cancelled():
                        raise ProjectWorkspaceError("project_capture_cancelled")
                    publication_attempted = True
                    result = self._publish_index(room, revisions, manifest, files, "folder-import",
                                                 fingerprint, identity, excluded)
                    return self._summary(result, blobs)
                finally:
                    if not publication_attempted:
                        # Only this capture's names may be removed. Once index
                        # publication is attempted its outcome can be uncertain.
                        for sha in promoted:
                            os.unlink(sha, dir_fd=blobs)
                        os.fsync(blobs)
                    os.close(blobs)
        except ProjectCatalogError as error:
            raise ProjectWorkspaceError(error.code) from None
        except OSError:
            raise ProjectWorkspaceError("project_file_unavailable") from None
        finally:
            for descriptor in (source, stage):
                if descriptor is not None:
                    os.close(descriptor)
            if parent is not None:
                try:
                    shutil.rmtree(stage_name, dir_fd=parent)
                finally:
                    os.close(parent)

    def _capture_tree(self, source, blobs, *, cancelled=None):
        deadline = time.monotonic() + CAPTURE_SECONDS
        observed, files, excluded = {}, [], Counter()
        seen, total, scanned = set(), 0, 0

        def check_budget():
            if cancelled is not None and cancelled():
                raise ProjectWorkspaceError("project_capture_cancelled")
            if time.monotonic() >= deadline:
                raise ProjectWorkspaceError("project_capture_limit")

        def directory_names(directory):
            nonlocal scanned
            names = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_budget()
                    scanned += 1
                    if scanned > MAX_SOURCE_FILES * 4:
                        raise ProjectWorkspaceError("project_capture_limit")
                    names.append(entry.name)
            return sorted(names)

        def walk(directory, prefix, rules):
            nonlocal total
            check_budget()
            before = os.fstat(directory)
            names = directory_names(directory)
            local_rules = list(rules)
            if ".gitignore" in names:
                metadata = os.stat(".gitignore", dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode):
                    descriptor = os.open(".gitignore", _FILE_FLAGS, dir_fd=directory)
                    try:
                        if os.fstat(descriptor).st_size > MAX_IGNORE_BYTES:
                            raise ProjectWorkspaceError("project_capture_limit")
                        raw = os.read(descriptor, MAX_IGNORE_BYTES + 1)
                        after = os.fstat(descriptor)
                        if _attributes(metadata) != _attributes(after) or len(raw) != after.st_size:
                            raise ProjectWorkspaceError("project_source_changed")
                        observed[prefix + ".gitignore"] = _attributes(after)
                        try:
                            patterns = raw.decode("utf-8").splitlines()
                            if len(patterns) > MAX_IGNORE_PATTERNS:
                                raise ProjectWorkspaceError("project_capture_limit")
                            local_rules.append((prefix, GitIgnoreSpec.from_lines(patterns, backend="simple")))
                            check_budget()
                        except (UnicodeError, ValueError):
                            raise ProjectWorkspaceError("invalid_project_files") from None
                    finally:
                        os.close(descriptor)
            for name in names:
                check_budget()
                path = prefix + name
                try:
                    _path(path)
                except ProjectWorkspaceError:
                    excluded["invalid_path"] += 1
                    continue
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                observed[path] = _attributes(metadata)
                reason = _excluded(path)
                if reason:
                    excluded[reason] += 1
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    excluded["symlink"] += 1
                    continue
                directory_entry = stat.S_ISDIR(metadata.st_mode)
                ignored = False
                for base, spec in local_rules:
                    relative = path[len(base):] + ("/" if directory_entry else "")
                    match = spec.check_file(relative)
                    if match.include is not None:
                        ignored = match.include
                if ignored:
                    excluded["gitignore"] += 1
                    continue
                if directory_entry:
                    child = os.open(name, _DIR_FLAGS, dir_fd=directory)
                    try:
                        if _attributes(os.fstat(child)) != _attributes(metadata):
                            raise ProjectWorkspaceError("project_source_changed")
                        walk(child, path + "/", local_rules)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(metadata.st_mode):
                    if len(files) >= MAX_SOURCE_FILES or total + metadata.st_size > MAX_SOURCE_BYTES:
                        raise ProjectWorkspaceError("project_capture_limit")
                    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
                    try:
                        if _attributes(os.fstat(descriptor)) != _attributes(metadata):
                            raise ProjectWorkspaceError("project_source_changed")
                        entry = self._capture_file(descriptor, blobs, metadata, check_budget)
                        if entry is None:
                            excluded["sensitive_content"] += 1
                        else:
                            files.append({"path": path, **entry, "owner": None, "changed": False})
                            total += entry["bytes"]
                            if path.casefold() in seen:
                                raise ProjectWorkspaceError("project_file_conflict")
                            seen.add(path.casefold())
                    finally:
                        os.close(descriptor)
                else:
                    excluded["special_file"] += 1
            if _attributes(os.fstat(directory)) != _attributes(before) or directory_names(directory) != names:
                raise ProjectWorkspaceError("project_source_changed")
            observed[prefix.rstrip("/")] = _attributes(before)

        try:
            walk(source, "", [])
            # Re-open every recorded path from the anchored root, without following
            # links. This catches changes to files already copied earlier in the walk.
            for path, expected in observed.items():
                check_budget()
                directory = os.dup(source)
                try:
                    parts = path.split("/") if path else []
                    for part in parts[:-1]:
                        child = os.open(part, _DIR_FLAGS, dir_fd=directory)
                        os.close(directory)
                        directory = child
                    actual = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False) if parts else os.fstat(directory)
                    if _attributes(actual) != expected:
                        raise ProjectWorkspaceError("project_source_changed")
                finally:
                    os.close(directory)
        except OSError:
            raise ProjectWorkspaceError("project_source_changed") from None
        return sorted(files, key=lambda item: item["path"]), dict(sorted(excluded.items()))

    def _capture_file(self, descriptor, blobs, metadata, check_budget):
        temporary = ".capture-" + str(uuid4())
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600, dir_fd=blobs)
        digest, size, text, sensitive, tail = hashlib.sha256(), 0, True, False, ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with os.fdopen(output, "wb") as stream:
                while True:
                    check_budget()
                    chunk = os.read(descriptor, 256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > metadata.st_size or size > MAX_SOURCE_BYTES:
                        raise ProjectWorkspaceError("project_source_changed")
                    digest.update(chunk)
                    stream.write(chunk)
                    if text:
                        try:
                            decoded = decoder.decode(chunk)
                            text = not any(ord(char) < 32 and char not in "\r\n\t" for char in decoded)
                            sensitive = sensitive or bool(_SECRET.search(tail + decoded))
                            tail = decoded[-16384:]
                        except UnicodeError:
                            text = False
                if text:
                    try:
                        sensitive = sensitive or bool(_SECRET.search(tail + decoder.decode(b"", final=True)))
                    except UnicodeError:
                        text = False
                stream.flush()
                os.fsync(stream.fileno())
            if size != metadata.st_size or _attributes(os.fstat(descriptor)) != _attributes(metadata):
                raise ProjectWorkspaceError("project_source_changed")
            if sensitive:
                return None
            sha = digest.hexdigest()
            try:
                # Hard-link only within the private blob store, then remove the
                # temporary name before any blob is opened by a consumer.
                os.link(temporary, sha, src_dir_fd=blobs, dst_dir_fd=blobs, follow_symlinks=False)
            except FileExistsError:
                with os.fdopen(self._blob(blobs, {"sha256": sha, "bytes": size}), "rb") as existing:
                    existing_hash = hashlib.file_digest(existing, "sha256").hexdigest()
                if existing_hash != sha:
                    raise ProjectWorkspaceError("project_storage_corrupt")
            return {"sha256": sha, "bytes": size, "text": text,
                    "executable": bool(metadata.st_mode & 0o111)}
        finally:
            os.unlink(temporary, dir_fd=blobs)
            os.fsync(blobs)

    def inspect(self, request, revision=None):
        if (not isinstance(request, dict) or set(request) != {"kind", "path", "query", "offset", "limit"}
                or request["kind"] not in {"list", "read", "search"}
                or type(request["offset"]) is not int or request["offset"] < 0
                or type(request["limit"]) is not int or not 1 <= request["limit"] <= 200
                or (request["query"] is not None and not isinstance(request["query"], str))):
            raise ProjectWorkspaceError("invalid_project_request")
        path = _path(request["path"], root=request["kind"] != "read")
        excluded = _excluded(path)
        if excluded and _excluded(path, captured=True):
            raise ProjectWorkspaceError("project_sensitive_content")
        with self._locked() as (room, revisions, _):
            index = self._index(revisions, self._manifest(room), revision)
            selected = [item for item in index["files"] if not path or item["path"] == path
                        or item["path"].startswith(path + "/")]
            if excluded and not selected:
                raise ProjectWorkspaceError("project_sensitive_content")
            if request["kind"] == "list":
                page, used = [], 0
                for item in selected[request["offset"]:request["offset"] + request["limit"]]:
                    size = len(canonical_json(item))
                    if used + size > MAX_RESPONSE_BYTES:
                        break
                    page.append(item)
                    used += size
                next_offset = request["offset"] + len(page)
                return {"revision": index["revision"], "entries": page, "total": len(selected),
                        "next_offset": next_offset if next_offset < len(selected) else None}
            blobs = _open_directory("blobs", parent=room)
            try:
                if request["kind"] == "read":
                    entry = next((item for item in selected if item["path"] == path), None)
                    if entry is None:
                        raise ProjectWorkspaceError("project_file_unavailable")
                    return self._read_lines(blobs, entry, index["revision"], request["offset"], request["limit"])
                return self._search(blobs, selected, index["revision"], request)
            finally:
                os.close(blobs)

    def _read_lines(self, blobs, entry, revision, offset, limit):
        if not entry["text"]:
            raise ProjectWorkspaceError("project_binary_file")
        lines, used, truncated, next_offset = [], 0, False, None
        number, scanned, deadline = 0, 0, time.monotonic() + 2
        with os.fdopen(self._verified_blob(blobs, entry), "rb") as stream:
            while True:
                raw = stream.readline(MAX_RESPONSE_BYTES + 1)
                if not raw:
                    break
                scanned += len(raw)
                if scanned > MAX_SEARCH_BYTES or time.monotonic() >= deadline:
                    raise ProjectWorkspaceError("project_read_limit_exceeded")
                if number < offset:
                    if raw.endswith(b"\n"):
                        number += 1
                    continue
                if len(lines) >= limit:
                    truncated, next_offset = True, number
                    break
                remaining = MAX_RESPONSE_BYTES - used
                if len(raw) > remaining:
                    # A single long line is explicitly a partial line, never
                    # represented as complete input for exact edits.
                    piece = raw[:remaining].decode("utf-8", errors="ignore")
                    lines.append(piece)
                    truncated, next_offset = True, number
                    break
                lines.append(raw.decode("utf-8"))
                used += len(raw)
                number += 1
        return {"revision": revision, "path": entry["path"], "sha256": entry["sha256"],
                "owner": entry["owner"], "offset": offset, "content": "".join(lines),
                "truncated": truncated, "next_offset": next_offset}

    def _search(self, blobs, selected, revision, request):
        query = request["query"]
        try:
            query_size = len(query.encode("utf-8")) if isinstance(query, str) else 0
        except UnicodeError:
            raise ProjectWorkspaceError("invalid_project_request") from None
        if not query or query_size > 1024 or "\n" in query or "\r" in query:
            raise ProjectWorkspaceError("invalid_project_request")
        matches, seen, scanned, used = [], 0, 0, 0
        deadline = time.monotonic() + 2
        for entry in selected:
            if not entry["text"]:
                continue
            if scanned + entry["bytes"] > MAX_SEARCH_BYTES:
                raise ProjectWorkspaceError("project_search_limit_exceeded")
            with os.fdopen(self._verified_blob(blobs, entry), "rb") as stream:
                number = -1
                while True:
                    raw = stream.readline(MAX_FILE_BYTES + 1)
                    if not raw:
                        break
                    number += 1
                    if len(raw) > MAX_FILE_BYTES:
                        raise ProjectWorkspaceError("project_search_limit_exceeded")
                    scanned += len(raw)
                    if scanned > MAX_SEARCH_BYTES or time.monotonic() >= deadline:
                        raise ProjectWorkspaceError("project_search_limit_exceeded")
                    line = raw.decode("utf-8")
                    if query not in line:
                        continue
                    seen += 1
                    if seen <= request["offset"]:
                        continue
                    result = {"path": entry["path"], "sha256": entry["sha256"], "line": number,
                              "text": line[:1000], "line_truncated": len(line) > 1000}
                    size = len(canonical_json(result))
                    if len(matches) >= request["limit"] or used + size > MAX_RESPONSE_BYTES:
                        return {"revision": revision, "matches": matches,
                                "next_offset": request["offset"] + len(matches)}
                    matches.append(result)
                    used += size
        return {"revision": revision, "matches": matches, "next_offset": None}

    def apply(self, provider, proposal_id, base_revision, files, *, allow_disjoint=True, ownership_grants=None):
        if (not safe_agent_id(provider) or not isinstance(proposal_id, str) or not _ID.fullmatch(proposal_id)
                or type(base_revision) is not int or base_revision < 0 or type(allow_disjoint) is not bool
                or not isinstance(files, list) or not 1 <= len(files) <= MAX_CHANGE_FILES):
            raise ProjectWorkspaceError("invalid_project_request")
        prepared, names, total = [], set(), 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "previous_sha256", "content"}:
                raise ProjectWorkspaceError("invalid_project_files")
            path = _path(item["path"])
            # The ZIP exporter owns this root manifest name. A directory with
            # that name would also collide when the archive is extracted.
            if path.split("/", 1)[0].casefold() == ".beanest-export.json":
                raise ProjectWorkspaceError("invalid_project_files")
            if _excluded(path):
                raise ProjectWorkspaceError("project_sensitive_content")
            if path.casefold() in names or not isinstance(item["content"], str):
                raise ProjectWorkspaceError("invalid_project_files")
            names.add(path.casefold())
            try:
                raw = item["content"].encode("utf-8")
            except UnicodeError:
                raise ProjectWorkspaceError("invalid_project_files") from None
            if any(ord(char) < 32 and char not in "\t\r\n" for char in item["content"]):
                raise ProjectWorkspaceError("invalid_project_files")
            if _SECRET.search(item["content"]):
                raise ProjectWorkspaceError("project_sensitive_content")
            if len(raw) > MAX_FILE_BYTES:
                raise ProjectWorkspaceError("project_limit_exceeded")
            previous = item["previous_sha256"]
            if previous is not None and (not isinstance(previous, str) or not _HASH.fullmatch(previous)):
                raise ProjectWorkspaceError("invalid_project_files")
            total += len(raw)
            prepared.append({"path": path, "previous_sha256": previous, "content": item["content"]})
        if total > MAX_CHANGE_BYTES:
            raise ProjectWorkspaceError("project_limit_exceeded")
        prepared.sort(key=lambda item: item["path"])
        parameters = {"provider": provider, "base_revision": base_revision,
                      "files": prepared, "allow_disjoint": allow_disjoint}
        if ownership_grants:
            parameters["ownership_grants"] = ownership_grants
        fingerprint = _hash(canonical_json(parameters))
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            for revision, commit in enumerate(manifest["commits"]):
                if commit["proposal_id"] == proposal_id:
                    if commit["fingerprint"] != fingerprint:
                        raise ProjectWorkspaceError("project_proposal_conflict")
                    blobs = _open_directory("blobs", parent=room)
                    try:
                        return {**self._summary(self._index(revisions, manifest, revision), blobs), "replayed": True}
                    finally:
                        os.close(blobs)
            base = self._index(revisions, manifest, base_revision)
            current = self._index(revisions, manifest)
            if current["source"] is None:
                raise ProjectWorkspaceError("project_not_seeded")
            if not allow_disjoint and manifest["revision"] != base_revision:
                raise ProjectWorkspaceError("project_revision_conflict")
            original = {item["path"]: item for item in base["files"]}
            merged = {item["path"]: item for item in current["files"]}
            folded = {path.casefold(): path for path in merged}
            for item in prepared:
                path, previous = item["path"], original.get(item["path"])
                if ((path.casefold() in folded and folded[path.casefold()] != path)
                        or item["previous_sha256"] != (previous["sha256"] if previous else None)):
                    raise ProjectWorkspaceError("project_file_conflict")
                grant = (ownership_grants or {}).get(path, {})
                authorized_transfer = (previous is not None and grant.get("old_owner") == previous["owner"]
                                       and grant.get("new_owner") == provider and grant.get("sha256") == previous["sha256"])
                if previous is not None and (not previous["text"] or previous["owner"] not in {None, provider} and not authorized_transfer):
                    raise ProjectWorkspaceError("project_owner_conflict")
                for number in range(base_revision + 1, manifest["revision"] + 1):
                    intervening = {entry["path"]: entry for entry in self._index(revisions, manifest, number)["files"]}
                    if intervening.get(path) != previous:
                        raise ProjectWorkspaceError("project_file_conflict")
                # File/directory collisions must be refused even on a case-insensitive host.
                lower = path.casefold()
                if any(other != lower and (other.startswith(lower + "/") or lower.startswith(other + "/"))
                       for other in folded):
                    raise ProjectWorkspaceError("project_file_conflict")
                merged[path] = {"path": path, "sha256": _hash(item["content"].encode("utf-8")),
                                "bytes": len(item["content"].encode("utf-8")), "text": True,
                                "owner": provider, "changed": True}
                if previous is not None and "executable" in previous:
                    merged[path]["executable"] = previous["executable"]
                folded[path.casefold()] = path
            if len(merged) > MAX_SOURCE_FILES or sum(item["bytes"] for item in merged.values()) > MAX_SOURCE_BYTES:
                raise ProjectWorkspaceError("project_limit_exceeded")
            if sum(item["bytes"] for item in merged.values() if item["changed"]) > MAX_CHANGED_TOTAL_BYTES:
                raise ProjectWorkspaceError("project_limit_exceeded")
            blobs = _open_directory("blobs", parent=room)
            try:
                for item in prepared:
                    self._store_bytes(blobs, item["content"].encode("utf-8"))
                result = self._publish_index(room, revisions, manifest, sorted(merged.values(), key=lambda item: item["path"]),
                                             proposal_id, fingerprint, current["source"], current["excluded"])
                return {**self._summary(result, blobs), "replayed": False}
            finally:
                os.close(blobs)
