"""A bounded adapter for the explicitly configured, local Paradev dev host.

Session ownership, dispatch limits and durable records belong to the caller.
Stopping a CLI subprocess does not stop the agent it may already have started.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import signal
from typing import Any
from uuid import UUID

MAX_STDOUT = 2 * 1024 * 1024
MAX_STDERR = 64 * 1024
MAX_PROMPT = 128 * 1024
AGENT_MODES = {"claude": "default", "codex": "read-only"}
SESSION_STATUSES = {"starting", "running", "awaiting_input", "idle", "not_loaded", "offline", "dead"}
TURN_STATUSES = {"running", "completed", "failed", "interrupted"}
TOOL_STATUSES = {"pending", "running", "in_progress", "completed", "failed", "cancelled"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"\b(?:[A-Z_]*(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|REFRESH_TOKEN)|"
               r"apiKey|accessToken|refreshToken|clientSecret)\s*[:=]\s*"
               r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.IGNORECASE),
)


class ParadevError(RuntimeError):
    """A fixed diagnostic, with no raw CLI output attached."""

    def __init__(self, code: str, *, remote_state_unknown: bool = False):
        self.code = code
        self.remote_state_unknown = remote_state_unknown
        message = f"Paradev operation failed ({code})."
        if remote_state_unknown:
            message += " Remote session state must be checked; stopping the CLI is not a remote cancellation."
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "remote_state_unknown": self.remote_state_unknown}


@dataclass(frozen=True)
class ParadevConfig:
    repo: Path
    bun: Path
    workspace: str
    command_timeout: float = 20
    node_directory: Path | None = None

    def __post_init__(self) -> None:
        for name in ("repo", "bun"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
            object.__setattr__(self, name, value.resolve())
        if not self.repo.is_dir() or not (self.repo / "scripts/dev-cli.ts").is_file():
            raise ValueError("repo must contain scripts/dev-cli.ts")
        if not self.bun.is_file() or not os.access(self.bun, os.X_OK):
            raise ValueError("bun must be an existing executable file")
        if not isinstance(self.workspace, str):
            raise ValueError("workspace must be an explicit UUID")
        try:
            object.__setattr__(self, "workspace", str(UUID(self.workspace)))
        except ValueError:
            raise ValueError("workspace must be an explicit UUID") from None
        if (isinstance(self.command_timeout, bool)
                or not isinstance(self.command_timeout, (float, int))
                or not math.isfinite(self.command_timeout) or self.command_timeout <= 0):
            raise ValueError("command_timeout must be a finite positive number")
        if self.node_directory is not None:
            directory = Path(self.node_directory)
            if not directory.is_absolute() or not directory.is_dir():
                raise ValueError("node_directory must be an existing absolute directory")
            object.__setattr__(self, "node_directory", directory.resolve())


class _OutputLimit(Exception):
    pass


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        return None
    return value if _redact(value) == value else None


def _redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _session_id(value: object) -> str:
    result = _identifier(value)
    if result is None:
        raise ValueError("session_id must be an explicit identifier")
    return result


def _prompt(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_PROMPT or "\0" in value:
        raise ValueError("prompt must be nonempty text within the size limit")
    return value


def _cursor(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"epoch", "seq"}
            or _identifier(value.get("epoch")) is None
            or type(value.get("seq")) is not int or value["seq"] < 0):
        raise ValueError("before must be an explicit journal cursor")
    return {"epoch": value["epoch"], "seq": value["seq"]}


def _environment(config: ParadevConfig) -> dict[str, str]:
    allowed = {"HOME", "PATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
               "USER", "LOGNAME", "SHELL", "SYSTEMROOT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    directories = [str(config.bun.parent)]
    if config.node_directory is not None:
        directories.append(str(config.node_directory))
    directories.extend(env.get("PATH", os.defpath).split(os.pathsep))
    env["PATH"] = os.pathsep.join(dict.fromkeys(directories))
    return env


async def _read_limited(stream: asyncio.StreamReader, maximum: int) -> bytes:
    result = bytearray()
    while chunk := await stream.read(64 * 1024):
        if len(result) + len(chunk) > maximum:
            raise _OutputLimit
        result.extend(chunk)
    return bytes(result)


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(64 * 1024):
        pass


async def _stop_cli(process: asyncio.subprocess.Process) -> None:
    """Reap this CLI group, including pipe-holding children, without retaining output."""
    def send(sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    drains = [asyncio.create_task(_drain(stream))
              for stream in (process.stdout, process.stderr) if stream is not None]
    try:
        send(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 0.3)
        except asyncio.TimeoutError:
            pass
        send(signal.SIGKILL)
        try:
            await asyncio.wait_for(asyncio.gather(process.wait(), *drains), 1)
        except asyncio.TimeoutError:
            raise ParadevError("cli_cleanup_failed", remote_state_unknown=True) from None
    finally:
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


def _error_code(stderr: bytes) -> str:
    message = stderr.decode("utf-8", errors="replace").lower()
    if "not logged in" in message or "unauthorized" in message or "authentication" in message:
        return "authentication_required"
    if "not found" in message or "isn't running" in message or "manifest is stale" in message:
        return "not_found"
    if "rate limit" in message or "quota" in message:
        return "rate_limited"
    return "cli_error"


class ParadevAdapter:
    def __init__(self, config: ParadevConfig):
        self.config = config

    async def _call(self, operation: str, arguments: list[str]) -> Any:
        command = [str(self.config.bun), "scripts/dev-cli.ts", "sessions", operation,
                   "--local", "--workspace", self.config.workspace, "--json", *arguments]
        mutating = operation in {"create", "send", "close"}
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=self.config.repo, env=_environment(self.config),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except OSError:
            raise ParadevError("cli_unavailable") from None
        assert process.stdout is not None and process.stderr is not None
        readers = [asyncio.create_task(_read_limited(process.stdout, MAX_STDOUT)),
                   asyncio.create_task(_read_limited(process.stderr, MAX_STDERR))]
        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(*readers, process.wait()), self.config.command_timeout)
        except (asyncio.TimeoutError, _OutputLimit, asyncio.CancelledError) as error:
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await asyncio.shield(_stop_cli(process))
            if isinstance(error, asyncio.CancelledError):
                raise
            code = "command_timeout" if isinstance(error, asyncio.TimeoutError) else "output_limit"
            raise ParadevError(code, remote_state_unknown=True) from None
        if process.returncode != 0:
            raise ParadevError(_error_code(stderr), remote_state_unknown=mutating)
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise ParadevError("invalid_json", remote_state_unknown=mutating) from None

    async def create(self, agent: str, prompt: str) -> dict[str, Any]:
        if not isinstance(agent, str) or agent not in AGENT_MODES:
            raise ValueError("agent must be claude or codex")
        mode = AGENT_MODES[agent]
        result = await self._call("create", ["--agent", agent, "--mode", mode, "--prompt=" + _prompt(prompt)])
        if not isinstance(result, dict) or result.get("kind") != "chat" or _identifier(result.get("sessionId")) is None:
            raise ParadevError("invalid_response", remote_state_unknown=True)
        return {"sessionId": result["sessionId"], "workspaceId": self.config.workspace,
                "agent": agent, "mode": mode}

    async def read(self, session_id: str, limit: int = 100,
                   before: dict[str, Any] | None = None) -> dict[str, Any]:
        session_id = _session_id(session_id)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        cursor = _cursor(before)
        arguments = ["--session", session_id, "--limit", str(limit)]
        if cursor is not None:
            arguments.extend(["--before", json.dumps(cursor, separators=(",", ":"))])
        result = await self._call("read", arguments)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ParadevError("invalid_response")
        if not result["ok"]:
            raise ParadevError("journal_reset")
        if not isinstance(result.get("envelopes"), list):
            raise ParadevError("invalid_response")
        try:
            next_before = _cursor(result.get("nextBefore"))
        except ValueError:
            raise ParadevError("invalid_response") from None
        return _read_page(session_id, result["envelopes"], next_before)

    async def send(self, session_id: str, prompt: str) -> dict[str, Any]:
        result = await self._call("send", ["--session", _session_id(session_id), "--prompt=" + _prompt(prompt)])
        if not isinstance(result, dict) or _identifier(result.get("sessionId")) is None:
            raise ParadevError("invalid_response", remote_state_unknown=True)
        safe = {"sessionId": result["sessionId"]}
        if _identifier(result.get("itemId")) is not None:
            safe["itemId"] = result["itemId"]
        if type(result.get("queued")) is bool:
            safe["queued"] = result["queued"]
        return safe

    async def close(self, session_id: str) -> dict[str, Any]:
        session_id = _session_id(session_id)
        result = await self._call("close", ["--session", session_id])
        if not isinstance(result, dict) or result.get("sessionId") != session_id or result.get("closed") is not True:
            raise ParadevError("invalid_response", remote_state_unknown=True)
        return {"sessionId": session_id, "closed": True}

    async def list(self) -> list[dict[str, Any]]:
        """Internal discovery only: callers must filter by their ownership ledger."""
        result = await self._call("list", [])
        if not isinstance(result, list):
            raise ParadevError("invalid_response")
        sessions = []
        for value in result:
            if not isinstance(value, dict) or _identifier(value.get("sessionId")) is None:
                continue
            safe = {"sessionId": value["sessionId"]}
            if _identifier(value.get("harness")) is not None:
                safe["harness"] = value["harness"]
            if _is_status(value.get("status"), SESSION_STATUSES):
                safe["status"] = value["status"]
            if type(value.get("live")) is bool:
                safe["live"] = value["live"]
            sessions.append(safe)
        return sessions


def _read_page(session_id: str, envelopes: list, next_before: dict | None) -> dict[str, Any]:
    session = None
    turns: dict[str, dict] = {}
    items: dict[str, dict] = {}
    for envelope in envelopes:
        if not isinstance(envelope, dict) or not isinstance(envelope.get("event"), dict):
            continue
        event = envelope["event"]
        cursor = envelope.get("cursor")
        seq = cursor.get("seq") if isinstance(cursor, dict) else None
        position = {"seq": seq} if type(seq) is int and seq >= 0 else {}
        if event.get("type") == "session" and isinstance(event.get("session"), dict):
            state = event["session"]
            if (_is_status(state.get("status"), SESSION_STATUSES)
                    and (session is None or session.get("seq", -1) <= position.get("seq", -1))):
                session = {"status": state["status"], **position}
                for key in ("modeId", "modelId"):
                    if _identifier(state.get(key)) is not None:
                        session[key] = state[key]
        elif event.get("type") == "turn" and isinstance(event.get("turn"), dict):
            turn = event["turn"]
            identity = _identifier(turn.get("id"))
            if identity is not None and _is_status(turn.get("status"), TURN_STATUSES):
                _keep_latest(turns, identity, {"id": identity, "status": turn["status"], **position})
        elif event.get("type") == "item" and isinstance(event.get("item"), dict):
            item = event["item"]
            identity = _identifier(item.get("id"))
            if identity is None:
                continue
            safe = {"id": identity, **position}
            kind = item.get("kind")
            if kind == "agent_message" and isinstance(item.get("text"), str):
                safe.update(role="assistant", text=_redact(item["text"]))
            elif kind == "user_message" and isinstance(item.get("content"), list):
                texts = [part["text"] for part in item["content"]
                         if isinstance(part, dict) and part.get("type") == "text"
                         and part.get("hidden") is not True and isinstance(part.get("text"), str)
                         and not _is_hidden_context(part["text"])]
                safe.update(role="user", text=_redact("\n".join(texts)))
            elif kind == "tool_call" and _is_status(item.get("status"), TOOL_STATUSES):
                safe["status"] = item["status"]
                if _identifier(item.get("toolName")) is not None:
                    safe["toolName"] = item["toolName"]
            else:
                continue
            _keep_latest(items, identity, safe)
    return {"sessionId": session_id, "session": session, "turns": list(turns.values()),
            "messages": [item for item in items.values() if "role" in item],
            "tools": [item for item in items.values() if "role" not in item],
            "nextBefore": next_before}


def _keep_latest(values: dict[str, dict], identity: str, value: dict) -> None:
    previous = values.get(identity)
    if previous is not None and previous.get("seq", -1) > value.get("seq", -1):
        return
    values[identity] = value


def _is_hidden_context(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<superset-context>") and stripped.endswith("</superset-context>")


def _is_status(value: object, allowed: set[str]) -> bool:
    return isinstance(value, str) and value in allowed
