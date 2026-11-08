"""Kimi Code's native ACP transport and non-secret managed-route projection.

The provider configuration is read only to select its existing managed OAuth
route. Credentials and raw configuration never leave this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import tomllib
from uuid import uuid4

from . import adapters


_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MODEL = re.compile(r"kimi-code/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ENDPOINT = "https://api.kimi.com/coding/v1"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024


def _provider_overrides(provider):
    environment = provider.get("env", {})
    headers = provider.get("custom_headers", {})
    if not isinstance(environment, dict) or not isinstance(headers, dict):
        return True
    # Only actual routing/authentication overrides matter. User-agent and
    # unrelated native configuration remain owned by Kimi Code.
    endpoint = environment.get("KIMI_BASE_URL")
    return (bool(environment.get("KIMI_API_KEY"))
            or bool(endpoint and (not isinstance(endpoint, str) or endpoint.rstrip("/") != _ENDPOINT))
            or any(isinstance(key, str) and key.lower() in {"authorization", "x-api-key", "api-key"} and bool(value)
                   for key, value in headers.items()))


def managed_configuration(path=None):
    """Project only safe metadata; model auth/endpoint overrides are excluded.

    Kimi resolves model credentials before provider credentials. A managed
    alias alone is therefore insufficient; the selected alias must inherit
    the official managed provider without a model-level credential override.
    """
    unavailable = {"state": "unavailable", "reason": "auth_unconfirmed", "models": [], "default_model": None}
    path = Path(path) if path is not None else adapters.native_home("kimi") / "config.toml"
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            return unavailable
        config = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return unavailable
    providers, models = config.get("providers"), config.get("models")
    if not isinstance(providers, dict) or not isinstance(models, dict):
        return unavailable
    provider = providers.get("managed:kimi-code")
    endpoint = provider.get("base_url") if isinstance(provider, dict) else None
    if (not isinstance(provider, dict) or provider.get("type") != "kimi"
            or not isinstance(endpoint, str) or endpoint.rstrip("/") != _ENDPOINT
            or not provider.get("oauth")
            or any(provider.get(key) for key in ("api_key", "api_key_env")) or _provider_overrides(provider)):
        return unavailable
    result = []
    for identifier, model in models.items():
        if (not isinstance(identifier, str) or _MODEL.fullmatch(identifier) is None
                or not isinstance(model, dict) or model.get("provider") != "managed:kimi-code"
                or any(model.get(key) for key in ("api_key", "api_key_env", "oauth", "base_url", "provider_id"))):
            continue
        efforts = model.get("support_efforts", [])
        if not isinstance(efforts, list) or any(not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) is None for value in efforts):
            efforts = []
        default_effort = model.get("default_effort")
        name = model.get("display_name", identifier)
        result.append({"id": identifier, "name": name if isinstance(name, str) and len(name) <= 200 else identifier,
                       "efforts": list(dict.fromkeys(efforts)),
                       "default_effort": default_effort if default_effort in efforts else None,
                       "is_default": identifier == config.get("default_model")})
    default = next((item["id"] for item in result if item["is_default"]), None)
    return {"state": "ready" if default is not None else "unavailable", "reason": None if default else "auth_unconfirmed",
            "models": result, "default_model": default}


class ACPError(RuntimeError):
    def __init__(self, code, *, rpc_code=None):
        self.code, self.rpc_code = code, rpc_code
        self.rpc_method = None
        super().__init__(code)


class ACPConnection:
    """One owned stdio peer; reverse requests never execute host file tools."""
    def __init__(self, process, *, on_update=None, permission="plan"):
        self.process, self.on_update, self.permission = process, on_update, permission
        self.pending, self.sequence = {}, 0
        self.reader = asyncio.create_task(self._read())
        self.stderr = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        while await self.process.stderr.read(65536):
            pass

    async def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
            return await future
        except ACPError as error:
            error.rpc_method = method
            # -32000 also carries ordinary provider/session failures. Only an
            # actual authenticate failure establishes this handshake label.
            if method == "authenticate" and error.rpc_code == -32000:
                error.code = "native_auth_unconfirmed"
                error.args = (error.code,)
            raise
        finally:
            self.pending.pop(identifier, None)

    async def _read(self):
        failure = "native_protocol_invalid"
        try:
            while raw := await self.process.stdout.readline():
                row = adapters._strict_json(raw)
                if not isinstance(row, dict) or row.get("jsonrpc") != "2.0":
                    raise ACPError(failure)
                if "method" in row:
                    method, params = row["method"], row.get("params", {})
                    if method == "session/update" and "id" not in row:
                        if self.on_update is not None:
                            await self.on_update(params)
                    elif "id" in row:
                        # A native permission question remains a real question.
                        # The coordinator cannot silently approve it for a user.
                        if method == "session/request_permission" and isinstance(params, dict):
                            if self.on_update is not None:
                                tool = params.get("toolCall", {})
                                title = tool.get("title") if isinstance(tool, dict) else None
                                options = [item.get("name") for item in params.get("options", [])
                                           if isinstance(item, dict) and isinstance(item.get("name"), str)]
                                await self.on_update({"permission_required": True,
                                    "question": title if isinstance(title, str) else "Kimi Code 请求确认一项原生操作。",
                                    "options": options})
                            await self.send({"jsonrpc": "2.0", "id": row["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                        else:
                            await self.send({"jsonrpc": "2.0", "id": row["id"], "error": {"code": -32601, "message": "Method not supported"}})
                elif type(row.get("id")) is int and row["id"] in self.pending:
                    future = self.pending[row["id"]]
                    if future.done():
                        raise ACPError(failure)
                    if "error" in row:
                        error = row["error"]
                        if not isinstance(error, dict) or type(error.get("code")) is not int:
                            raise ACPError(failure)
                        future.set_exception(ACPError("native_provider_error", rpc_code=error["code"]))
                    elif isinstance(row.get("result"), dict):
                        future.set_result(row["result"])
                    else:
                        raise ACPError(failure)
                else:
                    raise ACPError(failure)
        except ACPError as error:
            failure = error.code
        except (OSError, ValueError, UnicodeError, RecursionError, AttributeError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ACPError(failure))

    async def close(self):
        if self.process.stdin is not None:
            self.process.stdin.close()
        for task in (self.reader, self.stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(self.reader, self.stderr, return_exceptions=True)


async def start(cwd, *, on_update=None, permission="plan"):
    process = await asyncio.create_subprocess_exec(
        adapters.find_kimi(), "acp", cwd=cwd, env=adapters._environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=adapters.MAX_OUTPUT + 1)
    connection = ACPConnection(process, on_update=on_update, permission=permission)
    return process, connection


async def initialize(connection):
    result = await connection.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                      "clientInfo": {"name": "beanest", "version": "0.2.0"}})
    if result.get("protocolVersion") != 1:
        raise ACPError("native_protocol_invalid")
    await connection.request("authenticate", {"methodId": "login"})
    return result


async def status(cwd, *, timeout=15):
    """No prompt or session is created for an explicit account check."""
    configuration = managed_configuration()
    if configuration["state"] != "ready":
        return {"ready": False, "version": None}
    process, connection, owned = None, None, {}
    from .native_adapters import _cleanup, _processes, _remember, _settle_cleanup
    try:
        process, connection = await start(cwd)
        _remember(process.pid, owned, await asyncio.to_thread(_processes))
        result = await asyncio.wait_for(initialize(connection), timeout)
        version = result.get("agentInfo", {}).get("version")
        if not isinstance(version, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.]+)?", version) is None:
            raise ACPError("native_protocol_invalid")
    finally:
        async def cleanup():
            if connection is not None:
                await connection.close()
            return await _cleanup(process, owned) if process is not None else True
        clean, interrupted = await _settle_cleanup(cleanup())
        if interrupted:
            raise asyncio.CancelledError()
        if not clean:
            raise ACPError("native_cleanup_unconfirmed")
    return {"ready": True, "version": version}


def session_wire(cwd, session_id, *, home=None):
    """The wire of one session under the profile's Kimi home (bound or explicit)."""
    if not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None:
        raise ACPError("native_session_identity_mismatch")
    normalized = str(cwd).replace("\\", "/").rstrip("/")
    base = normalized.rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-")[:40].strip("-")
    if slug in {"", ".", ".."}:
        slug = "workspace"
    key = "wd_" + slug + "_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return adapters.native_home("kimi", home) / "sessions" / key / session_id / "agents/main/wire.jsonl"


def wire_offset(path):
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None, 0
    if not stat.S_ISREG(info.st_mode):
        raise ACPError("native_turn_unconfirmed")
    return (info.st_dev, info.st_ino), info.st_size


def completed_turn(path, initial):
    """Read only newly appended typed records of this exact owned session."""
    identity, offset = initial
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or identity is not None and identity != (info.st_dev, info.st_ino)
                or info.st_size < offset or info.st_size - offset > 64 * 1024 * 1024):
            raise ACPError("native_turn_unconfirmed")
        stream.seek(offset)
        started, ended, prompts = [], [], 0
        while raw := stream.readline(adapters.MAX_OUTPUT + 1):
            if len(raw) > adapters.MAX_OUTPUT or not raw.endswith(b"\n"):
                raise ACPError("native_turn_unconfirmed")
            row = adapters._strict_json(raw)
            if not isinstance(row, dict):
                raise ACPError("native_turn_unconfirmed")
            # 0.34's per-agent file omits agentId; newer releases include it.
            # The exact /agents/main/ path supplies the old format's scope.
            if row.get("agentId") not in {None, "main"}:
                continue
            kind, turn = row.get("type"), row.get("turnId")
            if kind == "turn.prompt":
                prompts += 1
            if kind in {"turn.started", "turn.ended"}:
                if type(turn) is not int or turn < 0:
                    raise ACPError("native_turn_unconfirmed")
                if kind == "turn.started":
                    started.append(turn)
                else:
                    ended.append((turn, row.get("reason")))
        if (len(ended) != 1 or ended[0][1] != "completed"
                or not (prompts == 1 and (not started or started == [ended[0][0]])
                        or prompts == 0 and started == [ended[0][0]])):
            raise ACPError("native_turn_unconfirmed")
        return {"turn_id": ended[0][0], "reason": "completed", "wire_offset": offset, "wire_through": info.st_size}


def failed_turn_diagnostic(path, initial):
    """Project only a newly appended failed turn; never export provider text.

    This is diagnostic evidence, not completion proof or retry authorization.
    In particular a compaction failure occurs after session/prompt was sent.
    """
    identity, offset = initial
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or identity is not None and identity != (info.st_dev, info.st_ino)
                    or info.st_size < offset or info.st_size - offset > 64 * 1024 * 1024):
                return None
            stream.seek(offset)
            prompts, ends, compacting = 0, [], False
            while raw := stream.readline(adapters.MAX_OUTPUT + 1):
                if len(raw) > adapters.MAX_OUTPUT or not raw.endswith(b"\n"):
                    return None
                row = adapters._strict_json(raw)
                if not isinstance(row, dict):
                    return None
                if row.get("agentId") not in {None, "main"}:
                    continue
                kind = row.get("type")
                if kind == "turn.prompt":
                    prompts += 1
                elif kind == "full_compaction.begin":
                    compacting = True
                elif kind in {"full_compaction.end", "full_compaction.complete"}:
                    compacting = False
                elif kind == "llm.request" and row.get("kind") in {"compaction", "full_compaction", "loop"}:
                    compacting = row["kind"] in {"compaction", "full_compaction"}
                elif kind == "turn.ended":
                    ends.append(row)
        if prompts != 1 or len(ends) != 1 or ends[0].get("reason") != "failed":
            return None
        error = ends[0].get("error")
        if not isinstance(error, dict):
            return None
        if error.get("code") == "compaction.failed" and isinstance(error.get("cause"), dict):
            # Compaction wraps the typed provider refusal; classify that cause.
            error = error["cause"]
        details = error.get("details")
        status = details.get("statusCode") if isinstance(details, dict) else None
        result = {"phase": "compaction" if compacting else "prompt", "reason": "provider_error"}
        if type(status) is int and 400 <= status <= 599:
            result["http_status"] = status
        message = error.get("message", "")
        if (status == 403 and isinstance(message, str) and "current plan" in message.lower()
                and "supports" in message.lower() and "context" in message.lower()
                and ("upgrade" in message.lower() or "higher" in message.lower())):
            result["reason"] = "provider_context_limit"
        elif (error.get("code") == "provider.api_error" and error.get("name") == "APIProviderQuotaExhaustedError"
                and status == 429 and error.get("retryable") is False):
            result["reason"] = "provider_quota_exhausted"
        elif error.get("code") == "provider.auth_error":
            result["reason"] = "provider_auth_error"
        return result
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def context_limit_recovery_proof(cwd, session_id, **kwargs):
    """Prove one exact, failed compaction; never replay or discard its history."""
    return _failed_turn_recovery_proof(cwd, session_id, quota=False, **kwargs)


def quota_failure_recovery_proof(cwd, session_id, **kwargs):
    """Prove a typed quota failure at the exact owned work prompt and wire tail."""
    return _failed_turn_recovery_proof(cwd, session_id, quota=True, **kwargs)


def compaction_quota_recovery_proof(cwd, session_id, **kwargs):
    """Prove a typed quota refusal raised by this prompt's automatic compaction."""
    return _failed_turn_recovery_proof(cwd, session_id, quota=True, compaction=True, **kwargs)


def _quota_cleanup_footer(row, tasks, ended_at):
    """Recognize an owned process kill receipt, never a new native action.

    Kimi closes background processes after recording the failed turn. Only a
    process started inside this exact prompt may close here; its immutable
    identity must match and no terminal notification may remain queued.
    """
    if (row.get("type") != "task.terminated"
            or set(row) - {"type", "time", "agentId", "info", "outputTail"}
            or "outputTail" in row and not isinstance(row["outputTail"], str)):
        return False
    info = row.get("info")
    allowed = {"taskId", "description", "status", "detached", "startedAt", "endedAt", "stopReason",
               "terminalNotificationSuppressed", "timeoutMs", "kind", "command", "pid", "exitCode"}
    if (not isinstance(info, dict) or set(info) - allowed or info.get("kind") != "process"
            or info.get("status") != "killed" or info.get("terminalNotificationSuppressed") is not True
            or type(info.get("pid")) is not int or info["pid"] <= 0
            or type(info.get("exitCode")) is not int or info["exitCode"] == 0
            or "detached" in info and type(info["detached"]) is not bool
            or "timeoutMs" in info and (type(info["timeoutMs"]) not in {int, float}
                                        or not math.isfinite(info["timeoutMs"]))
            or not isinstance(info.get("taskId"), str) or not info["taskId"]
            or not isinstance(info.get("stopReason"), str)
            or type(info.get("endedAt")) not in {int, float} or not math.isfinite(info["endedAt"])
            or not ended_at <= info["endedAt"] <= row["time"]):
        return False
    previous = tasks.get(info["taskId"])
    if (not isinstance(previous, dict) or previous.get("status") != "running"
            or any(type(previous.get(key)) is not type(info.get(key)) or previous.get(key) != info.get(key)
                   for key in ("taskId", "kind", "description", "startedAt", "command", "pid", "detached", "timeoutMs"))
            or not isinstance(info.get("command"), str) or not isinstance(info.get("description"), str)
            or type(info.get("startedAt")) not in {int, float} or not math.isfinite(info["startedAt"])
            or info["startedAt"] > ended_at):
        return False
    del tasks[info["taskId"]]  # A duplicate terminal record is not fresh proof.
    return True


def _failed_turn_recovery_proof(cwd, session_id, *, claimed_at, finished_at, model, validate_prompt, quota, compaction=False):
    """Bind a failed turn to this owned prompt and the current wire tail.

    Quota proof may include matched background-process cleanup metadata. It
    never accepts a new action or proves private file effects safe; recovery
    still audits those independently. An old error elsewhere is not authority.
    No native text, prompt, request ID or authentication value is returned.
    """
    path = session_wire(cwd, session_id)
    if any(part.is_symlink() for part in (path, *path.parents)):
        return None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
            return None
        offset, prompts, failed, request, started = None, 0, None, None, False
        turn, prompt_sha256, started_turns = None, None, []
        tasks, ended_at, last_timestamp = {}, None, None
        digest = hashlib.sha256()
        while True:
            position = stream.tell()
            raw = stream.readline(adapters.MAX_OUTPUT + 1)
            if not raw:
                break
            if len(raw) > adapters.MAX_OUTPUT or not raw.endswith(b"\n"):
                return None
            try:
                row = adapters._strict_json(raw)
            except (ValueError, UnicodeError, RecursionError):
                return None
            if not isinstance(row, dict):
                return None
            timestamp = row.get("time")
            if offset is not None and (type(timestamp) not in {int, float} or not math.isfinite(timestamp)):
                return None
            if offset is not None:
                if not claimed_at * 1000 <= timestamp <= finished_at * 1000 or timestamp < last_timestamp:
                    return None  # Every byte after the prompt belongs to this ordered attempt.
                last_timestamp = timestamp
            if type(timestamp) not in {int, float} or not claimed_at * 1000 <= timestamp <= finished_at * 1000:
                continue
            if offset is not None and failed is not None:
                if (not quota or row.get("agentId") not in {None, "main"}
                        or not _quota_cleanup_footer(row, tasks, ended_at)):
                    return None
                digest.update(raw)
                continue
            if row.get("agentId") not in {None, "main"}:
                continue
            kind = row.get("type")
            if kind == "turn.prompt":
                parts = row.get("input")
                if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
                        or parts[0].get("type") != "text" or not isinstance(parts[0].get("text"), str)
                        or not validate_prompt(parts[0]["text"])):
                    return None
                prompt_sha256 = hashlib.sha256(parts[0]["text"].encode()).hexdigest()
                prompts += 1
                offset, last_timestamp = position, timestamp
                digest.update(raw)
            elif offset is not None:
                digest.update(raw)
                if kind == "full_compaction.begin":
                    started = True
                elif kind == "llm.request":
                    request = row
                elif kind == "turn.started":
                    started_turns.append(row.get("turnId"))
                elif kind == "task.started":
                    task_info = row.get("info")
                    if isinstance(task_info, dict) and isinstance(task_info.get("taskId"), str):
                        if task_info["taskId"] in tasks:
                            return None
                        started_at = task_info.get("startedAt")
                        valid_time = (type(started_at) in {int, float} and math.isfinite(started_at)
                                      and claimed_at * 1000 <= started_at <= timestamp)
                        tasks[task_info["taskId"]] = task_info if valid_time else None
                elif kind == "task.terminated":
                    task_info = row.get("info")
                    if isinstance(task_info, dict):
                        tasks.pop(task_info.get("taskId"), None)
                elif kind == "turn.ended":
                    failed, turn = row.get("reason") == "failed", row.get("turnId")
                    ended_at = timestamp
        compacting = compaction or not quota
        if (prompts != 1 or (compacting and not started) or not failed or type(turn) is not int or started_turns not in ([], [turn]) or not isinstance(request, dict)
                or request.get("kind") not in ({"compaction", "full_compaction"} if compacting else {"loop"})
                or request.get("modelAlias") != model):
            return None
        diagnostic = failed_turn_diagnostic(path, ((info.st_dev, info.st_ino), offset))
        expected = ({"phase": "compaction" if compaction else "prompt", "reason": "provider_quota_exhausted", "http_status": 429} if quota else
                    {"phase": "compaction", "reason": "provider_context_limit", "http_status": 403})
        if diagnostic != expected:
            return None
        after = os.fstat(stream.fileno())
        if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
            return None
        return {"sha256": digest.hexdigest(), "prompt_sha256": prompt_sha256, "wire_offset": offset, "wire_through": info.st_size,
                "turn_id": turn, "diagnostic": diagnostic}


def safe_failure_diagnostic(value):
    """One whitelist for the transport receipt and persisted host projection."""
    if not isinstance(value, dict):
        return None
    result = {}
    if value.get("phase") in {"handshake", "session", "configuration", "prompt", "compaction"}:
        result["phase"] = value["phase"]
    if value.get("reason") in {"provider_error", "provider_auth_error", "provider_context_limit", "provider_quota_exhausted"}:
        result["reason"] = value["reason"]
    if value.get("rpc_method") in {"initialize", "authenticate", "session/new", "session/resume", "session/load",
                                   "session/set_model", "session/set_config_option", "session/set_mode", "session/prompt", "session/close"}:
        result["rpc_method"] = value["rpc_method"]
    if type(value.get("rpc_code")) is int and -(2**31) <= value["rpc_code"] < 2**31:
        result["rpc_code"] = value["rpc_code"]
    if type(value.get("http_status")) is int and 400 <= value["http_status"] <= 599:
        result["http_status"] = value["http_status"]
    if type(value.get("prompt_sent")) is bool:
        result["prompt_sent"] = value["prompt_sent"]
    return result or None


class TurnUpdates:
    def __init__(self, callback, progress_fingerprint=None):
        from .native_adapters import _Stream
        self.callback = callback
        self.active = False
        self.session_id = None
        self.tools, self.events = {}, []
        self.text = ""
        self.permission_required = False
        self.question = None
        self.possible_effects = False
        self.progress = _Stream("kimi", True, None, progress_fingerprint)

    async def __call__(self, params):
        if params.get("permission_required"):
            self.permission_required = True
            question = params.get("question", "Kimi Code 请求确认一项原生操作。")
            options = params.get("options", [])
            text = question + ("\n" + " / ".join(options) if options else "")
            if len(text.encode("utf-8")) <= adapters.MAX_TEXT:
                self.question = text
            return
        if not self.active:
            return  # session/load's replay is not a new tool invocation.
        if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
            raise ACPError("native_session_identity_mismatch")
        update = params.get("update")
        if not isinstance(update, dict):
            raise ACPError("native_protocol_invalid")
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                self.text += content["text"]
                if len(self.text.encode("utf-8")) > adapters.MAX_TEXT:
                    raise ACPError("output_limit")
        elif kind in {"tool_call", "tool_call_update"}:
            identifier = update.get("toolCallId")
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256:
                raise ACPError("native_protocol_invalid")
            if identifier not in self.tools:
                self.tools[identifier] = {"kind": update.get("kind", "other"), "state": "pending"}
                self.text = ""
            tool = self.tools[identifier]
            tool["kind"] = update.get("kind", tool["kind"])
            if "rawInput" in update:
                self.progress.remember_input(identifier, tool["kind"], update["rawInput"])
            native_state = update.get("status", tool["state"])
            if native_state not in {"pending", "in_progress", "completed", "failed"}:
                raise ACPError("native_protocol_invalid")
            tool["state"] = native_state
            if native_state == "failed" and not tool.get("failure_observed", False):
                tool["failure_observed"] = True
                await self.progress.failed_tool(identifier, update.get("rawOutput", update.get("content")))
                if self.progress.failure is not None:
                    raise ACPError(self.progress.failure)
            tool_kind = {"read": "file_read", "edit": "file_write", "delete": "file_write",
                         "execute": "shell", "search": "file_search", "fetch": "network"}.get(tool["kind"], "tool")
            self.possible_effects |= tool["kind"] not in {"read", "search"}
            state = {"pending": "started", "in_progress": "running"}.get(native_state, native_state)
            # ACP streams content updates while a tool keeps the same state.
            # Persist activity transitions per tool, not each output chunk;
            # inputs, failure evidence and pairing above still see every update.
            activity = (tool_kind, state)
            if tool.get("activity") == activity:
                return
            tool["activity"] = activity
            event = {"type": "native_tool", "kind": tool_kind, "state": state,
                     "description": "工具已返回" if state in {"completed", "failed"} else "正在使用工具"}
            self.events.append(event)
            if self.callback is not None:
                returned = self.callback(dict(event))
                if inspect.isawaitable(returned):
                    await returned


async def invoke_native(prompt, cwd, session_id=None, timeout=None, *, permission="plan",
                        response_schema=None, runtime_settings=None, on_activity=None,
                        receipt_dir=None, progress_fingerprint=None):
    from .native_adapters import _cleanup, _processes, _remember, _settle_cleanup
    from .message_capacity import MAX_PROMPT_BYTES
    if (not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES
            or not isinstance(permission, str) or permission not in {"plan", "full_access", "read_only", "workspace_write"}):
        raise ValueError("invalid_prompt")
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("invalid_timeout")
    if session_id is not None and (not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None):
        raise ValueError("invalid_session_id")
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("invalid_workspace")
    configuration = managed_configuration()
    settings = runtime_settings or {}
    if not isinstance(settings, dict) or set(settings) - {"model", "effort"}:
        raise ValueError("invalid_runtime_settings")
    model = settings.get("model") or configuration["default_model"]
    selected = next((item for item in configuration["models"] if item["id"] == model), None)
    if selected is None:
        raise ACPError("native_auth_unconfirmed")
    effort = settings.get("effort")
    if effort is not None and effort not in selected["efforts"]:
        raise ACPError("runtime_effort_unsupported")
    if response_schema is not None:
        if not isinstance(response_schema, dict) or response_schema.get("type") != "object":
            raise ValueError("invalid_response_schema")
        schema = json.dumps(response_schema, ensure_ascii=False, allow_nan=False)
        if len(schema.encode("utf-8")) > 64 * 1024:
            raise ValueError("invalid_response_schema")
        prompt += "\nReturn your final answer as one JSON object matching this schema, without Markdown fences:\n" + schema
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("invalid_prompt")
    process, connection, owned = None, None, {}
    updates = TurnUpdates(on_activity, progress_fingerprint)
    result = {"session_id": session_id, "text": "", "status": "failed", "usage": None, "events": []}
    started = time.monotonic()
    proof, receipt_path, cancelled = None, None, None
    path, before, prompt_sent, diagnostic = None, None, False, None
    async def turn():
        nonlocal process, connection, session_id, proof, path, before, prompt_sent
        process, connection = await start(cwd, on_update=updates, permission=permission)
        _remember(process.pid, owned, await asyncio.to_thread(_processes))
        initialized = await initialize(connection)
        if session_id is None:
            session = await connection.request("session/new", {"cwd": str(cwd), "mcpServers": []})
            session_id = session.get("sessionId")
        else:
            capabilities = initialized.get("agentCapabilities", {}).get("sessionCapabilities", {})
            method = "session/resume" if "resume" in capabilities else "session/load"
            session = await connection.request(method, {"sessionId": session_id, "cwd": str(cwd), "mcpServers": []})
        if not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None:
            raise ACPError("native_session_identity_mismatch")
        if session.get("sessionId", session_id) != session_id:
            raise ACPError("native_session_identity_mismatch")
        await connection.request("session/set_model", {"sessionId": session_id, "modelId": model})
        if effort is not None:
            await connection.request("session/set_config_option", {"sessionId": session_id, "configId": "thinking", "value": effort})
        mode = {"plan": "plan", "read_only": "plan", "full_access": "yolo", "workspace_write": "auto"}[permission]
        await connection.request("session/set_mode", {"sessionId": session_id, "modeId": mode})
        path = session_wire(cwd, session_id)
        before = wire_offset(path)
        updates.session_id, updates.active = session_id, True
        prompt_sent = True  # A failed/cancelled send can still have reached the owned peer.
        response = await connection.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]})
        updates.active = False
        if updates.permission_required:
            raise ACPError("native_permission_required")
        if response.get("stopReason") != "end_turn":
            raise ACPError("native_turn_incomplete")
        if any(item["state"] not in {"completed", "failed"} for item in updates.tools.values()):
            raise ACPError("native_tool_unpaired")
        # Native completion is independent of the coordinator's business
        # response format. Preserve raw text and finish the owned session;
        # the service parses the response and can request a format correction.
        await connection.request("session/close", {"sessionId": session_id})
        connection.process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        if process.returncode != 0:
            raise ACPError("cli_nonzero_exit")
        # Kimi's durable writer may flush after the ACP response. Closing the
        # session and awaiting normal EOF/exit is the native flush boundary;
        # the original offset still limits this proof to the current turn.
        proof = completed_turn(path, before)

    tracker = None
    try:
        async def track():
            while True:
                if process is not None:
                    _remember(process.pid, owned, await asyncio.to_thread(_processes))
                await asyncio.sleep(.2)
        tracker = asyncio.create_task(track())
        if timeout is not None:
            await asyncio.wait_for(turn(), timeout)
        else:
            await turn()
        result["status"] = "completed"
    except asyncio.CancelledError as error:
        cancelled = error
        if connection is not None and session_id is not None:
            try:
                await connection.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
            except (OSError, RuntimeError):
                pass
    except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as error:
        result["failure_code"] = error.code if isinstance(error, ACPError) else "native_turn_unconfirmed"
        method = error.rpc_method if isinstance(error, ACPError) else None
        phase = ("handshake" if method in {"initialize", "authenticate"} else
                 "session" if method in {"session/new", "session/load", "session/resume"} else
                 "configuration" if method in {"session/set_model", "session/set_config_option", "session/set_mode"} else "prompt")
        diagnostic = safe_failure_diagnostic({"phase": phase, "rpc_method": method,
            "rpc_code": error.rpc_code if isinstance(error, ACPError) else None, "prompt_sent": prompt_sent})
    finally:
        async def cleanup():
            clean = await _cleanup(process, owned) if process is not None else True
            if connection is not None:
                await connection.close()
            if tracker is not None:
                tracker.cancel()
                await asyncio.gather(tracker, return_exceptions=True)
            return clean
        clean, interrupted = await _settle_cleanup(cleanup())
        if interrupted and cancelled is None:
            cancelled = asyncio.CancelledError()
        if diagnostic is not None and prompt_sent and path is not None and before is not None:
            wire_failure = failed_turn_diagnostic(path, before)
            if wire_failure is not None:
                diagnostic = safe_failure_diagnostic({**diagnostic, **wire_failure})
        if diagnostic is not None and diagnostic.get("reason") == "provider_quota_exhausted" and result["status"] == "failed":
            result["failure_code"] = "subscription_limit_reached"
        if receipt_dir is not None:
            receipt_dir = Path(receipt_dir)
            receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if receipt_dir.is_symlink() or receipt_dir.parent.is_symlink():
                raise ACPError("native_receipt_unavailable")
            receipt_path = str(receipt_dir / (str(uuid4()) + ".jsonl"))
            descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "kimi_native_turn", "session_id": session_id,
                    "model": model, "permission": permission, "proof": proof,
                    "tool_calls": len(updates.tools), "tool_results": sum(item["state"] in {"completed", "failed"} for item in updates.tools.values()),
                    "cleanup_confirmed": clean, "failure_diagnostic": diagnostic}, allow_nan=False) + "\n")
        observation = {"agent": "kimi", "permission": permission, "auth_method": "kimi-code",
            "tool_calls": len(updates.tools), "tool_results": sum(item["state"] in {"completed", "failed"} for item in updates.tools.values()),
            "side_effects_possible": updates.possible_effects, "cleanup_confirmed": clean,
            "exit_code": process.returncode if process else None, "receipt_path": receipt_path,
            "duration_seconds": round(time.monotonic() - started, 3)}
        if diagnostic is not None:
            observation["failure_diagnostic"] = diagnostic
        if cancelled is not None:
            cancelled.native_execution = observation
            raise cancelled
    result.update(session_id=session_id, text=updates.text, events=updates.events,
                  cleanup_confirmed=clean, native_execution=observation)
    if updates.permission_required and updates.question is not None:
        result["native_question"] = updates.question
    if not clean:
        result.update(status="failed", failure_code="native_cleanup_unconfirmed")
    return result


async def delete_owned_session(target, *, timeout=15):
    """Official deletion of one recorded room-owned session, without a prompt.

    The session lives in the home it was created in; a frozen target names a
    Beanest profile home, otherwise it is the local Kimi login.
    """
    home = target.get("account_home")
    if home is not None and (not isinstance(home, str) or not Path(home).is_absolute()):
        return "pending"
    with adapters.account_home("kimi", home):
        return await _delete_owned_session(target, timeout=timeout)


async def _delete_owned_session(target, *, timeout=15):
    from .native_adapters import _cleanup, _processes, _remember, _settle_cleanup
    session_id = target.get("session_id")
    if target.get("shared_room_ids") != [] or _SESSION.fullmatch(session_id or "") is None:
        return "pending"
    roots = []
    for cwd in target.get("owned_cwds", []):
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return "pending"
        root = session_wire(cwd, session_id).parents[2]
        if root.exists() or root.is_symlink():
            if root.is_symlink() or not root.is_dir():
                return "pending"
            roots.append((cwd, root))
    if not roots:
        return "already_missing"
    if len(roots) != 1:
        return "pending"
    cwd, root = roots[0]
    process, connection, owned = None, None, {}
    deleted = False
    try:
        process, connection = await start(cwd)
        _remember(process.pid, owned, await asyncio.to_thread(_processes))
        await asyncio.wait_for(initialize(connection), timeout)
        await asyncio.wait_for(connection.request("session/delete", {"sessionId": session_id}), timeout)
        deleted = not root.exists()
    except (OSError, RuntimeError, asyncio.TimeoutError):
        pass
    finally:
        async def cleanup():
            clean = await _cleanup(process, owned) if process is not None else True
            if connection is not None:
                await connection.close()
            return clean
        clean, interrupted = await _settle_cleanup(cleanup())
        if interrupted:
            raise asyncio.CancelledError()
    return "provider_managed_deleted" if deleted and clean else "pending"
