"""Atomic, versioned file proposals; this is not an execution sandbox.

The service supplies a trusted private runtime root, never a source project
path. Python or imported text files remain data in immutable JSON revisions.
Text drafts retain their baseline and diffs without writing to their source.
A separate execution layer may materialize explicitly supported snapshots.
"""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from uuid import UUID, uuid4

from .agent_registry import safe_agent_id
from .project_catalog import (MAX_TEXT_BYTES, ProjectCatalogError, source_hash,
                              validate_text_path, validate_text_content)


FORMAT = "local-agent-toy-project-v1"
TEXT_FORMAT = "beanest-text-project-v1"
NEW_TEXT_FORMAT = "beanest-text-artifacts-v1"
MAX_FILES = 16
MAX_BYTES = 64 * 1024
MAX_METADATA = 1024 * 1024
LOCK_SECONDS = 5
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\.py\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


class ProjectWorkspaceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _uuid(value) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _private(descriptor, *, directory=False):
    metadata = os.fstat(descriptor)
    kind = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (not kind or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
            or (not directory and metadata.st_nlink != 1)):
        raise ProjectWorkspaceError("project_unsafe_path")


def _open_directory(name, *, parent=None) -> int:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise ProjectWorkspaceError("project_unknown_workspace") from None
    except OSError:
        raise ProjectWorkspaceError("project_unsafe_path") from None
    try:
        _private(descriptor, directory=True)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _path_and_content(path, content, *, kind="python") -> bytes:
    if kind == "text":
        try:
            validate_text_path(path)
            return validate_text_content(content)
        except ProjectCatalogError as error:
            raise ProjectWorkspaceError(error.code) from None
    if not isinstance(path, str) or not _PATH.fullmatch(path):
        raise ProjectWorkspaceError("invalid_project_files")
    if not isinstance(content, str) or "\0" in content:
        raise ProjectWorkspaceError("invalid_project_files")
    try:
        raw = content.encode("utf-8")
    except UnicodeError:
        raise ProjectWorkspaceError("invalid_project_files") from None
    if len(raw) > MAX_BYTES:
        raise ProjectWorkspaceError("project_limit_exceeded")
    return raw


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


class ProjectWorkspace:
    def __init__(self, root: Path, room_id: str, *, create: bool = True, kind: str = "python",
                 allow_new_text_files: bool = False):
        try:
            self.root = Path(root)
        except TypeError:
            raise ProjectWorkspaceError("invalid_project_root") from None
        self.room_id = _uuid(room_id)
        if (kind not in ("python", "text") or type(allow_new_text_files) is not bool
                or allow_new_text_files and kind != "text"):
            raise ProjectWorkspaceError("invalid_project_request")
        self.kind = kind
        self.allow_new_text_files = allow_new_text_files
        self.format = NEW_TEXT_FORMAT if allow_new_text_files else TEXT_FORMAT if kind == "text" else FORMAT
        self.max_bytes = MAX_TEXT_BYTES if kind == "text" else MAX_BYTES
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ProjectWorkspaceError("invalid_project_root")
        if self.room_id is None:
            raise ProjectWorkspaceError("invalid_project_room")
        if type(create) is not bool:
            raise ProjectWorkspaceError("invalid_project_request")
        try:
            with self._locked(create=create) as (room, revisions, created):
                if created:
                    manifest = {"format": self.format, "room_id": self.room_id, "revision": -1, "commits": []}
                    if self.kind == "text":
                        manifest["kind"] = "text"
                    self._publish(room, revisions, manifest, [], None, None)
                else:
                    self._manifest(room)
        except OSError:
            raise ProjectWorkspaceError("project_storage_unavailable") from None

    def _root(self, *, create=False) -> int:
        # Open every ancestor without following links. Only the selected root
        # must be private; ordinary OS ancestors need not have mode 0700.
        descriptor = os.open(self.root.anchor, _DIRECTORY_FLAGS)
        try:
            for part in self.root.parts[1:]:
                try:
                    next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise ProjectWorkspaceError("project_unknown_workspace") from None
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                    next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            _private(descriptor, directory=True)
            return descriptor
        except OSError:
            os.close(descriptor)
            raise ProjectWorkspaceError("project_unsafe_path") from None
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _locked(self, *, create=False):
        root = lock = room = revisions = None
        staging = None
        initial_rename_attempted = False
        try:
            root = self._root(create=create)
            lock_name = "." + self.room_id + ".lock"
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            if create:
                try:
                    lock = os.open(lock_name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
                except FileExistsError:
                    lock = os.open(lock_name, flags, dir_fd=root)
            else:
                try:
                    lock = os.open(lock_name, flags, dir_fd=root)
                except FileNotFoundError:
                    raise ProjectWorkspaceError("project_unknown_workspace") from None
            _private(lock)
            deadline = time.monotonic() + LOCK_SECONDS
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ProjectWorkspaceError("project_busy") from None
                    time.sleep(0.01)
            created = False
            if create:
                try:
                    os.stat(self.room_id, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    candidate = ".init-" + str(uuid4())
                    os.mkdir(candidate, mode=0o700, dir_fd=root)
                    staging = candidate
                    created = True
            room = _open_directory(staging if created else self.room_id, parent=root)
            if created:
                os.mkdir("revisions", mode=0o700, dir_fd=room)
            else:
                # Do not adopt an arbitrary pre-existing directory, even empty.
                try:
                    os.stat("manifest.json", dir_fd=room, follow_symlinks=False)
                except FileNotFoundError:
                    raise ProjectWorkspaceError("project_unknown_workspace") from None
            revisions = _open_directory("revisions", parent=room)
            yield room, revisions, created
            if staging is not None:
                # The formal UUID appears only after its complete zero revision.
                # Existing unknown UUID directories are never adopted or erased.
                try:
                    os.stat(self.room_id, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ProjectWorkspaceError("project_unknown_workspace")
                initial_rename_attempted = True
                os.rename(staging, self.room_id, src_dir_fd=root, dst_dir_fd=root)
                staging = None
                try:
                    os.fsync(root)
                except OSError:
                    raise ProjectWorkspaceError("project_commit_unconfirmed") from None
        except ProjectWorkspaceError as error:
            if staging is not None and not initial_rename_attempted and error.code == "project_commit_unconfirmed":
                raise ProjectWorkspaceError("project_storage_unavailable") from None
            raise
        except OSError as error:
            if initial_rename_attempted:
                raise ProjectWorkspaceError("project_commit_unconfirmed") from None
            code = "project_unsafe_path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "project_storage_unavailable"
            raise ProjectWorkspaceError(code) from None
        finally:
            if staging is not None and root is not None:
                try:
                    shutil.rmtree(staging, dir_fd=root)
                except OSError:
                    pass
            for descriptor in (revisions, room, lock, root):
                if descriptor is not None:
                    os.close(descriptor)

    def _read(self, directory, filename) -> bytes:
        descriptor = None
        try:
            descriptor = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
            _private(descriptor)
            if os.fstat(descriptor).st_size > MAX_METADATA:
                raise ProjectWorkspaceError("project_corrupt")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_METADATA + 1)
            if len(raw) > MAX_METADATA:
                raise ProjectWorkspaceError("project_corrupt")
            return raw
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _decode(self, raw):
        try:
            return json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ProjectWorkspaceError("project_corrupt") from None

    def _manifest(self, room):
        value = self._decode(self._read(room, "manifest.json"))
        expected = {"format", "room_id", "revision", "commits"} | ({"kind"} if self.kind == "text" else set())
        if (not isinstance(value, dict) or set(value) != expected
                or value["format"] != self.format or value.get("kind", "python") != self.kind
                or value["room_id"] != self.room_id
                or type(value["revision"]) is not int or not isinstance(value["commits"], list)
                or value["revision"] != len(value["commits"]) - 1 or value["revision"] < 0):
            raise ProjectWorkspaceError("project_corrupt")
        proposals, identifiers = set(), set()
        for number, commit in enumerate(value["commits"]):
            if (not isinstance(commit, dict) or set(commit) != {"snapshot_id", "sha256", "proposal_id", "fingerprint"}
                    or not isinstance(commit["snapshot_id"], str)
                    or _uuid(commit["snapshot_id"]) != commit["snapshot_id"]
                    or commit["snapshot_id"] in identifiers
                    or not isinstance(commit["sha256"], str) or not _HASH.fullmatch(commit["sha256"])):
                raise ProjectWorkspaceError("project_corrupt")
            identifiers.add(commit["snapshot_id"])
            if number == 0:
                valid = commit["proposal_id"] is None and commit["fingerprint"] is None
            else:
                valid = (isinstance(commit["proposal_id"], str) and _ID.fullmatch(commit["proposal_id"])
                         and commit["proposal_id"] not in proposals and isinstance(commit["fingerprint"], str)
                         and _HASH.fullmatch(commit["fingerprint"]))
                if valid:
                    proposals.add(commit["proposal_id"])
            if not valid:
                raise ProjectWorkspaceError("project_corrupt")
        return value

    def _snapshot(self, revisions, manifest, revision):
        if type(revision) is not int or not 0 <= revision <= manifest["revision"]:
            raise ProjectWorkspaceError("project_revision_not_found")
        commit = manifest["commits"][revision]
        raw = self._read(revisions, commit["snapshot_id"] + ".json")
        if _digest(raw) != commit["sha256"]:
            raise ProjectWorkspaceError("project_corrupt")
        value = self._decode(raw)
        expected = {"format", "room_id", "revision", "files"} | ({"kind", "source"} if self.kind == "text" else set())
        if (not isinstance(value, dict) or set(value) != expected
                or value["format"] != self.format or value.get("kind", "python") != self.kind
                or value["room_id"] != self.room_id
                or type(value["revision"]) is not int or value["revision"] != revision
                or not isinstance(value["files"], list) or len(value["files"]) > MAX_FILES):
            raise ProjectWorkspaceError("project_corrupt")
        seen, size = set(), 0
        for item in value["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "owner", "content"}:
                raise ProjectWorkspaceError("project_corrupt")
            try:
                content = _path_and_content(item["path"], item["content"], kind=self.kind)
            except ProjectWorkspaceError:
                raise ProjectWorkspaceError("project_corrupt") from None
            if (item["path"].casefold() in seen or not safe_agent_id(item["owner"])
                    or item["sha256"] != _digest(content)):
                raise ProjectWorkspaceError("project_corrupt")
            seen.add(item["path"].casefold())
            size += len(content)
        if size > self.max_bytes:
            raise ProjectWorkspaceError("project_corrupt")
        result = {"revision": revision, "files": value["files"]}
        if self.kind == "text":
            source = value["source"]
            if revision == 0:
                if source is not None or value["files"]:
                    raise ProjectWorkspaceError("project_corrupt")
            elif self.allow_new_text_files:
                if source is not None:
                    raise ProjectWorkspaceError("project_corrupt")
            else:
                try:
                    self._validate_source(source)
                except ProjectWorkspaceError:
                    raise ProjectWorkspaceError("project_corrupt") from None
            result["source"] = source
        return result

    @staticmethod
    def _write_new(directory, name, raw):
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # O_EXCL succeeded, so this helper owns exactly this incomplete file.
            try:
                os.unlink(name, dir_fd=directory)
            except OSError:
                pass
            raise

    def _publish(self, room, revisions, manifest, files, proposal_id, fingerprint, *, source=None):
        number = manifest["revision"] + 1
        identifier, temporary = str(uuid4()), ".manifest-" + str(uuid4())
        snapshot = {"format": self.format, "room_id": self.room_id, "revision": number, "files": files}
        if self.kind == "text":
            snapshot.update(kind="text", source=source)
        raw = _json(snapshot)
        commit = {"snapshot_id": identifier, "sha256": _digest(raw),
                  "proposal_id": proposal_id, "fingerprint": fingerprint}
        updated = {**manifest, "revision": number, "commits": [*manifest["commits"], commit]}
        encoded = _json(updated)
        if len(encoded) > MAX_METADATA:
            raise ProjectWorkspaceError("project_limit_exceeded")
        published = attempted_publish = revision_written = temporary_written = False
        try:
            self._write_new(revisions, identifier + ".json", raw)
            revision_written = True
            os.fsync(revisions)
            self._write_new(room, temporary, encoded)
            temporary_written = True
            attempted_publish = True
            os.replace(temporary, "manifest.json", src_dir_fd=room, dst_dir_fd=room)
            published = True
            os.fsync(room)
        except OSError:
            if published:
                raise ProjectWorkspaceError("project_commit_unconfirmed") from None
            if attempted_publish:
                try:
                    active = self._manifest(room)
                except (ProjectWorkspaceError, OSError):
                    raise ProjectWorkspaceError("project_commit_unconfirmed") from None
                if any(item["snapshot_id"] == identifier for item in active["commits"]):
                    raise ProjectWorkspaceError("project_commit_unconfirmed") from None
            # A revision is invisible until the manifest references it. A crash
            # may leave an orphan, which future reads and commits safely ignore.
            if revision_written:
                try:
                    os.unlink(identifier + ".json", dir_fd=revisions)
                except OSError:
                    pass
            raise ProjectWorkspaceError("project_storage_unavailable") from None
        finally:
            if temporary_written:
                try:
                    os.unlink(temporary, dir_fd=room)
                except OSError:
                    pass
        result = {"revision": number, "files": files}
        if self.kind == "text":
            result["source"] = source
        return result

    @staticmethod
    def _validate_source(source):
        if (not isinstance(source, dict) or set(source) != {"id", "name", "path", "source_hash", "files"}
                or not isinstance(source["id"], str) or _uuid(source["id"]) != source["id"]
                or not isinstance(source["name"], str)
                or not 1 <= len(source["name"]) <= 80 or any(ord(char) < 32 for char in source["name"])
                or not isinstance(source["path"], str)
                or not Path(source["path"]).is_absolute() or ".." in Path(source["path"]).parts
                or "\0" in source["path"] or source["path"] == Path(source["path"]).anchor
                or not isinstance(source["files"], list) or not 1 <= len(source["files"]) <= 12):
            raise ProjectWorkspaceError("invalid_project_files")
        try:
            validate_text_content(source["name"])
            source["path"].encode("utf-8")
        except (ProjectCatalogError, UnicodeError):
            raise ProjectWorkspaceError("invalid_project_files") from None
        seen = set()
        for item in source["files"]:
            if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                    or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])):
                raise ProjectWorkspaceError("invalid_project_files")
            try:
                validate_text_path(item["path"])
            except ProjectCatalogError:
                raise ProjectWorkspaceError("invalid_project_files") from None
            if item["path"].casefold() in seen:
                raise ProjectWorkspaceError("invalid_project_files")
            seen.add(item["path"].casefold())
        if source["source_hash"] != source_hash(source["files"]):
            raise ProjectWorkspaceError("invalid_project_files")

    @staticmethod
    def _diff(path, before, after):
        lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                     fromfile="a/" + path, tofile="b/" + path)
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in lines)

    def _decorate(self, result, revisions, manifest):
        if self.kind != "text":
            return result
        changes = []
        if result["revision"] > 0:
            baseline = self._snapshot(revisions, manifest, 0 if self.allow_new_text_files else 1)
            if result["source"] != baseline["source"]:
                raise ProjectWorkspaceError("project_corrupt")
            if not self.allow_new_text_files:
                source_files = {item["path"]: item["sha256"] for item in baseline["source"]["files"]}
                if {item["path"]: item["sha256"] for item in baseline["files"]} != source_files:
                    raise ProjectWorkspaceError("project_corrupt")
            original = {item["path"]: item for item in baseline["files"]}
            for item in result["files"]:
                previous = original.get(item["path"])
                if previous is not None and previous["sha256"] == item["sha256"]:
                    continue
                changes.append({
                    "path": item["path"], "status": "modified" if previous else "added",
                    "previous_sha256": previous["sha256"] if previous else None,
                    "sha256": item["sha256"],
                    "diff": self._diff(item["path"], previous["content"] if previous else "", item["content"]),
                })
        return {**result, "changes": changes}

    def seed(self, capture, owner) -> dict:
        if self.kind != "text" or self.allow_new_text_files or not safe_agent_id(owner) or not isinstance(capture, dict):
            raise ProjectWorkspaceError("invalid_project_request")
        if set(capture) != {"id", "name", "path", "files", "created_at", "source_hash"} or not isinstance(capture["files"], list):
            raise ProjectWorkspaceError("invalid_project_files")
        source = {key: capture[key] for key in ("id", "name", "path", "source_hash")}
        source["files"] = [{"path": item.get("path"), "sha256": item.get("sha256")}
                           for item in capture["files"] if isinstance(item, dict)]
        self._validate_source(source)
        files, total = [], 0
        for item in capture["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "content"}:
                raise ProjectWorkspaceError("invalid_project_files")
            raw = _path_and_content(item["path"], item["content"], kind="text")
            if _digest(raw) != item["sha256"]:
                raise ProjectWorkspaceError("invalid_project_files")
            total += len(raw)
            files.append({**item, "owner": owner})
        if total > self.max_bytes:
            raise ProjectWorkspaceError("project_limit_exceeded")
        files.sort(key=lambda item: item["path"])
        source["files"].sort(key=lambda item: item["path"])
        fingerprint = _digest(_json({"source": source, "owner": owner}))
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            if manifest["revision"] > 0:
                commit = manifest["commits"][1]
                if commit["proposal_id"] == "source-import" and commit["fingerprint"] == fingerprint:
                    return {**self._decorate(self._snapshot(revisions, manifest, 1), revisions, manifest), "replayed": True}
                raise ProjectWorkspaceError("project_revision_conflict")
            result = self._publish(room, revisions, manifest, files, "source-import", fingerprint, source=source)
            return {**self._decorate(result, revisions, self._manifest(room)), "replayed": False}

    def assign_source_owners(self, mapping, *, allowed_owners) -> dict:
        """Assign selected baseline paths once, before any text proposal exists.

        The caller supplies the frozen room roster. This changes only logical
        ownership in the isolated snapshot, never the selected source files.
        """
        if (self.kind != "text" or not isinstance(mapping, dict) or not mapping
                or not isinstance(allowed_owners, (list, tuple)) or not 1 <= len(allowed_owners) <= 8
                or any(not safe_agent_id(owner) for owner in allowed_owners)
                or len(set(allowed_owners)) != len(allowed_owners)):
            raise ProjectWorkspaceError("invalid_project_request")
        for path, owner in mapping.items():
            try:
                validate_text_path(path)
            except ProjectCatalogError:
                raise ProjectWorkspaceError("invalid_project_files") from None
            if not safe_agent_id(owner) or owner not in allowed_owners:
                raise ProjectWorkspaceError("invalid_project_request")
        fingerprint = _digest(_json({"source_owners": mapping}))
        proposal_id = "source-owners"
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            if manifest["revision"] < 1:
                raise ProjectWorkspaceError("project_not_seeded")
            baseline = self._snapshot(revisions, manifest, 1)
            self._decorate(baseline, revisions, manifest)
            paths = {item["path"] for item in baseline["files"]}
            if not set(mapping) <= paths:
                raise ProjectWorkspaceError("invalid_project_files")
            assigned = [{**item, "owner": mapping.get(item["path"], item["owner"])}
                        for item in baseline["files"]]
            for number, commit in enumerate(manifest["commits"]):
                if commit["proposal_id"] == proposal_id:
                    if commit["fingerprint"] != fingerprint:
                        raise ProjectWorkspaceError("project_proposal_conflict")
                    original = self._snapshot(revisions, manifest, number)
                    if (original["files"] != assigned or original["source"] != baseline["source"]):
                        raise ProjectWorkspaceError("project_corrupt")
                    return {**self._decorate(original, revisions, manifest), "replayed": True}
            if manifest["revision"] != 1:
                raise ProjectWorkspaceError("project_revision_conflict")
            result = self._publish(room, revisions, manifest, assigned, proposal_id, fingerprint,
                                   source=baseline["source"])
            return {**self._decorate(result, revisions, self._manifest(room)), "replayed": False}

    def seed_python(self, snapshot, *, source_room_id, allowed_owners) -> dict:
        """Inherit an already captured Python snapshot without accessing its source.

        The caller verifies the source room and its stable revision before this
        operation. Keeping each file's owner lets later stages retain the same
        write boundaries. A matching import replays its original revision, not
        any subsequent edits in this workspace.
        """
        if (self.kind != "python" or not isinstance(source_room_id, str)
                or _uuid(source_room_id) != source_room_id or source_room_id == self.room_id
                or not isinstance(allowed_owners, (list, tuple))
                or not 1 <= len(allowed_owners) <= 8
                or any(not safe_agent_id(owner) for owner in allowed_owners)
                or len(set(allowed_owners)) != len(allowed_owners)
                or not isinstance(snapshot, dict) or set(snapshot) != {"revision", "files"}
                or type(snapshot["revision"]) is not int or snapshot["revision"] < 1):
            raise ProjectWorkspaceError("invalid_project_request")
        if not isinstance(snapshot["files"], list) or not 1 <= len(snapshot["files"]) <= MAX_FILES:
            raise ProjectWorkspaceError("invalid_project_files")
        prepared, seen, total = [], set(), 0
        for item in snapshot["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "owner", "content"}:
                raise ProjectWorkspaceError("invalid_project_files")
            raw = _path_and_content(item["path"], item["content"])
            if (item["path"].casefold() in seen or not safe_agent_id(item["owner"])
                    or item["owner"] not in allowed_owners
                    or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])
                    or item["sha256"] != _digest(raw)):
                raise ProjectWorkspaceError("invalid_project_files")
            seen.add(item["path"].casefold())
            total += len(raw)
            prepared.append(dict(item))
        if total > self.max_bytes:
            raise ProjectWorkspaceError("project_limit_exceeded")
        prepared.sort(key=lambda item: item["path"])
        proposal_id = "python-import:" + source_room_id
        try:
            fingerprint = _digest(_json({"source_room_id": source_room_id,
                                        "source_revision": snapshot["revision"], "files": prepared}))
        except ValueError:
            # Reject integers too large for the configured JSON encoder rather
            # than leaking its exception through the storage contract.
            raise ProjectWorkspaceError("invalid_project_request") from None
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            if manifest["revision"] > 0:
                commit = manifest["commits"][1]
                if commit["proposal_id"] != proposal_id or commit["fingerprint"] != fingerprint:
                    raise ProjectWorkspaceError("project_revision_conflict")
                original = self._snapshot(revisions, manifest, 1)
                if original["files"] != prepared:
                    raise ProjectWorkspaceError("project_corrupt")
                return {**original, "replayed": True}
            if self._snapshot(revisions, manifest, 0)["files"]:
                raise ProjectWorkspaceError("project_revision_conflict")
            result = self._publish(room, revisions, manifest, prepared, proposal_id, fingerprint)
            return {**result, "replayed": False}

    def snapshot(self, revision=None) -> dict:
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            result = self._snapshot(revisions, manifest, manifest["revision"] if revision is None else revision)
            return self._decorate(result, revisions, manifest)

    def _disjoint_snapshot(self, revisions, manifest, base_revision, prepared):
        base = self._snapshot(revisions, manifest, base_revision)
        self._decorate(base, revisions, manifest)
        original = {item["path"].casefold(): item for item in base["files"]}
        for item in prepared:
            previous = original.get(item["path"].casefold())
            if (previous is not None and previous["path"] != item["path"]
                    or item["previous_sha256"] != (previous["sha256"] if previous else None)):
                raise ProjectWorkspaceError("project_file_conflict")
        current = base
        # Inspect intervening immutable revisions too: equal heads alone cannot
        # distinguish untouched paths from deletion/recreation or other ABA changes.
        for number in range(base_revision + 1, manifest["revision"] + 1):
            current = self._snapshot(revisions, manifest, number)
            indexed = {item["path"].casefold(): item for item in current["files"]}
            if any(indexed.get(item["path"].casefold()) != original.get(item["path"].casefold())
                   for item in prepared):
                raise ProjectWorkspaceError("project_file_conflict")
        return current

    def apply(self, provider, proposal_id, base_revision, files, *, allow_disjoint=False, ownership_grants=None) -> dict:
        if (not safe_agent_id(provider) or not isinstance(proposal_id, str) or not _ID.fullmatch(proposal_id)
                or type(base_revision) is not int or base_revision < 0 or type(allow_disjoint) is not bool):
            raise ProjectWorkspaceError("invalid_project_request")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise ProjectWorkspaceError("invalid_project_files")
        prepared, seen, total = [], set(), 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "previous_sha256", "content"}:
                raise ProjectWorkspaceError("invalid_project_files")
            raw = _path_and_content(item["path"], item["content"], kind=self.kind)
            if item["path"].casefold() in seen:
                raise ProjectWorkspaceError("invalid_project_files")
            seen.add(item["path"].casefold())
            previous = item["previous_sha256"]
            if previous is not None and (not isinstance(previous, str) or not _HASH.fullmatch(previous)):
                raise ProjectWorkspaceError("invalid_project_files")
            total += len(raw)
            prepared.append(dict(item))
        if total > self.max_bytes:
            raise ProjectWorkspaceError("project_limit_exceeded")
        prepared.sort(key=lambda item: item["path"])
        parameters = {"provider": provider, "base_revision": base_revision, "files": prepared}
        # Keep legacy fingerprints stable; opting in is distinct proposal semantics.
        if allow_disjoint:
            parameters["allow_disjoint"] = True
        if ownership_grants:
            parameters["ownership_grants"] = ownership_grants
        fingerprint = _digest(_json(parameters))
        with self._locked() as (room, revisions, _):
            manifest = self._manifest(room)
            for number, commit in enumerate(manifest["commits"]):
                if commit["proposal_id"] == proposal_id:
                    if commit["fingerprint"] != fingerprint:
                        raise ProjectWorkspaceError("project_proposal_conflict")
                    if allow_disjoint:
                        base = self._snapshot(revisions, manifest, base_revision)
                        self._decorate(base, revisions, manifest)
                    original = self._snapshot(revisions, manifest, number)
                    return {**self._decorate(original, revisions, manifest), "replayed": True}
            if manifest["revision"] != base_revision and not allow_disjoint:
                raise ProjectWorkspaceError("project_revision_conflict")
            current = (self._disjoint_snapshot(revisions, manifest, base_revision, prepared)
                       if allow_disjoint else self._snapshot(revisions, manifest, base_revision))
            if self.kind == "text" and current["source"] is None and not self.allow_new_text_files:
                raise ProjectWorkspaceError("project_not_seeded")
            self._decorate(current, revisions, manifest)
            merged = {item["path"]: item for item in current["files"]}
            names = {path.casefold(): path for path in merged}
            for item in prepared:
                previous = merged.get(item["path"])
                if item["path"].casefold() in names and names[item["path"].casefold()] != item["path"]:
                    raise ProjectWorkspaceError("project_file_conflict")
                grant = (ownership_grants or {}).get(item["path"], {})
                authorized_transfer = (previous is not None and grant.get("old_owner") == previous["owner"]
                                       and grant.get("new_owner") == provider and grant.get("sha256") == previous["sha256"])
                if previous is not None and previous["owner"] != provider and not authorized_transfer:
                    raise ProjectWorkspaceError("project_owner_conflict")
                if item["previous_sha256"] != (previous["sha256"] if previous else None):
                    raise ProjectWorkspaceError("project_file_conflict")
                merged[item["path"]] = {"path": item["path"], "owner": provider,
                                         "sha256": _digest(item["content"].encode("utf-8")),
                                         "content": item["content"]}
            if len(merged) > MAX_FILES or sum(len(item["content"].encode("utf-8")) for item in merged.values()) > self.max_bytes:
                raise ProjectWorkspaceError("project_limit_exceeded")
            result = self._publish(room, revisions, manifest, sorted(merged.values(), key=lambda item: item["path"]),
                                   proposal_id, fingerprint, source=current.get("source"))
            return {**self._decorate(result, revisions, self._manifest(room)), "replayed": False}
