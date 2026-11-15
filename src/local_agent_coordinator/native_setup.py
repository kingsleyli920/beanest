"""Known native tools and their user-triggered official install and login processes.

Discovery only resolves known executable locations. Install output is discarded.
Login output is discarded after extracting the official sign-in URL; neither
credentials nor raw output are exposed through this module.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from . import adapters


@dataclass(frozen=True)
class NativeAdapter:
    id: str
    name: str
    provider: str
    install_url: str
    install_script: str
    install_shell: str
    login_arguments: tuple[str, ...]
    login_hosts: frozenset[str] = frozenset()
    login_supported: bool = True
    # Billed per call to the user's own API key; it joins only as an API model partner.
    pay_per_use: bool = False

    def resolve(self):
        return getattr(adapters, "find_" + self.id)()


NATIVE_ADAPTERS = (
    NativeAdapter("claude", "Claude Code", "anthropic", "https://code.claude.com/docs/en/setup",
                  "https://claude.ai/install.sh", "/bin/bash", ("auth", "login", "--claudeai"),
                  frozenset({"claude.ai", "claude.com", "platform.claude.com"})),
    NativeAdapter("codex", "Codex", "openai", "https://developers.openai.com/codex/cli/",
                  "https://chatgpt.com/codex/install.sh", "/bin/sh", ("login",), frozenset({"auth.openai.com"})),
    NativeAdapter("kimi", "Kimi Code", "moonshot", "https://www.kimi.com/code/docs/kimi-code-cli/guides/getting-started.html",
                  "https://code.kimi.com/kimi-code/install.sh", "/bin/bash", ("login",)),
    # Installed from npm (@deepseek-ai/dsh); there is no official install script to run.
    NativeAdapter("dsh", "DeepSeek Harness", "deepseek", "https://github.com/deepseek-ai/deepseek-harness",
                  "", "", (), login_supported=False, pay_per_use=True),
)
INSTALL_SECONDS = 600
_DOWNLOAD_FAILED = 90
# Downloaded before running so a broken connection cannot run half a script.
_INSTALL_SCRIPT = ('set -eu; file="$(mktemp "${TMPDIR:-/tmp}/beanest-install.XXXXXX")"; trap \'rm -f "$file"\' EXIT; '
                   'curl -fsSL --connect-timeout 20 --max-time 180 "$1" -o "$file" || exit 90; "$2" "$file"')
NATIVE_IDS = frozenset(item.id for item in NATIVE_ADAPTERS)


def native_adapter(identifier):
    return next((item for item in NATIVE_ADAPTERS if item.id == identifier), None)


def native_spec_supported(spec):
    # Older configured JSON connectors used the name "kimi" too. Their frozen
    # transport must never acquire native tools just because the ID is known.
    return bool(spec is not None and spec.adapter in NATIVE_IDS
                and (spec.adapter != "kimi" or spec.supports_runtime_settings))


def device_url(raw):
    """Accept the official device endpoint, never an arbitrary printed link."""
    for candidate in re.findall(r"https://[^\s<>\"\x1b]+", raw):
        candidate = candidate.rstrip(".,;)")
        try:
            parsed = urlsplit(candidate)
            query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
            if (parsed.scheme != "https" or parsed.netloc not in {"kimi.com", "www.kimi.com"}
                    or parsed.path != "/code/authorize_device" or parsed.fragment
                    or set(query) - {"user_code"}):
                continue
            codes = query.get("user_code", [])
            if "user_code" in query and len(codes) != 1:
                continue
            code = codes[0] if len(codes) == 1 else None
            if code is not None and re.fullmatch(r"[A-Za-z0-9-]{1,64}", code) is None:
                continue
            return candidate, code
        except ValueError:
            continue
    return None


def sign_in_url(descriptor, raw):
    """The first link on this tool's own sign-in host, for when no browser opens."""
    for candidate in re.findall(r"https://[^\s<>\"\x1b]+", raw):
        candidate = candidate.rstrip(".,;)")
        try:
            if urlsplit(candidate).netloc in descriptor.login_hosts:
                return candidate
        except ValueError:
            continue
    return None


class NativeLogin:
    """One in-memory operation per adapter; cancellation is operation-bound."""
    def __init__(self, cwd, *, on_ready=None):
        self.cwd, self.on_ready = cwd, on_ready
        self.operations = {}
        self.tasks = {}

    def snapshot(self, adapter):
        operation = self.operations.get(adapter)
        return dict(operation) if operation is not None else None

    def start(self, adapter):
        descriptor = native_adapter(adapter)
        if descriptor is None or not descriptor.login_supported:
            raise ValueError("native_login_unsupported")
        task = self.tasks.get(adapter)
        if task is not None and not task.done():
            return self.snapshot(adapter)
        operation = {"id": str(uuid4()), "adapter": adapter, "state": "starting",
                     "verification_uri": None, "user_code": None, "reason": None}
        self.operations[adapter] = operation
        self.tasks[adapter] = asyncio.create_task(self._run(descriptor, operation))
        return self.snapshot(adapter)

    async def cancel(self, adapter, operation_id):
        operation = self.operations.get(adapter)
        if operation is None or operation["id"] != operation_id:
            raise ValueError("login_operation_changed")
        task = self.tasks.get(adapter)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.snapshot(adapter)

    async def close(self):
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _supervise(self, argv, consume, *, seconds=None):
        """Run one child to its end and report how it ended, always cleaning it up."""
        from .native_adapters import _cleanup, _processes, _remember, _settle_cleanup
        process, owned, readers = None, {}, []
        outcome = {"code": None, "cancelled": False, "clean": True, "error": False, "timed_out": False}
        deadline = None if seconds is None else time.monotonic() + seconds
        try:
            Path(self.cwd).mkdir(mode=0o700, parents=True, exist_ok=True)  # absent until something is first saved
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=self.cwd, env=adapters._environment(),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=65536)
            _remember(process.pid, owned, await asyncio.to_thread(_processes))
            readers = [asyncio.create_task(consume(stream)) for stream in (process.stdout, process.stderr)]
            while process.returncode is None and not outcome["timed_out"]:
                _remember(process.pid, owned, await asyncio.to_thread(_processes))
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), .2)
                except asyncio.TimeoutError:
                    outcome["timed_out"] = deadline is not None and time.monotonic() > deadline
            if not outcome["timed_out"]:
                await asyncio.gather(*readers)
                outcome["code"] = process.returncode
        except asyncio.CancelledError:
            outcome["cancelled"] = True
        except (OSError, RuntimeError, ValueError):
            outcome["error"] = True
        finally:
            async def cleanup():
                clean = await _cleanup(process, owned) if process is not None else True
                for task in readers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                return clean
            outcome["clean"], interrupted = await _settle_cleanup(cleanup())
            outcome["cancelled"] |= interrupted
        return outcome

    async def _run(self, descriptor, operation):
        from .kimi_acp import managed_configuration
        try:
            executable = descriptor.resolve()
        except RuntimeError:
            operation.update(state="failed", reason="cli_missing")
            return

        async def consume(stream):
            while raw := await stream.readline():
                text = raw.decode("utf-8", "replace")
                if descriptor.id == "kimi":
                    match = device_url(text)
                    if match:
                        operation.update(state="waiting", verification_uri=match[0], user_code=match[1])
                elif operation["state"] == "starting" or operation["verification_uri"] is None:
                    url = sign_in_url(descriptor, text)
                    operation.update(state="waiting", verification_uri=url or operation["verification_uri"])

        if descriptor.id != "kimi":
            operation.update(state="waiting")  # the tool opens the browser by itself
        outcome = await self._supervise((executable, *descriptor.login_arguments), consume)
        operation.update(verification_uri=None, user_code=None)
        if outcome["cancelled"]:
            operation.update(state="cancelled", reason="login_cancelled" if outcome["clean"] else "login_cleanup_unconfirmed")
        elif outcome["code"] == 0 and outcome["clean"]:
            ready = descriptor.id != "kimi" or managed_configuration()["state"] == "ready"
            operation.update(state="completed" if ready else "failed", reason=None if ready else "login_route_unconfirmed")
            if ready and self.on_ready is not None:
                self.on_ready(descriptor.id)
        else:
            operation.update(state="failed", reason="login_failed" if outcome["clean"] else "login_cleanup_unconfirmed")


class NativeInstall(NativeLogin):
    """Runs a tool's own official installer, only when the user asks for it."""

    def start(self, adapter):
        descriptor = native_adapter(adapter)
        if descriptor is None or not descriptor.install_script:
            raise ValueError("native_install_unsupported")
        task = self.tasks.get(adapter)
        if task is not None and not task.done():
            return self.snapshot(adapter)
        operation = {"id": str(uuid4()), "adapter": adapter, "state": "starting", "reason": None}
        self.operations[adapter] = operation
        self.tasks[adapter] = asyncio.create_task(self._run(descriptor, operation))
        return self.snapshot(adapter)

    async def _run(self, descriptor, operation):
        async def consume(stream):
            while await stream.read(65536):
                pass  # installer chatter can hold paths and is never shown

        outcome = await self._supervise(("/bin/bash", "-c", _INSTALL_SCRIPT, "beanest-install",
                                         descriptor.install_script, descriptor.install_shell),
                                        consume, seconds=INSTALL_SECONDS)
        if outcome["cancelled"]:
            operation.update(state="cancelled", reason="install_cancelled" if outcome["clean"] else "install_cleanup_unconfirmed")
            return
        reason = ("install_cleanup_unconfirmed" if not outcome["clean"] else "install_timeout" if outcome["timed_out"]
                  else "download_failed" if outcome["code"] == _DOWNLOAD_FAILED else None if outcome["code"] == 0 else "install_failed")
        if reason is None:
            try:
                descriptor.resolve()
            except RuntimeError:
                reason = "install_not_found"
        operation.update(state="failed" if reason else "completed", reason=reason)
        if reason is None and self.on_ready is not None:
            self.on_ready(descriptor.id)
