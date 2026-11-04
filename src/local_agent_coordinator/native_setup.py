"""Known native tools and their user-triggered official login processes.

Discovery only resolves known executable locations. Login output is discarded
after extracting the official device URL; neither credentials nor raw output
are exposed through this module.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from . import adapters


@dataclass(frozen=True)
class NativeAdapter:
    id: str
    name: str
    provider: str
    install_url: str
    login_supported: bool = False

    def resolve(self):
        return getattr(adapters, "find_" + self.id)()


NATIVE_ADAPTERS = (
    NativeAdapter("claude", "Claude Code", "anthropic", "https://code.claude.com/docs/en/setup"),
    NativeAdapter("codex", "Codex", "openai", "https://developers.openai.com/codex/cli/"),
    NativeAdapter("kimi", "Kimi Code", "moonshot", "https://www.kimi.com/code/docs/kimi-code-cli/guides/getting-started.html", True),
)
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

    async def _run(self, descriptor, operation):
        from .native_adapters import _cleanup, _processes, _remember, _settle_cleanup
        from .kimi_acp import managed_configuration
        process, owned, readers = None, {}, []
        cancelled, code = False, None
        try:
            try:
                executable = descriptor.resolve()
            except RuntimeError:
                operation["reason"] = "cli_missing"
                return
            process = await asyncio.create_subprocess_exec(
                executable, "login", cwd=self.cwd, env=adapters._environment(),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=65536)
            _remember(process.pid, owned, await asyncio.to_thread(_processes))

            async def consume(stream):
                while raw := await stream.readline():
                    match = device_url(raw.decode("utf-8", "replace"))
                    if match:
                        operation.update(state="waiting", verification_uri=match[0], user_code=match[1])

            readers = [asyncio.create_task(consume(stream)) for stream in (process.stdout, process.stderr)]
            while process.returncode is None:
                _remember(process.pid, owned, await asyncio.to_thread(_processes))
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), .2)
                except asyncio.TimeoutError:
                    pass
            await asyncio.gather(*readers)
            code = process.returncode
        except asyncio.CancelledError:
            cancelled = True
        except (OSError, RuntimeError, ValueError):
            operation["reason"] = "login_failed"
        finally:
            async def cleanup():
                clean = await _cleanup(process, owned) if process is not None else True
                for task in readers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                return clean
            clean, interrupted = await _settle_cleanup(cleanup())
            cancelled |= interrupted
            operation.update(verification_uri=None, user_code=None)
            if cancelled:
                operation.update(state="cancelled", reason="login_cancelled" if clean else "login_cleanup_unconfirmed")
            elif code == 0 and clean:
                configuration = managed_configuration()
                ready = configuration["state"] == "ready"
                operation.update(state="completed" if ready else "failed",
                                 reason=None if ready else "login_route_unconfirmed")
                if ready and self.on_ready is not None:
                    self.on_ready(descriptor.id)
            else:
                operation.update(state="failed", reason=(operation["reason"] or "login_failed") if clean else "login_cleanup_unconfirmed")
