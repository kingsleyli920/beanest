"""Real stdio ACP against an owned fake peer and fictional config/wire files."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters, kimi_acp, native_adapters
from test_kimi_native_setup import MODEL, configuration


SID = "session_00000000-0000-4000-8000-000000000034"
FINAL = {"answer": "Fictional completed response"}
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
PRIVATE = "FICTIONAL_PROVIDER_ERROR_NEVER_EXPOSE"


FAKE_ACP = textwrap.dedent(r'''
    import hashlib
    import json
    import os
    from pathlib import Path
    import re
    import subprocess
    import sys
    import time

    assert sys.argv[1:] == ["acp"]
    root = Path(__file__).parent
    scenario = json.loads((root / "scenario.json").read_text())
    sid = scenario["session_id"]
    case = scenario["case"]
    pending_prompt = None
    pending_wire = []
    wire = None

    def trace(value):
        with (root / "trace.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    def send(value):
        print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)

    def response(identifier, value):
        send({"id": identifier, "result": value})

    def update(value):
        send({"method": "session/update", "params": {"sessionId": sid, "update": value}})

    def message(text):
        update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    def tool(identifier, *, done=False):
        update({"sessionUpdate": "tool_call_update" if done else "tool_call",
                "toolCallId": identifier, "kind": "read", "status": "completed" if done else "in_progress"})

    def wire_path(cwd):
        normalized = cwd.replace("\\", "/").rstrip("/")
        base = normalized.rsplit("/", 1)[-1].lower()
        slug = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-")[:40].strip("-")
        if slug in {"", ".", ".."}:
            slug = "workspace"
        bucket = "wd_" + slug + "_" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
        return Path(os.environ["HOME"]) / ".kimi-code/sessions" / bucket / sid / "agents/main/wire.jsonl"

    def append_wire(value):
        if case == "missing_wire":
            return
        record = {"time": time.time() * 1000, **value}
        if scenario.get("new_wire"):
            record["agentId"] = "main"
        if case == "flush_on_close":
            pending_wire.append(record)
            return
        with wire.open("a") as stream:
            stream.write(json.dumps(record) + "\n")

    def finish_prompt(identifier):
        if case == "no_progress":
            for index in range(3):
                identifier = "23:failed-read-" + str(index)
                update({"sessionUpdate": "tool_call", "toolCallId": identifier,
                        "kind": "read", "status": "in_progress", "rawInput": {"path": "fictional-missing.txt"}})
                update({"sessionUpdate": "tool_call_update", "toolCallId": identifier,
                        "kind": "read", "status": "failed", "rawOutput": {"error": "fictional unchanged error"}})
        elif case == "streamed_activity":
            for status in ("pending", "in_progress", "completed"):
                for index in range(40):
                    update({"sessionUpdate": "tool_call" if status == "pending" and index == 0 else "tool_call_update",
                            "toolCallId": "23:streamed-read", "kind": "read", "status": status,
                            "content": [{"type": "text", "text": str(index)}]})
        elif case != "permission" and not scenario.get("no_tools"):
            tool("23:current-read")
            if case != "unclosed_tool":
                tool("23:current-read", done=True)
        final = "[]" if case == "array_final" else json.dumps({"answer": "Fictional completed response"})
        final = scenario.get("final", final)
        message(final)
        for record in scenario.get("wire_usage", []):
            append_wire(record)
        terminal = {"type": "turn.ended", "turnId": 23, "durationMs": 12,
                    "reason": "failed" if case == "wire_failed" else "completed"}
        if case == "wire_failed":
            terminal["error"] = {"message": "FICTIONAL_PROVIDER_ERROR_NEVER_EXPOSE"}
        append_wire(terminal)
        response(identifier, {"stopReason": "end_turn"})

    (root / "parent-pid").write_text(str(os.getpid()))
    print("FICTIONAL_PROVIDER_ERROR_NEVER_EXPOSE", file=sys.stderr, flush=True)
    for raw in sys.stdin:
        row = json.loads(raw)
        method = row.get("method")
        params = row.get("params", {})
        if method is None:
            trace({"reverse_id": row.get("id"), "result": row.get("result")})
            if row.get("id") == 9001:
                finish_prompt(pending_prompt)
            continue
        summary = {"method": method}
        for key in ("sessionId", "cwd", "modelId", "modeId", "configId", "value"):
            if key in params:
                summary[key] = params[key]
        if method == "session/prompt":
            text = params["prompt"][0]["text"]
            summary["has_schema"] = "Return your final answer as one JSON object matching this schema" in text
            summary["has_answer_field"] = '"answer"' in text
        trace(summary)
        if method == "initialize":
            sessions = {"resume": {}} if scenario.get("resume", True) else {}
            response(row["id"], {"protocolVersion": 1, "agentInfo": {"name": "Fictional Kimi", "version": "0.34.0"},
                                 "agentCapabilities": {"sessionCapabilities": sessions}})
        elif method == "authenticate":
            response(row["id"], {})
        elif method in {"session/new", "session/resume", "session/load"}:
            wire = wire_path(params["cwd"])
            if case != "missing_wire":
                wire.parent.mkdir(parents=True, exist_ok=True)
                if not wire.exists():
                    wire.write_text(json.dumps({"type": "metadata", "protocol_version": "1.5"}) + "\n")
            if method != "session/new":
                tool("2:replayed-old-read")
                tool("2:replayed-old-read", done=True)
                message('{"answer":"OLD REPLAY MUST NOT BECOME THE NEW FINAL"}')
            response(row["id"], {"sessionId": sid})
        elif method in {"session/set_model", "session/set_mode", "session/set_config_option"}:
            response(row["id"], {})
        elif method == "session/prompt":
            # Installed 0.34 persists neither agentId nor turnId on turn.prompt.
            append_wire({"type": "turn.prompt", "origin": {"kind": "user"}, "input": []})
            if scenario.get("new_wire"):
                append_wire({"type": "turn.started", "turnId": 23})
            if case == "hold":
                child = subprocess.Popen([sys.executable, "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)"],
                    start_new_session=True)
                (root / "child-pid").write_text(str(child.pid))
                tool("23:unfinished-read")
                (root / "prompt-held").touch()
            elif case == "permission":
                pending_prompt = row["id"]
                send({"id": 9001, "method": "session/request_permission", "params": {
                    "sessionId": sid, "toolCall": {"toolCallId": "23:permission", "title": "Fictional native question"},
                    "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"}]}})
            else:
                finish_prompt(row["id"])
        elif method == "session/cancel":
            (root / "cancel-received").touch()
        elif method == "session/close":
            if case == "flush_on_close":
                existing = [json.loads(line) for line in wire.read_text().splitlines()]
                trace({"flush_on_close": len(pending_wire),
                       "preclose_turn_records": sum(row.get("type", "").startswith("turn.") for row in existing)})
                with wire.open("a") as stream:
                    for record in pending_wire:
                        stream.write(json.dumps(record) + "\n")
            response(row["id"], {})
            sys.exit(7 if case == "nonzero" else 0)
        else:
            raise AssertionError("unexpected fake ACP method")
''')


class KimiACPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.cwd = self.root / "中文 ACP workspace"
        self.cwd.mkdir()
        self.home = self.root / "fictional-home"
        config_path = self.home / ".kimi-code/config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(configuration(), encoding="utf-8")
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        self.executable = self.root / "fake-kimi"
        self.executable.write_text(f"#!{sys.executable}\n" + FAKE_ACP, encoding="utf-8")
        self.executable.chmod(0o700)
        self.patchers = [
            patch.object(adapters, "find_kimi", return_value=str(self.executable)),
            patch.object(adapters, "_environment", return_value={
                "HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            }),
            patch.object(kimi_acp.Path, "home", return_value=self.home),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.pending = None
        self.owned_births = {}
        self.activities = []

    async def asyncTearDown(self):
        try:
            if self.pending is not None and not self.pending.done():
                self.pending.cancel()
                await asyncio.gather(self.pending, return_exceptions=True)
            table = await asyncio.to_thread(native_adapters._processes)
            for pid, birth in self.owned_births.items():
                if pid in table and table[pid][2] == birth and "Z" not in table[pid][3]:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        finally:
            for patcher in reversed(self.patchers):
                patcher.stop()
            self.temporary.cleanup()

    def wire_path(self):
        # Official implicit-workspace derivation; no session index scan.
        normalized = str(self.cwd).replace("\\", "/").rstrip("/")
        slug = re.sub(r"[^a-z0-9._-]+", "-", self.cwd.name.lower()).strip("-")[:40].strip("-")
        bucket = "wd_" + slug + "_" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
        return self.home / ".kimi-code/sessions" / bucket / SID / "agents/main/wire.jsonl"

    def scenario(self, case="completed", **options):
        (self.root / "scenario.json").write_text(json.dumps({
            "case": case, "session_id": SID, **options,
        }), encoding="utf-8")
        (self.root / "trace.jsonl").unlink(missing_ok=True)

    def trace(self):
        return [json.loads(line) for line in (self.root / "trace.jsonl").read_text().splitlines()]

    async def invoke(self, *, session_id=None, permission="plan", progress_fingerprint=None):
        return await kimi_acp.invoke_native("Return the fictional answer.", self.cwd,
            session_id=session_id, permission=permission, timeout=8, response_schema=SCHEMA,
            runtime_settings={"model": MODEL, "effort": "high"},
            on_activity=self.activities.append, receipt_dir=self.receipts,
            progress_fingerprint=progress_fingerprint)

    def receipt(self, result):
        path = Path(result["native_execution"]["receipt_path"])
        self.assertEqual(path.parent, self.receipts)
        value = json.loads(path.read_text())
        self.assertNotIn(PRIVATE, json.dumps(value))
        return value

    def assert_failed(self, result, code):
        self.assertEqual((result["status"], result.get("failure_code")), ("failed", code))
        self.assertTrue(result["cleanup_confirmed"])
        self.assertTrue(result["native_execution"]["cleanup_confirmed"])
        self.assertNotIn(PRIVATE, json.dumps(result))

    async def test_completed_exact_turn_schema_tools_and_opaque_session(self):
        for new_shape in (False, True):
            with self.subTest(new_wire=new_shape):
                self.scenario(new_wire=new_shape)
                self.wire_path().unlink(missing_ok=True)
                self.activities.clear()
                result = await self.invoke()
                self.assertEqual(result["status"], "completed", result.get("failure_code"))
                self.assertEqual(result["session_id"], SID)
                self.assertEqual(json.loads(result["text"]), FINAL)
                self.assertTrue(result["cleanup_confirmed"])
                observation = result["native_execution"]
                self.assertEqual((observation["tool_calls"], observation["tool_results"], observation["exit_code"]), (1, 1, 0))
                self.assertFalse(observation["side_effects_possible"])
                self.assertEqual([event["state"] for event in self.activities], ["running", "completed"])
                rows = self.trace()
                prompt = next(row for row in rows if row.get("method") == "session/prompt")
                self.assertTrue(prompt["has_schema"])
                self.assertTrue(prompt["has_answer_field"])
                self.assertEqual(next(row for row in rows if row.get("method") == "session/set_mode")["modeId"], "plan")
                self.assertEqual(next(row for row in rows if row.get("method") == "session/set_config_option")["value"], "high")
                proof = self.receipt(result)["proof"]
                self.assertEqual((proof["turn_id"], proof["reason"]), (23, "completed"))
                self.assertGreater(proof["wire_through"], proof["wire_offset"])

    async def test_wire_counts_become_the_result_usage_without_touching_the_outcome(self):
        # Catches usage staying None on the real call path, or a bad usage record failing an otherwise good turn.
        used = {"inputOther": 120, "output": 30, "inputCacheRead": 4000, "inputCacheCreation": 0}
        record = {"type": "usage.record", "usage": used, "usageScope": "turn"}
        step = {"type": "context.append_loop_event", "event": {"type": "step.end", "messageId": "fictional-1", "usage": used}}
        for name, rows, expected in (
                ("reported", [record, step], {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 4000,
                                              "cache_creation_input_tokens": 0}),
                ("nothing reported", [], None),
                ("unusable counters", [{"type": "usage.record", "usage": {"inputOther": "lots"}}], None)):
            with self.subTest(name):
                self.scenario(wire_usage=rows)
                self.wire_path().unlink(missing_ok=True)
                result = await self.invoke()
                self.assertEqual(result["status"], "completed", result.get("failure_code"))
                self.assertEqual(result["usage"], expected)

    async def test_streamed_tool_content_emits_only_distinct_activity_states(self):
        self.scenario("streamed_activity")
        result = await self.invoke(permission="full_access")
        self.assertEqual(result["status"], "completed", result.get("failure_code"))
        self.assertEqual([event["state"] for event in self.activities], ["started", "running", "completed"])
        self.assertEqual(result["events"], self.activities)
        observation = result["native_execution"]
        self.assertEqual((observation["tool_calls"], observation["tool_results"], observation["exit_code"]), (1, 1, 0))
        self.assertTrue(observation["cleanup_confirmed"])
        self.assertEqual(self.receipt(result)["proof"]["reason"], "completed")

    async def test_activity_dedup_keeps_kind_changes_distinct_tools_and_raw_inputs(self):
        events = []
        updates = kimi_acp.TurnUpdates(events.append)
        updates.session_id, updates.active = SID, True
        async def send(identifier, status, kind=None, raw_input=None):
            value = {"sessionUpdate": "tool_call_update", "toolCallId": identifier, "status": status}
            if kind is not None:
                value["kind"] = kind
            if raw_input is not None:
                value["rawInput"] = raw_input
            await updates({"sessionId": SID, "update": value})
        with patch.object(updates.progress, "remember_input", wraps=updates.progress.remember_input) as remembered:
            await send("a", "pending", "read")
            for index in range(10):
                await send("a", "pending", raw_input={"path": str(index)})
            await send("a", "in_progress")
            await send("a", "in_progress", "execute")
            await send("a", "completed")
            await send("a", "completed")
            await send("b", "pending", "read")
            await send("b", "completed")
        self.assertEqual(remembered.call_count, 10)
        self.assertEqual([(event["kind"], event["state"]) for event in events],
                         [("file_read", "started"), ("file_read", "running"), ("shell", "running"),
                          ("shell", "completed"), ("file_read", "started"), ("file_read", "completed")])
        self.assertTrue(updates.possible_effects)

    async def test_failed_activity_fingerprint_is_observed_once_per_tool(self):
        events = []
        updates = kimi_acp.TurnUpdates(events.append, lambda: "0" * 64)
        updates.session_id, updates.active = SID, True
        async def send(status):
            await updates({"sessionId": SID, "update": {"sessionUpdate": "tool_call_update",
                "toolCallId": "a", "kind": "read", "status": status, "rawInput": {"path": "missing.txt"},
                "rawOutput": {"error": "fictional unchanged error"}}})
        with patch.object(updates.progress, "failed_tool", wraps=updates.progress.failed_tool) as failed:
            await send("pending")
            await send("failed")
            await send("failed")
            await send("in_progress")
            await send("failed")
        self.assertEqual(failed.call_count, 1)
        self.assertEqual([event["state"] for event in events], ["started", "failed", "running", "failed"])

    async def test_streamed_activity_persists_transitions_through_real_http_and_sqlite(self):
        from test_kimi_fenced_http import FencedACPFixture
        fixture = FencedACPFixture(case="streamed_activity")
        try:
            fixture.create()
            final = fixture.settled()
            self.assertEqual(final["objective"]["status"], "complete", final["team"])
            jobs = [job for job in final["jobs"] if job["provider"] == "kimi"]
            self.assertTrue(jobs)
            with fixture.service.store._connection() as db:
                for job in jobs:
                    states = [row[0] for row in db.execute("SELECT json_extract(value_json,'$.state') FROM events "
                        "WHERE json_extract(value_json,'$.kind')='native_tool_activity' "
                        "AND json_extract(value_json,'$.job_id')=? ORDER BY seq", (job["id"],))]
                    self.assertEqual(states, ["started", "running", "completed"])
            self.assertTrue(all(call["permission"] == "full_access" for call in fixture.native_calls))
        finally:
            fixture.close()

    async def test_completed_wire_flushed_only_during_session_close_is_verified_after_exit(self):
        self.scenario("flush_on_close")
        result = await self.invoke()
        self.assertEqual(result["status"], "completed", result.get("failure_code"))
        self.assertEqual(result["session_id"], SID)
        self.assertEqual(json.loads(result["text"]), FINAL)
        observation = result["native_execution"]
        self.assertTrue(observation["cleanup_confirmed"])
        self.assertEqual((observation["tool_calls"], observation["tool_results"], observation["exit_code"]), (1, 1, 0))
        flushed = next(row for row in self.trace() if "flush_on_close" in row)
        self.assertEqual(flushed, {"flush_on_close": 2, "preclose_turn_records": 0})
        wire = self.wire_path().read_bytes()
        records = [json.loads(line) for line in wire.splitlines()]
        self.assertEqual([record["type"] for record in records], ["metadata", "turn.prompt", "turn.ended"])
        proof = self.receipt(result)["proof"]
        self.assertEqual((proof["turn_id"], proof["reason"]), (23, "completed"))
        self.assertEqual(proof["wire_offset"], len(wire.splitlines(keepends=True)[0]))
        self.assertEqual(proof["wire_through"], len(wire))

    async def test_complete_fenced_object_without_tools_keeps_native_completion_checks(self):
        for wrapper in ("```json\n%s\n```", "```JSON\r\n%s\r\n```", "```\n%s\n```"):
            with self.subTest(wrapper=wrapper):
                final = wrapper % json.dumps(FINAL)
                self.scenario(final=final, no_tools=True)
                self.wire_path().unlink(missing_ok=True)
                result = await self.invoke(permission="full_access")
                self.assertEqual(result["status"], "completed", result.get("failure_code"))
                self.assertEqual(result["text"], final)
                observation = result["native_execution"]
                self.assertEqual((observation["tool_calls"], observation["tool_results"], observation["exit_code"]), (0, 0, 0))
                self.assertTrue(observation["cleanup_confirmed"])
                self.assertEqual(self.receipt(result)["proof"]["reason"], "completed")
                self.assertIn("session/close", [row.get("method") for row in self.trace()])
                self.assertEqual(next(row for row in self.trace() if row.get("method") == "session/set_mode")["modeId"], "yolo")

    async def test_business_format_does_not_replace_native_completion_or_raw_text(self):
        values = ('```json\n[]\n```', 'before ```json\n{}\n```',
                  '```json\n{}\n```\nafter', '```json\n{}\n```\n```json\n{}\n```',
                  '{} {}', '{"answer":"one","answer":"two"}', '{"answer":NaN}',
                  '```json\n{"answer":\n```', '', 'Ordinary unstructured text.')
        for final in values:
            with self.subTest(final=final):
                self.scenario(final=final, no_tools=True)
                self.wire_path().unlink(missing_ok=True)
                result = await self.invoke()
                self.assertEqual(result["status"], "completed", result.get("failure_code"))
                self.assertNotIn("failure_code", result)
                self.assertEqual(result["text"], final)
                observation = result["native_execution"]
                self.assertEqual((observation["tool_calls"], observation["tool_results"], observation["exit_code"]), (0, 0, 0))
                self.assertTrue(observation["cleanup_confirmed"])
                self.assertEqual(self.receipt(result)["proof"]["reason"], "completed")
                self.assertIn("session/close", [row.get("method") for row in self.trace()])

    async def test_fenced_final_does_not_override_failed_wire_or_nonzero_exit(self):
        final = "```json\n" + json.dumps(FINAL) + "\n```"
        for case, code in (("wire_failed", "native_turn_unconfirmed"),
                           ("missing_wire", "native_turn_unconfirmed"),
                           ("nonzero", "cli_nonzero_exit"),
                           ("unclosed_tool", "native_tool_unpaired")):
            with self.subTest(case=case):
                self.scenario(case, final=final)
                self.wire_path().unlink(missing_ok=True)
                result = await self.invoke()
                self.assert_failed(result, code)
                self.assertIsNone(self.receipt(result)["proof"])

    async def test_invalid_business_text_does_not_mask_real_native_failures(self):
        for case, code in (("wire_failed", "native_turn_unconfirmed"),
                           ("nonzero", "cli_nonzero_exit"),
                           ("unclosed_tool", "native_tool_unpaired")):
            with self.subTest(case=case):
                self.scenario(case, final="{broken business JSON")
                self.wire_path().unlink(missing_ok=True)
                result = await self.invoke()
                self.assert_failed(result, code)
                self.assertEqual(result["text"], "{broken business JSON")
                self.assertIsNone(self.receipt(result)["proof"])

    async def test_resume_and_load_replayed_tools_and_old_terminals_do_not_count(self):
        for supports_resume in (True, False):
            with self.subTest(resume=supports_resume):
                self.scenario(resume=supports_resume)
                wire = self.wire_path()
                wire.parent.mkdir(parents=True, exist_ok=True)
                old = "\n".join(json.dumps(row) for row in [
                    {"type": "metadata", "protocol_version": "1.5"},
                    {"type": "turn.prompt"},
                    {"type": "turn.ended", "turnId": 2, "reason": "completed"},
                ]) + "\n"
                wire.write_text(old, encoding="utf-8")
                self.activities.clear()
                result = await self.invoke(session_id=SID)
                self.assertEqual(result["status"], "completed", result.get("failure_code"))
                self.assertEqual(json.loads(result["text"]), FINAL)
                observation = result["native_execution"]
                self.assertEqual((observation["tool_calls"], observation["tool_results"]), (1, 1))
                self.assertEqual(len(self.activities), 2)
                methods = [row.get("method") for row in self.trace()]
                self.assertIn("session/resume" if supports_resume else "session/load", methods)
                self.assertNotIn("session/new", methods)
                proof = self.receipt(result)["proof"]
                self.assertEqual((proof["turn_id"], proof["wire_offset"]), (23, len(old.encode())))

    async def test_failed_or_missing_wire_cannot_promote_valid_final_and_end_turn(self):
        for case in ("wire_failed", "missing_wire"):
            with self.subTest(case=case):
                self.scenario(case)
                self.wire_path().unlink(missing_ok=True)
                result = await self.invoke()
                self.assertEqual(json.loads(result["text"]), FINAL)
                self.assert_failed(result, "native_turn_unconfirmed")
                self.assertIsNone(self.receipt(result)["proof"])

    async def test_unclosed_tool_nonzero_exit_and_no_progress_are_rejected(self):
        cases = {"unclosed_tool": "native_tool_unpaired", "nonzero": "cli_nonzero_exit",
                 "no_progress": "native_no_progress"}
        for case, code in cases.items():
            with self.subTest(case=case):
                self.scenario(case)
                result = await self.invoke(progress_fingerprint=lambda: "0" * 64)
                self.assert_failed(result, code)
                if case == "unclosed_tool":
                    self.assertEqual(result["native_execution"]["tool_results"], 0)
                elif case == "nonzero":
                    self.assertEqual(result["native_execution"]["exit_code"], 7)

    async def test_real_native_permission_request_is_cancelled_without_auto_approval(self):
        self.scenario("permission")
        result = await self.invoke(permission="full_access")
        self.assert_failed(result, "native_permission_required")
        reverse = next(row for row in self.trace() if row.get("reverse_id") == 9001)
        self.assertEqual(reverse["result"], {"outcome": {"outcome": "cancelled"}})
        mode = next(row for row in self.trace() if row.get("method") == "session/set_mode")
        self.assertEqual(mode["modeId"], "yolo")
        self.assertIsNone(self.receipt(result)["proof"])

    async def test_cancel_confirms_cleanup_of_owned_peer_and_detached_child(self):
        self.scenario("hold")
        self.pending = asyncio.create_task(self.invoke())

        async def held():
            while not (self.root / "prompt-held").exists():
                if self.pending.done():
                    self.fail(f"fake ACP exited before hold: {self.pending.result()}")
                await asyncio.sleep(.02)
        await asyncio.wait_for(held(), timeout=5)
        parent = int((self.root / "parent-pid").read_text())
        child = int((self.root / "child-pid").read_text())
        table = await asyncio.to_thread(native_adapters._processes)
        for pid in (parent, child):
            self.assertIn(pid, table)
            self.owned_births[pid] = table[pid][2]
        self.assertNotEqual(table[child][1], parent)
        self.pending.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.pending
        observation = caught.exception.native_execution
        self.assertTrue(observation["cleanup_confirmed"])
        self.assertEqual((observation["tool_calls"], observation["tool_results"]), (1, 0))
        after = await asyncio.to_thread(native_adapters._processes)
        for pid in (parent, child):
            self.assertTrue(pid not in after or "Z" in after[pid][3])
        receipt = json.loads(Path(observation["receipt_path"]).read_text())
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertIsNone(receipt["proof"])
        self.assertNotIn(PRIVATE, json.dumps(receipt))


if __name__ == "__main__":
    unittest.main()


class SetSessionMode(unittest.IsolatedAsyncioTestCase):
    class Peer:
        """A Kimi Code that refuses plan on a resumed session until it has been through default."""
        def __init__(self):
            self.modes, self.left_plan = [], False

        async def request(self, method, params):
            self.modes.append(params["modeId"])
            if params["modeId"] == "default":
                self.left_plan = True
            elif params["modeId"] == "plan" and not self.left_plan:
                raise kimi_acp.ACPError("native_provider_error", rpc_code=-32603)
            return {}

    async def test_a_resumed_plan_session_goes_through_default(self):
        # Catches every second call of a read-only room failing before its prompt is even sent.
        peer = self.Peer()
        await kimi_acp.set_session_mode(peer, "s", "plan", True)
        self.assertEqual(peer.modes, ["plan", "default", "plan"])

    async def test_other_refusals_still_fail(self):
        # Catches the workaround hiding a real failure on a new session or a different mode.
        for mode, resumed, code in (("plan", False, -32603), ("yolo", True, -32603), ("plan", True, -32000)):
            class Refusing:
                calls = 0

                async def request(self, method, params):
                    type(self).calls += 1
                    raise kimi_acp.ACPError("native_provider_error", rpc_code=code)
            with self.subTest(mode=mode, resumed=resumed, code=code):
                with self.assertRaises(kimi_acp.ACPError):
                    await kimi_acp.set_session_mode(Refusing(), "s", mode, resumed)
                self.assertEqual(Refusing.calls, 1, "no second attempt")
