"""Real owned local HTTP boundaries: transient quota failures do not become limits."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters, kimi_usage
from local_agent_coordinator.quota_gate import QuotaGate
from tests.test_kimi_usage import quota_body
from tests import test_kimi_usage as fixture


class KimiUsageRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="beanest-kimi-retry-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def program(self, replies):
        program = self.root / "fictional-kimi"
        program.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f"""
            import json,os,sys
            from http.server import BaseHTTPRequestHandler,HTTPServer
            from pathlib import Path
            root=Path(__file__).parent
            (root/'pid').write_text(str(os.getpid()))
            assert sys.argv[1:6]==['web','--no-open','--host','127.0.0.1','--port']
            assert 'KIMI_API_KEY' not in os.environ and 'OPENAI_API_KEY' not in os.environ
            replies={replies!r};count=0
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    global count
                    assert self.headers['Authorization']=='Bearer TEST_LOCAL_ONLY_TOKEN_123456'
                    with (root/'requests').open('a') as out:out.write(self.path+'\\n')
                    status=200
                    if self.path=={kimi_usage._AUTH!r}:
                        data={{'managed_provider':{{'name':'managed:kimi-code','status':'authenticated'}}}}
                    elif self.path=={kimi_usage._PROFILE!r}:
                        data={{'kind':'ok','userInfo':{{'userId':'TEST_ACCOUNT','userLevelName':'Plus'}}}}
                    elif self.path=={kimi_usage._USAGE!r}:
                        status,data=replies[min(count,len(replies)-1)];count+=1
                        if data=='disconnect':self.connection.close();return
                    else:raise AssertionError('No model/session endpoint allowed')
                    body=json.dumps({{'code':0,'data':data}}).encode()
                    self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            port=int(sys.argv[6]);server=HTTPServer(('127.0.0.1',port),Handler)
            print('http://127.0.0.1:'+str(port)+'/#token=TEST_LOCAL_ONLY_TOKEN_123456',flush=True)
            server.serve_forever()
        """))
        program.chmod(0o700)
        return str(program)

    async def query(self, replies):
        (self.root / "requests").unlink(missing_ok=True)
        with patch.object(adapters, "find_kimi", return_value=self.program(replies)):
            result = await kimi_usage.account_status(self.root)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)
        self.assertNotIn("TEST_LOCAL_ONLY", json.dumps(result))
        requests = (self.root / "requests").read_text().splitlines()
        self.assertTrue(all(path in (kimi_usage._AUTH, kimi_usage._USAGE, kimi_usage._PROFILE) for path in requests))
        return result, requests.count(kimi_usage._USAGE)

    async def test_transient_http_provider_and_transport_failures_retry_only_read_and_recover(self):
        for first in ((503, {}), (200, {"kind":"error", "status":502}), (200, "disconnect")):
            with self.subTest(first=first):
                result, attempts = await self.query([first, (200, quota_body())])
                self.assertEqual(attempts, 2)
                self.assertEqual(result["state"], "ready")
                self.assertEqual(result["quota"]["windows"][0]["remaining_percent"], 99)

    async def test_auth_rate_limit_invalid_data_and_unsupported_do_not_blindly_retry(self):
        cases = ((401, {}, "provider_usage_auth_unconfirmed"),
                 (200, {"kind":"error","status":403}, "provider_usage_auth_unconfirmed"),
                 (429, {}, "provider_usage_rate_limited"),
                 (404, {}, "provider_quota_not_supported"),
                 (200, {"status":503}, "provider_usage_invalid_response"),
                 (200, {"kind":"ok","limits":[]}, "provider_usage_invalid_response"))
        for status,data,reason in cases:
            with self.subTest(reason=reason):
                result, attempts = await self.query([(status,data), (200, quota_body())])
                self.assertEqual(attempts, 1)
                self.assertEqual(result["quota"]["windows"], [])
                self.assertEqual(result["quota"]["reason"], reason)

    async def test_repeated_transient_failure_is_unknown_not_zero_or_exhausted(self):
        for failure, reason in [((503, {}), "provider_usage_temporarily_unavailable"),
                                ((200, "disconnect"), "provider_usage_transport_error")]:
            with self.subTest(reason=reason):
                result, attempts = await self.query([failure])
                self.assertEqual(attempts, 2)
                self.assertEqual(result["quota"]["reason"], reason)
                self.assertEqual(result["quota"]["windows"], [])
                self.assertEqual(result["state"], "partial")
                self.assertEqual(result["plan"], "Plus")

    async def test_deadline_is_classified_without_retrying_after_overall_timeout(self):
        with patch.object(adapters, "find_kimi", return_value=fixture.KimiQuotaProcessTests.program(self, "hang")):
            result = await kimi_usage.account_status(self.root, timeout=.3)
        self.assertEqual(result["quota"]["reason"], "account_check_timeout")
        self.assertEqual(result["quota"]["windows"], [])
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)

    def test_kimi_telemetry_support_does_not_invent_identity_or_persist_dispatch_gate(self):
        gate = QuotaGate(self.root, lambda:1000)
        for quota in (kimi_usage.parse_quota(quota_body()), kimi_usage.parse_quota(None)):
            account = {"state":"partial", "email":None, "auth_method":"kimi-code", "source":"kimi_managed_oauth",
                       "checked_at":1000, "stale":False, "quota":quota}
            self.assertIsNone(gate.identity(account))
            gate.observe_account("kimi", account)
            value = gate.snapshot("kimi", account)
            self.assertEqual(value["reason"], "quota_unknown")
            self.assertTrue(value["selectable"])
        self.assertFalse(gate.path.exists())
        self.assertEqual(gate.snapshot("other", {})["reason"], "adapter_unsupported")
