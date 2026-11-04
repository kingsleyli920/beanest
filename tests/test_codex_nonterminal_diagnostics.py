"""Real fake-CLI stdin/schema/exit/cleanup boundaries; no models or account reads."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters
from local_agent_coordinator import native_adapters as native
import test_native_adapters as fixtures


SESSION, FINAL, SCHEMA = fixtures.SESSION, fixtures.FINAL, fixtures.SCHEMA
PROMPT = "Synthetic owned native diagnostic fixture.\n"
PRIVATE_DIAGNOSTIC = "DO_NOT_RETAIN_DIAGNOSTIC /private/example token=fake-only"


def diagnostic_turn():
    legacy_notice = (
        "Codex is ignoring 1 unrecognized configuration setting. "
        "Check for typos or deprecated settings.\n"
        "  user (/Users/example/.codex/config.toml): `features.rmcp_client` is ignored."
    )
    events = [
        {"type": "thread.started", "thread_id": SESSION},
        {"type": "item.completed", "item": {"id": "config-1", "type": "error",
                                             "message": adapters.CODE_MODE_DISABLED_NOTICE}},
        {"type": "item.completed", "item": {"id": "config-2", "type": "error", "message": legacy_notice}},
        {"type": "turn.started"},
        *({"type": "error", "message": PRIVATE_DIAGNOSTIC + str(index)} for index in range(4)),
        {"type": "item.completed", "item": {"id": "diagnostic", "type": "error",
                                             "message": PRIVATE_DIAGNOSTIC}},
    ]
    for index in range(4):
        events.extend([
            {"type": "item.started", "item": {"id": f"tool-{index}", "type": "command_execution",
                                               "command": "rg synthetic", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": f"tool-{index}", "type": "command_execution",
                "aggregated_output": "synthetic tool result", "exit_code": 0, "status": "completed"}},
        ])
    return events + [
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": json.dumps(FINAL)}},
        {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 7}},
    ]


class CodexNonterminalDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown

    def fake_cli(self, events, *, exit_code=0):
        # Both authentication and exec are real child processes. Only the fake
        # program is selected; stdin and the private --output-schema file are
        # consumed before any diagnostic/terminal events are emitted.
        self.stub.write_text(f'''#!{sys.executable}
import json, pathlib, sys
if 'status' in sys.argv:
    print('Logged in using ChatGPT')
    raise SystemExit(0)
assert sys.stdin.read() == {PROMPT!r}
schema_index = sys.argv.index('--output-schema') + 1
assert json.loads(pathlib.Path(sys.argv[schema_index]).read_text()) == {SCHEMA!r}
for event in {events!r}:
    print(json.dumps(event), flush=True)
raise SystemExit({exit_code!r})
''')
        self.stub.chmod(0o700)
        return patch.object(adapters, "find_codex", return_value=str(self.stub))

    async def run_turn(self, events, *, exit_code=0, session_id=None):
        with self.fake_cli(events, exit_code=exit_code):
            return await native.invoke_native("codex", PROMPT, self.root,
                session_id=session_id, permission="full_access", response_schema=SCHEMA,
                receipt_dir=self.root / "receipts")

    async def test_successful_unique_turn_retains_earlier_diagnostics_as_sanitized_warnings(self):
        result = await self.run_turn(diagnostic_turn())
        self.assertEqual(result["status"], "completed", result.get("failure_code"))
        self.assertEqual(json.loads(result["text"]), FINAL)
        self.assertEqual(result["session_id"], SESSION)
        self.assertEqual((result["native_execution"]["tool_calls"], result["native_execution"]["tool_results"]), (4, 4))
        self.assertEqual(result["native_execution"]["exit_code"], 0)
        self.assertTrue(result["cleanup_confirmed"])
        warnings = [event for event in result["events"] if event["type"] == "warning"]
        self.assertEqual(sum(event.get("code") == "codex_nonterminal_diagnostic" for event in warnings), 5)
        self.assertFalse(any(event["type"] in {"error", "provider_error"} for event in result["events"]))
        self.assertNotIn(PRIVATE_DIAGNOSTIC, json.dumps(result))
        receipt = Path(result["native_execution"]["receipt_path"]).read_text()
        self.assertNotIn(PRIVATE_DIAGNOSTIC, receipt)
        self.assertNotIn("/Users/example", receipt)

    async def test_valid_final_after_the_success_terminal_is_not_an_extra_order_restriction(self):
        events = diagnostic_turn()
        final = events.pop(-2)
        events.append(final)
        result = await self.run_turn(events)
        self.assertEqual(result["status"], "completed", result.get("failure_code"))
        self.assertEqual(json.loads(result["text"]), FINAL)
        self.assertEqual(sum(event.get("code") == "codex_nonterminal_diagnostic"
                             for event in result["events"]), 5)
        self.assertTrue(result["cleanup_confirmed"])

    async def test_terminal_and_required_evidence_failures_never_become_success(self):
        normal = diagnostic_turn()
        no_final = [event for event in normal if event.get("item", {}).get("type") != "agent_message"]
        missing_tool = [event for event in normal if not (
            event["type"] == "item.completed" and event.get("item", {}).get("id") == "tool-2")]
        late_tool = next(event for event in normal if
                        event["type"] == "item.completed" and event.get("item", {}).get("id") == "tool-2")
        late_tool_result = missing_tool + [late_tool]
        invalid_final = copy.deepcopy(normal)
        invalid_final[-2]["item"]["text"] = "[]"
        empty_final = copy.deepcopy(normal)
        empty_final[-2]["item"]["text"] = "{}"
        malformed_error = copy.deepcopy(normal)
        malformed_error[4]["message"] = {"private": PRIVATE_DIAGNOSTIC}
        cases = [
            ("late top error", normal + [{"type": "error", "message": PRIVATE_DIAGNOSTIC}], 0, None),
            ("late item error", normal + [{"type": "item.completed", "item": {
                "type": "error", "message": adapters.CODE_MODE_DISABLED_NOTICE}}], 0, None),
            ("failed turn", normal[:-1] + [{"type": "turn.failed", "error": {"message": PRIVATE_DIAGNOSTIC}}], 0, None),
            ("multiple terminals", normal + [normal[-1]], 0, None),
            ("no terminal", normal[:-1], 0, None),
            ("no final", no_final, 0, None),
            ("invalid final", invalid_final, 0, None),
            ("empty final", empty_final, 0, None),
            ("nonzero exit", normal, 1, None),
            ("missing tool result", missing_tool, 0, None),
            ("tool still open at terminal", late_tool_result, 0, None),
            ("session mismatch", normal, 0, "42619ac2-4bfa-446c-a773-5193109566af"),
            ("malformed diagnostic", malformed_error, 0, None),
        ]
        for label, events, exit_code, session in cases:
            with self.subTest(case=label):
                result = await self.run_turn(events, exit_code=exit_code, session_id=session)
                self.assertNotEqual(result["status"], "completed")
                self.assertTrue(result.get("failure_code"))
                self.assertFalse(any(event.get("code") == "codex_nonterminal_diagnostic"
                                     for event in result["events"]))
                self.assertNotIn(PRIVATE_DIAGNOSTIC, json.dumps(result))

    async def test_unconfirmed_cleanup_and_legacy_parser_remain_strict(self):
        with patch.object(native, "_processes", side_effect=RuntimeError("synthetic inspection unavailable")):
            result = await self.run_turn(diagnostic_turn())
        self.assertEqual(result["failure_code"], "native_cleanup_unconfirmed")
        self.assertFalse(result["cleanup_confirmed"])
        self.assertFalse(any(event.get("code") == "codex_nonterminal_diagnostic" for event in result["events"]))
        legacy = adapters.parse_events("codex", [json.dumps(event).encode() for event in diagnostic_turn()], structured=True)
        self.assertEqual(legacy["status"], "failed")
        self.assertEqual(legacy["failure_code"], "unknown_provider_error")


if __name__ == "__main__":
    unittest.main()
