"""Owned, bounded tasks over the local Paradev session transport."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from .paradev import ParadevAdapter, ParadevConfig, ParadevError
from .task_store import StoreError, TaskStore


class BridgeError(RuntimeError):
    def __init__(self, code: str, task_id: str | None = None):
        self.code = code
        self.task_id = task_id
        super().__init__(code)


@dataclass(frozen=True)
class BridgeConfig:
    paradev: ParadevConfig
    state_path: Path
    max_tasks: int = 4
    max_turns: int = 3
    max_total_turns: int = 8
    task_timeout: float = 180

    def __post_init__(self):
        for name, maximum in (("max_tasks", 16), ("max_turns", 8), ("max_total_turns", 32)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"Invalid {name}")
        if (isinstance(self.task_timeout, bool) or not isinstance(self.task_timeout, (int, float))
                or not math.isfinite(self.task_timeout) or not 10 <= self.task_timeout <= 600):
            raise ValueError("Invalid task_timeout")
        if not self.state_path.is_absolute():
            raise ValueError("state_path must be absolute")
        if self.paradev.command_timeout > 60:
            raise ValueError("command_timeout must not exceed 60 seconds")

    @property
    def scope(self) -> str:
        identity = f"{self.paradev.repo}\n{self.paradev.workspace}"
        return hashlib.sha256(identity.encode()).hexdigest()

    @classmethod
    def load(cls, path: Path) -> BridgeConfig:
        path = path.resolve()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ValueError("Bridge configuration unavailable or invalid") from None
        required = {"paradev_repo", "bun", "workspace_id", "state_path"}
        optional = {"node_directory", "command_timeout", "max_tasks", "max_turns",
                    "max_total_turns", "task_timeout"}
        if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
            raise ValueError("Bridge configuration has missing or unknown fields")
        for key in required | ({"node_directory"} if "node_directory" in data else set()):
            if not isinstance(data[key], str) or not data[key]:
                raise ValueError("Bridge path and workspace fields must be nonempty strings")
        state_path = Path(data["state_path"])
        if not state_path.is_absolute():
            state_path = path.parent / state_path
        return cls(
            ParadevConfig(Path(data["paradev_repo"]), Path(data["bun"]), data["workspace_id"],
                          data.get("command_timeout", 20),
                          Path(data["node_directory"]) if "node_directory" in data else None),
            state_path.absolute(), **{key: data[key] for key in optional - {"node_directory", "command_timeout"}
                                   if key in data},
        )


def _prompt(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 8000 or "\0" in text:
        raise BridgeError("invalid_prompt")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise BridgeError("invalid_prompt") from None
    return text


class Bridge:
    def __init__(self, config: BridgeConfig, adapter=None, store=None):
        self.config = config
        self.adapter = adapter or ParadevAdapter(config.paradev)
        self.store = store or TaskStore(config.state_path, config.scope,
                                        max_tasks=config.max_tasks, max_turns=config.max_turns,
                                        max_total_turns=config.max_total_turns)
        self.started_here: set[str] = set()
        self.watchdog_error: str | None = None

    def _snapshot(self, task_id: str, *, cached=True) -> dict[str, Any]:
        return {"task": self.store.get(task_id), "history": self.store.history(task_id),
                "cached": cached, "usage": None,
                "usage_note": "Unavailable in this bridge; provider quota is separate from task tokens."}

    async def status(self) -> dict[str, Any]:
        sessions = await self.adapter.list()
        owned = {task["session_id"] for task in self.store.list() if task["session_id"]}
        return {"connected": True, "workspace_id": self.config.paradev.workspace,
                "configured_agents": ["claude", "codex"],
                "owned_live_sessions": sum(s.get("live") is True for s in sessions if s["sessionId"] in owned),
                "limits": {"max_tasks": self.config.max_tasks, "max_turns_per_task": self.config.max_turns,
                           "max_total_turns": self.config.max_total_turns,
                           "task_timeout_seconds": self.config.task_timeout},
                "scope": "Only tasks created by this bridge in the configured workspace.",
                "watchdog_error": self.watchdog_error,
                "usage": None}

    def list_tasks(self) -> dict[str, Any]:
        return {"tasks": self.store.list(), "cached": True}

    async def start(self, agent: str, prompt: str, request_id: str,
                    parent_task_id: str | None = None) -> dict[str, Any]:
        if not isinstance(agent, str) or agent not in {"claude", "codex"}:
            raise BridgeError("agent_not_supported")
        prompt = _prompt(prompt)
        task, fresh = self.store.reserve(agent, prompt, request_id, parent_task_id,
                                         timeout=self.config.task_timeout)
        task_id = task["id"]
        if not fresh:
            return self._snapshot(task_id)
        self.started_here.add(task_id)
        instruction = ("This is a bounded local trial task. Reply in text; do not modify files, "
                       "read credentials, contact other services, create/delegate agents or change settings. "
                       "If tools or additional permissions are needed, explain and wait.\n\nTask:\n" + prompt)
        try:
            result = await self.adapter.create(agent, instruction)
        except asyncio.CancelledError:
            self.store.finish(task_id, status="unknown", error="request_interrupted", identity_uncertain=True)
            raise
        except ParadevError as error:
            self.store.finish(task_id, status="unknown" if error.remote_state_unknown else "failed",
                              error=error.code, identity_uncertain=error.remote_state_unknown)
            raise BridgeError(error.code, task_id) from None
        task = self.store.finish(task_id, session_id=result["sessionId"], status="running")
        if task["cancel_requested"]:
            return await self._cancel_remote(task_id)
        return self._snapshot(task_id)

    async def read(self, task_id: str, refresh: bool = True,
                   before: dict | None = None) -> dict[str, Any]:
        task = self.store.get(task_id)
        if not refresh:
            return self._snapshot(task_id)
        if not task["session_id"]:
            return self._snapshot(task_id)
        page = await self.adapter.read(task["session_id"], before=before)
        self.store.record_page(task_id, page)
        current = self.store.get(task_id)
        if (before is None and page["session"] and not current["operation"]
                and current["turns"] == task["turns"]
                and current["session_id"] == page["sessionId"]):
            self.store.observe(task_id, page["session"]["status"],
                               session_id=page["sessionId"], seq=page["session"].get("seq"))
        result = self._snapshot(task_id, cached=False)
        result["next_before"] = page["nextBefore"]
        result["observed_at"] = time.time()
        return result

    async def reply(self, task_id: str, prompt: str) -> dict[str, Any]:
        prompt = _prompt(prompt)
        task = self.store.get(task_id)
        if task["deadline"] < time.time():
            await self.cancel(task_id)
            raise BridgeError("task_deadline_exceeded", task_id)
        task = self.store.begin_reply(task_id)
        self.started_here.add(task_id)
        try:
            result = await self.adapter.send(task["session_id"], prompt)
        except asyncio.CancelledError:
            self.store.finish(task_id, status="unknown", error="request_interrupted", identity_uncertain=True)
            raise
        except ParadevError as error:
            self.store.finish(task_id, status="unknown" if error.remote_state_unknown else "failed",
                              error=error.code, identity_uncertain=error.remote_state_unknown)
            raise BridgeError(error.code, task_id) from None
        task = self.store.finish(task_id, session_id=result["sessionId"], status="running")
        if task["cancel_requested"]:
            return await self._cancel_remote(task_id)
        return self._snapshot(task_id)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.request_cancel(task_id)
        if task["status"] == "cancelled":
            return {**self._snapshot(task_id), "cancellation_confirmed": True}
        if task["operation"]:
            return {**self._snapshot(task_id), "cancellation_confirmed": False,
                    "note": "Cancellation requested; waiting for the in-flight operation to settle."}
        return await self._cancel_remote(task_id)

    async def _cancel_remote(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if not task["session_id"]:
            return {**self._snapshot(task_id), "cancellation_confirmed": False,
                    "note": "Remote session identity is unknown; inspect the trial workspace. Do not retry creation."}
        try:
            await self.adapter.close(task["session_id"])
            sessions = await self.adapter.list()
        except ParadevError as error:
            self.store.finish(task_id, status="cancel_requested", error=error.code)
            raise BridgeError(error.code, task_id) from None
        known_stopped = any(s["sessionId"] == task["session_id"] and s.get("live") is False for s in sessions)
        # A lost send response may hide a resumed session with a different ID.
        confirmed = known_stopped and not task["identity_uncertain"]
        self.store.finish(task_id, status="cancelled" if confirmed else "cancel_requested",
                          error=None if confirmed else "cancellation_unconfirmed")
        result = {**self._snapshot(task_id), "cancellation_confirmed": confirmed}
        if task["identity_uncertain"]:
            result["note"] = "Known session stop attempted; an unreturned continuation may exist. Inspect the trial workspace."
        return result

    async def expire_tasks(self) -> None:
        for task in self.store.list():
            if task["deadline"] <= time.time() and task["status"] not in {"cancelled", "failed"}:
                try:
                    await self.cancel(task["id"])
                except (BridgeError, ParadevError):
                    pass  # The durable record carries the unconfirmed cancellation.

    async def shutdown(self) -> None:
        # Normal disconnects stop this process's active work; idle sessions remain inspectable.
        for task_id in self.started_here:
            task = self.store.get(task_id)
            if task["status"] in {"starting", "running", "awaiting_input", "cancel_requested", "unknown"}:
                try:
                    await self.cancel(task_id)
                except (BridgeError, ParadevError):
                    pass
