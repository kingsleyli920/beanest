import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.bridge import Bridge, BridgeConfig, BridgeError
from local_agent_coordinator.paradev import ParadevConfig, ParadevError
from local_agent_coordinator.task_store import StoreError, TaskStore


def page(session_id, status="idle", seq=10, text=None):
    return {
        "sessionId": session_id,
        "session": {"status": status, "seq": seq},
        "messages": [] if text is None else [
            {"id": "message-1", "role": "assistant", "text": text, "seq": seq}],
        "turns": [], "tools": [], "nextBefore": None,
    }


@dataclass
class Gate:
    entered: asyncio.Event
    release: asyncio.Event


class FakeAdapter:
    """Remote session effects are separate from the real SQLite task store."""

    def __init__(self):
        self.calls = []
        self.sessions = {}
        self.pages = {}
        self.gates = {}
        self.create_error = None
        self.send_error = None
        self.next_send_session = None
        self.close_changes_live = True
        self.omit_sessions_from_list = False
        self.created_count = 0

    def pause(self, operation):
        gate = Gate(asyncio.Event(), asyncio.Event())
        self.gates.setdefault(operation, deque()).append(gate)
        return gate

    async def _wait(self, operation):
        gates = self.gates.get(operation)
        if gates:
            gate = gates.popleft()
            gate.entered.set()
            await gate.release.wait()

    async def create(self, agent, prompt):
        self.calls.append(("create", agent, prompt))
        self.created_count += 1
        session_id = f"session-{self.created_count}"
        await self._wait("create")
        self.sessions[session_id] = {"sessionId": session_id, "harness": agent + "-acp",
                                     "status": "running", "live": True}
        if self.create_error is not None:
            raise self.create_error
        return {"sessionId": session_id}

    async def read(self, session_id, limit=100, before=None):
        self.calls.append(("read", session_id, before))
        captured = deepcopy(self.pages.get(session_id, page(session_id, "running", 1)))
        await self._wait("read")
        return captured

    async def send(self, session_id, prompt):
        self.calls.append(("send", session_id, prompt))
        resumed = self.next_send_session or session_id
        self.next_send_session = None
        await self._wait("send")
        if resumed != session_id:
            self.sessions[session_id]["live"] = False
            self.sessions[resumed] = {**self.sessions[session_id], "sessionId": resumed, "live": True}
        await self._wait("send-response")
        if self.send_error is not None:
            raise self.send_error
        return {"sessionId": resumed, "itemId": "follow-up", "queued": False}

    async def close(self, session_id):
        self.calls.append(("close", session_id))
        await self._wait("close")
        if self.close_changes_live:
            self.sessions[session_id]["live"] = False
        return {"sessionId": session_id, "closed": True}

    async def list(self):
        self.calls.append(("list",))
        await self._wait("list")
        return [] if self.omit_sessions_from_list else deepcopy(list(self.sessions.values()))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve() / "lab"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts/dev-cli.ts").write_text("// fixture only\n")
        self.config = BridgeConfig(
            ParadevConfig(self.repo, Path(sys.executable).resolve(), str(uuid4())),
            self.repo / "private/tasks.sqlite3", task_timeout=10)
        self.adapter = FakeAdapter()
        self.bridge = Bridge(self.config, adapter=self.adapter)

    async def start(self, request="first", agent="claude"):
        return (await self.bridge.start(agent, "bounded fixture task", request))["task"]

    async def idle(self, task, text=None, seq=10):
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "idle", seq, text)
        return (await self.bridge.read(task["id"]))["task"]

    def calls(self, operation):
        return [call for call in self.adapter.calls if call[0] == operation]

    def launch(self, coroutine):
        task = asyncio.create_task(coroutine)

        async def clean():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(clean)
        return task

    async def entered(self, gate):
        await asyncio.wait_for(gate.entered.wait(), 1)

    async def test_unknown_task_ids_are_rejected_before_remote_access(self):
        unknown = str(uuid4())
        for operation in (
            self.bridge.read(unknown), self.bridge.reply(unknown, "follow up"),
            self.bridge.cancel(unknown),
            self.bridge.start("codex", "child", "child", parent_task_id=unknown),
        ):
            with self.assertRaises(StoreError) as caught:
                await operation
            self.assertEqual(caught.exception.code, "task_not_found")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.bridge.store.list(), [])

    async def test_invalid_unicode_prompt_does_not_reserve_task_or_turn(self):
        with self.assertRaises(BridgeError) as caught:
            await self.bridge.start("claude", "\ud800", "invalid-unicode")
        self.assertEqual(caught.exception.code, "invalid_prompt")
        self.assertEqual(self.bridge.store.list(), [])
        self.assertEqual(self.adapter.calls, [])

        task = await self.idle(await self.start())
        calls = list(self.adapter.calls)
        with self.assertRaises(BridgeError) as caught:
            await self.bridge.reply(task["id"], "\udfff")
        self.assertEqual(caught.exception.code, "invalid_prompt")
        self.assertEqual(self.bridge.store.get(task["id"]), task)
        self.assertEqual(self.adapter.calls, calls)

    async def test_start_request_is_idempotent_even_while_create_is_inflight(self):
        gate = self.adapter.pause("create")
        first = self.launch(self.bridge.start("claude", "same prompt", "same-request"))
        await self.entered(gate)
        duplicate = await self.bridge.start("claude", "same prompt", "same-request")
        self.assertEqual(duplicate["task"]["status"], "starting")
        self.assertEqual(len(self.calls("create")), 1)
        gate.release.set()
        result = await first
        repeated = await self.bridge.start("claude", "same prompt", "same-request")
        self.assertEqual(result["task"]["id"], duplicate["task"]["id"])
        self.assertEqual(repeated["task"], result["task"])
        self.assertEqual(len(self.bridge.store.list()), 1)
        self.assertEqual(len(self.calls("create")), 1)
        with self.assertRaises(StoreError) as caught:
            await self.bridge.start("claude", "changed prompt", "same-request")
        self.assertEqual(caught.exception.code, "request_conflict")
        self.assertEqual(len(self.calls("create")), 1)

    async def test_uncertain_create_is_durable_unknown_and_retry_never_recreates(self):
        self.adapter.create_error = ParadevError("command_timeout", remote_state_unknown=True)
        with self.assertRaises(BridgeError) as caught:
            await self.bridge.start("codex", "same prompt", "lost-response")
        task_id = caught.exception.task_id
        task = self.bridge.store.get(task_id)
        self.assertEqual(task["status"], "unknown")
        self.assertIsNone(task["session_id"])
        self.assertIsNone(task["operation"])
        self.assertTrue(self.adapter.sessions["session-1"]["live"])
        reopened = Bridge(self.config, adapter=self.adapter)
        duplicate = await reopened.start("codex", "same prompt", "lost-response")
        self.assertEqual(duplicate["task"]["id"], task_id)
        self.assertEqual(duplicate["task"]["status"], "unknown")
        self.assertEqual(len(self.calls("create")), 1)
        cancellation = await reopened.cancel(task_id)
        self.assertFalse(cancellation["cancellation_confirmed"])
        self.assertEqual(self.calls("close"), [])

    async def test_resume_changes_session_but_retains_task_and_both_histories(self):
        task = await self.start()
        await self.idle(task, text="first answer")
        self.adapter.next_send_session = "resumed-session"
        reply = await self.bridge.reply(task["id"], "clarification")
        self.assertEqual(reply["task"]["id"], task["id"])
        self.assertEqual(reply["task"]["session_id"], "resumed-session")
        self.assertEqual(reply["task"]["turns"], 2)
        await self.idle(reply["task"], text="second answer", seq=2)
        reopened = TaskStore(self.config.state_path, self.config.scope)
        history = reopened.history(task["id"])["messages"]
        self.assertEqual([message["text"] for message in history], ["first answer", "second answer"])
        self.assertEqual([message["sourceSession"] for message in history],
                         [task["session_id"], "resumed-session"])
        self.assertEqual(len(reopened.list()), 1)

    async def test_cancel_during_create_waits_then_closes_actual_late_session(self):
        gate = self.adapter.pause("create")
        creation = self.launch(self.bridge.start("codex", "fixture", "create-race"))
        await self.entered(gate)
        reserved = self.bridge.store.list()[0]
        pending = await self.bridge.cancel(reserved["id"])
        self.assertFalse(pending["cancellation_confirmed"])
        self.assertEqual(pending["task"]["operation"], "create")
        self.assertEqual(self.calls("close"), [])
        gate.release.set()
        settled = await creation
        self.assertEqual(settled["task"]["session_id"], "session-1")
        self.assertEqual(self.calls("close"), [("close", "session-1")])
        self.assertFalse(self.adapter.sessions["session-1"]["live"])
        self.assertTrue(settled["cancellation_confirmed"])
        self.assertEqual(settled["task"]["status"], "cancelled")

    async def test_cancel_during_send_closes_late_resumed_identity(self):
        task = await self.start()
        await self.idle(task)
        self.adapter.next_send_session = "resumed-session"
        gate = self.adapter.pause("send")
        sending = self.launch(self.bridge.reply(task["id"], "follow up"))
        await self.entered(gate)
        pending = await self.bridge.cancel(task["id"])
        self.assertFalse(pending["cancellation_confirmed"])
        self.assertEqual(pending["task"]["operation"], "send")
        self.assertEqual(self.calls("close"), [])
        gate.release.set()
        settled = await sending
        self.assertEqual(settled["task"]["session_id"], "resumed-session")
        self.assertEqual(self.calls("close"), [("close", "resumed-session")])
        self.assertFalse(self.adapter.sessions["resumed-session"]["live"])
        self.assertTrue(settled["cancellation_confirmed"])

    async def test_close_ack_without_live_false_never_confirms_cancellation(self):
        task = await self.start()
        self.adapter.close_changes_live = False
        unconfirmed = await self.bridge.cancel(task["id"])
        self.assertFalse(unconfirmed["cancellation_confirmed"])
        self.assertEqual(unconfirmed["task"]["status"], "cancel_requested")
        self.assertEqual(unconfirmed["task"]["error"], "cancellation_unconfirmed")
        self.adapter.omit_sessions_from_list = True
        missing = await self.bridge.cancel(task["id"])
        self.assertFalse(missing["cancellation_confirmed"])
        self.adapter.omit_sessions_from_list = False
        self.adapter.close_changes_live = True
        confirmed = await self.bridge.cancel(task["id"])
        self.assertTrue(confirmed["cancellation_confirmed"])
        self.assertEqual(confirmed["task"]["status"], "cancelled")
        self.assertIsNone(confirmed["task"]["error"])

    async def test_watchdog_expires_active_work_without_closing_unexpired_idle(self):
        with patch("local_agent_coordinator.bridge.time.time", return_value=100):
            active = await self.start("active")
        with patch("local_agent_coordinator.bridge.time.time", return_value=105):
            idle = await self.start("idle")
            await self.idle(idle)
        with patch("local_agent_coordinator.bridge.time.time", return_value=111):
            await self.bridge.expire_tasks()
        self.assertEqual(self.calls("close"), [("close", active["session_id"])])
        self.assertEqual(self.bridge.store.get(active["id"])["status"], "cancelled")
        self.assertEqual(self.bridge.store.get(idle["id"])["status"], "idle")
        self.assertTrue(self.adapter.sessions[idle["session_id"]]["live"])

    async def test_normal_shutdown_stops_active_and_waiting_work_keeps_idle(self):
        running = await self.start("running")
        waiting = await self.start("waiting")
        self.adapter.pages[waiting["session_id"]] = page(waiting["session_id"], "awaiting_input")
        await self.bridge.read(waiting["id"])
        idle = await self.start("idle")
        await self.idle(idle)
        self.adapter.sessions["unrelated"] = {"sessionId": "unrelated", "live": True}
        await self.bridge.shutdown()
        self.assertEqual({call[1] for call in self.calls("close")},
                         {running["session_id"], waiting["session_id"]})
        self.assertTrue(self.adapter.sessions[idle["session_id"]]["live"])
        self.assertTrue(self.adapter.sessions["unrelated"]["live"])
        self.assertEqual(self.bridge.store.get(idle["id"])["status"], "idle")

    async def test_late_read_crossing_reply_cannot_finish_the_new_turn(self):
        task = await self.start()
        await self.idle(task, seq=10)
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "idle", 20)
        gate = self.adapter.pause("read")
        stale = self.launch(self.bridge.read(task["id"]))
        await self.entered(gate)
        await self.bridge.reply(task["id"], "new turn")
        gate.release.set()
        result = await stale
        self.assertEqual(result["task"]["turns"], 2)
        self.assertEqual(result["task"]["status"], "running")

    async def test_read_during_send_does_not_consume_the_idle_observation(self):
        task = await self.start()
        await self.idle(task, seq=10)
        gate = self.adapter.pause("send")
        sending = self.launch(self.bridge.reply(task["id"], "follow up"))
        await self.entered(gate)
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "idle", 20)
        during = await self.bridge.read(task["id"])
        self.assertEqual(during["task"]["operation"], "send")
        self.assertEqual(during["task"]["status"], "running")
        gate.release.set()
        await sending
        after = await self.bridge.read(task["id"])
        self.assertIsNone(after["task"]["operation"])
        self.assertEqual(after["task"]["status"], "idle")

    async def test_reopened_bridge_owns_cleanup_for_a_replied_idle_task(self):
        task = await self.start()
        await self.idle(task)
        reopened = Bridge(self.config, adapter=self.adapter)
        reply = await reopened.reply(task["id"], "continue in the new connection")
        self.assertEqual(reply["task"]["status"], "running")
        await reopened.shutdown()
        self.assertEqual(self.calls("close"), [("close", task["session_id"])])
        self.assertFalse(self.adapter.sessions[task["session_id"]]["live"])
        self.assertEqual(reopened.store.get(task["id"])["status"], "cancelled")

    async def test_uncertain_send_shutdown_stops_known_id_without_false_confirmation(self):
        task = await self.start()
        await self.idle(task)
        self.adapter.next_send_session = "unidentified-resumed-session"
        self.adapter.send_error = ParadevError("command_timeout", remote_state_unknown=True)
        with self.assertRaises(BridgeError) as caught:
            await self.bridge.reply(task["id"], "follow up")
        self.assertEqual(caught.exception.code, "command_timeout")
        unknown = self.bridge.store.get(task["id"])
        self.assertEqual(unknown["status"], "unknown")
        self.assertTrue(unknown["identity_uncertain"])
        self.assertEqual(unknown["session_id"], task["session_id"])
        await self.bridge.shutdown()
        self.assertEqual(self.calls("close"), [("close", task["session_id"])])
        self.assertTrue(self.adapter.sessions["unidentified-resumed-session"]["live"])
        cancellation = await self.bridge.cancel(task["id"])
        self.assertFalse(cancellation["cancellation_confirmed"])
        self.assertNotEqual(cancellation["task"]["status"], "cancelled")
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "idle", 99)
        await self.bridge.read(task["id"])
        persisted = TaskStore(self.config.state_path, self.config.scope).get(task["id"])
        self.assertTrue(persisted["identity_uncertain"])
        self.assertNotEqual(persisted["status"], "cancelled")

    async def test_interrupted_send_expiry_preserves_uncertainty_after_best_effort_stop(self):
        with patch("local_agent_coordinator.bridge.time.time", return_value=100):
            task = await self.start()
            await self.idle(task)
            self.adapter.next_send_session = "unidentified-resumed-session"
            gate = self.adapter.pause("send-response")
            sending = self.launch(self.bridge.reply(task["id"], "follow up"))
            await self.entered(gate)
            self.assertTrue(self.adapter.sessions["unidentified-resumed-session"]["live"])
            sending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sending
        unknown = self.bridge.store.get(task["id"])
        self.assertEqual(unknown["status"], "unknown")
        self.assertTrue(unknown["identity_uncertain"])
        with patch("local_agent_coordinator.bridge.time.time", return_value=111):
            await self.bridge.expire_tasks()
        self.assertEqual(self.calls("close"), [("close", task["session_id"])])
        self.assertFalse(self.adapter.sessions[task["session_id"]]["live"])
        self.assertTrue(self.adapter.sessions["unidentified-resumed-session"]["live"])
        cancellation = await self.bridge.cancel(task["id"])
        self.assertFalse(cancellation["cancellation_confirmed"])
        persisted = TaskStore(self.config.state_path, self.config.scope).get(task["id"])
        self.assertTrue(persisted["identity_uncertain"])
        self.assertNotEqual(persisted["status"], "cancelled")

    async def test_late_read_from_old_session_cannot_replace_resumed_status(self):
        task = await self.start()
        await self.idle(task)
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "idle", 500, "late old answer")
        gate = self.adapter.pause("read")
        stale = self.launch(self.bridge.read(task["id"]))
        await self.entered(gate)
        self.adapter.next_send_session = "resumed-session"
        await self.bridge.reply(task["id"], "new turn")
        self.adapter.pages["resumed-session"] = page("resumed-session", "running", 1)
        await self.bridge.read(task["id"])
        gate.release.set()
        result = await stale
        self.assertEqual(result["task"]["session_id"], "resumed-session")
        self.assertEqual(result["task"]["status"], "running")
        self.assertEqual(result["task"]["provider_status"], "running")
        self.assertEqual(result["history"]["messages"][0]["sourceSession"], task["session_id"])

    async def test_out_of_order_reads_and_history_pagination_do_not_regress_status(self):
        task = await self.start()
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "running", 5)
        gate = self.adapter.pause("read")
        stale = self.launch(self.bridge.read(task["id"]))
        await self.entered(gate)
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "idle", 10)
        await self.bridge.read(task["id"])
        gate.release.set()
        self.assertEqual((await stale)["task"]["status"], "idle")
        self.adapter.pages[task["session_id"]] = page(task["session_id"], "running", 20)
        older_page = await self.bridge.read(task["id"], before={"epoch": "epoch-1", "seq": 30})
        self.assertEqual(older_page["task"]["status"], "idle")
        self.assertEqual(older_page["task"]["provider_status"], "idle")

    async def test_status_counts_only_owned_live_sessions(self):
        task = await self.start()
        self.adapter.sessions["unrelated"] = {"sessionId": "unrelated", "live": True}
        status = await self.bridge.status()
        self.assertEqual(status["owned_live_sessions"], 1)
        self.assertNotIn("unrelated", str(status))
        self.assertIsNone(status["usage"])
        self.assertEqual(self.bridge.list_tasks()["tasks"][0]["id"], task["id"])

    def test_config_load_preserves_state_symlink_for_store_rejection(self):
        linked = self.config.state_path.parent / "linked.sqlite3"
        linked.symlink_to(self.config.state_path)
        config_file = self.repo / "bridge-config.json"
        config_file.write_text(json.dumps({
            "paradev_repo": str(self.repo), "bun": str(self.config.paradev.bun),
            "workspace_id": self.config.paradev.workspace,
            "state_path": "private/linked.sqlite3",
        }))
        loaded = BridgeConfig.load(config_file)
        self.assertEqual(loaded.state_path, linked)
        self.assertTrue(loaded.state_path.is_symlink())
        with self.assertRaises(StoreError) as caught:
            Bridge(loaded, adapter=self.adapter)
        self.assertEqual(caught.exception.code, "invalid_store_path")
        self.assertEqual(self.adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
