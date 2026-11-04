"""The official Claude SDK usage exchange has no user frames or model turn."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator import adapters


def usage():
    return {"rate_limits_available": True, "rate_limits": {"limits": [
        {"kind": "session", "percent": 0, "resets_at": "2026-11-01T14:29:59.671856+00:00",
         "scope": None, "is_active": True},
        {"kind": "weekly_all", "percent": 13.5, "resets_at": "2026-11-07T15:59:59Z",
         "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "percent": 100, "resets_at": None,
         "scope": {"model": {"display_name": "Opus"}, "surface": None}, "is_active": True}]},
        "session": {"total_cost_usd": 0}, "PRIVATE": "not normalized"}


def models():
    return [
        {"value": "opus", "resolvedModel": "claude-opus-example", "displayName": "Opus Example",
         "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
        {"value": "claude-sonnet-previous", "resolvedModel": "claude-sonnet-previous", "displayName": "Sonnet Previous",
         "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "max"]},
        {"value": "claude-haiku-previous", "resolvedModel": "claude-haiku-previous", "displayName": "Haiku Previous"},
    ]


class ClaudeUsageProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def program(self, mode="normal"):
        path = self.root / "usage_rpc.py"
        path.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            f"mode={mode!r}; usage={usage()!r}; models={models()!r}\n"
            "Path('pid').write_text(str(os.getpid()))\n"
            "if mode=='wait': time.sleep(30)\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " with open('requests','a') as trace: trace.write(json.dumps(request)+'\\n')\n"
            " if mode=='flood': print('x'*140000,flush=True); time.sleep(30)\n"
            " if mode=='stderr_flood': print('x'*140000,file=sys.stderr,flush=True); time.sleep(30)\n"
            " if mode in ('assistant','result','stream_event','control_request'):\n"
            "  print(json.dumps({'type':mode,'PRIVATE':'secret'}),flush=True); continue\n"
            " if mode=='invalid': print('{broken',flush=True); continue\n"
            " if mode=='duplicate': print('{\"type\":\"control_response\",\"type\":\"result\"}',flush=True); continue\n"
            " ident=request['request_id']\n"
            " account={'email':None if mode=='missing_email' else 'other@example.test' if mode=='switch' else 'bean@example.test','apiProvider':'other' if mode=='other_provider' else 'firstParty'}\n"
            " result={'account':account} if request['request']['subtype']=='initialize' else usage\n"
            " if request['request']['subtype']=='initialize' and mode!='no_models': result['models']=None if mode=='invalid_models' else models\n"
            " response={'subtype':'error' if mode=='error' else 'success','request_id':'wrong' if mode=='wrong_id' else ident,'response':result}\n"
            " print(json.dumps({'type':'control_response','response':response}),flush=True)\n")
        return [sys.executable, str(path)]

    def assert_gone(self):
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    async def test_only_two_control_reads_and_no_user_turn(self):
        result = await adapters._claude_usage_rpc(self.program(), self.root, expected_email="bean@example.test")
        self.assertEqual(result, usage())
        frames = [json.loads(line) for line in (self.root / "requests").read_text().splitlines()]
        self.assertEqual([frame["type"] for frame in frames], ["control_request", "control_request"])
        self.assertEqual([frame["request"] for frame in frames], [
            {"subtype": "initialize", "hooks": {}}, {"subtype": "get_usage", "skip_behaviors": True}])
        self.assert_gone()

    async def test_rejected_frames_and_protocol_errors_are_cleaned_without_answering(self):
        for mode in ("assistant", "result", "stream_event", "control_request", "invalid", "duplicate", "wrong_id", "error"):
            with self.subTest(mode=mode):
                (self.root / "requests").unlink(missing_ok=True)
                with self.assertRaises(adapters.AccountTelemetryError) as caught:
                    await adapters._claude_usage_rpc(self.program(mode), self.root)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(len((self.root / "requests").read_text().splitlines()), 1)
                self.assert_gone()

    async def test_initialize_account_mismatch_never_queries_usage(self):
        for mode in ("switch", "missing_email", "other_provider"):
            with self.subTest(mode=mode):
                (self.root / "requests").unlink(missing_ok=True)
                with self.assertRaises(adapters.AccountTelemetryError):
                    await adapters._claude_usage_rpc(self.program(mode), self.root, expected_email="bean@example.test")
                self.assertEqual(len((self.root / "requests").read_text().splitlines()), 1)
                self.assert_gone()

    async def test_model_catalog_uses_one_initialize_and_no_usage_or_user_frame(self):
        result = await adapters._claude_control_rpc(self.program(), self.root, query="models")
        self.assertEqual(result[0]["id"], "opus")
        self.assertEqual(result[0]["resolved_model"], "claude-opus-example")
        self.assertIn("xhigh", result[0]["efforts"])
        self.assertNotIn("xhigh", result[1]["efforts"])
        self.assertEqual(result[2]["efforts"], [], "missing per-model effort is not inherited from another model")
        self.assertNotIn("email", json.dumps(result))
        frames = [json.loads(line) for line in (self.root / "requests").read_text().splitlines()]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["request"]["subtype"], "initialize")
        self.assert_gone()

    async def test_catalog_missing_capability_and_malformed_response_have_distinct_errors(self):
        for mode, reason in (("no_models", "runtime_catalog_not_exposed"), ("invalid_models", "account_protocol_invalid")):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(adapters.AccountTelemetryError, reason):
                    await adapters._claude_control_rpc(self.program(mode), self.root, query="models")
                self.assert_gone()

    async def test_bounded_output_timeout_and_cancel_cleanup(self):
        for mode in ("flood", "stderr_flood", "wait", "cancel"):
            with self.subTest(mode=mode):
                (self.root / "pid").unlink(missing_ok=True)
                task = asyncio.create_task(adapters._claude_usage_rpc(
                    self.program("wait" if mode == "cancel" else mode), self.root, timeout=.2))
                if mode == "cancel":
                    for _ in range(100):
                        if (self.root / "pid").exists():
                            break
                        await asyncio.sleep(.005)
                    task.cancel()
                with self.assertRaises((ValueError, adapters.AccountTelemetryError, asyncio.TimeoutError, asyncio.CancelledError)):
                    await task
                self.assert_gone()

    async def test_account_query_binds_identity_before_initialize_and_after_usage(self):
        identity = {"email": "bean@example.test", "plan": "max", "auth_method": "claude.ai"}
        with patch.object(adapters, "find_claude", return_value="/trusted/claude"), \
             patch.object(adapters, "_claude_auth_status", new=AsyncMock(return_value=identity)) as auth, \
             patch.object(adapters, "_claude_usage_rpc", new=AsyncMock(return_value=usage())) as rpc:
            result = await adapters.account_status("claude", self.root)
        self.assertEqual(auth.await_count, 2)
        self.assertEqual(rpc.call_args.kwargs["expected_email"], identity["email"])
        command = rpc.call_args.args[0]
        for option in ("--safe-mode", "--restricted", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(option, command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(json.loads(command[command.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertEqual(result["state"], "ready")
        self.assertNotIn("PRIVATE", json.dumps(result))
        changed = {**identity, "email": "second@example.test"}
        with patch.object(adapters, "find_claude", return_value="/trusted/claude"), \
             patch.object(adapters, "_claude_auth_status", new=AsyncMock(side_effect=[identity, changed])), \
             patch.object(adapters, "_claude_usage_rpc", new=AsyncMock(return_value=usage())):
            result = await adapters.account_status("claude", self.root)
        self.assertEqual(result["email"], changed["email"])
        self.assertEqual(result["quota"]["windows"], [])
        self.assertEqual(result["quota"]["reason"], "account_changed")

    async def test_unsupported_usage_does_not_invent_zero_and_unconfirmed_auth_skips_rpc(self):
        identity = {"email": "bean@example.test", "plan": "max", "auth_method": "claude.ai"}
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed), \
                 patch.object(adapters, "find_claude", return_value="/trusted/claude"), \
                 patch.object(adapters, "_claude_auth_status", new=AsyncMock(return_value=identity if confirmed else dict.fromkeys(identity))), \
                 patch.object(adapters, "_claude_usage_rpc", new=AsyncMock(side_effect=adapters.AccountTelemetryError("unavailable"))) as rpc:
                result = await adapters.account_status("claude", self.root)
                self.assertEqual(result["quota"]["windows"], [])
                self.assertEqual(result["quota"]["reason"], "provider_usage_unavailable")
                self.assertEqual(rpc.await_count, 1 if confirmed else 0)


class ClaudeUsageNormalizationTests(unittest.TestCase):
    def test_model_descriptor_validation_never_inherits_or_invents_effort(self):
        rows = models() + [models()[0], {"value": "bad;cmd", "displayName": "bad"},
                           {"value": "safe", "resolvedModel": "bad;cmd", "supportsEffort": False,
                            "supportedEffortLevels": ["low"], "PRIVATE": "secret"}]
        result = adapters._claude_models(rows)
        self.assertEqual(len(result), 4)
        self.assertIsNone(result[-1]["resolved_model"])
        self.assertEqual(result[-1]["efforts"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))
        with self.assertRaises(adapters.AccountTelemetryError):
            adapters._claude_models(models() * 40)

    def test_shared_and_model_meters_keep_distinct_scope_and_real_zero(self):
        result = adapters._claude_quota(usage())
        self.assertEqual(result["scope"], "provider_account_and_model")
        self.assertEqual([row["used_percent"] for row in result["windows"]], [0, 13.5, 100])
        self.assertEqual([row["remaining_percent"] for row in result["windows"]], [100, 86.5, 0])
        self.assertEqual([row["scope"] for row in result["windows"]], ["provider_account", "provider_account", "model"])
        self.assertEqual(result["windows"][2]["model"], "Opus")
        self.assertIsInstance(result["windows"][0]["resets_at"], float)

    def test_only_known_bounded_fields_are_normalized_and_scoped_shared_kind_is_not_global(self):
        raw = usage()
        good = raw["rate_limits"]["limits"][0]
        raw["rate_limits"]["limits"] = [dict(good, percent=bad) for bad in (True, None, -1, 101, float("nan"), float("inf"), 10 ** 1000)]
        raw["rate_limits"]["limits"] += [dict(good, kind=[]), dict(good, scope={"surface": "code"}),
            dict(good, scope={"model": {"display_name": "Sonnet"}}), {"kind": "unknown", "percent": 0}]
        self.assertEqual(adapters._claude_quota(raw)["windows"], [])
        for reset in (None, "not date", "2026-11-01", "2026-11-01T00:00:00", "x" * 100):
            result = adapters._claude_quota({"rate_limits": {"limits": [dict(good, resets_at=reset)]}})
            self.assertIsNone(result["windows"][0]["resets_at"])

    def test_legacy_usage_shape_falls_back_without_double_counting_canonical_limits(self):
        raw = {"rate_limits": {"five_hour": {"utilization": 8, "resets_at": None},
               "seven_day": {"utilization": 13, "resets_at": None},
               "model_scoped": [{"display_name": "Sonnet", "utilization": 4, "resets_at": None}]}}
        self.assertEqual(len(adapters._claude_quota(raw)["windows"]), 3)
        raw["rate_limits"]["limits"] = []
        self.assertEqual(adapters._claude_quota(raw)["windows"], [])
        raw["rate_limits_available"] = False
        self.assertEqual(adapters._claude_quota(raw)["state"], "unavailable")
