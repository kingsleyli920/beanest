"""Real local HTTP/CLI boundaries for read-only Kimi quota, without a model."""
import asyncio
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator import adapters, kimi_usage
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager


def quota_body():
    return {"kind": "ok", "summary": None, "limits": [
        {"window": {"duration": 5, "unit": "hour"}, "used": 1, "limit": 100,
         "reset_at": "2026-11-03T08:00:00+00:00"}], "extra_usage": {"PRIVATE": "wallet"}}


class KimiQuotaParserTests(unittest.TestCase):
    def test_installed_034_windows_and_documented_newer_windows_keep_true_ratios(self):
        actual = kimi_usage.parse_quota(quota_body())
        window = actual["windows"][0]
        self.assertEqual((window["name"], window["used_percent"], window["remaining_percent"], window["window_minutes"]),
                         ("kimi:limit5h", 1, 99, 300))
        self.assertIsInstance(window["resets_at"], float)
        newer = kimi_usage.parse_quota({"kind": "ok", "quota": {"usages": {
            "limit7d": {"usedRatio": .25, "resetAt": "2026-11-04T08:00:00Z"},
            "monthCode": {"usedRatio": 0}}, "extraUsage": {"PRIVATE": "wallet"}}})
        self.assertEqual([(row["name"], row["used_percent"]) for row in newer["windows"]],
                         [("kimi:limit7d", 25), ("kimi:monthCode", 0)])
        self.assertNotIn("PRIVATE", json.dumps([actual, newer]))

    def test_official_raw_or_already_masked_phone_is_projected_as_country_and_tail_only(self):
        for number in ("15512340567", "155****0567", "****0567"):
            with self.subTest(number_form="masked" if "*" in number else "raw"):
                display = kimi_usage._display_identity({"nickname": "Member Test", "phone": {"countryCode": "+86", "number": number}})
                self.assertEqual(display, {"nickname": "Member Test", "masked_phone": "+86 **** 0567"})
                self.assertNotIn("15512340567", json.dumps(display))
        for phone in ({"countryCode": "86", "number": "0567"}, {"countryCode": "INVALID", "number": "15512340567"},
                      {"countryCode": "86", "number": "private0567"}):
            self.assertNotIn("masked_phone", kimi_usage._display_identity({"phone": phone}))

    def test_missing_invalid_and_provider_errors_never_become_zero_or_exhausted(self):
        for used in (None, True, -1, 101, math.nan, "10"):
            body = quota_body()
            body["limits"][0]["used"] = used
            result = kimi_usage.parse_quota(body)
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["windows"], [])
        result = kimi_usage.parse_quota({"kind": "error", "message": "PRIVATE", "status": 403})
        self.assertEqual(result["reason"], "provider_usage_unavailable")
        self.assertEqual(result["usage_url"], kimi_usage.USAGE_URL)
        self.assertNotIn("PRIVATE", json.dumps(result))


class KimiQuotaProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="beanest-kimi-quota-test-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def program(self, mode="ok"):
        program = self.root / "fictional-kimi"
        program.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f"""
            import json, os, sys, time
            from http.server import BaseHTTPRequestHandler, HTTPServer
            from pathlib import Path
            root=Path(__file__).parent
            (root/'pid').write_text(str(os.getpid()))
            assert sys.argv[1:6] == ['web','--no-open','--host','127.0.0.1','--port']
            assert 'KIMI_API_KEY' not in os.environ and 'OPENAI_API_KEY' not in os.environ
            port=int(sys.argv[6]); mode={mode!r}; profile_reads=0
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_GET(self):
                    global profile_reads
                    with (root/'requests').open('a') as out:
                        out.write(json.dumps({{'path':self.path,'local_auth_matches':self.headers.get('Authorization')=='Bearer TEST_LOCAL_ONLY_TOKEN_123456'}})+'\\n')
                    status=200
                    if self.path=='/api/v1/auth':
                        data={{'managed_provider':{{'name':'managed:kimi-code','status':'unauthenticated' if mode=='unauthenticated' else 'authenticated'}},'PRIVATE':'secret'}}
                    elif self.path=='/api/v1/oauth/userinfo?provider=managed%3Akimi-code':
                        profile_reads+=1
                        data={{'kind':'ok','userInfo':{{'userId':'other' if mode=='profile-changed' and profile_reads>1 else 'TEST_ACCOUNT', 'userLevelName':'Plus', 'nickname':'Member Test', 'phone':{{'countryCode':'86','number':'15512340567'}}, 'PRIVATE':'secret'}}}}
                        if mode=='profile-email':data['userInfo']['email']='member@example.test'
                        if mode=='profile-missing':data['userInfo'].pop('userLevelName')
                        if mode=='profile-error':data={{'kind':'error','status':503,'message':'PRIVATE'}}
                    elif self.path=='/api/v1/oauth/usage?provider=managed%3Akimi-code':
                        if mode=='hang':
                            (root/'usage-started').write_text('yes');time.sleep(30)
                        data={quota_body()!r}
                        if mode=='unsupported':status=404
                        if mode=='redirect':status=302
                        if mode=='error':data={{'kind':'error','message':'PRIVATE','status':403}}
                        if mode=='profile-usage-error':data={{'kind':'error','message':'PRIVATE','status':503}}
                    else:raise AssertionError('unexpected endpoint')
                    body=json.dumps({{'code':0,'data':data}}).encode()
                    self.send_response(status)
                    if status==302:self.send_header('Location','https://outside.invalid/PRIVATE')
                    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            server=HTTPServer(('127.0.0.1',port),Handler)
            print('http://127.0.0.1:'+str(port+1 if mode=='wrong-port' else port)+'/#token=TEST_LOCAL_ONLY_TOKEN_123456',flush=True)
            server.serve_forever()
        """), encoding="utf-8")
        program.chmod(0o700)
        return str(program)

    def assert_gone(self):
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def requests(self):
        path = self.root / "requests"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def test_native_profile_projects_only_real_email_and_membership_for_stable_account(self):
        with patch.object(adapters, "find_kimi", return_value=self.program("profile-email")):
            result = await adapters.account_status("kimi", self.root)
        self.assertEqual(result["email"], "member@example.test")
        self.assertEqual(result["plan"], "Plus")
        self.assertEqual(result["display_identity"], {"masked_phone": "+86 **** 0567", "nickname": "Member Test"})
        self.assertNotIn("15512340567", json.dumps(result))
        self.assertEqual(result["field_reasons"], {"email": None, "plan": None})
        self.assertNotIn("TEST_ACCOUNT", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assert_gone()

    async def test_absent_profile_fields_and_profile_failure_do_not_invent_identity_or_erase_valid_quota(self):
        for mode, plan, reason in [("ok", "Plus", "provider_not_provided"),
                                   ("profile-missing", None, "provider_not_provided"),
                                   ("profile-error", None, "profile_query_unavailable")]:
            with self.subTest(mode=mode), patch.object(adapters, "find_kimi", return_value=self.program(mode)):
                result = await adapters.account_status("kimi", self.root)
                self.assertIsNone(result["email"])
                self.assertEqual(result["plan"], plan)
                self.assertEqual(result["field_reasons"]["email"], reason)
                self.assertEqual(result["quota"]["state"], "available")
                self.assert_gone()

    async def test_quota_failure_still_reads_stable_membership_without_reusing_old_account_fields(self):
        with patch.object(adapters, "find_kimi", return_value=self.program("profile-usage-error")):
            result = await adapters.account_status("kimi", self.root)
        self.assertEqual(result["plan"], "Plus")
        self.assertIsNone(result["email"])
        self.assertEqual(result["field_reasons"]["email"], "provider_not_provided")
        self.assertEqual(result["quota"]["state"], "unavailable")
        self.assertEqual(result["quota"]["reason"], "provider_usage_temporarily_unavailable")
        self.assert_gone()

    async def test_profile_account_switch_clears_fields_and_quota_instead_of_crossing_accounts(self):
        with patch.object(adapters, "find_kimi", return_value=self.program("profile-changed")):
            result = await adapters.account_status("kimi", self.root)
        self.assertIsNone(result["email"]); self.assertIsNone(result["plan"])
        self.assertEqual(result["display_identity"], {})
        self.assertEqual(result["quota"]["state"], "unavailable")
        self.assertEqual(result["quota"]["windows"], [])
        self.assertEqual(result["quota"]["reason"], "account_changed")
        self.assert_gone()

    async def test_exact_local_read_queries_filter_secrets_and_cleanup_owned_process(self):
        with patch.object(adapters, "find_kimi", return_value=self.program()), patch.dict(os.environ, {"KIMI_API_KEY": "PRIVATE", "OPENAI_API_KEY": "PRIVATE"}):
            result = await adapters.account_status("kimi", self.root)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["auth_method"], "kimi-code")
        self.assertIsNone(result["email"])
        self.assertEqual(result["plan"], "Plus")
        self.assertEqual(result["field_reasons"]["email"], "provider_not_provided")
        self.assertEqual(result["quota"]["source"], kimi_usage.SOURCE)
        self.assertEqual([request["path"] for request in self.requests()], [kimi_usage._AUTH, kimi_usage._PROFILE, kimi_usage._USAGE, kimi_usage._PROFILE, kimi_usage._AUTH])
        self.assertTrue(all(request["local_auth_matches"] for request in self.requests()))
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assert_gone()

    async def test_unsupported_redirect_and_error_remain_unknown_and_never_follow_external_url(self):
        for mode, reason in (("unsupported", "provider_quota_not_supported"), ("redirect", "provider_usage_unavailable"), ("error", "provider_usage_auth_unconfirmed")):
            with self.subTest(mode=mode), patch.object(adapters, "find_kimi", return_value=self.program(mode)):
                (self.root / "requests").unlink(missing_ok=True)
                result = await kimi_usage.account_status(self.root)
                self.assertEqual(result["quota"]["windows"], [])
                self.assertEqual(result["quota"]["reason"], reason)
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertTrue(all(request["path"] in {kimi_usage._AUTH, kimi_usage._USAGE, kimi_usage._PROFILE} for request in self.requests()))
                self.assert_gone()

    async def test_unconfirmed_auth_and_wrong_port_do_not_query_usage_or_adopt_another_service(self):
        for mode in ("unauthenticated", "wrong-port"):
            with self.subTest(mode=mode), patch.object(adapters, "find_kimi", return_value=self.program(mode)):
                (self.root / "requests").unlink(missing_ok=True)
                result = await kimi_usage.account_status(self.root)
                self.assertEqual(result["state"], "unavailable")
                self.assertEqual(result["quota"]["windows"], [])
                self.assertNotIn(kimi_usage._USAGE, [request["path"] for request in self.requests()])
                self.assert_gone()

    async def test_timeout_and_cancel_cleanup_without_touching_an_unrelated_process(self):
        other = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time;time.sleep(30)", start_new_session=True)
        try:
            with patch.object(adapters, "find_kimi", return_value=self.program("hang")):
                result = await kimi_usage.account_status(self.root, timeout=.3)
                self.assertEqual(result["quota"]["windows"], [])
                self.assert_gone()
                (self.root / "usage-started").unlink(missing_ok=True)
                task = asyncio.create_task(kimi_usage.account_status(self.root))
                for _ in range(100):
                    if (self.root / "usage-started").exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue((self.root / "usage-started").exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assert_gone()
            self.assertIsNone(other.returncode)
        finally:
            other.terminate()
            await other.wait()


class KimiQuotaCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_kimi_aliases_share_existing_refresh_cache_and_unknown_is_selectable(self):
        with tempfile.TemporaryDirectory(prefix="beanest-kimi-cache-") as root:
            manager = PartnerManager(Path(root).resolve() / "partners", default_registry(), clock=lambda: 1000)
            manager.add(id="kimi", name="Kimi", adapter="kimi")
            manager.add(id="kimi-other", name="Other Kimi", adapter="kimi")
            observed = {"state": "ready", "email": None, "plan": None, "auth_method": "kimi-code",
                        "source": "kimi_managed_oauth", "scope": "default_cli_login", "reason": None,
                        "field_reasons": {"email": "provider_not_provided", "plan": None},
                        "display_identity": {"masked_phone": "+86 **** 0567", "nickname": "Member Test", "phone": "15512340567"},
                        "quota": kimi_usage.parse_quota(quota_body())}
            with patch.object(adapters, "account_status", new=AsyncMock(return_value=observed)) as probe:
                self.assertEqual(manager.get("kimi")["account"]["quota"]["usage_url"], kimi_usage.USAGE_URL)
                probe.assert_not_called()
                await asyncio.gather(manager.refresh_account("kimi"), manager.refresh_account("kimi-other"))
                self.assertEqual(probe.await_count, 1)
                for identifier in ("kimi", "kimi-other"):
                    profile = manager.get(identifier)
                    self.assertEqual(profile["account"]["quota"]["windows"][0]["remaining_percent"], 99)
                    self.assertEqual(profile["account"]["quota"]["usage_url"], kimi_usage.USAGE_URL)
                    self.assertEqual(profile["account"]["field_reasons"], observed["field_reasons"])
                    self.assertEqual(profile["account"]["display_identity"], {"masked_phone": "+86 **** 0567", "nickname": "Member Test"})
                    self.assertNotIn("15512340567", json.dumps(profile["account"]))
                    self.assertTrue(profile["availability"]["selectable"])
                await manager.refresh_account("kimi")
                self.assertEqual(probe.await_count, 1)


if __name__ == "__main__":
    unittest.main()
