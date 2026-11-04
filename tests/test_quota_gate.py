"""Quota evidence crosses private storage and dispatch; no native CLI is run."""
import asyncio
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from local_agent_coordinator import adapters
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.quota_gate import QuotaGate
from local_agent_coordinator.room_service import RoomError, RoomService
from tests import test_room_server as http
from tests.test_claude_usage import usage as claude_usage


def account(adapter="codex", *, email="person@example.test", used=20, pool="codex", resets=2000):
    return {"state": "ready", "email": email, "plan": "pro", "auth_method": "chatgpt" if adapter == "codex" else "claude.ai",
        "source": "codex_app_server" if adapter == "codex" else "claude_auth_status",
        "scope": "default_cli_login", "reason": None,
        "quota": {"state": "available" if used is not None else "unavailable", "windows":
            [{"name": pool + ":primary", "used_percent": used, "remaining_percent": 100 - used,
              "window_minutes": 10080, "resets_at": resets}] if used is not None else [],
            "source": "codex_app_server_account_rate_limits" if adapter == "codex" else "claude_auth_status",
            "scope": "provider_account", "reason": None if used is not None else "provider_usage_not_exposed"}}


class QuotaGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.now = 1000.0
        self.base = default_registry(invoke=AsyncMock(side_effect=AssertionError("No model calls")))
        self.manager = PartnerManager(self.root / "partners", self.base, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def observe(self, value, partner="codex"):
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=value)):
            return await self.manager.refresh_account(partner, force=True)

    async def test_only_fresh_positive_shared_codex_pool_is_available(self):
        self.assertEqual(self.manager.get("codex")["availability"]["state"], "unknown")
        value = await self.observe(account(pool="other-model", used=0))
        self.assertEqual(value["availability"]["state"], "unknown")
        value = await self.observe(account(used=30))
        self.assertEqual(value["availability"]["state"], "available")
        self.now += 91
        value = self.manager.get("codex")
        self.assertEqual(value["availability"]["state"], "unknown")
        self.assertTrue(value["availability"]["stale"])
        self.assertTrue(value["availability"]["selectable"])

    async def test_shared_exhaustion_blocks_aliases_without_changing_preferences_or_catalog_membership(self):
        self.manager.add(id="spare", name="Another session", adapter="codex")
        await self.observe(account(used=100))
        for identifier in ("codex", "spare"):
            value = self.manager.get(identifier)
            self.assertEqual(value["availability"]["state"], "blocked")
            self.assertFalse(value["selectable"])
            self.assertTrue(value["enabled"])
            self.assertIn(identifier, self.manager.available_ids())
        raw = (self.manager.root / "quota-gates.json").read_text()
        self.assertNotIn("person@example.test", raw)
        self.assertNotIn('"email"', raw)
        self.assertEqual(stat.S_IMODE((self.manager.root / "quota-gates.json").stat().st_mode), 0o600)

    async def test_other_model_pool_exhaustion_does_not_block_all_models(self):
        result = await self.observe(account(pool="special-model", used=100))
        self.assertEqual(result["availability"]["state"], "unknown")
        self.assertTrue(result["selectable"])

    def claude_account(self, rows=None):
        value = account("claude", used=None)
        raw = claude_usage()
        if rows is not None:
            raw["rate_limits"]["limits"] = rows
        value["quota"] = adapters._claude_quota(raw)
        return value

    async def test_claude_shared_positive_does_not_claim_the_exhausted_model_is_usable(self):
        rows = claude_usage()["rate_limits"]["limits"]
        rows[2]["is_active"] = False
        result = await self.observe(self.claude_account(rows), "claude")
        self.assertEqual(result["availability"]["state"], "available")
        self.assertEqual(result["availability"]["source"], "claude_shared_pool")
        self.assertEqual(result["account"]["quota"]["windows"][2]["remaining_percent"], 0)
        identity = self.manager.account_identity("claude")
        self.assertFalse(self.manager.quota_gate.record_limit("claude", result["account"], bound_identity=identity))
        self.assertEqual(self.manager.get("claude")["availability"]["state"], "available")
        model_only = [claude_usage()["rate_limits"]["limits"][2]]
        result = await self.observe(self.claude_account(model_only), "claude")
        self.assertEqual(result["availability"]["state"], "unknown")
        self.assertTrue(result["selectable"])

    async def test_claude_shared_exhaustion_is_persisted_across_aliases_with_its_reset(self):
        self.manager.add(id="claude-alias", name="Alias", adapter="claude")
        raw = claude_usage()["rate_limits"]["limits"]
        raw[0]["percent"] = 100
        value = self.claude_account(raw)
        result = await self.observe(value, "claude")
        identity = self.manager.account_identity("claude")
        self.assertTrue(self.manager.quota_gate.record_limit("claude", result["account"], bound_identity=identity))
        for identifier in ("claude", "claude-alias"):
            blocked = self.manager.get(identifier)["availability"]
            self.assertFalse(blocked["selectable"])
            self.assertEqual(blocked["source"], "claude_shared_pool")
            self.assertEqual(blocked["resets_at"], result["account"]["quota"]["windows"][0]["resets_at"])
        self.manager = PartnerManager(self.manager.root, self.base, clock=lambda: self.now)
        self.assertFalse(self.manager.get("claude-alias")["selectable"])
        recovered = await self.observe(self.claude_account(), "claude")
        self.assertEqual(recovered["availability"]["state"], "available")

    async def test_claude_shared_limit_blocks_even_when_not_the_server_headline(self):
        row = claude_usage()["rate_limits"]["limits"][0]
        row.update(percent=100, is_active=False)
        result = await self.observe(self.claude_account([row]), "claude")
        self.assertEqual(result["availability"]["state"], "blocked")
        self.assertFalse(result["selectable"])

    async def test_explicitly_scoped_claude_window_cannot_global_block(self):
        row = claude_usage()["rate_limits"]["limits"][0]
        row.update(percent=100, scope={"surface": "code"})
        result = await self.observe(self.claude_account([row]), "claude")
        self.assertEqual(result["availability"]["state"], "unknown")
        self.assertTrue(result["selectable"])

    async def test_persisted_limit_requires_matching_confirmed_identity_and_reset_is_not_recovery(self):
        await self.observe(account(used=100, resets=1100))
        self.manager = PartnerManager(self.manager.root, self.base, clock=lambda: self.now)
        pending = self.manager.get("codex")["availability"]
        self.assertEqual((pending["state"], pending["reason"]), ("blocked", "account_identity_unknown"))
        self.assertFalse(pending["selectable"])
        self.assertEqual((await self.observe(account(used=100, resets=1100)))["availability"]["state"], "blocked")
        self.now = 1101
        value = self.manager.get("codex")["availability"]
        self.assertEqual((value["state"], value["reason"]), ("unknown", "quota_reset_due"))
        self.assertTrue(value["selectable"])

    async def test_restart_identity_refresh_failure_keeps_known_gate_closed_until_confirmed_account_change(self):
        await self.observe(account(used=100))
        self.manager = PartnerManager(self.manager.root, self.base, clock=lambda: self.now)
        with patch.object(adapters, "account_status", new=AsyncMock(side_effect=RuntimeError("offline"))):
            value = await self.manager.refresh_account("codex", force=True)
        self.assertEqual((value["availability"]["state"], value["availability"]["selectable"]), ("blocked", False))
        self.assertEqual(value["availability"]["reason"], "account_identity_unknown")
        value = await self.observe(account(email="another@example.test", used=None))
        self.assertEqual(value["availability"]["state"], "unknown")
        self.assertTrue(value["availability"]["selectable"])

    async def test_refresh_failure_does_not_clear_a_known_block_and_confirmed_account_change_does(self):
        await self.observe(account(used=100))
        with patch.object(adapters, "account_status", new=AsyncMock(side_effect=RuntimeError("No connection"))):
            result = await self.manager.refresh_account("codex", force=True)
        self.assertEqual(result["availability"]["state"], "blocked")
        self.assertTrue(result["availability"]["stale"])
        result = await self.observe(account(email="new-person@example.test", used=None))
        self.assertEqual(result["availability"]["state"], "unknown")
        self.assertTrue(result["selectable"])
        self.assertEqual(self.manager.quota_gate.blocks, {})

    async def test_claude_limit_is_bound_to_confirmed_identity_not_login_as_quota(self):
        result = await self.observe(account("claude", used=None), "claude")
        self.assertEqual(result["availability"]["state"], "unknown")
        identity = self.manager.account_identity("claude")
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=account("claude", used=None))):
            await self.manager.record_subscription_limit("claude", identity)
        self.assertEqual(self.manager.get("claude")["availability"]["state"], "blocked")
        result = await self.observe(account("claude", used=None), "claude")
        self.assertEqual(result["availability"]["state"], "blocked", "auth status is not recovered quota evidence")

    async def test_old_limit_keeps_its_timestamp_while_fresh_identity_enables_explicit_recheck(self):
        await self.observe(account("claude", used=None), "claude")
        identity = self.manager.account_identity("claude")
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=account("claude", used=None))):
            await self.manager.record_subscription_limit("claude", identity)
        original = self.manager.get("claude")["availability"]
        self.now += 91
        expired_identity = self.manager.get("claude")["availability"]
        self.assertFalse(expired_identity["can_request_recheck"])
        fresh = (await self.observe(account("claude", used=None), "claude"))["availability"]
        self.assertTrue(fresh["can_request_recheck"])
        self.assertTrue(fresh["stale"], "the old limit observation must not be relabelled fresh quota")
        self.assertEqual(fresh["observed_at"], original["observed_at"])
        self.assertEqual(fresh["identity_checked_at"], self.now)
        self.assertEqual(fresh["state"], "blocked")
        self.now += 91
        self.assertFalse(self.manager.get("claude")["availability"]["can_request_recheck"])
        changed = (await self.observe(account("claude", email="changed@example.test", used=None), "claude"))["availability"]
        self.assertFalse(changed["can_request_recheck"])
        self.assertEqual(changed["state"], "unknown")

    async def test_unbound_native_limit_and_changed_account_during_call_do_not_block_current_login(self):
        result = await self.observe(account("claude", used=None), "claude")
        self.assertFalse(self.manager.quota_gate.record_limit("claude", result["account"], bound_identity=None))
        self.assertEqual(self.manager.get("claude")["availability"]["state"], "unknown")

    async def test_explicit_recheck_is_shared_unknown_persisted_and_rejected_for_codex_or_wrong_identity(self):
        self.manager.add(id="claude_alias", name="Alias", adapter="claude")
        await self.observe(account("claude", used=None), "claude")
        identity = self.manager.account_identity("claude")
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=account("claude", used=None))):
            await self.manager.record_subscription_limit("claude", identity)
        event = self.manager.request_availability_recheck("claude_alias")
        self.assertEqual(event["kind"], "quota_recheck_requested")
        self.assertNotIn("email", json.dumps(event))
        for identifier in ("claude", "claude_alias"):
            value = self.manager.get(identifier)["availability"]
            self.assertEqual((value["state"], value["source"]), ("unknown", "user_recheck_requested"))
            self.assertTrue(value["selectable"])
            self.assertFalse(value["can_request_recheck"])
        self.manager = PartnerManager(self.manager.root, self.base, clock=lambda: self.now)
        self.assertEqual((await self.observe(account("claude", used=None), "claude"))["availability"]["source"], "user_recheck_requested")
        await self.observe(account(used=100))
        with self.assertRaises(PartnerError) as caught:
            self.manager.request_availability_recheck("codex")
        self.assertEqual(caught.exception.code, "availability_recheck_unavailable")
        old = self.manager.account_identity("claude")
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=account("claude", email="other@example.test", used=None))):
            await self.manager.record_subscription_limit("claude", old)
        self.assertEqual(self.manager.get("claude")["availability"]["state"], "unknown")

    async def test_corrupt_persisted_gate_is_not_silently_forgotten(self):
        await self.observe(account(used=100))
        (self.manager.root / "quota-gates.json").write_text('{"version":1}')
        with self.assertRaises(PartnerError) as caught:
            PartnerManager(self.manager.root, self.base)
        self.assertEqual(caught.exception.code, "quota_state_corrupt")


class QuotaDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.calls = []
        async def fake(agent, prompt, cwd, **kwargs):
            self.calls.append(agent)
            return {"status": "completed", "session_id": str(uuid4()), "text": json.dumps(
                {"message": "Done", "state": "done", "actions": []}), "usage": None, "events": []}
        self.manager = PartnerManager(self.root / "partners", default_registry(fake), clock=lambda: 1000)
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   partners=self.manager, registry=self.manager.registry(), clock=lambda: 1000)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temporary.cleanup()

    async def exhaust(self):
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=account(used=100))):
            await self.manager.refresh_account("codex", force=True)

    async def test_task_creation_rejects_known_quota_before_room_and_model_call(self):
        await self.exhaust()
        with self.assertRaises(RoomError) as caught:
            await self.service.create_room("Work", "codex", "quota-create", participants=["codex"])
        self.assertEqual(caught.exception.code, "agent_quota_exhausted")
        self.assertEqual((await self.service.list_rooms())["rooms"], [])
        self.assertEqual(self.calls, [])
        self.assertIn("codex", (await self.service.status())["providers"])

    async def test_restart_with_identity_read_failure_cannot_dispatch_using_a_persisted_block(self):
        await self.exhaust()
        native = AsyncMock(side_effect=AssertionError("Known block must stop inference"))
        with patch.object(adapters, "invoke", new=native):
            manager = PartnerManager(self.manager.root, default_registry(), clock=lambda: 1000)
        self.service.partner_manager = manager
        self.service._sync_partners()
        with patch.object(adapters, "account_status", new=AsyncMock(side_effect=RuntimeError("offline"))) as metadata:
            with self.assertRaises(RoomError) as caught:
                await self.service.create_room("Work", "codex", "quota-restart", participants=["codex"])
        self.assertEqual(caught.exception.code, "agent_quota_unconfirmed")
        self.assertEqual(metadata.await_count, 1)
        native.assert_not_awaited()
        self.assertEqual((await self.service.list_rooms())["rooms"], [])

    async def test_dispatch_rechecks_after_queue_and_does_not_claim_budget_or_fake_cleanup(self):
        with patch.object(self.service, "_wake"):
            snapshot = await self.service.create_room("Work", "codex", "quota-queued", participants=["codex"])
        await self.exhaust()
        await self.service._work(snapshot["room"]["id"], "codex")
        saved = await self.service.read_room(snapshot["room"]["id"])
        self.assertEqual(saved["budget"]["used"], 0)
        self.assertEqual(saved["jobs"][0]["status"], "cancelled")
        self.assertEqual(saved["jobs"][0]["error"], "agent_quota_exhausted")
        self.assertEqual(self.calls, [])

    async def test_software_refresh_is_explicit_and_bounded_per_native_adapter(self):
        # Production registry selects a native observer; inference is still fake
        # and no create/send operation is performed in this metadata check.
        self.manager.base_registry = default_registry()
        self.manager.add(id="codex_alias", name="Alias", adapter="codex")
        with patch.object(adapters, "account_status", new=AsyncMock(side_effect=lambda adapter, *a, **k: account(adapter, used=20 if adapter == "codex" else None))) as probe:
            await self.service.list_partners()
            self.assertEqual(probe.await_count, 0)
            await self.service.refresh_availability()
            await self.service.refresh_availability()
        self.assertEqual(probe.await_count, 2)
        self.assertEqual(self.calls, [])

    async def test_frozen_dispatch_still_checks_an_inactive_partner_while_global_monitor_skips_it(self):
        self.manager.base_registry = default_registry()
        self.manager.set_enabled("codex", False)
        self.manager.set_enabled("claude", False)
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=account(used=20))) as probe:
            await self.manager.refresh_availability()
            self.assertEqual(probe.await_count, 0)
            await self.manager.refresh_availability(["codex"])
        self.assertEqual(probe.await_count, 1)
        self.assertFalse(self.manager.get("codex")["enabled"])
        self.assertEqual(self.calls, [])

    async def test_classified_native_limit_binds_current_account_and_blocks_alias_next_dispatch(self):
        await self.service.shutdown()
        attempts = []
        async def denied(agent, *args, **kwargs):
            attempts.append(agent)
            return {"status": "failed", "failure_code": "subscription_limit_reached", "session_id": str(uuid4()),
                    "text": "", "usage": None, "events": [{"type": "provider_error", "code": "subscription_limit_reached"}]}
        with patch.object(adapters, "invoke", new=denied):
            manager = PartnerManager(self.root / "native-partners", default_registry(), clock=lambda: 1000)
        manager.add(id="alias", name="Alias", adapter="claude")
        self.service = RoomService(self.root / "native.sqlite3", self.root / "native-workspaces",
                                   partners=manager, registry=manager.registry(), clock=lambda: 1000)
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=account("claude", used=None))) as metadata:
            await self.service.create_room("Work", "claude", "native-quota", participants=["claude"])
            while any(not task.done() for task in self.service._workers.values()):
                await asyncio.gather(*list(self.service._workers.values()))
            self.assertEqual(manager.get("alias")["availability"]["state"], "blocked")
            with self.assertRaises(RoomError) as caught:
                await self.service.create_room("Another task", "alias", "blocked-alias", participants=["alias"])
        self.assertEqual(caught.exception.code, "agent_quota_exhausted")
        self.assertEqual(attempts, ["claude"])
        self.assertEqual(metadata.await_count, 2, "one preflight and one identity confirmation after limit")


class QuotaHTTPTests(unittest.TestCase):
    setUp = http.RoomHTTPTests.setUp
    tearDown = http.RoomHTTPTests.tearDown
    request = http.RoomHTTPTests.request

    def test_background_refresh_is_explicit_web_only_and_returns_partner_snapshot(self):
        self.service.refresh_availability = AsyncMock(return_value={"partners": []})
        self.assertEqual(self.request("POST", "/api/availability/refresh", {})[0], 200)
        self.service.refresh_availability.assert_awaited_once()
        self.assertEqual(self.request("POST", "/api/mcp/availability/refresh", {},
            headers={"X-LAC-Instance": self.server.instance_id})[0], 403)
        self.assertEqual(self.request("GET", "/api/availability/refresh")[0], 404)

    def test_explicit_recheck_is_web_only_and_never_dispatches_a_model(self):
        self.service.request_availability_recheck = AsyncMock(return_value={"partners": []})
        self.assertEqual(self.request("POST", "/api/partners/claude/availability/recheck", {})[0], 200)
        self.service.request_availability_recheck.assert_awaited_once_with("claude")
        self.assertEqual(self.request("POST", "/api/mcp/partners/claude/availability/recheck", {},
            headers={"X-LAC-Instance": self.server.instance_id})[0], 403)
