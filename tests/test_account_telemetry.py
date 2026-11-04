"""Account telemetry uses bounded read-only CLI calls and a private memory cache."""
import asyncio
from copy import deepcopy
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator import adapters
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.room_server import create_room_server


def observed(email="bean@example.test", *, used=68):
    return {"state": "ready", "email": email, "plan": "pro", "auth_method": "chatgpt",
            "source": "codex_app_server", "scope": "default_cli_login", "reason": None,
            "quota": {"state": "available", "source": "codex_app_server_account_rate_limits",
                      "scope": "provider_account", "reason": None,
                      "windows": [{"name": "codex:primary", "used_percent": used,
                                   "remaining_percent": 100 - used, "window_minutes": 10080,
                                   "resets_at": 1793775142}]}}


class AccountProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def rpc_program(self, mode="normal"):
        program = self.root / "fake_rpc.py"
        program.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            f"mode={mode!r}\n"
            "Path('pid').write_text(str(os.getpid()))\n"
            "if mode=='wait': time.sleep(30)\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " with open('requests','a') as trace: trace.write(json.dumps(request)+'\\n')\n"
            " if 'id' not in request: continue\n"
            " ident=request['id']; method=request['method']\n"
            " if mode=='server_request':\n"
            "  print(json.dumps({'id':99,'method':'account/chatgptAuthTokens/refresh','params':{'PRIVATE':'secret'}}),flush=True); continue\n"
            " if mode=='flood': print('x'*70000,flush=True); time.sleep(30)\n"
            " if mode=='stderr_flood': print('x'*70000,file=sys.stderr,flush=True); time.sleep(30)\n"
            " if method=='initialize': result={}\n"
            " elif method=='account/read':\n"
            "  email='second@example.test' if mode=='switch' and ident==3 else 'bean@example.test'\n"
            "  result={'account':{'type':'chatgpt','email':email,'planType':'pro','accessToken':'PRIVATE-TOKEN'}}\n"
            " elif method=='account/rateLimits/read':\n"
            "  if mode=='quota_error':\n"
            "   print(json.dumps({'id':ident,'error':{'message':'PRIVATE-TOKEN'}}),flush=True); continue\n"
            "  result={'accountId':'PRIVATE-ID','credits':{'secret':'PRIVATE-CREDIT'},'rateLimits':{'primary':{'usedPercent':68,'windowDurationMins':10080,'resetsAt':1793775142},'secondary':None}}\n"
            " else: raise AssertionError('unexpected method')\n"
            " print(json.dumps({'id':ident,'result':result}),flush=True)\n")
        return [sys.executable, str(program)]

    def assert_process_gone(self):
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_rpc_is_a_closed_read_allowlist_and_filters_secrets(self):
        snapshot = await adapters._codex_account_rpc(self.rpc_program(), self.root)
        self.assertEqual(snapshot["email"], "bean@example.test")
        self.assertEqual(snapshot["quota"]["windows"][0]["remaining_percent"], 32)
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        requests = [json.loads(line) for line in (self.root / "requests").read_text().splitlines()]
        self.assertEqual([request["method"] for request in requests],
                         ["initialize", "initialized", "account/read", "account/rateLimits/read", "account/read"])
        self.assertEqual(requests[0]["params"]["capabilities"], {"experimentalApi": False})
        self.assertTrue(all(request["params"] == {"refreshToken": False}
                            for request in requests if request["method"] == "account/read"))
        self.assert_process_gone()

    async def test_account_switch_clears_quota_instead_of_binding_it_to_new_email(self):
        snapshot = await adapters._codex_account_rpc(self.rpc_program("switch"), self.root)
        self.assertEqual(snapshot["email"], "second@example.test")
        self.assertEqual(snapshot["quota"]["windows"], [])
        self.assertEqual(snapshot["quota"]["reason"], "account_changed")
        self.assert_process_gone()

    async def test_quota_failure_preserves_only_current_checked_identity(self):
        snapshot = await adapters._codex_account_rpc(self.rpc_program("quota_error"), self.root)
        self.assertEqual(snapshot["email"], "bean@example.test")
        self.assertEqual(snapshot["quota"]["state"], "error")
        self.assertEqual(snapshot["quota"]["windows"], [])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        self.assert_process_gone()

    async def test_server_auth_refresh_request_is_rejected_without_a_response(self):
        with self.assertRaisesRegex(adapters.AccountTelemetryError, "account_request_rejected"):
            await adapters._codex_account_rpc(self.rpc_program("server_request"), self.root)
        self.assertEqual(len((self.root / "requests").read_text().splitlines()), 1)
        self.assert_process_gone()

    async def test_timeout_and_cancellation_terminate_the_owned_child(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                (self.root / "pid").unlink(missing_ok=True)
                task = asyncio.create_task(adapters._codex_account_rpc(self.rpc_program("wait"), self.root, timeout=.2))
                for _ in range(100):
                    if (self.root / "pid").exists():
                        break
                    await asyncio.sleep(.005)
                if cancel:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
                    await task
                self.assert_process_gone()

    async def test_stdout_and_stderr_are_bounded_and_child_is_cleaned(self):
        for mode in ("flood", "stderr_flood"):
            with self.subTest(mode=mode):
                with self.assertRaises((ValueError, adapters.AccountTelemetryError)):
                    await adapters._codex_account_rpc(self.rpc_program(mode), self.root)
                self.assert_process_gone()

    async def test_cancellation_during_success_cleanup_is_not_swallowed(self):
        started, release = asyncio.Event(), asyncio.Event()
        terminate = adapters._terminate

        async def held_cleanup(process):
            started.set()
            await release.wait()
            await terminate(process)

        with patch.object(adapters, "_terminate", side_effect=held_cleanup):
            task = asyncio.create_task(adapters._codex_account_rpc(self.rpc_program(), self.root))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_process_gone()

    async def test_native_probe_commands_keep_default_environment_and_disabled_tools(self):
        with patch.object(adapters, "find_codex", return_value="/trusted/codex"), \
             patch.object(adapters, "_codex_account_rpc", new=AsyncMock(return_value=observed())) as probe:
            await adapters.account_status("codex", self.root)
        command = probe.call_args.args[0]
        self.assertEqual(command[:4], ["/trusted/codex", "app-server", "--listen", "stdio://"])
        self.assertNotIn("--ignore-user-config", command)
        self.assertNotIn("--ignore-rules", command)
        for feature in ("code_mode_host", "hooks", "plugins", "apps", "multi_agent"):
            self.assertIn(feature, command)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "PRIVATE", "CLAUDE_CONFIG_DIR": "/PRIVATE", "CODEX_HOME": "/PRIVATE"}):
            environment = adapters._environment()
        self.assertFalse({"OPENAI_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME"} & environment.keys())

    async def test_claude_identity_has_no_invented_quota_or_model(self):
        raw = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
               "subscriptionType": "max", "email": "claude@example.test", "configDirectory": "/PRIVATE",
               "accessToken": "PRIVATE-TOKEN", "unknown": {"model": "PRIVATE"}}
        with patch.object(adapters, "find_claude", return_value="/trusted/claude"), \
             patch.object(adapters, "_run", new=AsyncMock(return_value=([json.dumps(raw).encode()], 0))) as run, \
             patch.object(adapters, "_claude_usage_rpc", new=AsyncMock(return_value={})):
            result = await adapters.account_status("claude", self.root)
        self.assertEqual(run.call_args.args[0], ["/trusted/claude", "--safe-mode", "--restricted", "auth", "status", "--json"])
        self.assertEqual(result["email"], "claude@example.test")
        self.assertEqual(result["quota"]["windows"], [])
        self.assertEqual(result["quota"]["reason"], "provider_usage_unavailable")
        self.assertEqual(run.await_count, 2)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("model", result)

    async def test_ultra_plan_is_displayable_without_changing_inference_authentication(self):
        raw = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
               "subscriptionType": "Ultra", "email": "claude@example.test"}
        text = json.dumps(raw)
        with patch.object(adapters, "find_claude", return_value="/trusted/claude"), \
             patch.object(adapters, "_run", new=AsyncMock(return_value=([text.encode()], 0))), \
             patch.object(adapters, "_claude_usage_rpc", new=AsyncMock(return_value={})):
            result = await adapters.account_status("claude", self.root)
        self.assertEqual(result["plan"], "ultra")
        self.assertFalse(adapters._authentication("claude", 0, text)["subscription_confirmed"])

    def test_missing_invalid_quota_numbers_remain_null(self):
        for invalid in (None, True, -1, 101, float("nan"), float("inf"), "68"):
            window = adapters._quota_window("codex:primary", {"usedPercent": invalid})
            self.assertIsNone(window["used_percent"])
            self.assertIsNone(window["remaining_percent"])
            self.assertIsNone(window["resets_at"])

    def test_multi_bucket_limits_are_not_double_counted_with_legacy_view(self):
        bucket = {"primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1793775142},
                  "secondary": {"usedPercent": 68, "windowDurationMins": 10080, "resetsAt": 1793775142}}
        result = adapters._account_quota({"rateLimits": bucket, "rateLimitsByLimitId": {"codex": bucket},
                                         "accountId": "PRIVATE"})
        self.assertEqual(len(result["windows"]), 2)
        self.assertEqual(result["windows"][0]["remaining_percent"], 100)
        self.assertEqual(result["windows"][1]["remaining_percent"], 32)
        self.assertNotIn("PRIVATE", json.dumps(result))


class AccountCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "partners"
        self.now = 1000
        self.manager = PartnerManager(self.root, default_registry(), clock=lambda: self.now)
        self.manager.add(id="codex-alias", name="Another participant", adapter="codex")

    async def test_inspection_is_cache_only_and_aliases_share_one_observation(self):
        with patch.object(adapters, "account_status", new=AsyncMock(return_value=observed())) as probe:
            profiles = self.manager.profiles()
            self.assertTrue(all(item["account"]["checked_at"] is None for item in profiles))
            probe.assert_not_called()
            await asyncio.gather(self.manager.refresh_account("codex"), self.manager.refresh_account("codex-alias"))
            self.assertEqual(probe.await_count, 1)
            profiles = self.manager.profiles()
            accounts = [item["account"] for item in profiles if item["adapter"] == "codex"]
            self.assertEqual(accounts[0], accounts[1])
            self.assertEqual(accounts[0]["shared_profile_ids"], ["codex", "codex-alias"])
            self.assertEqual(accounts[0]["checked_at"], 1000)
            self.now += 40
            await self.manager.refresh_account("codex")
            self.assertEqual(probe.await_count, 1)
            self.now += 60
            self.assertTrue(self.manager.get("codex")["account"]["stale"])
            self.assertEqual(probe.await_count, 1)
        self.assertNotIn("bean@example.test", (self.root / "profiles.json").read_text())

    async def test_failed_refresh_retains_old_values_marked_stale(self):
        with patch.object(adapters, "account_status", new=AsyncMock(side_effect=[observed(), asyncio.TimeoutError()])):
            await self.manager.refresh_account("codex")
            self.now += 60
            await self.manager.refresh_account("codex")
        account = self.manager.get("codex")["account"]
        self.assertEqual(account["email"], "bean@example.test")
        self.assertTrue(account["stale"])
        self.assertEqual(account["reason"], "account_check_timeout")
        self.assertEqual(account["checked_at"], 1000)
        self.assertTrue(account["quota"]["stale"])
        self.assertEqual(account["quota"]["windows"][0]["used_percent"], 68)

    async def test_quota_failure_can_reuse_old_window_only_for_same_identity(self):
        same = observed()
        same.update(state="partial")
        same["quota"].update(state="error", windows=[], reason="provider_usage_unavailable")
        switched = deepcopy(same)
        switched["email"] = "new@example.test"
        with patch.object(adapters, "account_status", new=AsyncMock(side_effect=[observed(), same, switched])):
            await self.manager.refresh_account("codex")
            self.now += 60
            await self.manager.refresh_account("codex")
            cached = self.manager.get("codex")["account"]["quota"]
            self.assertEqual(cached["checked_at"], 1000)
            self.assertTrue(cached["stale"])
            self.assertEqual(cached["windows"][0]["used_percent"], 68)
            self.now += 60
            await self.manager.refresh_account("codex")
        account = self.manager.get("codex")["account"]
        self.assertEqual(account["email"], "new@example.test")
        self.assertEqual(account["quota"]["windows"], [])

    async def test_legacy_profile_json_reads_without_account_persistence(self):
        path = self.root / "profiles.json"
        data = json.loads(path.read_text())
        for item in data["profiles"]:
            item.pop("bean_style")
            item.pop("lifecycle_state")
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        manager = PartnerManager(self.root, default_registry(), clock=lambda: self.now)
        with patch.object(adapters, "account_status", new=AsyncMock()) as probe:
            self.assertEqual(manager.get("codex")["account"]["state"], "not_checked")
            probe.assert_not_called()
        self.assertEqual(path.read_bytes(), before)


class AccountHTTPFake:
    def __init__(self):
        self.refreshes = []

    async def list_partners(self):
        return {"partners": [{"id": "codex", "account": observed(), "connection": {"private": "PRIVATE"}}]}

    async def refresh_partner_account(self, identifier):
        self.refreshes.append(identifier)
        return await self.list_partners()

    async def expire_tasks(self):
        pass

    async def shutdown(self):
        pass


class AccountHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "index.html").write_text("<title>Test</title>")
        self.service = AccountHTTPFake()
        self.server = create_room_server(self.service, port=0, asset_dir=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, mcp=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               {"Content-Type": "application/json", "X-LAC-Token": self.server.mcp_token if mcp else self.server.token,
                                "X-LAC-Instance": self.server.instance_id})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_email_is_web_only_and_get_never_refreshes(self):
        status, result = self.request("GET", "/api/partners")
        self.assertEqual(status, 200)
        self.assertEqual(result["partners"][0]["account"]["email"], "bean@example.test")
        status, result = self.request("GET", "/api/mcp/partners", mcp=True)
        self.assertEqual(status, 200)
        self.assertNotIn("account", result["partners"][0])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.service.refreshes, [])

    def test_refresh_is_strictly_web_managed_and_has_no_agent_side_effect(self):
        path = "/api/partners/codex/account/refresh"
        self.assertEqual(self.request("POST", path, {})[0], 200)
        self.assertEqual(self.service.refreshes, ["codex"])
        self.assertEqual(self.request("POST", path, {"force": True})[0], 400)
        status, result = self.request("POST", "/api/mcp/partners/codex/account/refresh", {}, mcp=True)
        self.assertEqual((status, result["error"]), (403, "user_account_required"))
        self.assertEqual(self.service.refreshes, ["codex"])
