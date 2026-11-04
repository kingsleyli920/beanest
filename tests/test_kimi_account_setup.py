"""Fresh Kimi telemetry and explicit setup checks share one login observation."""
import copy
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator import adapters, kimi_usage
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from tests.test_kimi_usage import quota_body


def observation():
    return {"state":"ready", "email":None, "plan":None, "auth_method":"kimi-code",
            "source":"kimi_managed_oauth", "scope":"default_cli_login", "reason":None,
            "quota":kimi_usage.parse_quota(quota_body())}


class KimiAccountSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="beanest-account-setup-test-")
        self.addCleanup(temporary.cleanup)
        self.manager = PartnerManager(Path(temporary.name).resolve() / "partners", default_registry())
        self.manager.add(id="kimi", name="Kimi", adapter="kimi")

    def state(self):
        return next(row["setup"]["state"] for row in self.manager.supported_adapters() if row["id"] == "kimi")

    async def refresh(self, value):
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=value)) as query:
            result = await self.manager.refresh_account("kimi", force=True)
        query.assert_awaited_once()
        return result

    async def test_fresh_canonical_observation_updates_same_login_fact_without_more_queries(self):
        self.assertEqual(self.state(), "unknown")
        await self.refresh(observation())
        self.assertEqual(self.state(), "ready")
        self.manager._account_cache["kimi"]["quota"]["stale"] = True
        self.assertEqual(self.state(), "ready", "setup fact is not derived from an expired quota cache")
        with patch.object(adapters, "kimi_status", new=AsyncMock(return_value={"ready":False})):
            self.manager._discovery["tools"] = [{"adapter":"kimi", "installed":True}]
            await self.manager.refresh_setup()
        self.assertEqual(self.state(), "login_required", "an explicit fresh setup check remains authoritative")
        await self.refresh(observation())
        self.assertEqual(self.state(), "ready")

    async def test_stale_unconfirmed_wrong_source_and_invalid_windows_cannot_promote_setup(self):
        cases = []
        for field,value in (("state","partial"),("auth_method",None),("source","other"),("stale",True)):
            row = observation();row[field] = value;cases.append(row)
        for field,value in (("state","unavailable"),("source","other"),("scope","model"),("reason","account_check_timeout"),("stale",True),("windows",[])):
            row = observation();row["quota"][field] = value;cases.append(row)
        for value in (True, math.nan, 101, "10"):
            row = observation();row["quota"]["windows"][0]["remaining_percent"] = value;cases.append(row)
        row = observation();row["quota"]["windows"][0]["remaining_percent"] = 10;cases.append(row)
        for initial in (None, "login_required"):
            for value in cases:
                with self.subTest(initial=initial, account=value["state"], quota=value["quota"]["state"]):
                    self.manager._setup_observations.clear()
                    if initial:self.manager._setup_observations["kimi"] = initial
                    await self.refresh(copy.deepcopy(value))
                    self.assertEqual(self.state(), initial or "unknown")
        await self.refresh(observation())
        self.manager._setup_observations.clear()
        with patch.object(adapters, "account_status", new=AsyncMock(side_effect=TimeoutError())):
            await self.manager.refresh_account("kimi", force=True)
        self.assertEqual(self.state(), "unknown")

    async def test_other_adapters_and_passive_or_coalesced_cached_observations_do_not_promote(self):
        await self.refresh(observation())
        self.manager._setup_observations.clear()
        with patch.object(adapters, "account_status", new=AsyncMock()) as query:
            self.manager.get("kimi")
            await self.manager.refresh_account("kimi")
        query.assert_not_awaited()
        self.assertEqual(self.state(), "unknown")
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=observation())):
            await self.manager.refresh_account("claude", force=True)
        self.assertEqual(self.manager._setup_observations, {})
