"""Opt-in, bounded Python unittest execution in an explicitly configured Docker.

The host never imports the supplied files. Exit status and output are execution
observations, not a trusted test verdict: supplied Python can replace unittest,
print a fake report, or exit early. Docker is part of the trusted computing base.
"""
from __future__ import annotations

import asyncio
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import time
from uuid import UUID, uuid4
from urllib.parse import urlsplit


MAX_FILES = 16
MAX_SOURCE = 64 * 1024
MAX_OUTPUT = 32 * 1024
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\.py\Z")
_DIGEST = re.compile(r"docker\.io/library/python@sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = "local-agent-coordinator.python-runner"

# This is trusted, fixed bootstrap text, passed to the container interpreter.
# Sources for syscall/arch numbers and filter layout:
# https://github.com/torvalds/linux/blob/master/include/uapi/asm-generic/unistd.h
# https://github.com/torvalds/linux/blob/master/arch/x86/entry/syscalls/syscall_64.tbl
# https://github.com/torvalds/linux/blob/master/include/uapi/linux/{audit,seccomp,prctl,filter}.h
# https://kernel.org/doc/html/latest/userspace-api/seccomp_filter.html
BOOTSTRAP = r'''
import ctypes, errno, hashlib, json, os, platform, re, resource, sys, unittest

def fail():
    os.write(2, b"runner_bootstrap_failed\n")
    os._exit(125)

try:
    if sys.platform != "linux" or os.getuid() == 0:
        fail()
    payload = sys.stdin.buffer.read(524289)
    if len(payload) > 524288:
        fail()
    snapshot = json.loads(payload)
    files = snapshot["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 16:
        fail()
    os.chdir("/work")
    total, seen = 0, set()
    for file in files:
        path, content = file["path"], file["content"]
        if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}\.py", path)
                or path in seen or not isinstance(content, str) or "\0" in content):
            fail()
        seen.add(path)
        data = content.encode("utf-8")
        total += len(data)
        if total > 65536 or hashlib.sha256(data).hexdigest() != file["sha256"]:
            fail()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    machine = platform.machine().lower()
    if machine == "aarch64":
        arch = 0xc00000b7
        blocked = set(range(198, 213)) | {220, 221, 242, 243, 269, 281, 435}
    elif machine == "x86_64":
        arch = 0xc000003e
        blocked = set(range(41, 60)) | {288, 299, 307, 322, 435}
    else:
        fail()
    # io_uring can submit operations indirectly; prohibit its entry points too.
    blocked.update((425, 426, 427))
    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]
    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]
    # LD W ABS arch; JEQ expected; KILL wrong ABI; LD W ABS syscall number.
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, arch),
                    (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    # x32 shares AUDIT_ARCH_X86_64; never let its alternate numbers bypass us.
    if machine == "x86_64":
        instructions += [(0x45, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    for number in sorted(blocked):
        instructions += [(0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    instructions += [(0x06, 0, 0, 0x7fff0000)]
    filters = (Filter * len(instructions))(*(Filter(*item) for item in instructions))
    program = Program(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        fail()
    if libc.prctl(22, 2, ctypes.addressof(program), 0, 0) != 0:
        fail()
except BaseException:
    fail()

# No supplied module is loaded before the irreversible process-wide filter.
sys.path.insert(0, "/work")
suite = unittest.defaultTestLoader.discover("/work", pattern="test*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
'''


class _RunnerError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _OutputLimit(Exception):
    pass


class _CommandError(_RunnerError):
    def __init__(self, code, *, cancelled=False):
        super().__init__(code)
        self.cancelled = cancelled


async def _protected(awaitable):
    """Finish an owned launch/cleanup even if cancellation is repeated."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


async def _reap(process):
    # A launcher can exit while descendants keep our pipes open. The process
    # group is ours even after its leader exits, so always terminate the group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), .5)
        except asyncio.TimeoutError:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await asyncio.wait_for(process.wait(), 3)
    deadline = time.monotonic() + 1
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # EPERM is not proof of absence. Give group teardown the same
            # bounded grace period; only ESRCH confirms disappearance.
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("owned_process_group_unconfirmed")
        await asyncio.sleep(.01)


async def _exec(command, *, data=None, timeout=8, limit=8192):
    """Bound both pipes and own the CLI process group, including on cancellation."""
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "LANG": "C.UTF-8"},
        start_new_session=True))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        process, _ = await _protected(launch)
        try:
            await _protected(_reap(process))
        except (OSError, asyncio.TimeoutError):
            raise _CommandError("cli_cleanup_unconfirmed", cancelled=True) from None
        raise
    output = {"stdout": bytearray(), "stderr": bytearray()}

    async def read(name):
        while chunk := await getattr(process, name).read(4096):
            capacity = limit - sum(map(len, output.values()))
            output[name].extend(chunk[:capacity])
            if len(chunk) > capacity:
                raise _OutputLimit

    async def feed():
        if data is not None:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    tasks = [asyncio.create_task(read(name)) for name in output]
    tasks += [asyncio.create_task(feed()), asyncio.create_task(process.wait())]
    reason, cancelled, reap_error = None, False, None
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout)
    except asyncio.TimeoutError:
        reason = "timed_out"
    except _OutputLimit:
        reason = "output_limit"
    except asyncio.CancelledError:
        cancelled = True
    finally:
        try:
            _, recancelled = await _protected(_reap(process))
            cancelled |= recancelled
        except (OSError, asyncio.TimeoutError) as error:
            reap_error = error
        for task in tasks:
            if not task.done():
                task.cancel()
        _, recancelled = await _protected(asyncio.gather(*tasks, return_exceptions=True))
        cancelled |= recancelled
    if reap_error is not None:
        raise _CommandError("cli_cleanup_unconfirmed", cancelled=cancelled) from reap_error
    if cancelled:
        raise asyncio.CancelledError
    return {"code": process.returncode, "reason": reason,
            **{name: value.decode("utf-8", errors="replace") for name, value in output.items()}}


def _private(path, *, directory=False):
    metadata = path.lstat()
    kind = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (not kind or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
            or (not directory and metadata.st_nlink != 1)):
        raise _RunnerError("invalid_configuration")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_private_directory(path):
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        _private(directory, directory=True)
        _sync_directory(directory.parent)
    _private(path, directory=True)


def _local_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise _RunnerError("invalid_configuration")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/")
            or parsed.query or parsed.fragment or "\0" in endpoint):
        raise _RunnerError("local_unix_endpoint_required")
    try:
        if not stat.S_ISSOCK(Path(parsed.path).stat().st_mode):
            raise _RunnerError("docker_unavailable")
    except OSError:
        raise _RunnerError("docker_unavailable") from None
    return endpoint


def _snapshot(snapshot):
    if (not isinstance(snapshot, dict) or type(snapshot.get("revision")) is not int
            or snapshot["revision"] < 0 or not isinstance(snapshot.get("files"), list)
            or not 1 <= len(snapshot["files"]) <= MAX_FILES):
        raise _RunnerError("invalid_snapshot")
    files, paths, total = [], set(), 0
    for item in snapshot["files"]:
        if not isinstance(item, dict):
            raise _RunnerError("invalid_snapshot")
        path, content = item.get("path"), item.get("content")
        if (not isinstance(path, str) or not _NAME.fullmatch(path) or path in paths
                or not isinstance(content, str) or "\0" in content):
            raise _RunnerError("invalid_snapshot")
        try:
            raw = content.encode("utf-8")
        except UnicodeError:
            raise _RunnerError("invalid_snapshot") from None
        total += len(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if total > MAX_SOURCE or item.get("sha256") != digest:
            raise _RunnerError("invalid_snapshot")
        paths.add(path)
        files.append({"path": path, "content": content, "sha256": digest})
    return json.dumps({"revision": snapshot["revision"], "files": files},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class PythonRunner:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._ready_cache = None

    def _settings(self):
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise _RunnerError("invalid_configuration")
        if not (self.root / "runner-config.json").exists():
            raise _RunnerError("not_configured")
        try:
            _private(self.root, directory=True)
            config_path = self.root / "runner-config.json"
            _private(config_path)
            if config_path.stat().st_size > 4096:
                raise _RunnerError("invalid_configuration")
            config = json.loads(config_path.read_text())
            if not isinstance(config, dict) or set(config) != {"endpoint", "image_digest"}:
                raise _RunnerError("invalid_configuration")
            endpoint, digest = config["endpoint"], config["image_digest"]
            _local_endpoint(endpoint)
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise _RunnerError("pinned_official_image_required")
            client_config = self.root / "docker-config"
            _private(client_config, directory=True)
            _private(client_config / "config.json")
            if (client_config / "config.json").read_text().strip() != "{}":
                raise _RunnerError("anonymous_client_config_required")
            executable = shutil.which("docker")
            if executable is None:
                raise _RunnerError("docker_unavailable")
            return {**config, "command": [executable, "--config", str(client_config), "--host", endpoint],
                    "fingerprint": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()}
        except (OSError, ValueError) as error:
            if isinstance(error, _RunnerError):
                raise
            raise _RunnerError("invalid_configuration") from None

    def _journals(self):
        directory = self.root / "attempts"
        if not directory.exists():
            return []
        _private(directory, directory=True)
        records = []
        for path in sorted(directory.glob("*.json")):
            _private(path)
            if path.stat().st_size > 8192:
                raise _RunnerError("cleanup_pending")
            value = json.loads(path.read_text())
            if (not isinstance(value, dict) or value.get("run_id") != path.stem
                    or str(UUID(path.stem)) != path.stem
                    or value.get("name") != "lac-python-" + path.stem
                    or value.get("owner") != path.stem
                    or not isinstance(value.get("fingerprint"), str)
                    or not _ID.fullmatch(value["fingerprint"])
                    or type(value.get("cleanup_confirmed")) is not bool
                    or value.get("phase") not in {"prepared", "created", "running", "finished", "unknown"}
                    or (value.get("container_id") is not None
                        and (not isinstance(value["container_id"], str) or not _ID.fullmatch(value["container_id"])))):
                raise _RunnerError("cleanup_pending")
            records.append(value)
        return records

    def _save(self, record):
        directory = self.root / "attempts"
        directory.mkdir(mode=0o700, exist_ok=True)
        _private(directory, directory=True)
        temporary = directory / (".write-" + str(uuid4()))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / (record["run_id"] + ".json"))
            _sync_directory(directory)
            _sync_directory(self.root)  # Persist a newly created attempts entry too.
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _lock(self):
        fd = os.open(self.root / "runner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1 or metadata.st_mode & 0o077):
                raise _RunnerError("invalid_configuration")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _RunnerError("runner_busy") from None
            yield
        finally:
            os.close(fd)

    async def _command(self, settings, args, **kwargs):
        return await _exec(settings["command"] + args, **kwargs)

    async def _available(self, settings):
        version = await self._command(settings, ["version", "--format", "{{.Server.Version}}"])
        if version["reason"] or version["code"] != 0:
            raise _RunnerError("docker_unavailable")
        image = await self._command(settings, ["image", "inspect", "--format", "{{.Os}}/{{.Architecture}}",
                                               settings["image_digest"]])
        if image["reason"] or image["code"] != 0:
            raise _RunnerError("image_not_cached")
        if image["stdout"].strip() not in {"linux/arm64", "linux/amd64"}:
            raise _RunnerError("unsupported_architecture")
        return image["stdout"].strip()

    async def ready(self):
        try:
            settings = self._settings()
            if any(not record.get("cleanup_confirmed") for record in self._journals()):
                raise _RunnerError("cleanup_pending")
            if (self._ready_cache and self._ready_cache[0] == settings["fingerprint"]
                    and time.monotonic() - self._ready_cache[1] < 20):
                return dict(self._ready_cache[2])
            architecture = await self._available(settings)
            result = {"available": True, "reason": None, "image_digest": settings["image_digest"],
                      "architecture": architecture}
            self._ready_cache = (settings["fingerprint"], time.monotonic(), result)
            return dict(result)
        except (OSError, ValueError) as error:
            return {"available": False, "reason": error.code if isinstance(error, _RunnerError) else "invalid_configuration",
                    "image_digest": None}

    def cancellation_confirmed(self, run_id):
        """Return a persisted cleanup receipt, never infer it from cancellation."""
        try:
            if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
                return False
            return any(record["run_id"] == run_id and record.get("phase") == "finished"
                       and record.get("cleanup_confirmed") is True for record in self._journals())
        except (OSError, ValueError):
            return False

    @staticmethod
    def _absent(response, identity):
        # A missing daemon socket also says "no such file". That is not proof
        # that an owned container no longer exists.
        message = response["stderr"].lower()
        return (response["reason"] is None and response["code"] != 0
                and any(phrase + identity.lower() in message for phrase in
                        ("no such object: ", "no such container: ")))

    async def _cleanup(self, settings, record):
        if record["fingerprint"] != settings["fingerprint"]:
            return False
        target = record.get("container_id") or record["name"]
        inspected = await self._command(settings, ["container", "inspect", "--format",
            '{{.Id}}|{{index .Config.Labels "' + _LABEL + '"}}', target])
        if inspected["reason"]:
            return False
        if inspected["code"] != 0:
            return self._absent(inspected, target)
        identity = inspected["stdout"].strip().split("|")
        if (len(identity) != 2 or not _ID.fullmatch(identity[0]) or identity[1] != record["owner"]
                or (record.get("container_id") and record["container_id"] != identity[0])):
            return False
        removed = await self._command(settings, ["container", "rm", "--force", identity[0]])
        if removed["reason"] or removed["code"] != 0:
            return False
        checked = await self._command(settings, ["container", "inspect", "--format", "{{.Id}}", identity[0]])
        return self._absent(checked, identity[0])

    async def recover_owned(self):
        """Explicitly clean only attempts in this private journal; never replay."""
        try:
            settings = self._settings()
            with self._lock():
                for record in self._journals():
                    if record.get("cleanup_confirmed"):
                        continue
                    confirmed, cancelled = await _protected(self._cleanup(settings, record))
                    record.update(cleanup_confirmed=confirmed, phase="finished" if confirmed else "unknown",
                                  status="interrupted")
                    self._save(record)
                    if cancelled:
                        raise asyncio.CancelledError
                    if not confirmed:
                        return {"cleanup_confirmed": False, "reason": "cleanup_pending"}
            return {"cleanup_confirmed": True, "reason": None}
        except (OSError, ValueError) as error:
            return {"cleanup_confirmed": False,
                    "reason": error.code if isinstance(error, _RunnerError) else "invalid_configuration"}

    async def run(self, snapshot: dict, timeout=15, *, run_id=None):
        result = {"status": "failed", "reason": None, "exit_code": None, "stdout": "", "stderr": "",
                  "truncated": False, "cleanup_confirmed": True, "tests_trusted": False}
        try:
            data = _snapshot(snapshot)
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or not 0 < timeout <= 60):
                raise _RunnerError("invalid_timeout")
            run_id = str(uuid4()) if run_id is None else run_id
            if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
                raise _RunnerError("invalid_run_id")
            settings = self._settings()
            with self._lock():
                journals = self._journals()
                if any(record["run_id"] == run_id for record in journals):
                    raise _RunnerError("run_id_reused")
                if any(not record.get("cleanup_confirmed") for record in journals):
                    raise _RunnerError("cleanup_pending")
                await self._available(settings)
                record = {"run_id": run_id, "name": "lac-python-" + run_id, "owner": run_id,
                          "fingerprint": settings["fingerprint"], "revision": snapshot["revision"],
                          "snapshot_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                              ensure_ascii=True, separators=(",", ":")).encode()).hexdigest(),
                          "phase": "prepared", "container_id": None, "cleanup_confirmed": False}
                self._save(record)  # Crash recovery identity exists before Docker can create anything.
                cancelled, cli_confirmed = False, True
                result.update(run_id=run_id, revision=snapshot["revision"], image_digest=settings["image_digest"],
                              snapshot_sha256=record["snapshot_sha256"])
                try:
                    created = await self._command(settings, [
                        "create", "--pull=never", "--interactive", "--name", record["name"],
                        "--label", _LABEL + "=" + run_id, "--network=none", "--read-only",
                        "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges=true",
                        "--pids-limit=32", "--memory=256m", "--memory-swap=256m", "--cpus=1",
                        "--ulimit=nofile=64:64", "--ulimit=fsize=1048576:1048576", "--log-driver=none",
                        "--tmpfs=/work:rw,noexec,nosuid,nodev,size=8m,mode=0700,uid=65534,gid=65534",
                        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m,mode=0700,uid=65534,gid=65534",
                        "--workdir=/work", settings["image_digest"], "python", "-I", "-S", "-B", "-u", "-c", BOOTSTRAP,
                    ], timeout=15)
                    container_id = created["stdout"].strip()
                    if created["reason"] or created["code"] != 0 or not _ID.fullmatch(container_id):
                        result["reason"] = "container_create_failed"
                    else:
                        record.update(container_id=container_id, phase="created")
                        self._save(record)
                        record["phase"] = "running"
                        self._save(record)
                        execution = await self._command(settings, ["start", "--attach", "--interactive", container_id],
                                                        data=data, timeout=timeout, limit=MAX_OUTPUT)
                        result.update(stdout=execution["stdout"], stderr=execution["stderr"],
                                      truncated=execution["reason"] == "output_limit")
                        if execution["reason"]:
                            result.update(status=execution["reason"], reason=execution["reason"])
                        else:
                            observed = await self._command(settings, ["container", "inspect", "--format",
                                "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}", container_id])
                            state = observed["stdout"].strip().split("|")
                            if (observed["reason"] or observed["code"] != 0 or len(state) != 3
                                    or state[0] != "exited" or not state[1].isdigit()):
                                result.update(status="unknown", reason="execution_state_unknown")
                            else:
                                code = int(state[1])
                                result.update(status="completed" if code == 0 else "failed", exit_code=code,
                                              oom_killed=state[2] == "true")
                except asyncio.CancelledError:
                    cancelled = True
                    result.update(status="cancelled", reason="cancelled")
                except _CommandError as error:
                    cli_confirmed = False
                    cancelled |= error.cancelled
                    result.update(status="unknown", reason=error.code)
                except (OSError, ValueError):
                    result.update(status="unknown", reason="execution_state_unknown")
                finally:
                    try:
                        confirmed, recancelled = await _protected(self._cleanup(settings, record))
                    except (OSError, ValueError):
                        confirmed, recancelled = False, False
                    cancelled |= recancelled
                    confirmed &= cli_confirmed
                    result["cleanup_confirmed"] = confirmed
                    if not confirmed:
                        result.update(status="unknown", reason="cleanup_pending")
                    record.update(phase="finished" if confirmed else "unknown", status=result["status"],
                                  cleanup_confirmed=confirmed)
                    try:
                        self._save(record)
                    except (OSError, ValueError):
                        result.update(status="unknown", reason="journal_unconfirmed", cleanup_confirmed=False)
                if cancelled:
                    raise asyncio.CancelledError
        except (OSError, ValueError) as error:
            result["reason"] = error.code if isinstance(error, _RunnerError) else "invalid_configuration"
            if result["reason"] == "cleanup_pending":
                result.update(status="unknown", cleanup_confirmed=False)
            elif result["reason"] == "run_id_reused":
                result["cleanup_confirmed"] = self.cancellation_confirmed(run_id)
        return result


async def _prepare(root):
    """Explicit anonymous pull only; ordinary ready/run paths never pull."""
    runner = PythonRunner(root)
    try:
        if not runner.root.is_absolute() or ".." in runner.root.parts or runner.root.is_symlink():
            raise _RunnerError("invalid_configuration")
        if (runner.root / "runner-config.json").exists():
            raise _RunnerError("already_configured")
        if runner.root.exists() and runner._journals():
            raise _RunnerError("existing_attempts_require_configuration")
        executable = shutil.which("docker")
        if executable is None:
            raise _RunnerError("docker_unavailable")
        if not os.environ.get("DOCKER_CONTEXT") and os.environ.get("DOCKER_HOST"):
            endpoint = os.environ["DOCKER_HOST"]
        else:
            # Reading the selected context is offline. Only this query uses the
            # user's config; every daemon/pull command uses the empty private one.
            user_config = os.environ.get("DOCKER_CONFIG") or str(Path.home() / ".docker")
            args = [executable, "--config", user_config, "context", "inspect"]
            if os.environ.get("DOCKER_CONTEXT"):
                args.append(os.environ["DOCKER_CONTEXT"])
            queried = await _exec(args + ["--format", "{{json .Endpoints.docker.Host}}"])
            if queried["reason"] or queried["code"] != 0:
                raise _RunnerError("endpoint_unavailable")
            endpoint = json.loads(queried["stdout"])
        _local_endpoint(endpoint)  # Refuse remote targets before any daemon call.
        _make_private_directory(runner.root)
        with runner._lock():
            if (runner.root / "runner-config.json").exists():
                raise _RunnerError("already_configured")
            client = runner.root / "docker-config"
            client.mkdir(mode=0o700, exist_ok=True)
            _private(client, directory=True)
            anonymous = client / "config.json"
            if not anonymous.exists():
                fd = os.open(anonymous, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write("{}\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                _sync_directory(client)
            _private(anonymous)
            if anonymous.read_text().strip() != "{}":
                raise _RunnerError("anonymous_client_config_required")
            base = [executable, "--config", str(client), "--host", endpoint]
            available = await _exec(base + ["version", "--format", "{{.Server.Version}}"])
            if available["reason"] or available["code"] != 0:
                raise _RunnerError("docker_unavailable")
            pulled = await _exec(base + ["pull", "--quiet", "docker.io/library/python:3.14-slim"], timeout=180)
            if pulled["reason"] or pulled["code"] != 0:
                raise _RunnerError("image_preparation_unconfirmed")
            inspected = await _exec(base + ["image", "inspect", "--format", "{{json .RepoDigests}}",
                                           "docker.io/library/python:3.14-slim"])
            if inspected["reason"] or inspected["code"] != 0:
                raise _RunnerError("image_preparation_unconfirmed")
            digests = json.loads(inspected["stdout"])
            if not isinstance(digests, list):
                raise _RunnerError("image_preparation_unconfirmed")
            digest = next(("docker.io/library/" + item if item.startswith("python@") else item
                           for item in digests if isinstance(item, str)
                           and re.fullmatch(r"(?:docker.io/library/)?python@sha256:[0-9a-f]{64}", item)), None)
            if digest is None:
                raise _RunnerError("pinned_official_image_required")
            temporary = runner.root / (".config-" + str(uuid4()))
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump({"endpoint": endpoint, "image_digest": digest}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, runner.root / "runner-config.json")
                _sync_directory(runner.root)
            finally:
                temporary.unlink(missing_ok=True)
            # Reuse runtime configuration validation, rather than adding a
            # second interpretation of endpoints, digests, or client credentials.
            runner._settings()
        return {"prepared": True, "reason": None, "image_digest": digest}
    except (OSError, ValueError) as error:
        return {"prepared": False,
                "reason": error.code if isinstance(error, _RunnerError) else "invalid_configuration"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check", "recover"))
    parser.add_argument("--root", type=Path, required=True, help="Absolute private runner directory")
    args = parser.parse_args(argv)
    runner = PythonRunner(args.root)
    if args.command == "prepare":
        operation, success = _prepare(args.root), "prepared"
    elif args.command == "check":
        operation, success = runner.ready(), "available"
    else:
        operation, success = runner.recover_owned(), "cleanup_confirmed"
    result = asyncio.run(operation)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result[success] else 1


if __name__ == "__main__":
    raise SystemExit(main())
