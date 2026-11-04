"""Repeated bounded Kimi telemetry over host HTTP and a fictional owned CLI."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
import socket
import sys
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters, kimi_usage
from local_agent_coordinator.partner_manager import ACCOUNT_REFRESH_COALESCE_SECONDS
from test_kimi_setup_http import SetupFixture
from test_kimi_native_setup import configuration


_REAL_ACCOUNT_STATUS = adapters.account_status


class StabilityFixture(SetupFixture):
    def __init__(self, budget):
        super().__init__(discover=False)
        self.config.write_text(configuration(), encoding='utf-8')
        self.now = time.time()
        self.partners.clock = lambda: self.now
        self.partners.quota_gate.clock = lambda: self.now
        self.control = self.root / 'quota-control.json'
        self.events = self.root / 'quota-events.jsonl'
        self.set_case()
        self.executable.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            import json, os, sys, time
            from pathlib import Path
            root = Path(__file__).parent
            assert sys.argv[1:6] == ['web', '--no-open', '--host', '127.0.0.1', '--port']
            case = json.loads((root / 'quota-control.json').read_text())
            port = int(sys.argv[6])
            def record(kind, **fields):
                with (root / 'quota-events.jsonl').open('a') as stream:
                    stream.write(json.dumps(dict(kind=kind, pid=os.getpid(), port=port, **fields)) + '\\n')
            record('start')
            time.sleep(case['startup'])
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    assert self.headers.get('Authorization') == 'Bearer FICTIONAL_LOCAL_USAGE_TOKEN_1234'
                    record('request', path=self.path)
                    if self.path == '/api/v1/auth':
                        data = {'managed_provider': {'name': 'managed:kimi-code', 'status': 'authenticated'}}
                    elif self.path == '/api/v1/oauth/userinfo?provider=managed%3Akimi-code':
                        data = {'kind': 'ok', 'userInfo': {'userId': 'fictional-stability-user', 'userLevelName': 'Plus'}}
                    elif self.path == '/api/v1/oauth/usage?provider=managed%3Akimi-code':
                        time.sleep(case['usage'])
                        data = {'kind': 'ok', 'limits': [{'window': {'duration': 5, 'unit': 'hour'},
                                'used': case['used'], 'limit': 100}]}
                    else:
                        raise AssertionError('unexpected endpoint')
                    body = json.dumps({'code': 0, 'data': data}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    try: self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError): pass
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            print(f'http://127.0.0.1:{port}/#token=FICTIONAL_LOCAL_USAGE_TOKEN_1234', flush=True)
            server.serve_forever()
        '''), encoding='utf-8')
        if budget is not None:
            self.stack.enter_context(patch.object(kimi_usage, 'DEFAULT_TIMEOUT', budget))
        self.active_reads = 0
        self.read_lock = threading.Lock()
        get_json = kimi_usage._get_json

        def observed_read(*args, **kwargs):
            with self.read_lock:
                self.active_reads += 1
            try:
                return get_json(*args, **kwargs)
            finally:
                with self.read_lock:
                    self.active_reads -= 1

        # Observe the actual synchronous request worker; never replace its I/O.
        self.stack.enter_context(patch.object(kimi_usage, '_get_json', observed_read))

        async def account(adapter, *args, **kwargs):
            if adapter != 'kimi':
                raise AssertionError('Only fictional Kimi telemetry is requested')
            return await _REAL_ACCOUNT_STATUS(adapter, *args, **kwargs)

        self.stack.enter_context(patch.object(adapters, 'account_status', account))
        self.loop_errors = []

        def report(loop, context):
            self.loop_errors.append(context.get('message', 'unhandled loop exception'))
            loop.default_exception_handler(context)

        self.server.runner.loop.call_soon_threadsafe(self.server.runner.loop.set_exception_handler, report)

    def set_case(self, *, startup=0, usage=0, used=37):
        self.control.write_text(json.dumps(dict(startup=startup, usage=usage, used=used)), encoding='utf-8')
        self.now += ACCOUNT_REFRESH_COALESCE_SECONDS + 1

    def records(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def refresh(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=40)
        try:
            connection.request('POST', '/api/partners/kimi/account/refresh', body='{}',
                               headers={'X-LAC-Token': self.server.token, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            value = json.loads(response.read())
            if response.status != 200:
                raise AssertionError((response.status, value))
            return next(item for item in value['partners'] if item['id'] == 'kimi')
        finally:
            connection.close()


class KimiQuotaStabilityHTTPTests(unittest.TestCase):
    def fixture(self, budget=1.5):
        value = StabilityFixture(budget)
        self.addCleanup(value.close)
        return value

    def assert_settled(self, fixture):
        for item in fixture.records():
            if item['kind'] != 'start':
                continue
            with self.assertRaises(ProcessLookupError):
                os.kill(item['pid'], 0)
            with socket.socket() as client:
                client.settimeout(.2)
                self.assertNotEqual(client.connect_ex(('127.0.0.1', item['port'])), 0)
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.active_reads, 0)
        self.assertEqual(fixture.loop_errors, [])

    def test_real_manager_default_budget_allows_local_query_longer_than_old_ten_seconds(self):
        fixture = self.fixture(budget=None)
        self.assertEqual(kimi_usage.DEFAULT_TIMEOUT, 30)
        fixture.set_case(startup=4.8, usage=5.6, used=23)
        started = time.monotonic()
        quota = fixture.refresh()['account']['quota']
        elapsed = time.monotonic() - started
        self.assertGreater(elapsed, 10)
        self.assertLess(elapsed, 30)
        self.assertEqual((quota['state'], quota['windows'][0]['remaining_percent']), ('available', 77))
        self.assertEqual(quota['diagnostics']['phase'], 'complete')
        self.assertGreater(quota['diagnostics']['elapsed_seconds'], 10)
        self.assert_settled(fixture)

    def test_repeated_slow_start_and_usage_and_concurrent_refresh_coalesce(self):
        fixture = self.fixture()
        for used in (17, 42, 100):
            fixture.set_case(startup=.2, usage=.25, used=used)
            result = fixture.refresh()
            quota = result['account']['quota']
            self.assertEqual((quota['state'], quota['windows'][0]['remaining_percent']), ('available', 100-used))
            self.assertFalse(quota['stale'])
            self.assert_settled(fixture)
        fixture.set_case(startup=.2, usage=.25, used=11)
        starts = sum(item['kind'] == 'start' for item in fixture.records())
        barrier = threading.Barrier(4)
        def refresh():
            barrier.wait(timeout=3)
            return fixture.refresh()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: refresh(), range(4)))
        self.assertEqual([item['account']['quota']['windows'][0]['remaining_percent'] for item in results], [89]*4)
        self.assertEqual(len({item['account']['checked_at'] for item in results}), 1)
        records = fixture.records()
        self.assertEqual(sum(item['kind'] == 'start' for item in records), starts+1)
        self.assertEqual(sum(item['kind'] == 'request' and 'usage?' in item['path'] for item in records), 4)
        self.assert_settled(fixture)

    def test_slow_start_timeout_settles_before_a_later_success(self):
        fixture = self.fixture(budget=.65)
        fixture.set_case(startup=3)
        failed = fixture.refresh()['account']['quota']
        self.assertEqual((failed['reason'], failed['windows']), ('account_check_timeout', []))
        self.assertEqual(failed['diagnostics']['phase'], 'banner')
        self.assertEqual([row for row in fixture.records() if row['kind'] == 'request'], [])
        self.assert_settled(fixture)
        fixture.set_case(startup=.1, usage=.1, used=19)
        self.assertEqual(fixture.refresh()['account']['quota']['windows'][0]['remaining_percent'], 81)
        self.assert_settled(fixture)

    def test_repeated_slow_usage_timeout_then_fresh_recovery_and_failure_coalescing(self):
        fixture = self.fixture(budget=.65)
        fixture.set_case(used=10)
        self.assertEqual(fixture.refresh()['account']['quota']['windows'][0]['remaining_percent'], 90)
        for _ in range(2):
            fixture.set_case(usage=3)
            started = time.monotonic()
            failed = fixture.refresh()
            self.assertLess(time.monotonic()-started, 4)
            self.assertEqual(failed['account']['quota']['reason'], 'account_check_timeout')
            self.assertEqual(failed['account']['quota']['windows'], [])
            self.assertEqual(failed['account']['quota']['diagnostics']['phase'], 'usage')
            self.assertEqual(failed['account']['quota']['diagnostics']['reason'], 'account_check_timeout')
            self.assertEqual(failed['availability']['reason'], 'quota_unknown')
            self.assert_settled(fixture)
            calls = len(fixture.records())
            self.assertEqual(fixture.refresh()['account']['checked_at'], failed['account']['checked_at'])
            self.assertEqual(len(fixture.records()), calls)
        fixture.set_case(used=100)
        recovered = fixture.refresh()['account']['quota']
        self.assertEqual((recovered['state'], recovered['windows'][0]['remaining_percent']), ('available', 0))
        self.assertFalse(recovered['stale'])
        self.assert_settled(fixture)


if __name__ == '__main__':
    unittest.main()
