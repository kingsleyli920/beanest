"""Explicit text-file project registrations; source directories are read-only.

Registration never scans a directory. Each capture opens only the listed files
relative to an anchored directory descriptor, with no symlink traversal. The
private catalogue stores metadata, while a room owns its imported snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from uuid import UUID, uuid4


MAX_PROJECT_FILES = 12
MAX_TEXT_FILE_BYTES = 16 * 1024
MAX_TEXT_BYTES = 48 * 1024
MAX_CATALOG_BYTES = 1024 * 1024
MAX_PROJECTS = 100
FORMAT = "beanest-project-catalog-v1"
LIFECYCLE_STATES = frozenset({"active", "archived", "removed"})
_LIFECYCLE_ACTIONS = {"archive": "archived", "restore": "active", "remove": "removed"}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".css", ".html", ".xml", ".sql", ".rs", ".go", ".java",
    ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".sh", ".bash", ".gd", ".tscn",
    ".tres", ".vue", ".svelte", ".properties", ".csv", ".rst", ".cmake", ".svg",
})
_SENSITIVE_NAMES = frozenset({
    "credentials", "credential", "secrets", "secret", "id_rsa", "id_ed25519", "id_ecdsa",
    "private_key", "private-key", "api_key", "api-key", "access_token", "refresh_token",
    "keychain", "keychains", "auth.json", "config.json.credentials",
})
_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*|\bsk-[A-Za-z0-9_-]{12,}|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"\b(?:[A-Z_]*(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|REFRESH_TOKEN)|"
    r"apiKey|accessToken|refreshToken|clientSecret|password)\s*[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]+\"|'[^']+'|[^\s,;]+)", re.IGNORECASE)


class ProjectCatalogError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def source_hash(files: list[dict]) -> str:
    """Bind names and bytes, independently of a project name or host location."""
    values = sorted(({"path": item["path"], "sha256": item["sha256"]} for item in files), key=lambda item: item["path"])
    return hashlib.sha256(canonical_json(values)).hexdigest()


def validate_text_path(value) -> str:
    if (not isinstance(value, str) or not value or len(value) > 240 or "\\" in value
            or value.startswith("/") or len(value.split("/")) > 8):
        raise ProjectCatalogError("invalid_project_files")
    parts = value.split("/")
    for part in parts:
        lowered = part.casefold()
        stem = lowered.split(".")[0]
        if (not _SEGMENT.fullmatch(part) or ".." in part or lowered in _SENSITIVE_NAMES
                or stem in _SENSITIVE_NAMES or lowered.endswith((".pem", ".key", ".p12", ".pfx"))):
            raise ProjectCatalogError("invalid_project_files")
    filename = parts[-1]
    if Path(filename).suffix.casefold() not in _EXTENSIONS and filename not in {"LICENSE", "Makefile", "Dockerfile"}:
        raise ProjectCatalogError("invalid_project_files")
    return value


def validate_text_content(content) -> bytes:
    if not isinstance(content, str) or any(ord(char) < 32 and char not in "\t\r\n" for char in content):
        raise ProjectCatalogError("invalid_project_files")
    try:
        raw = content.encode("utf-8")
    except UnicodeError:
        raise ProjectCatalogError("invalid_project_files") from None
    if len(raw) > MAX_TEXT_FILE_BYTES:
        raise ProjectCatalogError("project_limit_exceeded")
    if _SECRET.search(content):
        raise ProjectCatalogError("project_sensitive_content")
    return raw


def _absolute_path(value) -> Path:
    if not isinstance(value, (str, Path)):
        raise ProjectCatalogError("invalid_project_path")
    candidate = Path(value)
    try:
        str(candidate).encode("utf-8")
    except UnicodeError:
        raise ProjectCatalogError("invalid_project_path") from None
    if (not candidate.is_absolute() or ".." in candidate.parts or str(candidate) == candidate.anchor
            or "\0" in str(candidate)):
        raise ProjectCatalogError("invalid_project_path")
    return candidate


def _file_list(files) -> list[str]:
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_PROJECT_FILES:
        raise ProjectCatalogError("invalid_project_files")
    values = [validate_text_path(item) for item in files]
    if len({item.casefold() for item in values}) != len(values):
        raise ProjectCatalogError("invalid_project_files")
    return sorted(values)


def _private(descriptor, *, directory=False):
    metadata = os.fstat(descriptor)
    if ((not stat.S_ISDIR(metadata.st_mode) if directory else not stat.S_ISREG(metadata.st_mode))
            or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
            or (not directory and metadata.st_nlink != 1)):
        raise ProjectCatalogError("project_unsafe_path")


def _open_absolute(path: Path, *, create=False, private=False) -> int:
    descriptor = os.open(path.anchor, _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if private:
            _private(descriptor, directory=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class ProjectCatalog:
    def __init__(self, root_private: Path):
        self.root = _absolute_path(root_private)
        with self._locked() as descriptor:
            self._load(descriptor)

    @contextmanager
    def _locked(self):
        root = lock = None
        try:
            root = _open_absolute(self.root, create=True, private=True)
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            created_lock = False
            try:
                lock = os.open(".catalog.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root)
                created_lock = True
                os.fsync(root)
            except FileExistsError:
                lock = os.open(".catalog.lock", flags, dir_fd=root)
            _private(lock)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ProjectCatalogError("project_busy") from None
                    time.sleep(0.01)
            try:
                os.stat("catalog.json", dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                if not created_lock:
                    raise ProjectCatalogError("project_catalog_corrupt") from None
                self._write(root, [])
            yield root
        except OSError as error:
            code = "project_unsafe_path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "project_storage_unavailable"
            raise ProjectCatalogError(code) from None
        finally:
            for descriptor in (lock, root):
                if descriptor is not None:
                    os.close(descriptor)

    def _load(self, directory):
        try:
            descriptor = os.open("catalog.json", _FILE_FLAGS, dir_fd=directory)
        except FileNotFoundError:
            raise ProjectCatalogError("project_catalog_corrupt") from None
        try:
            _private(descriptor)
            if os.fstat(descriptor).st_size > MAX_CATALOG_BYTES:
                raise ProjectCatalogError("project_catalog_corrupt")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_CATALOG_BYTES + 1)
            if len(raw) > MAX_CATALOG_BYTES:
                raise ValueError
            def reject(_value):
                raise ValueError
            value = json.loads(raw, object_pairs_hook=_unique, parse_constant=reject)
            if (not isinstance(value, dict) or set(value) != {"format", "projects"}
                    or value["format"] != FORMAT or not isinstance(value["projects"], list)
                    or len(value["projects"]) > MAX_PROJECTS):
                raise ValueError
            seen = set()
            for item in value["projects"]:
                if (not isinstance(item, dict)
                        or set(item) - {"selection_kind", "lifecycle_state"}
                        != {"id", "name", "path", "files", "created_at"}):
                    raise ValueError
                lifecycle = item.get("lifecycle_state", "active")
                if not isinstance(lifecycle, str) or lifecycle not in LIFECYCLE_STATES:
                    raise ValueError
                folder = item.get("selection_kind") == "folder"
                if "selection_kind" in item and not folder:
                    raise ValueError
                if (not isinstance(item["id"], str) or str(UUID(item["id"])) != item["id"]
                        or item["id"] in seen or self._name(item["name"]) != item["name"]
                        or str(_absolute_path(item["path"])) != item["path"]
                        or (item["files"] != [] if folder else _file_list(item["files"]) != item["files"])
                        or type(item["created_at"]) not in (int, float) or not math.isfinite(item["created_at"])
                        or item["created_at"] <= 0):
                    raise ValueError
                seen.add(item["id"])
                # Old catalogues stay byte-for-byte unchanged until an explicit
                # mutation. Callers still receive the current public shape.
                item["lifecycle_state"] = lifecycle
            return value["projects"]
        except (ValueError, UnicodeError, RecursionError, ProjectCatalogError) as error:
            if isinstance(error, ProjectCatalogError) and error.code == "project_unsafe_path":
                raise
            raise ProjectCatalogError("project_catalog_corrupt") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _name(value):
        if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 80
                or any(ord(char) < 32 for char in value) or _SECRET.search(value)):
            raise ProjectCatalogError("invalid_project_request")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ProjectCatalogError("invalid_project_request") from None
        return value.strip()

    def _write(self, directory, projects):
        raw = canonical_json({"format": FORMAT, "projects": projects})
        if len(raw) > MAX_CATALOG_BYTES:
            raise ProjectCatalogError("project_limit_exceeded")
        temporary = ".catalog-" + str(uuid4())
        created = attempted = False
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                 0o600, dir_fd=directory)
            created = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            attempted = True
            os.replace(temporary, "catalog.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except OSError:
            if attempted:
                try:
                    if self._load(directory) == projects:
                        raise ProjectCatalogError("project_commit_unconfirmed") from None
                except ProjectCatalogError:
                    raise ProjectCatalogError("project_commit_unconfirmed") from None
            raise ProjectCatalogError("project_storage_unavailable") from None
        finally:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _capture(metadata):
        root = None
        try:
            root = _open_absolute(Path(metadata["path"]))
            files, total = [], 0
            for name in metadata["files"]:
                descriptor = os.dup(root)
                try:
                    parts = name.split("/")
                    for part in parts[:-1]:
                        child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
                        os.close(descriptor)
                        descriptor = child
                    file_descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=descriptor)
                    try:
                        before = os.fstat(file_descriptor)
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                            raise ProjectCatalogError("project_unsafe_path")
                        if before.st_size > MAX_TEXT_FILE_BYTES:
                            raise ProjectCatalogError("project_limit_exceeded")
                        raw = os.read(file_descriptor, MAX_TEXT_FILE_BYTES + 1)
                        after = os.fstat(file_descriptor)
                        attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                        if any(getattr(before, key) != getattr(after, key) for key in attributes) or len(raw) != after.st_size:
                            raise ProjectCatalogError("project_file_unavailable")
                        try:
                            content = raw.decode("utf-8")
                        except UnicodeError:
                            raise ProjectCatalogError("invalid_project_files") from None
                        validate_text_content(content)
                        total += len(raw)
                        if total > MAX_TEXT_BYTES:
                            raise ProjectCatalogError("project_limit_exceeded")
                        files.append({"path": name, "content": content, "sha256": hashlib.sha256(raw).hexdigest()})
                    finally:
                        os.close(file_descriptor)
                finally:
                    os.close(descriptor)
            return {**metadata, "files": files, "source_hash": source_hash(files)}
        except OSError as error:
            code = "project_unsafe_path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "project_file_unavailable"
            raise ProjectCatalogError(code) from None
        finally:
            if root is not None:
                os.close(root)

    def register(self, name, path, files) -> dict:
        metadata = {"id": str(uuid4()), "name": self._name(name), "path": str(_absolute_path(path)),
                    "files": _file_list(files), "created_at": time.time(), "lifecycle_state": "active"}
        self._capture(metadata)
        with self._locked() as directory:
            projects = self._load(directory)
            if len(projects) >= MAX_PROJECTS:
                raise ProjectCatalogError("project_limit_exceeded")
            projects.append(metadata)
            self._write(directory, projects)
        return metadata

    def register_folder(self, name, path) -> dict:
        """Remember an explicitly selected directory without enumerating its contents."""
        metadata = {"id": str(uuid4()), "name": self._name(name), "path": str(_absolute_path(path)),
                    "files": [], "selection_kind": "folder", "created_at": time.time(),
                    "lifecycle_state": "active"}
        descriptor = None
        try:
            descriptor = _open_absolute(Path(metadata["path"]))
        except OSError as error:
            code = "project_unsafe_path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "project_file_unavailable"
            raise ProjectCatalogError(code) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        with self._locked() as directory:
            projects = self._load(directory)
            if len(projects) >= MAX_PROJECTS:
                raise ProjectCatalogError("project_limit_exceeded")
            projects.append(metadata)
            self._write(directory, projects)
        return metadata

    def list(self) -> list[dict]:
        with self._locked() as directory:
            return self._load(directory)

    @staticmethod
    def _validate_id(project_id):
        if not isinstance(project_id, str):
            raise ProjectCatalogError("invalid_project_request")
        try:
            if str(UUID(project_id)) != project_id:
                raise ValueError
        except ValueError:
            raise ProjectCatalogError("invalid_project_request") from None

    def get(self, project_id) -> dict:
        self._validate_id(project_id)
        for item in self.list():
            if item["id"] == project_id:
                if item["lifecycle_state"] != "active":
                    raise ProjectCatalogError("project_inactive")
                return item
        raise ProjectCatalogError("project_not_found")

    def set_lifecycle(self, project_id, action) -> dict:
        """Hide or restore a registration; source bytes and room copies stay put."""
        self._validate_id(project_id)
        if not isinstance(action, str) or action not in _LIFECYCLE_ACTIONS:
            raise ProjectCatalogError("invalid_lifecycle_action")
        state = _LIFECYCLE_ACTIONS[action]
        with self._locked() as directory:
            projects = self._load(directory)
            for item in projects:
                if item["id"] == project_id:
                    if item["lifecycle_state"] != state:
                        item["lifecycle_state"] = state
                        self._write(directory, projects)
                    return item
        raise ProjectCatalogError("project_not_found")

    def rename(self, project_id, name) -> dict:
        """Change only the display name; the path, files and room copies stay put.

        Archived and old removed registrations can be renamed too: the name is
        display metadata and does not make a registration usable again.
        """
        self._validate_id(project_id)
        name = self._name(name)
        with self._locked() as directory:
            projects = self._load(directory)
            for item in projects:
                if item["id"] == project_id:
                    if item["name"] != name:
                        item["name"] = name
                        self._write(directory, projects)
                    return item
        raise ProjectCatalogError("project_not_found")

    def remove_registration(self, project_id) -> dict:
        """Forget a registration permanently; never visit its source directory.

        Existing rooms own their captured copies independently. The service
        coordinates active captures before calling this configuration mutation.
        """
        self._validate_id(project_id)
        with self._locked() as directory:
            projects = self._load(directory)
            remaining = [item for item in projects if item["id"] != project_id]
            if len(remaining) != len(projects):
                self._write(directory, remaining)
        return {"id": project_id, "deleted": True}

    def capture(self, project_id) -> dict:
        metadata = self.get(project_id)
        if metadata.get("selection_kind") == "folder":
            raise ProjectCatalogError("project_folder_capture_required")
        # Registration lifecycle is mutable UI metadata, not part of a frozen
        # source import. Keep the existing seed shape and source fingerprint.
        source_metadata = {key: metadata[key] for key in ("id", "name", "path", "files", "created_at")}
        return self._capture(source_metadata)
