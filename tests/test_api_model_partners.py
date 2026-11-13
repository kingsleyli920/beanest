"""API model partners: a host CLI on the user's own endpoint, key, prices and caps.

Real storage, real subprocesses for the launch, and a synthetic native transport
for rooms. No model is called and no real key exists: the key below is made up.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, api_models, native_adapters
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.room_service import RoomService
from tests import test_native_room_service as native
from tests.test_native_room_service import native_reply
from tests.test_team_store import proposal
from wait_support import within

FAKE_KEY = "sk-beanest-test-7f3a91c2e8d04b6a"
SESSION = "42619ac2-4bfa-446c-a773-5193109566ae"
# Saturday 2027-01-16 08:00 UTC: off-peak in the preset's weekday peak hours.
SATURDAY = datetime(2027, 1, 16, 8, 0, tzinfo=timezone.utc).timestamp()
# Friday 2027-01-15 08:00 UTC: inside the preset's 06:00-10:00 peak window.
FRIDAY_PEAK = datetime(2027, 1, 15, 8, 0, tzinfo=timezone.utc).timestamp()
PROVIDER_VARIABLE = ("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_OAUTH")


def provider_variables(environment):
    """Every variable that could pick a provider account, key or route."""
    return {key: value for key, value in environment.items()
            if key.startswith(PROVIDER_VARIABLE) or key.endswith("_BASE_URL") or key.endswith("_API_KEY")}


def settings(key_path, **overrides):
    preset = api_models.presets()[0]
    value = {field: preset[field] for field in ("host", "base_url", "currency", "timezone", "peak_windows",
                                                "default_model", "models", "caps")}
    value["key"] = {"kind": "env_file", "path": str(key_path), "variable": "DEEPSEEK_API_KEY"}
    value.update(overrides)
    return value


def write_key(path, value=FAKE_KEY):
    path.write_text(f"# local secrets\nOTHER=1\nexport DEEPSEEK_API_KEY=\"{value}\"\n")
    path.chmod(0o600)


class Validation(unittest.TestCase):
    def test_base_url_must_be_https_with_no_trailing_slash_or_v1(self):
        # Catches a base URL the host CLI would append /v1/messages to twice, or one sent in clear text.
        self.assertEqual(api_models.base_url("https://api.example.test/anthropic"), "https://api.example.test/anthropic")
        for bad in ("http://api.example.test/anthropic", "https://api.example.test/anthropic/",
                    "https://api.example.test/anthropic/v1", "https://api.example.test/V1", "https://user:pw@api.example.test",
                    "https://api.example.test/a?x=1", "https://api.example.test/a#f", "https:///anthropic", "", None, 3):
            with self.assertRaises(api_models.ApiModelError, msg=repr(bad)):
                api_models.base_url(bad)

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


class Launch(unittest.IsolatedAsyncioTestCase):
    """What a child process receives, through the real binding and real subprocesses."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # The user's shell has its own keys and routes; none of them may reach any child.
        self.enterContext(patch.dict(os.environ, {"ANTHROPIC_API_KEY": "shell-anthropic", "OPENAI_API_KEY": "shell-openai",
                                                  "ANTHROPIC_BASE_URL": "https://shell.example.test",
                                                  "ANTHROPIC_AUTH_TOKEN": "shell-token", "OPENAI_BASE_URL": "https://shell-openai.example.test",
                                                  "CLAUDE_CONFIG_DIR": "/user/shell/claude", "HOME": str(self.root / "user")}))
        self.key_file = self.root / "keys.env"
        write_key(self.key_file)
        self.manager = PartnerManager(self.root / "partners", default_registry(), clock=lambda: SATURDAY)
        self.manager.add(id="ds", name="DeepSeek", adapter="claude", api_model=settings(self.key_file))
        self.manager.add(id="bean", name="Claude spare", adapter="claude")
        self.manager.set_account_source("bean", "profile_home")
        self.home = self.root / "partners" / "homes" / "ds"

    def bound(self, partner="ds"):
        return adapters.account_home("claude", self.manager.account_home(partner), self.manager.api_endpoint(partner))

    def test_an_api_partner_gets_exactly_its_two_variables_and_its_own_home(self):
        # Catches a missing or extra provider variable, the shell's key leaking through, or the user's own Claude home.
        with self.bound():
            environment = native_adapters.launch_environment("claude")
        self.assertEqual(provider_variables(environment), {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                                                          "ANTHROPIC_AUTH_TOKEN": FAKE_KEY})
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.home))
        self.assertTrue(self.home.is_dir())
        self.assertNotEqual(self.manager.account_home("ds"), Path.home() / ".claude")

    def test_every_other_partner_still_gets_none_of_them(self):
        # Catches the endpoint leaking to a subscription partner, or the stripping being loosened for everyone.
        for agent, home in (("claude", None), ("claude", self.manager.account_home("bean")), ("codex", None), ("kimi", None)):
            with adapters.account_home(agent, home):
                for environment in (native_adapters.launch_environment(agent), adapters._environment()):
                    self.assertEqual(provider_variables(environment), {}, (agent, home))
        self.assertIsNone(self.manager.api_endpoint("claude"))
        self.assertIsNone(self.manager.api_endpoint("bean"))

    def test_helper_children_never_receive_the_key(self):
        # Catches the key reaching process listings and other children Beanest starts while the call runs.
        with self.bound():
            self.assertEqual(provider_variables(adapters._environment()), {})

    def test_the_command_names_the_partner_model_and_maps_the_tool_aliases_to_it(self):
        # Catches a turn falling back to the host's own default model name, which the endpoint may refuse or price differently.
        with self.bound(), patch.object(adapters, "find_claude", return_value="/claude"):
            default = native_adapters.native_command("claude", permission="full_access")
            chosen = native_adapters.native_command("claude", permission="full_access",
                                                    runtime_settings={"model": "deepseek-flash", "effort": None})
            legacy = adapters._command("claude", None)
        self.assertEqual(default[default.index("--model") + 1], "deepseek-v4-pro")
        self.assertEqual(chosen[chosen.index("--model") + 1], "deepseek-flash")
        self.assertEqual(legacy[legacy.index("--model") + 1], "deepseek-v4-pro")
        aliases = json.loads(chosen[chosen.index("--settings") + 1])["env"]
        self.assertEqual({aliases[name] for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                                                     "CLAUDE_CODE_SUBAGENT_MODEL")}, {"deepseek-flash"})
        with patch.object(adapters, "find_claude", return_value="/claude"):
            plain = native_adapters.native_command("claude", permission="full_access")
        self.assertNotIn("--model", plain)
        self.assertNotIn("ANTHROPIC_MODEL", plain[plain.index("--settings") + 1])

    def stub(self, *, echo_key=True):
        record, stub = self.root / "stub-record.json", self.root / "claude-stub"
        events = [{"type": "system", "subtype": "init", "session_id": SESSION},
                  {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Bash",
                                                                 "input": {"command": "env"}}]}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a",
                                                            "content": "ANTHROPIC_AUTH_TOKEN=__KEY__", "is_error": False}]}},
                  {"type": "result", "subtype": "success", "session_id": SESSION, "result": "key is __KEY__",
                   "structured_output": {"message": "saw __KEY__", "action": "finish"},
                   "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 50}}]
        stub.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
names = [key for key in os.environ if key.startswith(("ANTHROPIC_", "OPENAI_")) or key.endswith(("_BASE_URL", "_API_KEY"))]
Path({str(record)!r}).write_text(json.dumps({{"argv": sys.argv[1:], "names": sorted(names),
    "home": os.environ.get("CLAUDE_CONFIG_DIR"), "base": os.environ.get("ANTHROPIC_BASE_URL"),
    "token_matches": os.environ.get("ANTHROPIC_AUTH_TOKEN") == {FAKE_KEY!r}}}))
if "status" in sys.argv:
    sys.exit(3)
key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "") if {echo_key!r} else ""
for event in {events!r}:
    print(json.dumps(event).replace("__KEY__", key), flush=True)
''')
        stub.chmod(0o700)
        return record, patch.object(adapters, "find_claude", return_value=str(stub))

    async def test_a_real_turn_skips_the_subscription_check_and_never_lets_the_key_out(self):
        # Catches a subscription auth check refusing the turn, and a key printed by a tool reaching the result or the receipt.
        record, found = self.stub()
        schema = {"type": "object", "properties": {"message": {"type": "string"}, "action": {"type": "string"}},
                  "required": ["message", "action"], "additionalProperties": False}
        progress = []
        with found, self.bound():
            try:
                result = await native_adapters.invoke_native("claude", "hello", self.root, permission="full_access",
                                                             response_schema=schema, receipt_dir=self.root / "receipts",
                                                             on_activity=progress.append)
            except RuntimeError as error:
                self.fail(f"the turn was refused before it ran: {error}")
        seen = json.loads(record.read_text())
        self.assertNotIn("status", seen["argv"], "no subscription auth check runs for an API partner")
        self.assertEqual(seen["names"], ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])
        self.assertEqual((seen["home"], seen["base"], seen["token_matches"]), (str(self.home), "https://api.deepseek.com/anthropic", True))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["usage"]["input_tokens"], 1000)
        self.assertEqual(result["native_execution"]["auth_method"], "api_key")
        receipt = Path(result["native_execution"]["receipt_path"]).read_text()
        for text in (json.dumps(result), json.dumps(progress), receipt):
            self.assertNotIn(FAKE_KEY, text)
        self.assertIn("[REDACTED]", receipt, "the tool output was kept, without the key")

    async def test_the_legacy_text_transport_takes_the_same_route(self):
        # Catches older room modes still demanding a subscription login or leaking the key in their text.
        record, found = self.stub()
        with found, self.bound():
            result = await adapters.invoke("claude", "hello", self.root, timeout=30)
        seen = json.loads(record.read_text())
        self.assertNotIn("status", seen["argv"])
        self.assertEqual(seen["names"], ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])
        self.assertNotIn(FAKE_KEY, json.dumps(result))

    async def test_a_missing_key_stops_before_anything_starts(self):
        # Catches a turn launched without its key (which the host would then send somewhere else) or a vague failure.
        record, found = self.stub()
        self.key_file.write_text("OTHER=1\n")
        with found, self.bound(), self.assertRaises(adapters.ApiKeyUnavailable) as caught:
            await native_adapters.invoke_native("claude", "hello", self.root, permission="full_access")
        self.assertTrue(caught.exception.native_execution["never_started"])
        self.assertFalse(record.exists(), "the host CLI never ran")
        self.assertNotIn(str(self.key_file), str(caught.exception))


class Manager(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.now = SATURDAY
        self.key_file = self.root / "keys.env"
        write_key(self.key_file)
        self.manager = PartnerManager(self.root / "partners", default_registry(), clock=lambda: self.now)
        self.manager.add(id="ds", name="DeepSeek", adapter="claude",
                         api_model=settings(self.key_file, caps={"daily": 1.0, "monthly": None}))

    def spend(self, input_tokens):
        return self.manager.record_api_spend("ds", {"values": {"input_tokens": input_tokens, "output_tokens": 0}})

    def test_the_card_shows_spend_against_the_cap_and_the_cap_closes_dispatch(self):
        # Catches a cap that is shown but never blocks, or blocks with a reason the room does not treat as a quota wait.
        profile = self.manager.get("ds")
        self.assertEqual((profile["spend"]["day_spent"], profile["spend"]["caps"]["daily"]), (0.0, 1.0))
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

    def test_near_the_cap_it_is_held_back_like_a_low_subscription(self):
        # Catches the reserve flag never reaching scheduling for a spending cap.
        self.assertFalse(self.manager.workload_profiles()["ds"][2])
        self.spend(1_300_000)  # 0.858: 14% left
        self.assertTrue(self.manager.workload_profiles()["ds"][2])
        self.assertEqual(self.manager.get("ds")["quota_reserve"], 14)
        self.assertEqual(self.manager.workload_profiles()["ds"][0], "api:ds", "its own account, not the local Claude login")

    def test_no_subscription_check_ever_runs_for_it(self):
        # Catches `claude auth status` or the usage SDK being run against the endpoint's home.
        homes = []

        async def account_status(agent, cwd, **options):
            homes.append(adapters.bound_account_home(agent))
            raise adapters.AccountTelemetryError("account_check_failed")

        async def run():
            with patch.object(adapters, "account_status", side_effect=account_status):
                try:
                    await self.manager.refresh_availability(["ds"])
                    await self.manager.refresh_availability()
                except PartnerError as error:
                    self.fail(f"an availability refresh tried the API partner's account: {error.code}")
                with self.assertRaises(PartnerError):
                    await self.manager.refresh_account("ds")
        with patch.object(adapters, "find_claude", side_effect=RuntimeError("missing")):
            asyncio.run(run())
        self.assertEqual(len(homes), 2, "only the two subscription partners were checked")
        self.assertNotIn(self.manager.account_home("ds"), homes)
        self.assertEqual(self.manager.limit_probe_targets(), {})
        with self.assertRaises(PartnerError):
            self.manager.set_account_source("ds", "local")

    def test_the_endpoint_cannot_be_swapped_under_existing_sessions_but_prices_can_change(self):
        # Catches rooms resuming sessions against a different endpoint, or the user's own prices being uneditable.
        current = self.manager.get("ds")["api_model"]
        editable = {key: value for key, value in current.items() if key != "host_name"}
        with self.assertRaisesRegex(PartnerError, "partner_identity_conflict"):
            self.manager.set_api_model("ds", {**editable, "base_url": "https://other.example.test/anthropic"})
        repriced = {**editable, "caps": {"daily": 9.0, "monthly": 90.0},
                    "models": [{**editable["models"][0], "input": 1.0}, editable["models"][1]]}
        updated = self.manager.set_api_model("ds", repriced)
        self.assertEqual((updated["api_model"]["caps"]["daily"], updated["api_model"]["models"][0]["input"]), (9.0, 1.0))
        self.assertEqual(PartnerManager(self.root / "partners", default_registry(), clock=lambda: self.now)
                         .get("ds")["api_model"]["caps"]["monthly"], 90.0)

    def test_check_reads_the_key_without_calling_the_endpoint(self):
        # Catches a check that passes with no readable key, or one that sends a real request.
        stub = self.root / "claude-version"
        stub.write_text(f"#!{sys.executable}\nimport sys\nprint('2.1.0 (Claude Code)')\n")
        stub.chmod(0o700)
        with patch.object(adapters, "find_claude", return_value=str(stub)):
            health = asyncio.run(self.manager.check("ds"))["health"]
            self.assertEqual((health["state"], health["auth"]["method"], health["version"]), ("ready", "api_key", "2.1.0"))
            self.key_file.write_text("")
            health = asyncio.run(self.manager.check("ds"))["health"]
        self.assertEqual((health["state"], health["reason"]), ("unavailable", "api_key_unavailable"))

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


class RoomWithApiPartner(unittest.IsolatedAsyncioTestCase):
    """A native room with two subscription partners and one API partner, through real storage and the job loop."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root / "user")}))
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("VALUE = 1\n")
        self.key_file = self.root / "keys.env"
        write_key(self.key_file)
        self.account_checks = []

        async def no_account_reads(agent, cwd, **options):
            # Subscription partners' account reads fail harmlessly; an API partner must never get here.
            self.account_checks.append((agent, str(adapters.bound_account_home(agent))))
            raise adapters.AccountTelemetryError("account_check_failed")
        self.enterContext(patch.object(adapters, "account_status", side_effect=no_account_reads))
        self.partners = PartnerManager(self.root / "partners", default_registry(), clock=lambda: SATURDAY)
        # 0.66 per API call below: the second call crosses the daily cap of 1.00.
        self.partners.add(id="ds", name="DeepSeek", adapter="claude",
                          api_model=settings(self.key_file, caps={"daily": 1.0, "monthly": None}))
        self.fake = native.NativeTransport()
        self.fake.handler = plan_four
        self.calls = []

        async def transport(agent, prompt, cwd, session_id=None, **options):
            caller = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])["self"]
            environment = native_adapters.launch_environment(agent)
            self.calls.append({"caller": caller, "phase": json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])["job"]["job_phase"],
                               "provider_variables": provider_variables(environment),
                               "home": environment.get("CLAUDE_CONFIG_DIR"), "model": (options.get("runtime_settings") or {}).get("model")})
            result = await self.fake(agent, prompt, cwd, session_id=session_id, **options)
            result["usage"] = {"input_tokens": 1_000_000, "output_tokens": 0}
            return result
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", partners=self.partners,
                                   native_invoke=transport)
        self.registration = await self.service.register_project("Example", str(self.source), selection_kind="folder")

    async def asyncTearDown(self):
        await self.service.shutdown()
        await asyncio.sleep(0)

    async def settle(self):
        async def wait():
            for _ in range(200):
                tasks = [task for task in self.service._workers.values() if not task.done()]
                if not tasks:
                    return
                for result in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
            self.fail("scheduler did not settle")
        await within(wait(), 20)
        self.assertIsNone(self.service._background_error)

    async def run_room(self):
        created = await self.service.create_room(
            "Write four small modules.", "claude", str(uuid4()), mode="native_project",
            project_id=self.registration["project"]["id"], participants=["claude", "codex", "ds"], work_mode="build",
            runtime_settings={"permission": "full_access", "members": {}}, execution_policy={"mode": "autonomous"})
        await self.settle()
        return await self.service.read_room(created["room"]["id"])

    async def test_crossing_the_cap_mid_run_stops_new_work_while_the_others_finish(self):
        # Catches a capped partner still being dispatched, the room stopping with it, or the card missing spend and cap.
        state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        api_calls = [index for index, call in enumerate(self.calls) if call["caller"] == "ds"]
        self.assertGreaterEqual(len(api_calls), 2, "the partner took part until the cap")
        self.assertEqual(len(api_calls), 2, "no call after the call that crossed the cap")
        others_after = [call for call in self.calls[api_calls[-1] + 1:] if call["caller"] != "ds"]
        self.assertTrue(others_after, "the other partners carried on")
        self.assertEqual({task["status"] for task in state["team"]["tasks"]}, {"accepted"})
        listed = next(item for item in (await self.service.list_partners())["partners"] if item["id"] == "ds")
        self.assertEqual((listed["spend"]["day_spent"], listed["spend"]["caps"]["daily"], listed["spend"]["capped"]),
                         (1.32, 1.0, ["daily"]))
        self.assertEqual((listed["availability"]["state"], listed["availability"]["reason"]), ("blocked", "spend_cap_reached"))

    async def test_only_its_calls_carry_the_endpoint_and_none_ran_an_account_check(self):
        # Catches the endpoint bound to the wrong partner's call, the default model missing, or a login check on the endpoint.
        await self.run_room()
        home = str(self.root / "partners" / "homes" / "ds")
        for call in self.calls:
            if call["caller"] == "ds":
                self.assertEqual(call["provider_variables"], {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                                                              "ANTHROPIC_AUTH_TOKEN": FAKE_KEY})
                self.assertEqual(call["home"], home)
            else:
                self.assertEqual(call["provider_variables"], {}, call["caller"])
                self.assertNotEqual(call["home"], home)
        self.assertTrue(any(call["caller"] == "ds" for call in self.calls))
        self.assertNotIn(home, [entry[1] for entry in self.account_checks])

    async def test_alone_and_capped_the_room_waits_instead_of_failing(self):
        # Catches a lone capped partner ending the objective as failed instead of waiting like any quota wait.
        self.partners.record_api_spend("ds", {"values": {"input_tokens": 2_000_000, "output_tokens": 0}})
        created = await self.service.create_room(
            "Write four small modules.", "ds", str(uuid4()), mode="native_project",
            project_id=self.registration["project"]["id"], participants=["ds", "codex"], work_mode="build",
            runtime_settings={"permission": "full_access", "members": {}}, execution_policy={"mode": "autonomous"})
        await self.settle()
        self.assertNotIn("ds", [call["caller"] for call in self.calls])
        state = await self.service.read_room(created["room"]["id"])
        self.assertEqual(state["objective"]["status"], "complete", "codex carried the room")

    async def test_the_key_never_reaches_storage_events_prompts_or_responses(self):
        # Catches the key value being copied anywhere Beanest writes or returns, not just where it was expected.
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
        self.assertGreater(scanned, 10)
        stored = json.loads((self.root / "partners" / "profiles.json").read_text())
        reference = next(item for item in stored["profiles"] if item["id"] == "ds")["api_model"]["key"]
        self.assertEqual(reference, {"kind": "env_file", "path": str(self.key_file), "variable": "DEEPSEEK_API_KEY"})


if __name__ == "__main__":
    unittest.main()
