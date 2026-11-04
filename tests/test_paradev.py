import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator.paradev import (
    MAX_STDERR, MAX_STDOUT, ParadevAdapter, ParadevConfig, ParadevError,
)

WORKSPACE = "423c0349-aab1-447b-b432-cf0f0c719802"
SESSION = "35136cad-bc94-4be7-9da1-e078a42cfb51"
RESUMED = "9371a2f8-5479-445b-90e9-589702c98e65"
# Synthetic redaction input, assembled so source scans cannot mistake the
# fixture for a checked-in credential. No provider ever receives this value.
SECRET = "sk-" + "synthetic_fixture_" * 3

FAKE_CLI = '''import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path.cwd()
fixture = json.loads((root / "fixture.json").read_text())
record = {"argv": sys.argv[1:], "cwd": str(root), "path": os.environ.get("PATH"),
          "api_key_inherited": "OPENAI_API_KEY" in os.environ,
          "superset_key_inherited": "SUPERSET_API_KEY" in os.environ}
with (root / "calls.jsonl").open("a") as file:
    file.write(json.dumps(record) + "\\n")
(root / "pid").write_text(str(os.getpid()))
mode = fixture.get("mode")
if mode == "sleep":
    time.sleep(30)
elif mode == "descendant":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (root / "child-pid").write_text(str(child.pid))
    def stop(signum, frame):
        child.wait(timeout=2)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    time.sleep(30)
elif mode == "closed-pipes":
    os.close(1)
    os.close(2)
    time.sleep(30)
elif mode == "overflow":
    stream = sys.stderr.buffer if fixture["stream"] == "stderr" else sys.stdout.buffer
    stream.write(b"x" * fixture["size"])
    stream.flush()
    time.sleep(30)
elif mode == "error":
    print(fixture["stderr"], file=sys.stderr)
    sys.exit(1)
elif mode == "malformed":
    print(fixture["raw"])
else:
    print(json.dumps(fixture["response"]))
'''


def envelope(seq, kind, value):
    fields = {"harness": "codex-acp"} if kind == "session" else {"startedAtMs": 1000}
    return {"v": 1, "sessionId": SESSION, "ts": 1000 + seq,
            "cursor": {"epoch": "epoch-1", "seq": seq},
            "event": {"type": kind, kind: {**fields, **value}}}


class ParadevProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve() / "lab repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts/dev-cli.ts").write_text("// never run by the fake CLI\n")
        self.bun = self.repo / "bin/fake-bun"
        self.bun.parent.mkdir()
        self.bun.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
        self.bun.chmod(0o700)
        self.node = self.repo / "node-bin"
        self.node.mkdir()
        self.adapter = ParadevAdapter(ParadevConfig(
            self.repo, self.bun, WORKSPACE, command_timeout=2,
            node_directory=self.node))
        self.fixture(response={"kind": "chat", "sessionId": SESSION})

    def fixture(self, **value):
        (self.repo / "fixture.json").write_text(json.dumps(value))

    def calls(self):
        return [json.loads(line) for line in (self.repo / "calls.jsonl").read_text().splitlines()]

    def assert_cli_gone(self):
        pid = int((self.repo / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_create_fixed_arguments_cwd_path_and_literal_prompt(self):
        literal = '--mode agent-full-access $(touch NEVER) `touch NEVER` "hello"\nsecond line'
        with patch.dict(os.environ, {"OPENAI_API_KEY": SECRET, "SUPERSET_API_KEY": SECRET}):
            result = await self.adapter.create("codex", literal)
        self.assertEqual(result, {"sessionId": SESSION, "workspaceId": WORKSPACE,
                                  "agent": "codex", "mode": "read-only"})
        record = self.calls()[0]
        self.assertEqual(record["argv"], ["scripts/dev-cli.ts", "sessions", "create",
            "--local", "--workspace", WORKSPACE, "--json", "--agent", "codex",
            "--mode", "read-only", "--prompt=" + literal])
        self.assertEqual(record["cwd"], str(self.repo))
        self.assertEqual(record["path"].split(os.pathsep)[:2], [str(self.bun.parent), str(self.node)])
        self.assertFalse(record["api_key_inherited"])
        self.assertFalse(record["superset_key_inherited"])
        self.assertFalse((self.repo / "NEVER").exists())
        await self.adapter.create("claude", "hello")
        self.assertIn("default", self.calls()[1]["argv"])

    async def test_bad_inputs_fail_before_any_process(self):
        for action in (
            self.adapter.create("unknown", "hello"),
            self.adapter.create("claude", ""),
            self.adapter.read("--last"),
            self.adapter.read(SESSION, limit=True),
            self.adapter.read(SESSION, before={"epoch": "x", "seq": -1}),
            self.adapter.read(SESSION, before={"epoch": "x", "seq": 1, "secret": SECRET}),
        ):
            with self.assertRaises(ValueError):
                await action
        self.assertFalse((self.repo / "calls.jsonl").exists())

    async def test_read_keeps_latest_visible_items_and_cursor_only(self):
        page = [
            envelope(1, "session", {"status": "running", "modeId": "read-only", "secret": SECRET}),
            envelope(2, "turn", {"id": "turn-1", "status": "running", "error": SECRET}),
            envelope(3, "item", {"id": "reasoning-1", "kind": "reasoning", "text": SECRET}),
            envelope(4, "item", {"id": "message-1", "kind": "agent_message", "text": "partial"}),
            envelope(6, "item", {"id": "message-1", "kind": "agent_message", "text": "answer " + SECRET}),
            envelope(5, "item", {"id": "message-1", "kind": "agent_message", "text": "older"}),
            envelope(7, "item", {"id": "user-1", "kind": "user_message", "content": [
                {"type": "text", "text": "visible"},
                {"type": "text", "text": SECRET, "hidden": True},
                {"type": "text", "text": "<superset-context>" + SECRET + "</superset-context>"},
                {"type": "attachment", "name": SECRET}]}),
            envelope(8, "item", {"id": "tool-1", "kind": "tool_call", "status": "running",
                                 "toolName": "Bash", "title": SECRET, "input": SECRET, "rawOutput": SECRET}),
            envelope(9, "item", {"id": "tool-1", "kind": "tool_call", "status": "completed",
                                 "toolName": "Bash", "content": [{"type": "text", "text": SECRET}]}),
            envelope(10, "turn", {"id": "turn-1", "status": "completed", "usage": {"secret": SECRET}}),
            envelope(11, "session", {"status": "idle", "modelId": "gpt-example", "credentials": SECRET}),
            envelope(12, "item", {"id": "approval-1", "kind": "approval_request", "input": SECRET}),
        ]
        cursor = {"epoch": "epoch-1", "seq": 2}
        self.fixture(response={"ok": True, "envelopes": page, "nextBefore": cursor, "raw": SECRET})
        result = await self.adapter.read(SESSION, limit=50, before={"epoch": "epoch-1", "seq": 20})
        self.assertEqual(result["session"], {"status": "idle", "modelId": "gpt-example", "seq": 11})
        self.assertEqual(result["turns"], [{"id": "turn-1", "status": "completed", "seq": 10}])
        self.assertEqual(result["messages"], [
            {"id": "message-1", "role": "assistant", "text": "answer [redacted]", "seq": 6},
            {"id": "user-1", "role": "user", "text": "visible", "seq": 7}])
        self.assertEqual(result["tools"], [{"id": "tool-1", "status": "completed", "toolName": "Bash", "seq": 9}])
        self.assertEqual(result["nextBefore"], cursor)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(json.loads(self.calls()[0]["argv"][-1]), {"epoch": "epoch-1", "seq": 20})

    async def test_send_uses_returned_resumed_session_and_close_checks_confirmation(self):
        self.fixture(response={"sessionId": RESUMED, "itemId": "user-1", "queued": False, "secret": SECRET})
        self.assertEqual(await self.adapter.send(SESSION, "follow up"),
                         {"sessionId": RESUMED, "itemId": "user-1", "queued": False})
        self.fixture(response={"sessionId": RESUMED, "closed": True, "secret": SECRET})
        self.assertEqual(await self.adapter.close(RESUMED), {"sessionId": RESUMED, "closed": True})
        self.fixture(response={"sessionId": SESSION, "closed": True})
        with self.assertRaises(ParadevError) as caught:
            await self.adapter.close(RESUMED)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertTrue(caught.exception.remote_state_unknown)
        self.assertTrue(all("--local" in call["argv"] for call in self.calls()))

    async def test_read_uses_cursor_sequence_and_flat_session_schema_fields(self):
        current = envelope(9, "item", {"id": "message-1", "kind": "agent_message", "text": "current"})
        current["seq"] = 1
        older = envelope(3, "item", {"id": "message-1", "kind": "agent_message", "text": "stale"})
        older["seq"] = 999
        state = envelope(10, "session", {"status": "idle", "modeId": "read-only",
            "modelId": "gpt-example", "config": {"modeId": "wrong-mode", "modelId": "wrong-model"}})
        stale_state = envelope(2, "session", {"status": "running", "modeId": "wrong-mode"})
        unsequenced_state = envelope(11, "session", {"status": "dead"})
        del unsequenced_state["cursor"]
        self.fixture(response={"ok": True,
            "envelopes": [current, older, state, stale_state, unsequenced_state], "nextBefore": None})
        result = await self.adapter.read(SESSION)
        self.assertEqual(result["messages"], [
            {"id": "message-1", "role": "assistant", "text": "current", "seq": 9}])
        self.assertEqual(result["session"], {"status": "idle", "modeId": "read-only",
                                            "modelId": "gpt-example", "seq": 10})

    async def test_list_excludes_titles_and_raw_fields_for_owner_filter(self):
        self.fixture(response=[{"sessionId": SESSION, "harness": "claude-acp", "status": "idle",
                                "live": True, "title": SECRET, "cwd": SECRET},
                               {"sessionId": "other-session", "status": {"secret": SECRET}}])
        self.assertEqual(await self.adapter.list(), [
            {"sessionId": SESSION, "harness": "claude-acp", "status": "idle", "live": True},
            {"sessionId": "other-session"}])

    async def test_malformed_json_and_cli_errors_never_expose_raw_output(self):
        for fixture, code in (
            ({"mode": "malformed", "raw": "{PRIVATE " + SECRET}, "invalid_json"),
            ({"mode": "error", "stderr": "Not logged in: " + SECRET}, "authentication_required"),
            ({"mode": "error", "stderr": "workspace not found: " + SECRET}, "not_found"),
            ({"mode": "error", "stderr": "unexpected private diagnostic " + SECRET}, "cli_error"),
        ):
            with self.subTest(code=code):
                self.fixture(**fixture)
                with self.assertRaises(ParadevError) as caught:
                    await self.adapter.create("claude", "hello")
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.remote_state_unknown)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertNotIn("PRIVATE", json.dumps(caught.exception.as_dict()))

    async def test_reset_and_invalid_read_schema_fail_safely(self):
        for response, code in (
            ({"ok": False, "reset": SECRET}, "journal_reset"),
            ({"ok": True, "envelopes": SECRET}, "invalid_response"),
            ({"ok": True, "envelopes": [], "nextBefore": {"epoch": SECRET, "seq": 1}}, "invalid_response"),
        ):
            self.fixture(response=response)
            with self.assertRaises(ParadevError) as caught:
                await self.adapter.read(SESSION)
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(caught.exception.remote_state_unknown)
            self.assertNotIn(SECRET, str(caught.exception))

    async def test_timeout_covers_process_even_after_pipes_close(self):
        for mode in ("sleep", "closed-pipes", "descendant"):
            self.fixture(mode=mode)
            (self.repo / "pid").unlink(missing_ok=True)
            (self.repo / "child-pid").unlink(missing_ok=True)
            adapter = ParadevAdapter(ParadevConfig(self.repo, self.bun, WORKSPACE, command_timeout=0.5))
            with self.assertRaises(ParadevError) as caught:
                await asyncio.wait_for(adapter.create("claude", "hello"), 2)
            self.assertEqual(caught.exception.code, "command_timeout")
            self.assertTrue(caught.exception.remote_state_unknown)
            self.assertIn("not a remote cancellation", str(caught.exception))
            self.assert_cli_gone()
            if mode == "descendant":
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((self.repo / "child-pid").read_text()), 0)

    async def test_both_output_limits_stop_cli_without_retaining_output(self):
        for stream, maximum in (("stdout", MAX_STDOUT), ("stderr", MAX_STDERR)):
            with self.subTest(stream=stream):
                self.fixture(mode="overflow", stream=stream, size=maximum + 1)
                with self.assertRaises(ParadevError) as caught:
                    await asyncio.wait_for(self.adapter.create("claude", "hello"), 2)
                self.assertEqual(caught.exception.code, "output_limit")
                self.assertTrue(caught.exception.remote_state_unknown)
                self.assertLess(len(str(caught.exception)), 250)
                self.assert_cli_gone()

    async def test_cancellation_reaps_cli_and_does_not_dispatch_close(self):
        self.fixture(mode="sleep")
        task = asyncio.create_task(self.adapter.create("claude", "hello"))
        for _ in range(100):
            if (self.repo / "pid").exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue((self.repo / "pid").exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_cli_gone()
        self.assertEqual(len(self.calls()), 1)

    def test_config_requires_fixed_paths_workspace_and_finite_timeout(self):
        for changes in ({"repo": Path("relative")}, {"bun": Path("relative")},
                        {"workspace": "last"}, {"command_timeout": float("inf")},
                        {"command_timeout": 0}, {"node_directory": Path("relative")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ParadevConfig(**{"repo": self.repo, "bun": self.bun, "workspace": WORKSPACE, **changes})


if __name__ == "__main__":
    unittest.main()
