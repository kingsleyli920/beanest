"""On-demand Git metadata for one explicitly bound directory; never fetch or write."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time

from .project_catalog import _open_absolute, ProjectCatalogError


MAX_OUTPUT_BYTES = 2 * 1024 * 1024
READ_SECONDS = 5
CODE_STATUS_SECONDS = 15
MAX_DETAIL_FILES = 20


def _read_deadline(deadline):
    return min(time.monotonic() + READ_SECONDS, deadline) if deadline is not None else time.monotonic() + READ_SECONDS


class _GitError(Exception):
    def __init__(self, code):
        self.code = code


def _run(command, directory, arguments, deadline, *, input_bytes=None, allowed_exit_codes=(0,)):
    if time.monotonic() >= deadline:
        raise _GitError("git_timeout")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    process = input_file = None
    try:
        if input_bytes is not None:
            input_file = tempfile.TemporaryFile()
            input_file.write(input_bytes)
            input_file.seek(0)
        process = subprocess.Popen([command, "--no-optional-locks", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=" + os.devnull, "-c", "status.relativePaths=false", *arguments],
            cwd=directory, env=environment, stdin=input_file if input_file is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _GitError("git_timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT_BYTES:
                            raise _GitError("git_output_too_large")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode not in allowed_exit_codes:
            raise _GitError("git_failed")
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise _GitError("git_timeout") from None
    except (OSError, subprocess.SubprocessError):
        raise _GitError("git_failed") from None
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except PermissionError:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait()
            finally:
                process.stdout.close()
        if input_file is not None:
            input_file.close()


def _uses_configured_filter(command, directory, deadline):
    # Git status can execute clean/process filters while comparing bytes.
    # Inspect names and attributes first; never run those project commands.
    config = _run(command, directory, ["config", "--null", "--get-regexp", r"^filter\..*\.(clean|process)$"],
                  deadline, allowed_exit_codes=(0, 1))
    active = set()
    for record in config.split(b"\0"):
        key, _, value = record.partition(b"\n")
        if value.strip():
            active.add(key[len(b"filter."):].rsplit(b".", 1)[0])
    if not active:
        return False
    paths = _run(command, directory, ["ls-files", "--cached", "-z", "--", "."], deadline)
    if not paths:
        return False
    attributes = _run(command, directory, ["check-attr", "-z", "--stdin", "filter"],
                      deadline, input_bytes=paths).split(b"\0")
    if attributes[-1:] == [b""]:
        attributes.pop()
    if len(attributes) % 3:
        raise _GitError("git_invalid_output")
    return any(attributes[index] in active for index in range(2, len(attributes), 3))


def _text(raw):
    return raw.decode("utf-8", errors="replace")


def _porcelain(raw, project_path):
    headers, files = {}, {}
    staged, unstaged, untracked, conflicted = set(), set(), set(), set()
    records = iter(raw.split(b"\0"))
    prefix = os.fsencode(project_path) + b"/" if project_path != "." else b""

    def display(path):
        return _text(path[len(prefix):] if prefix and path.startswith(prefix) else path)

    for record in records:
        if not record:
            continue
        if record.startswith(b"# "):
            key, _, value = record[2:].partition(b" ")
            headers[_text(key)] = _text(value)
            continue
        original = None
        if record.startswith(b"1 "):
            fields = record.split(b" ", 8)
            if len(fields) != 9:
                raise _GitError("git_invalid_output")
            path, xy = fields[8], fields[1]
        elif record.startswith(b"2 "):
            fields = record.split(b" ", 9)
            if len(fields) != 10:
                raise _GitError("git_invalid_output")
            path, xy = fields[9], fields[1]
            original = next(records, None)
            if not original:
                raise _GitError("git_invalid_output")
        elif record.startswith(b"u "):
            fields = record.split(b" ", 10)
            if len(fields) != 11:
                raise _GitError("git_invalid_output")
            path, xy = fields[10], fields[1]
            conflicted.add(path)
        elif record.startswith(b"? "):
            path, xy = record[2:], b"??"
            untracked.add(path)
        elif record.startswith(b"! "):
            continue
        else:
            raise _GitError("git_invalid_output")
        if not path or len(xy) != 2:
            raise _GitError("git_invalid_output")
        if path not in conflicted and path not in untracked:
            if xy[0:1] not in {b".", b" "}:
                staged.add(path)
            if xy[1:2] not in {b".", b" "}:
                unstaged.add(path)
        files[path] = {"path": display(path), "index": chr(xy[0]), "worktree": chr(xy[1]),
                       **({"original_path": display(original)} if original is not None else {})}
    return headers, list(files.values()), {"total": len(files), "staged": len(staged),
        "unstaged": len(unstaged), "untracked": len(untracked), "conflicted": len(conflicted), "tracked": len(files) - len(untracked)}


def _empty_mainline(reason="mainline_unknown"):
    return {"state": "unknown", "reason": reason, "name": None, "ref": None, "source": None,
            "relation": "unknown", "ahead": None, "behind": None, "head": None,
            "scope": "repository_local_refs"}


def _empty_merge_request(reason="mainline_unknown"):
    return {"available": False, "reason": reason, "target_branch": None, "source_branch": None,
            "repository_root": None, "head": None, "mainline_head": None,
            "scope": "repository_local_refs"}


def _mainline_status(command, directory, root, headers, deadline):
    """Describe committed branch ancestry from frozen local refs, never a merge approval."""
    mainline, request = _empty_mainline(), _empty_merge_request()
    request.update(repository_root=str(root), source_branch=headers.get("branch.head"))
    head = headers.get("branch.oid")
    if head == "(initial)":
        mainline["reason"] = request["reason"] = "unborn_branch"
        return mainline, request
    request["head"] = head
    try:
        remote_head = _text(_run(command, directory, ["symbolic-ref", "--quiet",
            "refs/remotes/origin/HEAD"], deadline, allowed_exit_codes=(0, 1))).strip()
        if remote_head.startswith("refs/remotes/origin/") and remote_head != "refs/remotes/origin/HEAD":
            target_ref, source = remote_head, "origin_head"
            target_name = remote_head[len("refs/remotes/origin/"):]
        else:
            refs = _text(_run(command, directory, ["for-each-ref", "--format=%(refname)",
                "refs/heads/main", "refs/heads/master"], deadline)).splitlines()
            refs = [ref for ref in refs if ref in {"refs/heads/main", "refs/heads/master"}]
            if len(refs) != 1:
                reason = "mainline_ambiguous" if refs else "mainline_unknown"
                mainline["reason"] = request["reason"] = reason
                return mainline, request
            target_ref, source = refs[0], "single_local_conventional"
            target_name = target_ref[len("refs/heads/"):]
        target_head = _text(_run(command, directory, ["rev-parse", "--verify",
            target_ref + "^{commit}"], deadline)).strip()
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target_head) is None:
            raise _GitError("git_invalid_output")
        # Use object IDs so a concurrent branch update cannot mix ancestry counts.
        counts = _text(_run(command, directory, ["rev-list", "--left-right", "--count",
            target_head + "..." + head], deadline)).strip().split()
        if len(counts) != 2 or not all(value.isdecimal() for value in counts):
            raise _GitError("git_invalid_output")
        behind, ahead = map(int, counts)
        current = headers.get("branch.head")
        detached = current == "(detached)"
        same_branch = not detached and current == target_name
        relation = "same_branch" if same_branch else "merged" if ahead == 0 else "unmerged"
        mainline.update(state="ready", reason=None, name=target_name, ref=target_ref,
            source=source, relation=relation, ahead=ahead, behind=behind, head=target_head)
        request.update(available=relation == "unmerged" and not detached,
            reason="detached_head" if detached else "already_on_mainline" if same_branch
                else "already_merged" if relation == "merged" else None,
            source_branch=None if detached else current, target_branch=target_name,
            mainline_head=target_head)
    except _GitError as error:
        # A failed supplementary ancestry read must not erase usable file status.
        mainline["reason"] = request["reason"] = error.code
    return mainline, request


def empty_status(reason, *, now=time.time):
    return {"state": "unavailable", "reason": reason, "observed_at": now(),
        "repository_root": None, "project_path": None, "branch": None, "detached": None, "unborn": None,
        "uncommitted": {key: None for key in ("total", "staged", "unstaged", "untracked", "conflicted", "tracked")},
        "files": [], "files_truncated": False, "last_commit": None,
        "upstream": {"state": "unknown", "name": None, "ahead": None, "behind": None,
                     "scope": "repository_local_refs"},
        "mainline": _empty_mainline(), "merge_request": _empty_merge_request()}


def repository_for_file(path, *, deadline=None):
    """Locate the nearest repository of an already authorized exact file.

    Only inspect its ancestors; the caller supplies the delivery authorization.
    A worktree's .git file is a marker too, never a directory to enumerate.
    """
    directory = Path(path).parent
    try:
        os.close(_open_absolute(directory))
        for parent in (directory, *directory.parents):
            try:
                mode = (parent / ".git").lstat().st_mode
            except FileNotFoundError:
                continue
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                return None
            command = shutil.which("git")
            if command is None:
                return None
            raw = _run(command, parent, ["rev-parse", "--show-toplevel"], _read_deadline(deadline))
            root = Path(os.fsdecode(raw.removesuffix(b"\n")))
            return root if root == parent else None
    except (OSError, ValueError, TypeError, ProjectCatalogError, _GitError):
        return None
    return None


def read_status(path, *, now=time.time, deadline=None):
    """Return a summary; unavailable metadata is never a clean/zero result."""
    result = empty_status(None, now=now)
    try:
        directory = Path(path)
        os.close(_open_absolute(directory))
    except (OSError, ValueError, TypeError, ProjectCatalogError):
        result["reason"] = "directory_unavailable"
        return result
    if not any((parent / ".git").exists() for parent in (directory, *directory.parents)):
        result.update(state="not_git", reason="not_git_repository")
        return result
    command = shutil.which("git")
    if command is None:
        result["reason"] = "git_unavailable"
        return result
    deadline = _read_deadline(deadline)
    try:
        root = Path(os.fsdecode(_run(command, directory, ["rev-parse", "--show-toplevel"], deadline).removesuffix(b"\n")))
        project_path = str(directory.relative_to(root)) or "."
        if _uses_configured_filter(command, directory, deadline):
            result.update(reason="git_filter_required", repository_root=str(root), project_path=project_path)
            return result
        raw = _run(command, directory, ["status", "--porcelain=v2", "-z", "--branch", "--ahead-behind",
                                      "--untracked-files=all", "--", "."], deadline)
        headers, files, counts = _porcelain(raw, project_path)
        if (not headers.get("branch.head") or headers.get("branch.oid") != "(initial)"
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", headers.get("branch.oid", "")) is None):
            raise _GitError("git_invalid_output")
        unborn = headers.get("branch.oid") == "(initial)"
        last_commit = None
        if not unborn:
            last = _run(command, directory, ["log", "--no-show-signature", "--no-decorate", "-1",
                "--format=%H%x00%h%x00%s%x00%cI%x00", "--", "."], deadline)
            if last:
                parts = last.split(b"\0")
                if len(parts) != 5 or re.fullmatch(rb"[0-9a-f]{40,64}", parts[0]) is None:
                    raise _GitError("git_invalid_output")
                last_commit = dict(zip(("hash", "short_hash", "subject", "committed_at"), map(_text, parts[:4])))
        # A captured, ignored directory inside an unrelated ancestor repository
        # has no project history to justify a reassuring "clean" label.
        if root != directory and last_commit is None and counts["total"] == 0:
            result.update(reason="no_tracked_project", repository_root=str(root), project_path=project_path)
            return result
        upstream = {"state": "none", "name": headers.get("branch.upstream"), "ahead": None, "behind": None,
                    "scope": "repository_local_refs"}
        if upstream["name"]:
            match = re.fullmatch(r"\+([0-9]+) -([0-9]+)", headers.get("branch.ab", ""))
            upstream.update(state="ready" if match else "unknown",
                            ahead=int(match[1]) if match else None, behind=int(match[2]) if match else None)
        detached = headers.get("branch.head") == "(detached)"
        mainline, merge_request = _mainline_status(command, directory, root, headers, deadline)
        result.update(state="ready", reason=None, repository_root=str(root), project_path=project_path,
            branch=None if detached else headers.get("branch.head"), detached=detached, unborn=unborn,
            uncommitted=counts, files=files[:MAX_DETAIL_FILES], files_truncated=len(files) > MAX_DETAIL_FILES,
            last_commit=last_commit, upstream=upstream, mainline=mainline, merge_request=merge_request)
    except _GitError as error:
        result["reason"] = error.code
    except (OSError, ValueError):
        result["reason"] = "git_invalid_output"
    return result


def repository_identity(path, *, deadline=None):
    """Identify an authorized Git directory without enumerating working files."""
    command, deadline = shutil.which("git"), _read_deadline(deadline)
    if command is None:
        raise _GitError("git_unavailable")
    try:
        directory = Path(path)
        os.close(_open_absolute(directory))
        root = Path(os.fsdecode(_run(command, directory, ["rev-parse", "--show-toplevel"], deadline).removesuffix(b"\n")))
        if not root.is_absolute():
            raise ValueError("relative Git root")
        directory.relative_to(root)
        common = Path(os.fsdecode(_run(command, directory,
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], deadline).removesuffix(b"\n")))
        if not common.is_absolute():
            raise ValueError("relative Git directory")
        metadata = common.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("invalid Git directory")
        return {"root": str(root), "common": str(common), "device": metadata.st_dev, "inode": metadata.st_ino}
    except (OSError, ValueError, TypeError, ProjectCatalogError):
        raise _GitError("git_invalid_output") from None


def local_delivery_refs(path, mainline_name=None, *, deadline=None):
    """Freeze this linked worktree and its actual local mainline, not origin's cache."""
    command, deadline = shutil.which("git"), _read_deadline(deadline)
    if command is None:
        raise _GitError("mainline_unknown")
    root = Path(os.fsdecode(_run(command, path, ["rev-parse", "--show-toplevel"], deadline).removesuffix(b"\n")))
    try:
        if not root.is_absolute():
            raise ValueError("relative Git root")
        project_path = str(Path(path).relative_to(root)) or "."
    except (TypeError, ValueError):
        raise _GitError("git_invalid_output") from None
    if not mainline_name:
        head = _text(_run(command, path, ["rev-parse", "--verify", "HEAD^{commit}"], deadline)).strip()
        branch = _text(_run(command, path, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline,
                            allowed_exit_codes=(0, 1))).strip() or "(detached)"
        mainline, _ = _mainline_status(command, path, root, {"branch.head": branch, "branch.oid": head}, deadline)
        if mainline["state"] != "ready":
            raise _GitError(mainline["reason"])
        mainline_name = mainline["name"]
    common = _text(_run(command, path, ["rev-parse", "--path-format=absolute", "--git-common-dir"], deadline)).strip()
    values = []
    for ref in ("HEAD", "refs/heads/" + mainline_name):
        value = _text(_run(command, path, ["rev-parse", "--verify", ref + "^{commit}"], deadline)).strip()
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
            raise _GitError("git_invalid_output")
        values.append(value)
    counts = _text(_run(command, path, ["rev-list", "--count", values[1] + ".." + values[0]], deadline)).strip()
    if not counts.isdecimal():
        raise _GitError("git_invalid_output")
    return {"common": common, "head": values[0], "main_head": values[1], "included": int(counts) == 0,
            "mainline_name": mainline_name, "repository_root": str(root), "project_path": project_path}


def _github_url(command, directory, deadline):
    """Use only configured canonical GitHub URLs; never execute remote helpers."""
    raw = _text(_run(command, directory, ["config", "--get", "remote.origin.url"], deadline,
        allowed_exit_codes=(0, 1))).strip()
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", raw)
    protocol = "https"
    if match is None:
        match = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", raw)
        protocol = "ssh"
    if match is None or any(part in {".", ".."} for part in match[1].split("/")):
        raise _GitError("github_remote_unavailable")
    url = ("https://github.com/" if protocol == "https" else "ssh://git@github.com/") + match[1] + ".git"
    rewrites = _run(command, directory, ["config", "--null", "--get-regexp", r"^url\..*\.insteadof$"],
        deadline, allowed_exit_codes=(0, 1))
    if any(raw.startswith(_text(record.partition(b"\n")[2])) or url.startswith(_text(record.partition(b"\n")[2]))
            for record in rewrites.split(b"\0") if b"\n" in record):
        raise _GitError("github_remote_rewrite")
    return url


def read_github_main(path, mainline_name, main_head, *, now=time.time, deadline=None):
    result = {"state": "unknown", "reason": "github_remote_unavailable", "checked_at": now()}
    command, deadline = shutil.which("git"), _read_deadline(deadline)
    if command is None:
        return result
    try:
        url = _github_url(command, path, deadline)
        raw = _run(command, path, ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "protocol.ssh.allow=always", "-c", "core.sshCommand=ssh -oBatchMode=yes",
            "-c", "http.followRedirects=false", "ls-remote", "--refs", "--exit-code", url,
            "refs/heads/" + mainline_name], deadline)
        lines = raw.splitlines()
        if len(lines) != 1:
            raise _GitError("remote_mainline_unknown")
        fields = lines[0].split(b"\t")
        if (len(fields) != 2 or fields[1] != ("refs/heads/" + mainline_name).encode()
                or re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]) is None):
            raise _GitError("git_invalid_output")
        remote_head = _text(fields[0])
        if remote_head == main_head:
            return {**result, "state": "confirmed", "reason": None}
        # A newer remote is also sufficient when its object is already local.
        counts = _text(_run(command, path, ["rev-list", "--left-right", "--count",
            main_head + "..." + remote_head], deadline)).strip().split()
        if len(counts) != 2 or not all(value.isdecimal() for value in counts):
            raise _GitError("remote_ancestry_unknown")
        local_only, remote_only = map(int, counts)
        if local_only == 0:
            return {**result, "state": "confirmed", "reason": None}
        return {**result, "state": "pending", "reason": "local_mainline_not_on_remote"}
    except _GitError as error:
        return {**result, "reason": error.code}


def committed_file(path, main_head, project_path, name, *, deadline=None):
    """Hash actual main-tree blob bytes; Git object IDs are not file SHA-256."""
    command, deadline = shutil.which("git"), _read_deadline(deadline)
    full_path = name if project_path == "." else project_path + "/" + name
    tree = _run(command, path, ["ls-tree", "--full-tree", "-z", main_head, "--", full_path], deadline)
    if not tree:
        return None
    records = tree.rstrip(b"\0").split(b"\0")
    if len(records) != 1:
        raise _GitError("git_invalid_output")
    metadata, separator, filename = records[0].partition(b"\t")
    fields = metadata.split()
    if not separator or filename != full_path.encode() or len(fields) != 3 or fields[1] != b"blob":
        raise _GitError("git_invalid_output")
    if fields[0] not in {b"100644", b"100755"}:
        raise _GitError("unsupported_mainline_file")
    raw = _run(command, path, ["cat-file", "blob", _text(fields[2])], deadline)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "executable": fields[0] == b"100755"}
