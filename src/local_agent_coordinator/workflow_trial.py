"""Explicit, bounded multi-phase experiment; this is not a product workflow engine.

Only ``main`` starts a service. Reading a journal, importing this module, or
reopening an interrupted trial never dispatches a model call.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time
from uuid import uuid4


MAX_SECONDS = 7200
MAX_CALLS = 96
MAX_EXECUTIONS = 24
MAX_CALL_SECONDS = 600
MAX_SOURCE_BYTES = 65536
PHASE_CALLS = 8
PHASE_EXECUTIONS = 2
PARTICIPANTS = ("claude", "codex")
JOURNAL_NAME = "workflow-trial.json"
RESULTS_NAME = "final-results.json"
_STATUSES = {"prepared", "running", "completed", "stopped", "unknown", "interrupted"}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


class TrialError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def validate_plan(plan):
    if (not isinstance(plan, dict) or set(plan) != {"goal", "phases"}
            or not _text(plan["goal"], 3000) or not isinstance(plan["phases"], list)
            or not 1 <= len(plan["phases"]) <= 16):
        raise TrialError("invalid_trial_plan")
    for phase in plan["phases"]:
        if (not isinstance(phase, dict) or set(phase) != {"title", "goal", "target"}
                or not _text(phase["title"], 160) or not _text(phase["goal"], 3000)
                or phase["target"] not in PARTICIPANTS):
            raise TrialError("invalid_trial_plan")
    try:
        return _json(json.dumps(plan, ensure_ascii=False, allow_nan=False))
    except (ValueError, UnicodeError):
        raise TrialError("invalid_trial_plan") from None


def validate_limits(*, max_calls, max_executions, total_timeout, call_timeout, poll_interval=1):
    if (type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS
            or type(max_executions) is not int or not 1 <= max_executions <= MAX_EXECUTIONS):
        raise TrialError("invalid_trial_limits")
    for value, maximum in ((total_timeout, MAX_SECONDS), (call_timeout, MAX_CALL_SECONDS),
                           (poll_interval, 5)):
        if type(value) not in (int, float) or not 0 < value <= maximum or not math.isfinite(value):
            raise TrialError("invalid_trial_limits")


def _directory(path, *, create=False, private=True):
    """Walk an absolute path without following an ancestor symlink."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise TrialError("unsafe_trial_path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise TrialError("unsafe_trial_path")
        result, descriptor = descriptor, None
        return result
    except OSError:
        raise TrialError("unsafe_trial_path") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_file(descriptor):
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise TrialError("unsafe_trial_storage")


def load_plan(path):
    path = Path(path).absolute()
    directory = _directory(path.parent, private=False)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise TrialError("invalid_trial_plan")
            text = stream.read(128 * 1024 + 1)
        if len(text) > 128 * 1024:
            raise TrialError("invalid_trial_plan")
        return validate_plan(_json(text))
    except (OSError, ValueError, UnicodeError):
        raise TrialError("invalid_trial_plan") from None
    finally:
        os.close(directory)


class TrialJournal:
    """Private atomic journal and a lifetime single-instance lock."""

    def __init__(self, runtime):
        self.root = _directory(runtime, create=True)
        self.lock = None
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                self.lock = os.open(".workflow-trial.lock", flags | os.O_CREAT | os.O_EXCL,
                                    0o600, dir_fd=self.root)
                os.fsync(self.root)
            except FileExistsError:
                self.lock = os.open(".workflow-trial.lock", flags, dir_fd=self.root)
            _private_file(self.lock)
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TrialError("trial_runtime_in_use") from None
            self.data = self._read()
        except OSError:
            self.close()
            raise TrialError("unsafe_trial_storage") from None
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            descriptor = os.open(JOURNAL_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=self.root)
        except FileNotFoundError:
            return None
        except OSError:
            raise TrialError("unsafe_trial_storage") from None
        try:
            _private_file(descriptor)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError
            data = _json(raw)
            if (not isinstance(data, dict) or data.get("schema") != 1
                    or data.get("status") not in _STATUSES
                    or not isinstance(data.get("phases"), list)):
                raise ValueError
            validate_plan(data.get("plan"))
            if len(data["phases"]) != len(data["plan"]["phases"]):
                raise ValueError
            for phase, planned in zip(data["phases"], data["plan"]["phases"]):
                if (not isinstance(phase, dict)
                        or any(phase.get(key) != planned[key] for key in ("title", "goal", "target"))
                        or not _text(phase.get("request_id"), 512)
                        or (phase.get("room_id") is not None and not _text(phase["room_id"], 512))
                        or type(phase.get("used")) is not int or not 0 <= phase["used"] <= PHASE_CALLS
                        or type(phase.get("executions")) is not int
                        or not 0 <= phase["executions"] <= PHASE_EXECUTIONS):
                    raise ValueError
            return data
        except (ValueError, UnicodeError, KeyError):
            raise TrialError("invalid_trial_journal") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    def read_only(cls, runtime):
        reader = cls.__new__(cls)
        reader.root, reader.lock = _directory(runtime), None
        try:
            return reader._read()
        finally:
            reader.close()

    def _atomic(self, filename, data, maximum):
        name = ".workflow-trial-" + uuid4().hex
        try:
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
            if len(raw) > maximum:
                raise TrialError("trial_journal_too_large")
            # Replacing a corrupt or externally substituted journal must fail closed.
            try:
                old = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                              dir_fd=self.root)
            except FileNotFoundError:
                pass
            else:
                try:
                    _private_file(old)
                finally:
                    os.close(old)
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=self.root)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, filename, src_dir_fd=self.root, dst_dir_fd=self.root)
            os.fsync(self.root)
        except (OSError, ValueError):
            raise TrialError("trial_journal_write_failed") from None
        finally:
            try:
                os.unlink(name, dir_fd=self.root)
            except FileNotFoundError:
                pass

    def save(self):
        self._atomic(JOURNAL_NAME, self.data, 1024 * 1024)

    def save_result(self, observation):
        self._atomic(RESULTS_NAME, {"schema": 1, **observation,
                                  "acceptance": "not_independently_verified"}, 4 * 1024 * 1024)

    def require_fresh_runtime(self):
        """Never let an absent trial journal authorize recovery of another service."""
        for name in os.listdir(self.root):
            if name == ".workflow-trial.lock":
                continue
            if not re.fullmatch(r"\.workflow-trial-[a-f0-9]{32}", name):
                raise TrialError("trial_runtime_not_empty")
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=self.root)
                try:
                    _private_file(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                raise TrialError("unsafe_trial_storage") from None

    def reopen(self, now=time.time):
        """A saved attempt is inspectable, never an implicit request to run again."""
        if self.data is not None and self.data["status"] in {"prepared", "running"}:
            self.data.update(status="interrupted", reason="trial_interrupted", updated_at=now())
            self.save()
        return self.data

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.root is not None:
            os.close(self.root)
            self.root = None

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()


class WorkflowTrial:
    def __init__(self, server, journal, plan, *, clock=time.time, monotonic=time.monotonic,
                 sleep=time.sleep, poll_interval=1, max_calls=MAX_CALLS,
                 max_executions=MAX_EXECUTIONS, total_timeout=MAX_SECONDS,
                 call_timeout=MAX_CALL_SECONDS, source_byte_limit=MAX_SOURCE_BYTES,
                 initial_source=None):
        validate_limits(max_calls=max_calls, max_executions=max_executions,
                        total_timeout=total_timeout, call_timeout=call_timeout, poll_interval=poll_interval)
        if type(source_byte_limit) is not int or not 1 <= source_byte_limit <= MAX_SOURCE_BYTES:
            raise TrialError("invalid_trial_limits")
        if initial_source is not None:
            if (not isinstance(initial_source, dict) or set(initial_source) != {"room_id", "revision", "digest"}
                    or not _text(initial_source["room_id"], 512)
                    or type(initial_source["revision"]) is not int or initial_source["revision"] < 1
                    or not isinstance(initial_source["digest"], str) or not _DIGEST.fullmatch(initial_source["digest"])):
                raise TrialError("invalid_initial_source")
        self.server, self.journal, self.plan = server, journal, validate_plan(plan)
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.poll_interval = poll_interval
        self.max_calls, self.max_executions, self.total_timeout = max_calls, max_executions, total_timeout
        self.call_timeout = call_timeout
        self.source_byte_limit = source_byte_limit
        # This is an explicit library input, never inferred from old journals
        # or exposed as an HTTP, MCP or command-line resume operation.
        self.initial_source = dict(initial_source) if initial_source is not None else None
        self.deadline = None
        self.last_observation = None
        self.active_phase_index = None

    def _persist(self):
        self.journal.data["updated_at"] = self.clock()
        self.journal.save()

    def _call(self, method, *args, **kwargs):
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise TrialError("trial_deadline_exceeded")
        async def operation():
            return await asyncio.wait_for(getattr(self.server.service, method)(*args, **kwargs),
                                          timeout=min(remaining, 60))
        return self.server.runner.call(operation)

    def _observe(self, phase, checkpoint):
        snapshot = checkpoint.get("snapshot") if isinstance(checkpoint, dict) else None
        if not isinstance(snapshot, dict):
            raise TrialError("phase_checkpoint_unknown")
        try:
            used = snapshot["budget"]["used"]
            executions = snapshot["executions"]
            if (type(used) is not int or not 0 <= used <= PHASE_CALLS
                    or not isinstance(executions, list) or len(executions) > PHASE_EXECUTIONS
                    or used < phase["used"] or len(executions) < phase["executions"]
                    or snapshot["room"]["id"] != phase["room_id"]):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise TrialError("phase_accounting_unknown") from None
        phase.update(used=used, executions=len(executions))
        self.last_observation = {
            "phase_index": self.journal.data["phases"].index(phase),
            "room_id": phase["room_id"], "observed_at": self.clock(), "checkpoint": checkpoint,
            "terminal": checkpoint.get("settled") is True and snapshot["room"].get("status") == "completed",
            "observation_scope": "last_observed_before_shutdown",
        }
        self.journal.data["totals"] = {
            "model_calls": sum(item["used"] for item in self.journal.data["phases"]),
            "executions": sum(item["executions"] for item in self.journal.data["phases"]),
        }
        self._persist()
        if (self.journal.data["totals"]["model_calls"] > self.max_calls
                or self.journal.data["totals"]["executions"] > self.max_executions):
            raise TrialError("trial_budget_exhausted")
        return snapshot

    @staticmethod
    def _completion(checkpoint, *, source_byte_limit=MAX_SOURCE_BYTES):
        """A machine-observed progression gate, never a claim of trusted acceptance."""
        snapshot = checkpoint["snapshot"]
        project = snapshot.get("project", {})
        digest = checkpoint.get("project_digest")
        members = snapshot.get("members", [])
        jobs = snapshot.get("jobs", [])
        if (checkpoint.get("settled") is not True or snapshot["room"].get("status") != "completed"
                or project.get("available") is not True or not project.get("files")
                or type(project.get("revision")) is not int or project["revision"] < 0
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                or not isinstance(members, list)
                or {(member.get("id"), member.get("adapter")) for member in members}
                   != {("claude", "claude"), ("codex", "codex")}
                or len(members) != 2
                or not isinstance(jobs, list)
                or not set(PARTICIPANTS).issubset({job.get("provider") for job in jobs
                                                  if job.get("status") == "completed"})):
            raise TrialError("phase_gate_failed")
        try:
            files = project["files"]
            if not isinstance(files, list) or any(not isinstance(file, dict)
                                                  or not isinstance(file.get("content"), str) for file in files):
                raise ValueError
            source_bytes = sum(len(file["content"].encode("utf-8")) for file in files)
        except (ValueError, UnicodeError):
            raise TrialError("phase_gate_failed") from None
        if source_bytes > source_byte_limit:
            raise TrialError("phase_source_limit_exceeded")
        for execution in snapshot["executions"]:
            result = execution.get("result") or {}
            if (execution.get("revision") == project["revision"]
                    and execution.get("snapshot_sha256") == digest
                    and execution.get("status") == "completed" and result.get("status") == "completed"
                    and type(result.get("exit_code")) is int and result["exit_code"] == 0
                    and result.get("cleanup_confirmed") is True):
                return {"room_id": snapshot["room"]["id"], "revision": project["revision"], "digest": digest}
        raise TrialError("phase_gate_failed")

    def _stop(self, status, reason):
        self.journal.data.update(status=status, reason=reason, totals_complete=status == "completed")
        self._persist()
        if self.last_observation is not None:
            try:
                self.journal.save_result({**self.last_observation, "active_phase_index": self.active_phase_index,
                                          "final_for_trial": status == "completed"})
            except TrialError:
                self.journal.data.update(status="unknown", reason="trial_result_unconfirmed", totals_complete=False)
                self._persist()
        return self.journal.data

    def run(self):
        if self.journal.data is not None:
            return self.journal.reopen(self.clock)
        started = self.clock()
        self.deadline = self.monotonic() + self.total_timeout
        self.journal.data = {
            "schema": 1, "trial_id": str(uuid4()), "status": "prepared", "reason": None,
            "created_at": started, "updated_at": started, "plan": self.plan,
            "caps": {"seconds": self.total_timeout, "model_calls": self.max_calls,
                     "executions": self.max_executions, "call_timeout": self.call_timeout,
                     "source_byte_limit": self.source_byte_limit,
                     "phase_model_calls": PHASE_CALLS,
                     "phase_executions": PHASE_EXECUTIONS},
            "totals": {"model_calls": 0, "executions": 0}, "totals_complete": False,
            "cleanup_confirmed": None,
            "initial_source": self.initial_source,
            "phases": [{**phase, "request_id": "workflow-trial-" + str(uuid4()),
                        "room_id": None, "source": None, "status": "pending", "used": 0,
                        "executions": 0, "completion": None} for phase in self.plan["phases"]],
        }
        self._persist()
        try:
            for phase_index, phase in enumerate(self.journal.data["phases"]):
                self.active_phase_index = phase_index
                source = None
                if phase_index == 0 and self.initial_source is not None:
                    checkpoint = self._call("phase_checkpoint", self.initial_source["room_id"])
                    # An explicitly selected old baseline may need shrinking.
                    # It must still satisfy the original hard source limit.
                    source = self._completion(checkpoint, source_byte_limit=MAX_SOURCE_BYTES)
                    if source != self.initial_source:
                        raise TrialError("phase_source_unconfirmed")
                # Refresh real counters, rather than assuming a phase consumed its reservation.
                for previous in self.journal.data["phases"]:
                    if previous["room_id"] is not None:
                        checkpoint = self._call("phase_checkpoint", previous["room_id"])
                        self._observe(previous, checkpoint)
                        source = self._completion(checkpoint, source_byte_limit=self.source_byte_limit)
                totals = self.journal.data["totals"]
                if (self.max_calls - totals["model_calls"] < PHASE_CALLS
                        or self.max_executions - totals["executions"] < PHASE_EXECUTIONS):
                    return self._stop("stopped", "trial_budget_exhausted")
                phase.update(status="prepared", source=source)
                self.journal.data["status"] = "running"
                self._persist()  # Stable key/source are durable before create can dispatch.
                extra = ({"source_room_id": source["room_id"], "source_revision": source["revision"],
                          "source_digest": source["digest"]} if source else {})
                try:
                    snapshot = self._call("create_artifact_phase", phase["goal"], phase["target"],
                                          phase["request_id"], participants=list(PARTICIPANTS), **extra)
                    phase["room_id"] = snapshot["room"]["id"]
                    if not isinstance(phase["room_id"], str) or not phase["room_id"]:
                        raise ValueError
                except Exception:
                    return self._stop("unknown", "phase_start_unknown")
                phase["status"] = "running"
                self._persist()
                while True:
                    checkpoint = self._call("phase_checkpoint", phase["room_id"])
                    snapshot = self._observe(phase, checkpoint)
                    status = snapshot["room"].get("status")
                    if status in {"unknown", "failed", "cancelled", "interrupted", "needs_user", "budget_exhausted"}:
                        phase["status"] = status
                        return self._stop("unknown" if status == "unknown" else "stopped", "phase_" + status)
                    if checkpoint.get("settled") is True:
                        phase.update(status="completed", completion=self._completion(
                            checkpoint, source_byte_limit=self.source_byte_limit))
                        self._persist()
                        break
                    remaining = self.deadline - self.monotonic()
                    if remaining <= 0:
                        raise TrialError("trial_deadline_exceeded")
                    self.sleep(min(self.poll_interval, remaining))
            return self._stop("completed", None)
        except TrialError as error:
            unknown = error.code in {"phase_checkpoint_unknown", "phase_accounting_unknown"}
            return self._stop("unknown" if unknown else "stopped", error.code)
        except Exception:
            return self._stop("unknown", "phase_checkpoint_unknown")
        except BaseException:
            self._stop("interrupted", "trial_interrupted")
            raise


def create_trial_server(service, port):
    from .room_server import RoomUIServer, _RoomHandler
    from .ui_server import _HTTPError

    class ReadOnlyHandler(_RoomHandler):
        async def _dispatch_room(self, path, query, body):
            if self.command != "GET":
                raise _HTTPError(403, "trial_read_only")
            return await super()._dispatch_room(path, query, body)

    server = RoomUIServer(service, port=port)
    server.RequestHandlerClass = ReadOnlyHandler
    return server


def summary(data):
    if data is None:
        return {"status": "not_started"}
    return {key: data.get(key) for key in ("trial_id", "status", "reason", "totals", "totals_complete", "cleanup_confirmed")} | {
        "phases": [{"number": number + 1, **{key: phase.get(key) for key in
                     ("status", "room_id", "used", "executions", "completion")}}
                   for number, phase in enumerate(data["phases"])]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="豆窝受控多阶段试验（不是自动恢复的产品工作流）")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--runner-root", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--call-timeout", type=float, default=MAX_CALL_SECONDS)
    parser.add_argument("--seconds", type=float, default=MAX_SECONDS)
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument("--max-executions", type=int, default=MAX_EXECUTIONS)
    parser.add_argument("--status", action="store_true", help="仅读已有试验记录，不启动服务")
    args = parser.parse_args(argv)
    server, thread, service = None, None, None
    http_started = False
    try:
        validate_limits(max_calls=args.max_calls, max_executions=args.max_executions,
                        total_timeout=args.seconds, call_timeout=args.call_timeout)
        if args.status:
            print(json.dumps(summary(TrialJournal.read_only(args.runtime)), ensure_ascii=False))
            return 0
        with TrialJournal(args.runtime) as journal:
            if journal.data is not None:
                print(json.dumps(summary(journal.reopen()), ensure_ascii=False))
                return 0 if journal.data["status"] == "completed" else 2
            journal.require_fresh_runtime()
            if args.plan is None or args.runner_root is None:
                raise TrialError("trial_plan_and_runner_required")
            plan = load_plan(args.plan)
            runner_directory = _directory(args.runner_root)
            os.close(runner_directory)
            from .python_runner import PythonRunner
            from .room_service import RoomService
            service = RoomService(args.runtime / "rooms.sqlite3", args.runtime / "workspaces",
                                  runner=PythonRunner(args.runner_root), call_timeout=args.call_timeout)
            try:
                server = create_trial_server(service, args.port)
                thread = threading.Thread(target=server.serve_forever, name="beanest-trial-http", daemon=True)
                thread.start()
                http_started = True
                print(server.url, flush=True)
                previous = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, lambda _number, _frame: (_ for _ in ()).throw(KeyboardInterrupt()))
                try:
                    result = WorkflowTrial(server, journal, plan, max_calls=args.max_calls,
                                           max_executions=args.max_executions,
                                           total_timeout=args.seconds, call_timeout=args.call_timeout).run()
                finally:
                    signal.signal(signal.SIGTERM, previous)
            finally:
                if server is not None:
                    if http_started:
                        server.shutdown()
                        thread.join(timeout=5)
                    server.server_close()
                    cleanup = (server.runner.shutdown_error is None
                               and getattr(service, "shutdown_cleanup_confirmed", None) is True)
                else:
                    asyncio.run(service.shutdown())
                    cleanup = getattr(service, "shutdown_cleanup_confirmed", None) is True
                if journal.data is not None:
                    journal.data["cleanup_confirmed"] = cleanup
                    if not cleanup:
                        journal.data.update(status="unknown", reason="trial_cleanup_unconfirmed", totals_complete=False)
                    journal.save()
            print(json.dumps({"status": result["status"], "reason": result["reason"],
                              "totals": result["totals"], "totals_complete": result["totals_complete"],
                              "cleanup_confirmed": result["cleanup_confirmed"]},
                             ensure_ascii=False))
            return 0 if result["status"] == "completed" and result["cleanup_confirmed"] else 2
    except Exception as error:
        code = error.code if isinstance(error, TrialError) else "trial_start_failed"
        print(code if isinstance(code, str) and _CODE.fullmatch(code) else "trial_start_failed", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("trial_interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
