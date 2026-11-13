"""DeepSeek Harness as a pay-per-use partner: dsh's own ACP, the user's key, prices and caps.

Real storage and real subprocesses: the transport talks to tests/fixtures/fake_dsh.py,
which replays messages captured from the real dsh 0.2.1-rc.2. No model is called and
no real key exists: the key below is made up.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, api_models, dsh_acp, native_adapters, room_service
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.room_service import RoomService
from tests import test_native_room_service as native
from tests.test_native_room_service import native_reply
from tests.test_team_store import proposal
from wait_support import within

FAKE_KEY = "sk-beanest-test-7f3a91c2e8d04b6a"
FAKE_DSH = Path(__file__).parent / "fixtures" / "fake_dsh.py"
# Saturday 2027-01-16 08:00 UTC: off-peak in the preset's weekday peak hours.
SATURDAY = datetime(2027, 1, 16, 8, 0, tzinfo=timezone.utc).timestamp()
# Friday 2027-01-15 08:00 UTC: inside the preset's 06:00-10:00 peak window.
FRIDAY_PEAK = datetime(2027, 1, 15, 8, 0, tzinfo=timezone.utc).timestamp()


def settings(key_path, **overrides):
    preset = api_models.presets()[0]
    value = {field: preset[field] for field in ("host", "currency", "timezone", "peak_windows",
                                                "default_model", "models", "caps")}
    value["key"] = {"kind": "env_file", "path": str(key_path), "variable": "DEEPSEEK_API_KEY"}
    value.update(overrides)
    return value


def write_key(path, value=FAKE_KEY):
    path.write_text(f"# local secrets\nOTHER=1\nexport DEEPSEEK_API_KEY=\"{value}\"\n")
    path.chmod(0o600)


def fake_dsh_executable(directory):
    """A `dsh` on disk that runs the captured-protocol fake with this interpreter."""
    path = Path(directory) / "dsh"
    path.write_text(f"#!/bin/sh\nexec {json.dumps(sys.executable)} {json.dumps(str(FAKE_DSH))} \"$@\"\n")
    path.chmod(0o700)
    return str(path)


def traces(home):
    directory = Path(home) / "fake-traces"
    return [json.loads(path.read_text()) for path in sorted(directory.iterdir())] if directory.is_dir() else []


class Validation(unittest.TestCase):
    def test_only_the_dsh_host_is_accepted_and_no_endpoint_can_be_set(self):
        # Catches the retired Claude-hosted path or a custom endpoint slipping back into saved partners.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.env"
            self.assertEqual(api_models.config(settings(path))["host"], "dsh")
            with self.assertRaises(api_models.ApiModelError) as caught:
                api_models.config(settings(path, host="claude"))
            self.assertEqual(caught.exception.code, "unsupported_api_host")
            with self.assertRaises(api_models.ApiModelError):
                api_models.config({**settings(path), "base_url": "https://api.deepseek.com/anthropic"})

    def test_a_key_value_is_refused_where_only_its_location_belongs(self):
        # Catches a form or caller saving the key itself into partner storage.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.env"
            for reference in ({"kind": "env_file", "path": str(path), "variable": "K", "value": FAKE_KEY},
                              {"kind": "literal", "value": FAKE_KEY}, {"kind": "env_file", "path": "keys.env", "variable": "K"},
                              {"kind": "keychain", "service": "svc", "password": FAKE_KEY}):
                with self.assertRaises(api_models.ApiModelError):
                    api_models.key_reference(reference)
            with self.assertRaises(api_models.ApiModelError):
                api_models.config({**settings(path), "key_value": FAKE_KEY})

    def test_env_file_reading_follows_shell_quoting_and_reports_nothing_about_the_file(self):
        # Catches a quoted or exported key being read with its quotes, or a missing key read as empty.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.env"
            write_key(path)
            reference = {"kind": "env_file", "path": str(path), "variable": "DEEPSEEK_API_KEY"}
            self.assertEqual(api_models.read_key(reference), FAKE_KEY)
            path.write_text("DEEPSEEK_API_KEY=plain-key # comment\n")
            self.assertEqual(api_models.read_key(reference), "plain-key")
            for text in ("DEEPSEEK_API_KEY=\n", "OTHER=1\n", "DEEPSEEK_API_KEY=two words\n"):
                path.write_text(text)
                with self.assertRaises(api_models.ApiModelError) as caught:
                    api_models.read_key(reference)
                self.assertEqual(str(caught.exception), "api_key_unavailable")
            path.unlink()
            with self.assertRaises(api_models.ApiModelError):
                api_models.read_key(reference)

    def test_the_preset_prices_are_deepseeks_published_ones(self):
        # Catches a typo in the shipped table: per 1M tokens, off-peak, USD, as published on 2026-11-13.
        models = {model["id"]: model for model in api_models.presets()[0]["models"]}
        self.assertEqual({key: models["deepseek-flash"][key] for key in ("input", "output", "cache_read")},
                         {"input": 0.15, "output": 0.6, "cache_read": 0.003})
        self.assertEqual({key: models["deepseek-v4-pro"][key] for key in ("input", "output", "cache_read")},
                         {"input": 0.66, "output": 1.98, "cache_read": 0.022})
        self.assertEqual(models["deepseek-v4-pro"]["peak"]["output"], 3.96)
        self.assertEqual(models["deepseek-v4-flash"]["input"], models["deepseek-flash"]["input"], "retired name, Flash price")


class Cost(unittest.TestCase):
    def setUp(self):
        self.settings = api_models.config(settings("/nonexistent/keys.env"))

    def test_every_token_kind_is_priced_including_cache_reads_and_writes(self):
        # Catches cache tokens being ignored or charged at the full input price.
        usage = {"values": {"input_tokens": 1_000_000, "output_tokens": 500_000,
                            "cache_read_input_tokens": 2_000_000, "cache_creation_input_tokens": 1_000_000}}
        cost = api_models.call_cost(self.settings, "deepseek-v4-pro", usage, SATURDAY)
        self.assertAlmostEqual(cost, 0.66 + 0.99 + 0.044 + 0.66)

    def test_peak_hours_cost_the_peak_price_and_only_on_their_days(self):
        # Catches the window being ignored, applied at the weekend, or read in local time instead of the table's zone.
        usage = {"values": {"input_tokens": 0, "output_tokens": 1_000_000}}
        self.assertAlmostEqual(api_models.call_cost(self.settings, "deepseek-v4-pro", usage, FRIDAY_PEAK), 3.96)
        self.assertAlmostEqual(api_models.call_cost(self.settings, "deepseek-v4-pro", usage, SATURDAY), 1.98)
        self.assertAlmostEqual(api_models.call_cost(self.settings, "deepseek-v4-pro", usage, FRIDAY_PEAK + 2 * 3600), 1.98,
                               "10:00 is the end of the window")
        self.assertAlmostEqual(api_models.call_cost(self.settings, "deepseek-flash", usage, FRIDAY_PEAK), 1.2)

    def test_a_window_past_midnight_belongs_to_the_day_it_started(self):
        # Catches a 22:00-02:00 window losing its early hours, or charging a Saturday morning for a Friday-only window.
        night = {**self.settings, "peak_windows": [{"start": "22:00", "end": "02:00", "days": [4]}]}
        usage = {"values": {"input_tokens": 0, "output_tokens": 1_000_000}}
        friday_late = datetime(2027, 1, 15, 23, 0, tzinfo=timezone.utc).timestamp()
        saturday_early = datetime(2027, 1, 16, 1, 0, tzinfo=timezone.utc).timestamp()
        sunday_early = datetime(2027, 1, 17, 1, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(api_models.call_cost(night, "deepseek-v4-pro", usage, friday_late), 3.96)
        self.assertAlmostEqual(api_models.call_cost(night, "deepseek-v4-pro", usage, saturday_early), 3.96)
        self.assertAlmostEqual(api_models.call_cost(night, "deepseek-v4-pro", usage, sunday_early), 1.98)

    def test_a_call_that_reported_nothing_has_no_cost_rather_than_zero(self):
        # Catches an unreported call silently counting as free.
        self.assertIsNone(api_models.call_cost(self.settings, "deepseek-v4-pro", None, SATURDAY))
        self.assertIsNone(api_models.call_cost(self.settings, "deepseek-v4-pro", {"values": {}}, SATURDAY))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.ledger = api_models.SpendLedger(self.root)
        self.settings = api_models.config(settings("/nonexistent/keys.env", caps={"daily": 2.0, "monthly": 3.0}))

    def test_a_new_day_starts_from_zero_and_the_month_keeps_counting(self):
        # Catches yesterday's spend holding today's daily cap, or a new day erasing the month.
        self.ledger.record("ds", self.settings, 1.5, SATURDAY)
        self.ledger.record("ds", self.settings, 0.6, SATURDAY + 60)
        today = self.ledger.snapshot("ds", self.settings, SATURDAY + 120)
        self.assertEqual((today["day_spent"], today["capped"], today["state"]), (2.1, ["daily"], "capped"))
        tomorrow = api_models.SpendLedger(self.root).snapshot("ds", self.settings, SATURDAY + 86400)
        self.assertEqual((tomorrow["day"], tomorrow["day_spent"], tomorrow["month_spent"]), ("2027-01-17", 0.0, 2.1))
        self.assertEqual(tomorrow["state"], "available")

    def test_a_new_month_starts_from_zero(self):
        # Catches the monthly cap never lifting.
        self.ledger.record("ds", self.settings, 1.0, SATURDAY)
        self.ledger.record("ds", self.settings, 1.9, SATURDAY + 86400)
        self.ledger.record("ds", self.settings, 0.2, SATURDAY + 2 * 86400)
        self.assertEqual(self.ledger.snapshot("ds", self.settings, SATURDAY + 2 * 86400)["capped"], ["monthly"])
        february = datetime(2027, 2, 1, 0, 30, tzinfo=timezone.utc).timestamp()
        snapshot = self.ledger.snapshot("ds", self.settings, february)
        self.assertEqual((snapshot["month"], snapshot["month_spent"], snapshot["month_calls"], snapshot["state"]),
                         ("2027-02", 0.0, 0, "available"))

    def test_near_the_tighter_cap_is_the_reserve(self):
        # Catches the reserve being measured against the looser cap, or at the wrong line.
        self.ledger.record("ds", self.settings, 1.7, SATURDAY)  # 15% of the daily cap left, 43% of the monthly
        snapshot = self.ledger.snapshot("ds", self.settings, SATURDAY)
        self.assertEqual((snapshot["state"], snapshot["left_percent"]), ("near_cap", 15.0))
        self.ledger.record("ds", self.settings, None, SATURDAY)
        snapshot = self.ledger.snapshot("ds", self.settings, SATURDAY)
        self.assertEqual((snapshot["month_calls"], snapshot["month_unpriced_calls"], snapshot["day_spent"]), (2, 1, 1.7))

    def test_without_caps_nothing_is_ever_capped(self):
        # Catches an unset cap reading as a zero cap.
        uncapped = {**self.settings, "caps": {"daily": None, "monthly": None}}
        self.ledger.record("ds", uncapped, 1000.0, SATURDAY)
        snapshot = self.ledger.snapshot("ds", uncapped, SATURDAY)
        self.assertEqual((snapshot["state"], snapshot["left_percent"], snapshot["capped"]), ("available", None, []))


class Transport(unittest.IsolatedAsyncioTestCase):
    """dsh_acp against the captured-protocol fake as a real child process."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "partners" / "homes" / "ds"
        self.home.mkdir(parents=True)
        self.work = self.root / "work"
        self.work.mkdir()
        self.key_file = self.root / "keys.env"
        write_key(self.key_file)
        self.enterContext(patch.object(adapters, "find_dsh", return_value=fake_dsh_executable(self.root)))
        self.endpoint = adapters.ApiEndpoint(host="dsh", key=tuple(sorted({"kind": "env_file", "path": str(self.key_file),
                                             "variable": "DEEPSEEK_API_KEY"}.items())), default_model="deepseek-flash")

    def scenario(self, case="text", **extra):
        (self.home / "fake-scenario.json").write_text(json.dumps({"case": case, **extra}))

    async def call(self, permission="workspace_write", session_id=None, endpoint=True, **options):
        with adapters.account_home("dsh", self.home, self.endpoint if endpoint else None):
            return await native_adapters.invoke_native("dsh", "Say hi.", self.work, session_id, 30,
                                                       permission=permission, receipt_dir=self.root / "receipts", **options)

    async def test_a_turn_runs_dsh_with_its_key_its_own_home_and_plain_session_logs(self):
        # Catches the key missing or renamed, the user's ~/.dsh being used, or logs left compressed and unreadable.
        self.scenario(answer="hi")
        result = await self.call()
        self.assertEqual((result["status"], result["text"]), ("completed", "hi"), result)
        [trace] = traces(self.home)
        self.assertEqual(trace["key_sha256"], hashlib.sha256(FAKE_KEY.encode()).hexdigest())
        self.assertEqual(trace["argv"][:3], ["--profile", "acp", "--patch"])
        self.assertEqual(trace["argv"][3], str(self.home / dsh_acp.PATCH_NAME))
        self.assertFalse(trace["base_url_variable"], "no endpoint override reaches dsh")
        self.assertEqual(trace["selected"], {"model": dsh_acp.model_value("deepseek-flash")}, "the partner's default, priced")
        self.assertEqual(trace["methods"][:4], ["initialize", "session/new", "session/set_config_option", "session/prompt"])
        logs = list((self.home / "sessions").rglob("session.v4.jsonl"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(result["api_usage"]["by_model"], {"deepseek-flash": {"input_tokens": 1500, "output_tokens": 7,
                         "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}})

    async def test_each_permission_is_dshs_own_mode_and_full_access_is_its_highest(self):
        # Catches Full Access mapped below danger-full-access, or a read-only call able to write.
        seen = {}
        for permission in ("plan", "read_only", "workspace_write", "full_access"):
            self.scenario()
            await self.call(permission)
            seen[permission] = traces(self.home)[-1]["permission_mode"]
        self.assertEqual(seen, {"plan": "read-only", "read_only": "read-only",
                                "workspace_write": "workspace-write", "full_access": "danger-full-access"})

    async def test_an_escalation_is_refused_below_full_access_and_never_asked_at_full_access(self):
        # Catches Beanest approving a wider sandbox the user never granted, or being stricter than dsh at Full Access.
        self.scenario("escalate")
        refused = await self.call("workspace_write")
        self.assertEqual(traces(self.home)[-1]["permission_answer"], "reject-once")
        self.assertFalse((self.work / "made-by-dsh.txt").exists())
        self.assertEqual(refused["status"], "completed", "a refused escalation is dsh's tool result, not a stop")
        allowed = await self.call("full_access")
        self.assertNotIn("permission_answer", traces(self.home)[-1], "danger-full-access never asks")
        self.assertTrue((self.work / "made-by-dsh.txt").exists())
        self.assertEqual(allowed["native_execution"]["tool_calls"], 1)

    async def test_typed_failures_become_handoff_rate_limit_and_key_codes(self):
        # Catches 402 not handing work over, 429 read as a hard stop, or a refused key read as a quota.
        for case, code, reason, status in (("quota", "subscription_limit_reached", "provider_quota_exhausted", 402),
                                           ("ratelimit", "provider_rate_limited", "provider_rate_limited", 429),
                                           ("auth", "api_key_rejected", "provider_auth_error", 401)):
            self.scenario(case)
            result = await self.call()
            self.assertEqual((result["status"], result["failure_code"]), ("failed", code), case)
            self.assertEqual(result["native_execution"]["failure_diagnostic"],
                             {"phase": "prompt", "reason": reason, "rpc_method": "session/prompt", "rpc_code": -32603,
                              "http_status": status, "prompt_sent": True}, case)
            self.assertNotIn("Insufficient", json.dumps(result), "provider text never leaves the transport")

    async def test_without_a_readable_key_nothing_starts(self):
        # Catches dsh being launched keyless, where it would fall back to the user's own stored credentials.
        self.key_file.write_text("OTHER=1\n")
        with self.assertRaises(adapters.ApiKeyUnavailable) as caught:
            await self.call()
        self.assertTrue(caught.exception.native_execution["never_started"])
        with self.assertRaises(adapters.ApiKeyUnavailable):
            await self.call(endpoint=False)
        self.assertEqual(traces(self.home), [], "no dsh process ran")

    async def test_a_subagents_tokens_are_counted_and_the_reply_is_the_last_message(self):
        # Catches a subagent's spend escaping the cap, or two root messages glued into an unparseable reply.
        self.scenario("subagent", answer='{"ok": true}')
        result = await self.call()
        self.assertEqual(result["text"], '{"ok": true}')
        self.assertEqual(result["api_usage"]["by_model"]["deepseek-flash"],
                         {"input_tokens": 1200 + 1500 + 300, "output_tokens": 40 + 7 + 3,
                          "cache_read_input_tokens": 1800, "cache_creation_input_tokens": 0})

    async def test_a_resumed_session_counts_only_this_calls_tokens(self):
        # Catches the whole session's history being billed again on every resumed call.
        self.scenario()
        first = await self.call()
        second = await self.call(session_id=first["session_id"])
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertIn("session/resume", traces(self.home)[-1]["methods"])
        self.assertEqual(second["api_usage"]["by_model"]["deepseek-flash"]["input_tokens"], 1500)

    async def test_a_model_dsh_does_not_offer_fails_before_any_prompt(self):
        # Catches a model outside dsh's catalog being sent anyway or billed at a guessed price.
        self.scenario()
        result = await self.call(runtime_settings={"model": "deepseek-reasoner"})
        self.assertEqual(result["failure_code"], "runtime_model_unavailable")
        self.assertNotIn("session/prompt", traces(self.home)[-1]["methods"])
        self.assertFalse(result["native_execution"]["failure_diagnostic"]["prompt_sent"])

    async def test_a_key_the_model_prints_is_scrubbed_from_text_events_and_receipts(self):
        # Catches the key reaching the room through the reply or a tool's output.
        self.scenario("leak", answer="your key is KEY")
        result = await self.call()
        self.assertNotIn(FAKE_KEY, json.dumps(result))
        self.assertIn(adapters.REDACTED, result["text"])
        for path in (self.root / "receipts").iterdir():
            self.assertNotIn(FAKE_KEY, path.read_text())

    async def test_deleting_a_room_session_removes_it_and_its_subagents_from_the_partners_home(self):
        # Catches a purge that leaves child sessions, or one that touches a session another cwd owns.
        self.scenario("subagent")
        result = await self.call()
        target = {"session_id": result["session_id"], "account_home": str(self.home), "shared_room_ids": [],
                  "owned_cwds": [str(self.root / "elsewhere")]}
        self.assertEqual(dsh_acp.delete_owned_session(target), "pending")
        self.assertEqual(len(dsh_acp.session_logs(self.home)), 2)
        target["owned_cwds"] = [str(self.work)]
        self.assertEqual(dsh_acp.delete_owned_session(target), "provider_managed_deleted")
        self.assertEqual(dsh_acp.session_logs(self.home), {})
        self.assertEqual(dsh_acp.delete_owned_session(target), "already_missing")


class Manager(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.now = SATURDAY
        self.key_file = self.root / "keys.env"
        write_key(self.key_file)
        self.manager = PartnerManager(self.root / "partners", default_registry(), clock=lambda: self.now)
        self.manager.add(id="ds", name="DeepSeek", adapter="dsh",
                         api_model=settings(self.key_file, caps={"daily": 1.0, "monthly": None}))

    def spend(self, input_tokens, model="deepseek-v4-pro"):
        return self.manager.record_api_spend("ds", {model: {"input_tokens": input_tokens, "output_tokens": 0}})

    def test_the_card_shows_spend_against_the_cap_and_the_cap_closes_dispatch(self):
        # Catches a cap that is shown but never blocks, or blocks with a reason the room does not treat as a quota wait.
        profile = self.manager.get("ds")
        self.assertEqual((profile["spend"]["day_spent"], profile["spend"]["caps"]["daily"]), (0.0, 1.0))
        self.assertEqual((profile["adapter"], profile["api_model"]["host_name"]), ("dsh", "DeepSeek Harness"))
        self.assertTrue(profile["selectable"])
        self.spend(1_000_000)  # 0.66
        self.assertEqual(self.manager.get("ds")["availability"]["state"], "available")
        self.spend(1_000_000)  # 1.32
        profile = self.manager.get("ds")
        self.assertEqual((profile["availability"]["state"], profile["availability"]["reason"], profile["selectable"]),
                         ("blocked", "spend_cap_reached", False))
        self.assertTrue(profile["configuration_selectable"], "a capped partner stays in the catalog")
        self.now += 86400
        self.assertEqual(self.manager.get("ds")["availability"]["state"], "available", "the next day the cap lifts")

    def test_each_model_is_priced_by_its_own_row(self):
        # Catches a Pro subagent billed at the Flash price, or an unlisted model billed as free.
        self.manager.record_api_spend("ds", {"deepseek-flash": {"output_tokens": 1_000_000},
                                             "deepseek-v4-pro": {"output_tokens": 1_000_000},
                                             None: {"output_tokens": 1_000_000}})
        self.assertAlmostEqual(self.manager.get("ds")["spend"]["day_spent"], 0.6 + 1.98 + 0.6)
        self.manager.record_api_spend("ds", {})
        self.assertEqual(self.manager.get("ds")["spend"]["month_unpriced_calls"], 1)

    def test_near_the_cap_it_is_held_back_like_a_low_subscription(self):
        # Catches the reserve flag never reaching scheduling for a spending cap.
        self.assertFalse(self.manager.workload_profiles()["ds"][2])
        self.spend(1_300_000)  # 0.858: 14% left
        self.assertTrue(self.manager.workload_profiles()["ds"][2])
        self.assertEqual(self.manager.get("ds")["quota_reserve"], 14)
        self.assertEqual(self.manager.workload_profiles()["ds"][0], "api:ds", "its own account")

    def test_a_refused_key_or_an_empty_balance_holds_it_until_its_check_passes(self):
        # Catches a dead key retried on every task, or a fault that never clears after the user fixes it.
        for fault in ("api_key_rejected", "api_balance_exhausted", "api_key_unavailable"):
            self.manager.record_api_fault("ds", fault)
            profile = self.manager.get("ds")
            self.assertEqual((profile["availability"]["state"], profile["availability"]["reason"], profile["selectable"]),
                             ("blocked", fault, False), fault)
            stub = self.root / "dsh-version"
            stub.write_text(f"#!{sys.executable}\nprint('0.2.1-rc.2')\n")
            stub.chmod(0o700)
            with patch.object(adapters, "find_dsh", return_value=str(stub)):
                health = asyncio.run(self.manager.check("ds"))["health"]
            self.assertEqual((health["state"], health["version"]), ("ready", "0.2.1-rc.2"))
            self.assertEqual(self.manager.get("ds")["availability"]["state"], "available")

    def test_check_reads_the_key_without_calling_anything(self):
        # Catches a check that passes with no readable key.
        stub = self.root / "dsh-version"
        stub.write_text(f"#!{sys.executable}\nprint('0.2.1-rc.2')\n")
        stub.chmod(0o700)
        self.key_file.write_text("")
        with patch.object(adapters, "find_dsh", return_value=str(stub)):
            health = asyncio.run(self.manager.check("ds"))["health"]
        self.assertEqual((health["state"], health["reason"]), ("unavailable", "api_key_unavailable"))
        self.assertEqual(self.manager.get("ds")["availability"]["reason"], "api_key_unavailable")

    def test_dsh_joins_only_with_a_key_and_caps(self):
        # Catches a keyless dsh partner that would run on whatever dsh finds on its own.
        with self.assertRaisesRegex(PartnerError, "api_model_required"):
            self.manager.add(id="plain", name="dsh", adapter="dsh")
        listed = {item["id"]: item for item in self.manager.supported_adapters()}
        self.assertTrue(listed["dsh"]["pay_per_use"])
        self.assertFalse(listed["dsh"]["setup"]["login_supported"])
        self.assertFalse(listed["dsh"]["setup"]["install_supported"])

    def test_its_home_is_its_own_and_no_subscription_check_runs(self):
        # Catches the partner sharing ~/.dsh, or account reads aimed at an API partner.
        self.assertEqual(self.manager.account_home("ds"), self.root / "partners" / "homes" / "ds")
        self.assertEqual(self.manager.limit_probe_targets(), {})
        with self.assertRaises(PartnerError):
            self.manager.set_account_source("ds", "local")
        with self.assertRaises(PartnerError):
            asyncio.run(self.manager.refresh_account("ds"))

    def test_the_currency_cannot_change_under_its_ledger_but_prices_can(self):
        # Catches a ledger silently mixing currencies, or the user's own prices being uneditable.
        current = {key: value for key, value in self.manager.get("ds")["api_model"].items() if key != "host_name"}
        with self.assertRaisesRegex(PartnerError, "partner_identity_conflict"):
            self.manager.set_api_model("ds", {**current, "currency": "CNY"})
        repriced = {**current, "caps": {"daily": 9.0, "monthly": 90.0},
                    "models": [{**current["models"][0], "input": 1.0}, *current["models"][1:]]}
        updated = self.manager.set_api_model("ds", repriced)
        self.assertEqual((updated["api_model"]["caps"]["daily"], updated["api_model"]["models"][0]["input"]), (9.0, 1.0))

    def test_an_unreadable_ledger_keeps_the_partner_closed(self):
        # Catches a corrupt spend file silently resetting spend to zero and reopening a capped partner.
        (self.root / "partners" / "api-spend.json").write_text("{not json")
        profile = self.manager.get("ds")
        self.assertEqual((profile["availability"]["state"], profile["selectable"]), ("blocked", False))


def plan_four(agent, context, cwd):
    """The plan author proposes four independent tasks and claims them; the room spreads them."""
    phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
    if phase in {"plan", "coordinate"}:
        commands = []
        if not board["tasks"] and agent == board["plan_author"]:
            for task_id in ("T1", "T2", "T3", "T4"):
                commands += [proposal(task_id, paths=[f"{task_id}.py"], capability="python"), {"type": "claim", "task_id": task_id}]
        if board["tasks"] or commands:
            commands.append({"type": "plan_ready"})
        return native_reply(context, commands)
    if phase == "work":
        (cwd / f"{task['id']}.py").write_text("VALUE = 1\n")
        return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Written."}])
    if phase == "review":
        return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact tree."}])
    if phase == "checkpoint":
        return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Done.", "next_goal": None,
                                       "scope_reference": context["objective"]["scope_reference"], "question": None}])
    if phase == "checkpoint_review":
        return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Compared.",
                                       "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
    raise AssertionError(phase)


class RoomWithDshPartner(unittest.IsolatedAsyncioTestCase):
    """A native room: two subscription partners on the in-process fake, the dsh partner on the real
    transport and the captured-protocol fake process, through real storage and the job loop."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root / "user")}))
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("VALUE = 1\n")
        self.key_file = self.root / "keys.env"
        write_key(self.key_file)
        self.enterContext(patch.object(adapters, "find_dsh", return_value=fake_dsh_executable(self.root)))
        self.enterContext(patch.object(room_service, "_RATE_LIMIT_RETRY_DELAY_SECONDS", 0))
        self.account_checks = []

        async def no_account_reads(agent, cwd, **options):
            self.account_checks.append((agent, str(adapters.bound_account_home(agent))))
            raise adapters.AccountTelemetryError("account_check_failed")
        self.enterContext(patch.object(adapters, "account_status", side_effect=no_account_reads))
        self.partners = PartnerManager(self.root / "partners", default_registry(), clock=lambda: SATURDAY)
        # Each fake dsh call reports 1500 uncached input, 7 output and 1000 cached tokens of deepseek-flash.
        self.partners.add(id="ds", name="DeepSeek", adapter="dsh",
                          api_model=settings(self.key_file, caps={"daily": 1.0, "monthly": None}))
        self.home = self.root / "partners" / "homes" / "ds"
        self.case = "text"
        self.fake = native.NativeTransport()
        self.fake.handler = plan_four
        self.calls = []

        async def transport(agent, prompt, cwd, session_id=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            self.calls.append({"caller": context["self"], "phase": context["job"]["job_phase"], "agent": agent,
                               "home": str(adapters.bound_account_home(agent))})
            if agent != "dsh":
                return await self.fake(agent, prompt, cwd, session_id=session_id, **options)
            # dsh itself runs: the reply is what the plan asks for, and a work call writes its file through dsh's tool.
            with tempfile.TemporaryDirectory() as scratch:
                reply = plan_four(agent, context, Path(scratch))
            scenario = {"case": self.case, "answer": json.dumps(reply)}
            if context["job"]["job_phase"] == "work" and self.case == "text":
                scenario.update(case="tool", write={"path": f"{context['task']['id']}.py", "text": "VALUE = 1\n"})
            (self.home / "fake-scenario.json").write_text(json.dumps(scenario))
            return await native_adapters.invoke_native(agent, prompt, cwd, session_id=session_id, **options)
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", partners=self.partners,
                                   native_invoke=transport)
        self.registration = await self.service.register_project("Example", str(self.source), selection_kind="folder")

    async def asyncTearDown(self):
        await self.service.shutdown()
        await asyncio.sleep(0)

    async def settle(self):
        async def wait():
            for _ in range(400):
                tasks = [task for task in self.service._workers.values() if not task.done()]
                if not tasks:
                    return
                for result in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
            self.fail("scheduler did not settle")
        await within(wait(), 120)
        self.assertIsNone(self.service._background_error)

    async def run_room(self, participants=("claude", "codex", "ds"), author="claude"):
        created = await self.service.create_room(
            "Write four small modules.", author, str(uuid4()), mode="native_project",
            project_id=self.registration["project"]["id"], participants=list(participants), work_mode="build",
            runtime_settings={"permission": "full_access", "members": {}}, execution_policy={"mode": "autonomous"})
        await self.settle()
        return await self.service.read_room(created["room"]["id"])

    def dsh_calls(self):
        return [call for call in self.calls if call["caller"] == "ds"]

    async def test_it_works_in_a_room_and_every_call_is_priced_into_its_ledger(self):
        # Catches calls going unbilled, billed at the wrong price, or the partner not doing real work.
        state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual({task["status"] for task in state["team"]["tasks"]}, {"accepted"})
        launched = traces(self.home)
        self.assertTrue(launched, "dsh ran")
        self.assertEqual({trace["permission_mode"] for trace in launched}, {"danger-full-access"}, "Full Access is dsh's highest")
        listed = next(item for item in (await self.service.list_partners())["partners"] if item["id"] == "ds")
        # 1500 x 0.15 + 7 x 0.6 + 1000 x 0.003 per 1M, per model call; work calls make two.
        per_reply = (1500 * 0.15 + 7 * 0.6 + 1000 * 0.003) / 1_000_000
        per_tool_step = (1200 * 0.15 + 40 * 0.6 + 800 * 0.003) / 1_000_000
        self.assertEqual(listed["spend"]["month_calls"], len(launched))
        self.assertEqual(listed["spend"]["month_unpriced_calls"], 0)
        works = sum(1 for call in self.dsh_calls() if call["phase"] == "work")
        self.assertAlmostEqual(listed["spend"]["day_spent"], round(len(launched) * per_reply + works * per_tool_step, 6), places=6)
        self.assertTrue(any(task["owner"] == "ds" and task["status"] == "accepted" for task in state["team"]["tasks"]),
                        "dsh finished a task of its own")
        self.assertTrue(any("session/resume" in trace["methods"] for trace in launched), "later calls resume its session")
        self.assertEqual({call["home"] for call in self.dsh_calls()}, {str(self.home)})
        self.assertNotIn(str(self.home), [entry[1] for entry in self.account_checks])

    async def test_an_empty_balance_hands_its_task_to_the_others_and_holds_the_partner(self):
        # Catches a 402 stopping the room, or the partner getting new work while its balance is empty.
        self.case = "quota"
        state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(len(self.dsh_calls()), 1, "no call after the balance ran out")
        listed = next(item for item in (await self.service.list_partners())["partners"] if item["id"] == "ds")
        self.assertEqual((listed["availability"]["state"], listed["availability"]["reason"]), ("blocked", "api_balance_exhausted"))
        self.assertEqual(listed["spend"]["month_calls"], 1, "the refused call still counts as a call")

    async def test_a_refused_key_hands_its_task_over_instead_of_stopping_the_room(self):
        # Catches a 401 ending the objective, or the dead key being tried again.
        self.case = "auth"
        state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(len(self.dsh_calls()), 1)
        listed = next(item for item in (await self.service.list_partners())["partners"] if item["id"] == "ds")
        self.assertEqual((listed["availability"]["reason"], listed["health"]["reason"]), ("api_key_rejected", "api_key_rejected"))

    async def test_a_rate_limit_is_retried_once_then_the_task_goes_to_the_others(self):
        # Catches a 429 stopping the whole objective, or the refused call being retried forever.
        self.case = "ratelimit"
        state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(len(traces(self.home)), 2, "one more try after the pause, then no more")
        listed = next(item for item in (await self.service.list_partners())["partners"] if item["id"] == "ds")
        self.assertEqual(listed["availability"]["state"], "available", "a rate limit is not a fault of the partner")
        self.assertEqual(listed["spend"]["month_calls"], 2)

    async def test_a_passing_rate_limit_costs_one_retry_and_nothing_else(self):
        # Catches a one-off 429 handing the partner's work away instead of trying again.
        self.case = "ratelimit_once"
        state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(self.dsh_calls()[:2], [self.dsh_calls()[0]] * 2, "the same plan call, made again")
        self.assertGreater(len(self.dsh_calls()), 2, "it kept working after the retry")

    async def test_an_unreadable_key_never_launches_dsh_and_the_others_carry_on(self):
        # Catches dsh launched keyless, or the room stopping because one partner's key file moved.
        self.key_file.write_text("OTHER=1\n")
        state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(traces(self.home), [])
        listed = next(item for item in (await self.service.list_partners())["partners"] if item["id"] == "ds")
        self.assertEqual(listed["availability"]["reason"], "api_key_unavailable")

    async def test_the_key_never_reaches_storage_events_prompts_or_responses(self):
        # Catches the key value being copied anywhere Beanest writes or returns, dsh's own files included.
        self.case = "text"
        state = await self.run_room()
        await self.partners.check("ds")
        responses = [json.dumps(state), json.dumps(await self.service.list_partners()),
                     json.dumps(await self.service.runtime_options()), json.dumps(self.fake.calls, default=str)]
        for text in responses:
            self.assertNotIn(FAKE_KEY, text)
        scanned = 0
        for path in self.root.rglob("*"):
            if path.is_file() and path != self.key_file:
                scanned += 1
                self.assertNotIn(FAKE_KEY.encode(), path.read_bytes(), path)
        self.assertGreater(scanned, 20)
        stored = json.loads((self.root / "partners" / "profiles.json").read_text())
        reference = next(item for item in stored["profiles"] if item["id"] == "ds")["api_model"]["key"]
        self.assertEqual(reference, {"kind": "env_file", "path": str(self.key_file), "variable": "DEEPSEEK_API_KEY"})
        self.assertTrue(any(trace["key_sha256"] == hashlib.sha256(FAKE_KEY.encode()).hexdigest() for trace in traces(self.home)),
                        "the key did reach dsh, so the scan above had something to miss")


if __name__ == "__main__":
    unittest.main()
