"""DeepSeek Harness (`dsh`) over its own ACP server: a pay-per-use partner.

What dsh 0.2.1 does on the wire (captured from the real binary, no key and a
local fake upstream; see tests/fixtures/fake_dsh.py):

- ``dsh --profile acp`` speaks ACP v1 on stdio. ``authenticate`` is a no-op,
  there are no session modes and no ``session/load``; ``session/resume`` and
  ``session/close`` exist. Config options are ``model`` (each value is the JSON
  text ``["deepseek-official","<model>"]``) and ``reasoning_effort``.
- Permission is not an ACP mode. dsh reads ``DSH_PERMISSION_MODE`` at launch:
  ``read-only`` and ``workspace-write`` run commands in dsh's own sandbox and
  ask ``session/request_permission`` before widening it; ``danger-full-access``
  never asks. That third one is dsh's highest mode.
- A failed turn answers ``session/prompt`` with JSON-RPC -32603 and only the
  provider's text. The typed code (AUTH, QUOTA, RATE_LIMIT, MISSING_CREDENTIAL)
  and HTTP status are in the session log's ``turn/end``.
- Spend is only in the session log: every model call's ``assistant/message``
  carries its model and token usage. ACP ``usage_update`` is context
  occupancy, not spend. Subagents log to child sessions naming their parent.
- The log is zstd unless the persistence plugin says ``compression: none``.
  Beanest owns each partner's DSH_HOME and passes that setting from the first
  run, so plain JSON lines can be read without a zstd decoder.

The key is read only for the child's environment, in DEEPSEEK_API_KEY, the
variable dsh reads; dsh does not pass it on to the commands it runs.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from uuid import uuid4

from . import adapters, api_models, job_progress
from .kimi_acp import ACPConnection, ACPError, TurnUpdates


PROVIDER = "deepseek-official"
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_LOG_NAME = re.compile(r"session\.v([1-9][0-9]{0,3})\.jsonl\Z")
_MAX_LOG_BYTES = 256 * 1024 * 1024
_MAX_LINE = 64 * 1024 * 1024
PATCH_NAME = "beanest-acp.patch.yml"
# The persistence plugin's whole config: its root as dsh ships it, and plain JSON lines.
PATCH = ("# Written by Beanest for this partner's own DSH_HOME. Session logs stay plain\n"
         "# JSON lines so Beanest can read each call's token usage and failure code.\n"
         "- id: session-persistence-jsonl\n"
         "  config:\n"
         "    root: !!js dshHomePath('sessions')\n"
         "    compression: none\n")
# Beanest's permission -> dsh's own permission preset. Full Access is dsh's highest.
PERMISSION_MODES = {"plan": "read-only", "read_only": "read-only",
                    "workspace_write": "workspace-write", "full_access": "danger-full-access"}
EFFORTS = api_models.HOSTS["dsh"]["efforts"]
_USAGE = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
          "cacheReadTokens": "cache_read_input_tokens", "cacheWriteTokens": "cache_creation_input_tokens"}
# dsh's typed failure codes -> what the room does with them.
KEY_CODES = frozenset({"AUTH", "MISSING_CREDENTIAL", "INVALID_CREDENTIAL"})
FAILURE_CODES = {"QUOTA": "subscription_limit_reached", "RATE_LIMIT": "provider_rate_limited",
                 **{code: "api_key_rejected" for code in KEY_CODES}}
DIAGNOSTIC_REASONS = {"QUOTA": "provider_quota_exhausted", "RATE_LIMIT": "provider_rate_limited",
                      **{code: "provider_auth_error" for code in KEY_CODES}}


class DshTurnUpdates(TurnUpdates):
    """dsh commits each assistant message whole, with its own messageId.

    A subagent's result starts another root turn, so one prompt can end with
    several messages; the reply is the last one, never their concatenation.
    """
    _message = None

    async def __call__(self, params):
        update = params.get("update") if isinstance(params, dict) else None
        if self.active and isinstance(update, dict) and update.get("sessionUpdate") == "agent_message_chunk":
            if update.get("messageId") != self._message:
                self._message, self.text = update.get("messageId"), ""
        await super().__call__(params)


def model_value(model):
    return json.dumps([PROVIDER, model], separators=(",", ":"))


def write_patch(home):
    """The partner's own patch file, rewritten only when its text differs."""
    home = Path(home)
    path = home / PATCH_NAME
    try:
        if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == PATCH:
            return path
    except (OSError, UnicodeError):
        pass
    if path.is_symlink():
        raise ACPError("unsafe_partner_storage")
    descriptor, name = tempfile.mkstemp(prefix=".beanest-patch-", dir=home)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(PATCH)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
    return path


# --- the session log ---------------------------------------------------------

def _header(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        raw = stream.readline(65536)
    try:
        header = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return header if isinstance(header, dict) and header.get("type") == "session" else None


def session_logs(home):
    """{session id: (log path, header)} for every plain session log under one DSH_HOME."""
    root = Path(home) / "sessions"
    result = {}
    if not root.is_dir() or root.is_symlink():
        return result
    for project in root.iterdir():
        if project.is_symlink() or not project.is_dir():
            continue
        for directory in project.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not _SESSION.fullmatch(directory.name):
                continue
            logs = sorted(((int(match[1]), child) for child in directory.iterdir()
                           if (match := _LOG_NAME.fullmatch(child.name))), reverse=True)
            if not logs:
                continue
            header = _header(logs[0][1])
            if header is not None and header.get("id") == directory.name:
                result[directory.name] = (logs[0][1], header)
    return result


def session_tree(home, session_id, cwd):
    """This session's log and every subagent log under it, as {session id: path}."""
    logs = session_logs(home)
    root = logs.get(session_id)
    if root is None or root[1].get("cwd") != str(cwd):
        return {}
    tree = {session_id: root[0]}
    grew = True
    while grew:
        grew = False
        for identifier, (path, header) in logs.items():
            if identifier not in tree and header.get("parentSession") in tree:
                tree[identifier] = path
                grew = True
    return tree


def log_offsets(home, session_id, cwd):
    result = {}
    for identifier, path in session_tree(home, session_id, cwd).items():
        info = path.stat(follow_symlinks=False)
        result[identifier] = ((info.st_dev, info.st_ino), info.st_size)
    return result


def _appended(path, before):
    """Rows appended after a recorded offset; a replaced or shrunk file is refused."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        identity, offset = before if before is not None else (None, 0)
        if (not stat.S_ISREG(info.st_mode) or identity is not None and identity != (info.st_dev, info.st_ino)
                or info.st_size < offset or info.st_size - offset > _MAX_LOG_BYTES):
            raise ACPError("native_turn_unconfirmed")
        stream.seek(offset)
        rows = []
        while raw := stream.readline(_MAX_LINE + 1):
            if len(raw) > _MAX_LINE or not raw.endswith(b"\n"):
                raise ACPError("native_turn_unconfirmed")
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ACPError("native_turn_unconfirmed")
            rows.append(row)
        return rows, info.st_size


def turn_facts(home, session_id, cwd, before):
    """What this call left in the logs: per-model usage, the root's turn ends, retries.

    Only numbers, model ids and dsh's typed codes are taken; no text leaves here.
    """
    usage, ends, retries, through = {}, [], 0, None
    for identifier, path in session_tree(home, session_id, cwd).items():
        rows, size = _appended(path, before.get(identifier))
        if identifier == session_id:
            through = size
        for row in rows:
            kind, data = row.get("type"), row.get("data")
            if not isinstance(data, dict):
                continue
            if kind == "assistant/message" and isinstance(data.get("usage"), dict):
                source = data.get("message", {}).get("source", {}) if isinstance(data.get("message"), dict) else {}
                model = source.get("model") if isinstance(source, dict) else None
                counts = usage.setdefault(model if isinstance(model, str) and len(model) <= 200 else None,
                                          dict.fromkeys(_USAGE.values(), 0))
                for wire, field in _USAGE.items():
                    value = data["usage"].get(wire, 0)
                    if type(value) is not int or value < 0:
                        raise ACPError("native_turn_unconfirmed")
                    counts[field] += value
            elif kind == "llm/retry" and identifier == session_id:
                retries += 1
            elif kind == "turn/end" and identifier == session_id:
                reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
                error = reason.get("error") if isinstance(reason.get("error"), dict) else {}
                status = error.get("status")
                ends.append({"kind": reason.get("kind") if isinstance(reason.get("kind"), str) else None,
                             "code": error.get("code") if isinstance(error.get("code"), str) and len(error["code"]) <= 64 else None,
                             "status": status if type(status) is int and 100 <= status <= 599 else None})
    return {"usage": usage, "ends": ends, "retries": retries, "log_through": through}


def total_usage(by_model):
    if not by_model:
        return None
    totals = dict.fromkeys(_USAGE.values(), 0)
    for counts in by_model.values():
        for field, value in counts.items():
            totals[field] += value
    return totals


def failure_from(facts):
    """(failure code, diagnostic) from the root's last turn end, or (None, None)."""
    last = facts["ends"][-1] if facts and facts["ends"] else None
    if last is None or last["kind"] != "error":
        return None, None
    diagnostic = {"phase": "prompt", "reason": DIAGNOSTIC_REASONS.get(last["code"], "provider_error")}
    if last["status"] is not None and 400 <= last["status"] <= 599:
        diagnostic["http_status"] = last["status"]
    return FAILURE_CODES.get(last["code"], "native_provider_error"), diagnostic


# --- one call ----------------------------------------------------------------

async def start(cwd, environment, home, *, on_update=None, permission="plan"):
    patch = write_patch(home)
    process = await asyncio.create_subprocess_exec(
        adapters.find_dsh(), "--profile", "acp", "--patch", str(patch), cwd=cwd, env=environment,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=adapters.MAX_OUTPUT + 1)
    # dsh's own sandbox holds read-only and workspace-write; an escalation it
    # asks about is refused once, and Full Access never asks (approved if it did).
    return process, ACPConnection(process, on_update=on_update, permission=permission, sandboxed=False)


async def initialize(connection):
    result = await connection.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                      "clientInfo": {"name": "beanest", "version": "0.3.0"}})
    if result.get("protocolVersion") != 1:
        raise ACPError("native_protocol_invalid")
    return result


def _option_values(options, identifier):
    for option in options if isinstance(options, list) else ():
        if isinstance(option, dict) and option.get("id") == identifier:
            values = []
            for entry in option.get("options", []):
                if isinstance(entry, dict) and isinstance(entry.get("options"), list):
                    values += [item.get("value") for item in entry["options"] if isinstance(item, dict)]
                elif isinstance(entry, dict):
                    values.append(entry.get("value"))
            return values
    return []


def not_started(error, permission):
    error.native_execution = {"agent": "dsh", "permission": permission, "never_started": True,
                              "tool_calls": 0, "tool_results": 0, "side_effects_possible": False,
                              "cleanup_confirmed": True, "exit_code": None, "receipt_path": None}
    return error


async def invoke_native(prompt, cwd, session_id=None, timeout=None, *, permission="plan",
                        response_schema=None, runtime_settings=None, on_activity=None,
                        receipt_dir=None, progress_fingerprint=None, progress_file=None):
    from .native_adapters import _cleanup, _processes, _remember, _settle_cleanup
    from .message_capacity import MAX_PROMPT_BYTES
    if (not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES
            or permission not in PERMISSION_MODES):
        raise ValueError("invalid_prompt")
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("invalid_timeout")
    if session_id is not None and (not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None):
        raise ValueError("invalid_session_id")
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("invalid_workspace")
    settings = runtime_settings or {}
    if not isinstance(settings, dict) or set(settings) - {"model", "effort"}:
        raise ValueError("invalid_runtime_settings")
    if response_schema is not None:
        if not isinstance(response_schema, dict) or response_schema.get("type") != "object":
            raise ValueError("invalid_response_schema")
        schema = json.dumps(response_schema, ensure_ascii=False, allow_nan=False)
        if len(schema.encode("utf-8")) > 64 * 1024:
            raise ValueError("invalid_response_schema")
        prompt += "\nReturn your final answer as one JSON object matching this schema, without Markdown fences:\n" + schema
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("invalid_prompt")
    endpoint = adapters.bound_api_endpoint("dsh")
    home = adapters.bound_account_home("dsh")
    # A dsh partner always runs on its own key in its own home; nothing starts without them.
    if endpoint is None or home is None:
        raise not_started(adapters.ApiKeyUnavailable("api_key_unavailable"), permission)
    model = settings.get("model") or endpoint.default_model
    effort = settings.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise not_started(ACPError("runtime_effort_unsupported"), permission)
    try:
        key_environment = adapters.api_key_environment("dsh")
    except adapters.ApiKeyUnavailable as error:
        raise not_started(error, permission) from None
    secret = next(iter(key_environment.values()))
    environment = {**adapters._environment(), **key_environment, "DSH_PERMISSION_MODE": PERMISSION_MODES[permission]}
    if progress_file is not None:
        environment[job_progress.ENVIRONMENT_VARIABLE] = str(progress_file)

    updates = DshTurnUpdates(on_activity, progress_fingerprint)

    async def scrubbed(params):
        # The key never reaches anything the room parses, stores or shows.
        await updates(adapters.scrub_secret(params, secret))

    result = {"session_id": session_id, "text": "", "status": "failed", "usage": None, "events": []}
    started = time.monotonic()
    process, connection, owned = None, None, {}
    cancelled, receipt_path, proof = None, None, None
    before, prompt_sent, diagnostic, facts = None, False, None, None

    async def turn():
        nonlocal process, connection, session_id, before, prompt_sent, proof, facts
        process, connection = await start(cwd, environment, home, on_update=scrubbed, permission=permission)
        _remember(process.pid, owned, await asyncio.to_thread(_processes))
        initialized = await initialize(connection)
        if session_id is None:
            session = await connection.request("session/new", {"cwd": str(cwd), "mcpServers": []})
            session_id = session.get("sessionId")
        else:
            capabilities = initialized.get("agentCapabilities", {}).get("sessionCapabilities", {})
            if "resume" not in capabilities:
                raise ACPError("native_protocol_invalid")
            session = await connection.request("session/resume", {"sessionId": session_id, "cwd": str(cwd), "mcpServers": []})
        if not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None:
            raise ACPError("native_session_identity_mismatch")
        if session.get("sessionId", session_id) != session_id:
            raise ACPError("native_session_identity_mismatch")
        options = session.get("configOptions")
        if model_value(model) not in _option_values(options, "model"):
            raise ACPError("runtime_model_unavailable")
        await connection.request("session/set_config_option", {"sessionId": session_id, "configId": "model",
                                                               "value": model_value(model)})
        if effort is not None:
            await connection.request("session/set_config_option", {"sessionId": session_id,
                                                                   "configId": "reasoning_effort", "value": effort})
        # A new session's log appears with its first event; zero is its start.
        before = await asyncio.to_thread(log_offsets, home, session_id, cwd)
        updates.session_id, updates.active = session_id, True
        prompt_sent = True  # A failed/cancelled send can still have reached the owned peer.
        response = await connection.request("session/prompt", {"sessionId": session_id,
                                                                "prompt": [{"type": "text", "text": prompt}]})
        updates.active = False
        if updates.permission_required:
            raise ACPError("native_permission_required")
        if response.get("stopReason") != "end_turn":
            raise ACPError("native_turn_incomplete")
        if any(item["state"] not in {"completed", "failed"} for item in updates.tools.values()):
            raise ACPError("native_tool_unpaired")
        await connection.request("session/close", {"sessionId": session_id})
        connection.process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        if process.returncode != 0:
            raise ACPError("cli_nonzero_exit")
        # Close flushes persistence; only then does the log prove the turn.
        facts = await asyncio.to_thread(turn_facts, home, session_id, cwd, before)
        if not facts["ends"] or facts["ends"][-1]["kind"] != "completed":
            raise ACPError("native_turn_unconfirmed")
        proof = {"turns": len(facts["ends"]), "log_offset": before.get(session_id, (None, 0))[1],
                 "log_through": facts["log_through"]}

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
                 "session" if method in {"session/new", "session/resume"} else
                 "configuration" if method == "session/set_config_option" else "prompt")
        diagnostic = {"phase": phase, "rpc_method": method,
                      "rpc_code": error.rpc_code if isinstance(error, ACPError) else None, "prompt_sent": prompt_sent}
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
        if facts is None and prompt_sent and before is not None:
            try:
                facts = await asyncio.to_thread(turn_facts, home, session_id, cwd, before)
            except (OSError, ValueError, RuntimeError):
                facts = None  # usage stays unknown; never a guess
        if diagnostic is not None and facts is not None:
            code, typed = failure_from(facts)
            if code is not None:
                result["failure_code"] = code
                diagnostic = {**diagnostic, **typed}
        from .kimi_acp import safe_failure_diagnostic
        diagnostic = safe_failure_diagnostic(diagnostic) if diagnostic is not None else None
        by_model = facts["usage"] if facts is not None else {}
        if receipt_dir is not None:
            receipt_dir = Path(receipt_dir)
            receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if receipt_dir.is_symlink() or receipt_dir.parent.is_symlink():
                raise ACPError("native_receipt_unavailable")
            receipt_path = str(receipt_dir / (str(uuid4()) + ".jsonl"))
            descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "dsh_native_turn", "session_id": session_id, "model": model,
                    "permission": permission, "dsh_permission_mode": PERMISSION_MODES[permission], "proof": proof,
                    "tool_calls": len(updates.tools),
                    "tool_results": sum(item["state"] in {"completed", "failed"} for item in updates.tools.values()),
                    "provider_retries": facts["retries"] if facts is not None else None,
                    "cleanup_confirmed": clean, "failure_diagnostic": diagnostic}, allow_nan=False) + "\n")
        observation = {"agent": "dsh", "permission": permission, "auth_method": "api_key",
            "tool_calls": len(updates.tools),
            "tool_results": sum(item["state"] in {"completed", "failed"} for item in updates.tools.values()),
            "side_effects_possible": updates.possible_effects, "cleanup_confirmed": clean,
            "exit_code": process.returncode if process else None, "receipt_path": receipt_path,
            "duration_seconds": round(time.monotonic() - started, 3)}
        if diagnostic is not None:
            observation["failure_diagnostic"] = diagnostic
        if cancelled is not None:
            cancelled.native_execution = observation
            cancelled.api_usage = {"by_model": by_model}
            raise cancelled
    result.update(session_id=session_id, text=adapters.scrub_secret(updates.text, secret), events=updates.events,
                  cleanup_confirmed=clean, native_execution=observation, usage=total_usage(by_model),
                  api_usage={"by_model": by_model, "model": model})
    if updates.permission_required and updates.question is not None:
        result["native_question"] = adapters.scrub_secret(updates.question, secret)
    if not clean:
        result.update(status="failed", failure_code="native_cleanup_unconfirmed")
    return result


def delete_owned_session(target):
    """Delete one room-owned session and its subagents from the partner's own DSH_HOME.

    dsh's ACP has no delete; these files live in a home Beanest created for
    this partner, so removing them is the tool's own data going with the room.
    """
    home, session_id = target.get("account_home"), target.get("session_id")
    if (target.get("shared_room_ids") != [] or not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None
            or not isinstance(home, str) or not Path(home).is_absolute()):
        return "pending"
    owned = {cwd for cwd in target.get("owned_cwds", []) if isinstance(cwd, str)}
    logs = session_logs(home)
    root = logs.get(session_id)
    if root is None:
        return "already_missing"
    if root[1].get("cwd") not in owned:
        return "pending"
    tree = session_tree(home, session_id, root[1]["cwd"])
    try:
        for identifier, path in tree.items():
            shutil.rmtree(path.parent)
            cache = Path(home) / "storages" / "session_projcache" / "sessions" / (identifier + ".json")
            if cache.is_file() and not cache.is_symlink():
                cache.unlink()
    except OSError:
        return "pending"
    return "provider_managed_deleted"
