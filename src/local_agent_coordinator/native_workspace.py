"""Private native working trees and explicit B/S/D handoff.

Inputs are verified exports from FolderWorkspace, never a user's source tree.
Working directories prevent accidental peer overwrites; they are not a sandbox
for a CLI running with Full Access. Only the host imports authorized changes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from uuid import uuid4

from .folder_workspace import MAX_FILE_BYTES, _excluded


class NativeWorkspaceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_CONTROL = re.compile(r"[\x00-\x1f]")


def _path(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or value.startswith("/") or _CONTROL.search(value)
            or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in value.split("/"))):
        raise NativeWorkspaceError("invalid_native_path")
    return value


def _entries(manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise NativeWorkspaceError("invalid_native_manifest")
    result, folded = {}, set()
    for entry in manifest["files"]:
        if (not isinstance(entry, dict) or type(entry.get("bytes")) is not int
                or entry["bytes"] < 0 or type(entry.get("text")) is not bool
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or entry.get("executable") is not None and type(entry["executable"]) is not bool):
            raise NativeWorkspaceError("invalid_native_manifest")
        name = _path(entry.get("path"))
        if name.casefold() in folded:
            raise NativeWorkspaceError("invalid_native_manifest")
        result[name] = dict(entry)
        folded.add(name.casefold())
    return result


def _directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise NativeWorkspaceError("invalid_native_directory")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise NativeWorkspaceError("invalid_native_directory")
    if not path.is_dir():
        raise NativeWorkspaceError("invalid_native_directory")
    return path


def _file(root, name, checked=None):
    name = _path(name)
    base = str(root)
    # The root and every parent directory are refused as symlinks. One tree
    # walk may share its checked directories, never another call's results.
    parents = [""] + ["/".join(name.split("/")[:index]) for index in range(1, name.count("/") + 1)]
    for parent in parents:
        if checked is not None and parent in checked:
            continue
        if os.path.islink(os.path.join(base, parent) if parent else base):
            raise NativeWorkspaceError("native_symlink_refused")
        if checked is not None:
            checked.add(parent)
    if os.path.islink(os.path.join(base, name)):
        raise NativeWorkspaceError("native_symlink_refused")
    return root / name


# Recovery audits re-read whole private trees on every UI poll. Reuse a digest
# only while the file's identity, size, mtime and ctime are all unchanged.
_DIGESTS = {}


def _file_digest(path):
    before = os.stat(path)
    key = (str(path), before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    sha = _DIGESTS.get(key)
    if sha is not None:
        return sha
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == key[2:]:
        if len(_DIGESTS) >= 200_000:
            _DIGESTS.clear()
        _DIGESTS[key] = sha
    return sha


def signature_paths(manifest, extra_names=()):
    """The absolute paths tree_signature() lstat()s for one private tree: its
    root, Git's untracked-file inputs, every baseline file, the named extra
    files, and every folder holding any of them. Unvalidated: only stat()ed."""
    root = str(manifest["project"])
    names = {"", ".git/index", ".git/HEAD", ".git/info/exclude"}
    listed = [entry.get("path") for entry in manifest.get("files", []) if isinstance(entry, dict)]
    for name in listed + list(extra_names):
        if not isinstance(name, str) or not name:
            continue
        names.add(name)
        parts = name.split("/")
        names.update("/".join(parts[:index]) for index in range(1, len(parts)))
    return tuple(os.path.join(root, name) if name else root for name in sorted(names))


def tree_signature(paths):
    """A cheap stand-in for "this private tree has not moved", for display
    caches only. It changes on an in-place edit, a mode change, an entry added
    to or removed from a known folder, and staging; it can miss a new file
    inside a folder holding no listed path. It uses the same identity, size,
    mtime and ctime that _file_digest trusts. It never replaces
    collect_changes: anything that imports, carries or permits work re-reads
    the tree."""
    digest = hashlib.sha256()
    for path in paths:
        try:
            info = os.lstat(path)
            row = (f"{path}\0{info.st_dev}\0{info.st_ino}\0{info.st_mode}\0{info.st_size}"
                   f"\0{info.st_mtime_ns}\0{info.st_ctime_ns}\n")
        except OSError as error:
            row = f"{path}\0!{error.errno}\n"
        digest.update(row.encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _cancelled(callback):
    if callback is not None and callback():
        raise NativeWorkspaceError("native_preparation_cancelled")


def _copy_verified(source, destination, entry, cancelled=None):
    source_path = _file(source, entry["path"])
    destination_path = _file(destination, entry["path"])
    destination_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest, size = hashlib.sha256(), 0
    temporary = destination_path.with_name(".beanest-copy-" + str(uuid4()))
    created = False
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise NativeWorkspaceError("invalid_native_file")
        output = temporary.open("xb")
        created = True
        with os.fdopen(descriptor, "rb") as stream, output:
            descriptor = None
            while chunk := stream.read(256 * 1024):
                _cancelled(cancelled)
                size += len(chunk)
                digest.update(chunk)
                output.write(chunk)
            after = os.fstat(stream.fileno())
        if (size != entry["bytes"] or digest.hexdigest() != entry["sha256"]
                or (before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise NativeWorkspaceError("native_export_changed")
        temporary.chmod(0o700 if entry.get("executable") else 0o600)
        os.replace(temporary, destination_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            temporary.unlink(missing_ok=True)


def _git(root, *args, binary=False, input_bytes=None):
    # This is an internal baseline in a new private tree, not a game commit.
    # No inherited repository variables, user hooks, filters or templates run.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    git_directory = root / ".git"
    if git_directory.is_symlink() or git_directory.exists() and not git_directory.is_dir():
        raise NativeWorkspaceError("invalid_native_repository")
    process = subprocess.run(["git", "--no-optional-locks", "-C", str(root),
        "--git-dir", str(git_directory), "--work-tree", str(root), "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", *args],
        env=environment, capture_output=True, input=input_bytes, timeout=60)
    if process.returncode:
        raise NativeWorkspaceError("native_baseline_failed")
    return process.stdout if binary else process.stdout.decode("utf-8")


def _baseline_store(root):
    store = root.parent / "baseline-objects"
    if not store.exists() and not store.is_symlink():
        store.mkdir(mode=0o700)
    return _directory(store)


def _remember_baseline(root, source, entry, cancelled=None):
    # Native agents own their .git, including GC and object layout. Host CAS
    # bases therefore live alongside the project and use exact captured bytes.
    store = _baseline_store(root)
    target = store / entry["sha256"]
    if target.exists() or target.is_symlink():
        _read_baseline(root, entry)
        return
    renamed = {**entry, "path": entry["sha256"]}
    staging = store / ("capture-" + str(uuid4()))
    staging.mkdir(mode=0o700)
    try:
        _copy_verified(source, staging, entry, cancelled)
        os.replace(staging / entry["path"], target)
        _read_baseline(root, renamed)
    finally:
        shutil.rmtree(staging)


def _read_baseline(root, entry):
    path = _file(_baseline_store(root), entry["sha256"])
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as error:
        raise NativeWorkspaceError("native_baseline_missing") from error
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise NativeWorkspaceError("invalid_native_file")
        raw = stream.read()
    if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise NativeWorkspaceError("native_baseline_changed")
    return raw


def _save_receipt(root, receipt):
    target = root.parent / "baseline.json"
    temporary = target.with_name(".baseline-" + str(uuid4()))
    try:
        with temporary.open("x") as stream:
            temporary.chmod(0o600)
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_workspace(destination, source_directory, source_manifest, *,
                      baseline_directory=None, baseline_manifest=None,
                      candidate_directory=None, candidate_manifest=None,
                      cancelled=None):
    """Copy S; apply D only where B proves it cannot overwrite newer source.

    Conflicting paths keep S. Exact old versions live next to the project for
    the responsible agent to resolve; no user source is changed by this helper.
    """
    destination = Path(destination)
    _directory(destination.parent)
    source = _directory(source_directory)
    entries = _entries(source_manifest)
    if (baseline_manifest is None) != (candidate_manifest is None):
        raise NativeWorkspaceError("invalid_native_handoff")
    baseline = _entries(baseline_manifest) if baseline_manifest is not None else {}
    candidate = _entries(candidate_manifest) if candidate_manifest is not None else {}
    old_root = _directory(baseline_directory) if baseline_manifest is not None else None
    candidate_root = _directory(candidate_directory) if candidate_manifest is not None else None
    _cancelled(cancelled)
    destination.mkdir(mode=0o700, exist_ok=False)
    project, handoff = destination / "project", destination / "handoff"
    try:
        project.mkdir(mode=0o700)
        handoff.mkdir(mode=0o700)
        for entry in entries.values():
            _copy_verified(source, project, entry, cancelled)
        conflicts, carried = [], []
        for name in sorted(set(baseline) | set(candidate)):
            before, previous, current = baseline.get(name), candidate.get(name), entries.get(name)
            old_sha = before["sha256"] if before else None
            previous_sha = previous["sha256"] if previous else None
            source_sha = current["sha256"] if current else None
            if old_sha == previous_sha or source_sha == previous_sha:
                continue
            if previous is not None and source_sha == old_sha:
                _copy_verified(candidate_root, project, previous, cancelled)
                entries[name] = previous
                carried.append(name)
                continue
            conflict = {"kind": "source_handoff", "path": name, "baseline_sha256": old_sha,
                        "source_sha256": source_sha, "candidate_sha256": previous_sha}
            for label, root, entry in (("baseline", old_root, before),
                                       ("previous", candidate_root, previous)):
                if entry is not None:
                    target = handoff / label
                    target.mkdir(mode=0o700, exist_ok=True)
                    _copy_verified(root, target, entry, cancelled)
                    conflict[label + "_path"] = str(target / name)
            conflicts.append(conflict)
        _cancelled(cancelled)
        _git(project, "-c", "init.templateDir=", "init", "-q")
        _git(project, "add", "--all", "--force", "--", ".")
        _git(project, "-c", "user.name=Beanest", "-c", "user.email=workspace@example.invalid",
             "commit", "-qm", "Private workspace baseline", "--allow-empty")
        for entry in entries.values():
            _remember_baseline(project, project, entry, cancelled)
        receipt = {"project": str(project), "handoff": str(handoff),
                   "baseline_commit": _git(project, "rev-parse", "HEAD").strip(),
                   "files": [entries[key] for key in sorted(entries)],
                   "conflicts": conflicts, "carried_paths": carried}
        _save_receipt(project, receipt)
        return receipt
    except BaseException:
        shutil.rmtree(destination)
        raise


def prepare_retained_workspace(destination, canonical_export, canonical_manifest,
                               source_private, retained_files):
    """Create a fresh S baseline, then carry host-audited unpublished D bytes.

    Ownership/conflict authorization belongs to the caller. This boundary
    rechecks the exact S/D binding and preserves S as the CAS baseline, so a
    subsequent sync cannot mistake retained D for replaceable shared bytes.
    Existing source and receiver trees, including their Git data, stay intact.
    """
    canonical = _entries(canonical_manifest)
    canonical_names = {name.casefold(): name for name in canonical}
    source = _directory(source_private)
    if not isinstance(retained_files, list):
        raise NativeWorkspaceError("invalid_native_retained_files")
    retained, seen = [], set()
    for item in retained_files:
        if not isinstance(item, dict):
            raise NativeWorkspaceError("invalid_native_retained_files")
        name = _path(item.get("path"))
        before = canonical.get(name)
        if (name.casefold() in seen or "previous_sha256" not in item
                or canonical_names.get(name.casefold(), name) != name):
            raise NativeWorkspaceError("invalid_native_retained_files")
        seen.add(name.casefold())
        if item.get("previous_sha256") != (before["sha256"] if before else None):
            raise NativeWorkspaceError("native_retained_baseline_changed")
        path = _file(source, name)
        if item.get("deleted") is True:
            # A retained deletion: the source tree must still lack the file.
            if before is None or item.get("sha256") is not None or path.exists() or path.is_symlink():
                raise NativeWorkspaceError("invalid_native_retained_files")
            retained.append({"path": name, "sha256": None, "bytes": 0, "deleted": True})
            continue
        content = item.get("content")
        if (type(item.get("bytes")) is not int or item["bytes"] < 0
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise NativeWorkspaceError("invalid_native_retained_files")
        if isinstance(content, str):
            try:
                raw = content.encode("utf-8")
            except UnicodeError as error:
                raise NativeWorkspaceError("invalid_native_retained_files") from error
            if (len(raw) > MAX_FILE_BYTES or len(raw) != item["bytes"]
                    or hashlib.sha256(raw).hexdigest() != item["sha256"]
                    or any(ord(char) < 32 and char not in "\r\n\t" for char in content)):
                raise NativeWorkspaceError("invalid_native_retained_files")
        elif item.get("text") is not False or "content" in item:
            raise NativeWorkspaceError("invalid_native_retained_files")
        # Binary bytes are verified against sha256/bytes when copied below.
        if "executable" in item and type(item["executable"]) is not bool:
            raise NativeWorkspaceError("invalid_native_retained_files")
        executable = item["executable"] if "executable" in item else bool(before.get("executable")) if before else False
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or bool(mode & 0o111) != executable:
            raise NativeWorkspaceError("native_retained_mode_changed")
        retained.append({"path": name, "sha256": item["sha256"], "bytes": item["bytes"],
                         "text": isinstance(content, str), "executable": executable})
    # Reuse verified copying, private baseline objects and Git initialization.
    # prepare_workspace owns rollback until it returns; afterwards only this
    # newly created destination can be removed if retained-copy validation fails.
    receipt = prepare_workspace(destination, canonical_export, canonical_manifest)
    try:
        project = Path(receipt["project"])
        for entry in retained:
            if entry.get("deleted"):
                if _file(source, entry["path"]).exists():
                    raise NativeWorkspaceError("native_export_changed")
                _file(project, entry["path"]).unlink()
                continue
            _copy_verified(source, project, entry)
            mode = _file(source, entry["path"]).stat().st_mode
            if not stat.S_ISREG(mode) or bool(mode & 0o111) != entry["executable"]:
                raise NativeWorkspaceError("native_retained_mode_changed")
        receipt["retained_paths"] = [entry["path"] for entry in retained]
        receipt["retained_files"] = [{"path": entry["path"], "sha256": entry["sha256"],
            "bytes": entry["bytes"], "previous_sha256": canonical.get(entry["path"], {}).get("sha256")}
            for entry in retained]
        _save_receipt(project, receipt)
        return receipt
    except BaseException:
        shutil.rmtree(destination)
        raise


def collect_changes(workspace, baseline_manifest, allowed_paths, *, resolved_conflicts=()):
    """Read every change to authorized paths; final owner/grant/CAS checks are external.

    A candidate is text {content}, binary {content_base64, text: False} or a
    deletion {deleted: True, sha256: None}; "executable" is present when the
    mode changed, or a new or previously unknown file is executable. Only real
    coordination rules block: a path this task does not own, an unresolved
    conflict, and an over-limit file. Paths the shared copy never holds (build
    and cache folders, credentials such as .env or keys) stay private and are
    reported under "generated", never imported and never a failure.
    """
    root = _directory(workspace)
    entries = _entries(baseline_manifest)
    if not isinstance(allowed_paths, (list, tuple, set)):
        raise NativeWorkspaceError("invalid_native_path")
    allowed = {_path(name) for name in allowed_paths}
    conflicts = {item["path"]: item for item in baseline_manifest.get("conflicts", [])}
    resolved = {_path(name) for name in resolved_conflicts}
    if not resolved <= allowed & set(conflicts):
        raise NativeWorkspaceError("invalid_native_resolution")
    candidates, blocked, generated = [], [], []
    untracked = {_path(name) for name in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if name}
    checked = set()
    for name in sorted(set(entries) | allowed | untracked):
        path = _file(root, name, checked)
        before = entries.get(name)
        exists = path.exists()
        if exists and before is None and name in allowed and path.is_dir():
            # A task may name a folder as a write path. The folder is not a
            # file to import; each file inside is judged under its own name.
            continue
        if exists and not path.is_file():
            raise NativeWorkspaceError("invalid_native_file")
        if _excluded(name) and (before is None or name in allowed):
            # Never project inputs: build/cache outputs and credentials. Keep
            # their bytes private without importing them or treating a routine
            # native check, or a local .env, as an unauthorized source edit.
            if exists:
                sha = _file_digest(path)
                if before is None or sha != before["sha256"]:
                    sensitive = any(_excluded(part) == "sensitive_path" for part in name.split("/"))
                    generated.append({"path": name, "sha256": sha, "bytes": path.stat().st_size,
                                      **({"reason": "sensitive_path"} if sensitive else {})})
            continue
        if not exists:
            if before is None:
                continue
            sha = None
        else:
            sha = _file_digest(path)
            executable = bool(path.stat().st_mode & 0o111)
            mode_changed = before is not None and before.get("executable") is not None and executable != before["executable"]
            if before and sha == before["sha256"] and not mode_changed:
                continue
        if name not in allowed:
            blocked.append({"path": name, "reason": "native_path_not_owned"})
            continue
        if (name in conflicts and name not in resolved
                and sha == conflicts[name]["candidate_sha256"]):
            blocked.append({"path": name, "reason": "native_unresolved_conflict"})
            continue
        previous = before["sha256"] if before else None
        if sha is None:
            candidates.append({"path": name, "previous_sha256": previous, "sha256": None, "bytes": 0, "deleted": True})
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            blocked.append({"path": name, "reason": "native_change_too_large"})
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise NativeWorkspaceError("native_export_changed")
        candidate = {"path": name, "previous_sha256": previous, "sha256": sha, "bytes": len(raw)}
        try:
            content = raw.decode("utf-8")
            if any(ord(c) < 32 and c not in "\r\n\t" for c in content):
                raise UnicodeError
            candidate["content"] = content
        except UnicodeError:
            candidate.update(content_base64=base64.b64encode(raw).decode("ascii"), text=False)
        if mode_changed or executable and (before is None or before.get("executable") is None):
            candidate["executable"] = executable
        candidates.append(candidate)
    return {"files": candidates, "blocked": blocked, **({"generated": generated} if generated else {})}


def import_file(candidate):
    """The shared-copy import form of one collect_changes candidate."""
    item = {"path": candidate["path"], "previous_sha256": candidate["previous_sha256"]}
    if candidate.get("deleted"):
        return {**item, "deleted": True}
    item.update({"content": candidate["content"]} if "content" in candidate
                else {"content_base64": candidate["content_base64"]})
    if "executable" in candidate:
        item["executable"] = candidate["executable"]
    return item


def acknowledge_published(workspace, previous_manifest, published_files, *, resolved_paths=()):
    """Advance CAS baselines only after the host has actually imported bytes.

    Host-owned SHA256 objects preserve exact three-way conflict bases.
    This does not commit, stage, revert or discard a native agent's other work.
    """
    root = _directory(workspace)
    entries = _entries(previous_manifest)
    seen, removed = set(), set()
    for item in published_files:
        name = _path(item.get("path"))
        if name in seen:
            raise NativeWorkspaceError("invalid_native_manifest")
        seen.add(name)
        path = _file(root, name)
        if item.get("deleted") is True and item.get("sha256") is None:
            # A published deletion: the baseline forgets the file only while
            # the private tree still agrees that it is gone.
            if path.exists() or path.is_symlink():
                raise NativeWorkspaceError("native_export_changed")
            entries.pop(name, None)
            removed.add(name)
            continue
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise NativeWorkspaceError("invalid_native_manifest")
        with path.open("rb") as stream:
            actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual_sha != item["sha256"]:
            raise NativeWorkspaceError("native_export_changed")
        old = entries.get(name, {})
        executable = item["executable"] if "executable" in item else old.get("executable")
        entries[name] = {"path": name, "sha256": actual_sha, "bytes": path.stat().st_size,
            "text": item.get("text", True), "executable": executable}
        _remember_baseline(root, root, entries[name])
    resolved = {_path(name) for name in resolved_paths}
    for name in resolved:
        if name in removed:
            continue
        if name not in entries:
            raise NativeWorkspaceError("invalid_native_resolution")
        with _file(root, name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != entries[name]["sha256"]:
                raise NativeWorkspaceError("native_resolution_not_published")
    receipt = {**previous_manifest, "files": [entries[key] for key in sorted(entries)],
               "conflicts": [item for item in previous_manifest.get("conflicts", [])
                             if item["path"] not in seen | resolved]}
    _save_receipt(root, receipt)
    return receipt


def discard_paths(workspace, previous_manifest, paths):
    """Put named paths back to their last published bytes in this private tree.

    Used only for work the user withdrew: its unsubmitted bytes must never be
    collected later. A file the baseline does not know is removed. Returns the
    new receipt and the paths that actually changed.
    """
    root = _directory(workspace)
    entries = _entries(previous_manifest)
    discarded = []
    for name in sorted({_path(value) for value in paths}):
        path = _file(root, name)
        before = entries.get(name)
        if not path.exists():
            if before is None:
                continue
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif before is None and path.is_dir():
            continue  # A folder named as a write path was never collected.
        elif not path.is_file():
            raise NativeWorkspaceError("invalid_native_file")
        elif before is None:
            path.unlink()
            discarded.append(name)
            continue
        elif _file_digest(path) == before["sha256"] and (
                before.get("executable") is None or bool(path.stat().st_mode & 0o111) == before["executable"]):
            continue
        raw = _read_baseline(root, before)
        temporary = path.with_name(".beanest-discard-" + str(uuid4()))
        try:
            with temporary.open("xb") as output:
                output.write(raw)
            temporary.chmod(0o700 if before.get("executable") else 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        discarded.append(name)
    names = set(discarded) | {_path(value) for value in paths}
    receipt = {**previous_manifest, "conflicts": [item for item in previous_manifest.get("conflicts", [])
                                                   if item["path"] not in names]}
    _save_receipt(root, receipt)
    return receipt, discarded


def sync_workspace(workspace, previous_manifest, source_directory, source_manifest, *, cancelled=None):
    """Sync one exact shared revision without replacing unsubmitted local work."""
    root, source = _directory(workspace), _directory(source_directory)
    previous, incoming = _entries(previous_manifest), _entries(source_manifest)
    result = dict(previous)
    conflicts = {item["path"]: item for item in previous_manifest.get("conflicts", [])}
    context = _directory(root.parent / "handoff") / ("sync-" + str(uuid4()))
    for name in sorted(set(previous) | set(incoming)):
        _cancelled(cancelled)
        before, wanted = previous.get(name), incoming.get(name)
        before_sha = before["sha256"] if before else None
        wanted_sha = wanted["sha256"] if wanted else None
        path = _file(root, name)
        if path.exists():
            with path.open("rb") as stream:
                local_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            local_sha = None
        if local_sha == wanted_sha:
            if wanted:
                # Same bytes, but the shared version may have flipped the
                # executable bit. Take it unless this tree changed the mode.
                wanted_mode, before_mode = wanted.get("executable"), (before or {}).get("executable")
                local_mode = bool(path.stat().st_mode & 0o111)
                if wanted_mode is not None and local_mode != wanted_mode and (before_mode is None or local_mode == before_mode):
                    mode = path.stat().st_mode
                    path.chmod(mode | (mode & 0o444) >> 2 if wanted_mode else mode & ~0o111)
                _remember_baseline(root, source, wanted, cancelled)
                result[name] = dict(wanted)
            else:
                result.pop(name, None)
            if conflicts.get(name, {}).get("kind") == "concurrent_changes":
                conflicts.pop(name)
            continue
        if local_sha == before_sha:
            if wanted:
                _copy_verified(source, root, wanted, cancelled)
                _remember_baseline(root, source, wanted, cancelled)
                result[name] = dict(wanted)
            else:
                path.unlink(missing_ok=True)
                result.pop(name, None)
            continue
        if wanted_sha == before_sha:
            continue  # Preserve unpublished native bytes for the same owner.
        context.mkdir(mode=0o700, parents=True, exist_ok=True)
        conflict = {"kind": "concurrent_changes", "path": name, "baseline_sha256": before_sha,
                    "source_sha256": wanted_sha, "candidate_sha256": local_sha}
        if wanted:
            export = context / "incoming"
            export.mkdir(mode=0o700, exist_ok=True)
            _copy_verified(source, export, wanted, cancelled)
            conflict["incoming_path"] = str(export / name)
        if before:
            raw = _read_baseline(root, before)
            target = context / "baseline" / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(raw)
            target.chmod(0o600)
            conflict["baseline_path"] = str(target)
        conflicts[name] = conflict
        # The local conflict is still unpublished, but any later host import
        # must compare against the incoming shared SHA, not the old baseline.
        if wanted:
            _remember_baseline(root, source, wanted, cancelled)
            result[name] = dict(wanted)
        else:
            result.pop(name, None)
    receipt = {**previous_manifest, "files": [result[key] for key in sorted(result)],
               "conflicts": [conflicts[key] for key in sorted(conflicts)],
               "synced_revision": source_manifest.get("revision"),
               "synced_index_sha256": source_manifest.get("index_sha256")}
    _save_receipt(root, receipt)
    return receipt
