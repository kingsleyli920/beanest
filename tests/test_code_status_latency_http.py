"""Bounded multi-repository code status through HTTP and an owned slow Git CLI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import http.client
import json
import os
import shutil
import sys
import textwrap
import time
import unittest
from unittest.mock import patch

from local_agent_coordinator import git_status
from test_code_delivery import local_github_transport
import test_code_status_http as fixtures


class CodeStatusLatencyHTTPTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.CodeStatusHTTPTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.git(fixture.source, 'restore', '--staged', 'tracked.txt')
        fixture.git(fixture.source, 'restore', 'tracked.txt')
        (fixture.source / '未跟踪 说明.txt').unlink()
        fixture.git(fixture.source, 'push', 'origin', 'main')
        fixture.slow = fixture.root / 'proof-control'
        fixture.git(fixture.root, 'clone', str(fixture.bare_upstream), str(fixture.slow))
        fixture.delay = fixture.root / 'git-delay.json'
        fixture.delay.write_text('6')
        fixture.trace = fixture.root / 'git-processes.jsonl'
        wrapper = fixture.root / 'owned-git'
        wrapper.write_text(f'#!{sys.executable}\n' + textwrap.dedent(f'''
            import json, os, sys, time
            from pathlib import Path
            root = Path(__file__).parent
            with (root / 'git-processes.jsonl').open('a') as stream:
                stream.write(json.dumps({{'pid': os.getpid()}}) + '\\n')
            if Path.cwd() == Path({str(fixture.slow)!r}) and 'status' in sys.argv:
                (root / 'slow-status-started').touch()
                time.sleep(float((root / 'git-delay.json').read_text()))
            os.execv({fixture.git_binary!r}, [{fixture.git_binary!r}, *sys.argv[1:]])
        '''), encoding='utf-8')
        wrapper.chmod(0o700)
        stack = ExitStack()
        self.addCleanup(stack.close)
        original = shutil.which
        stack.enter_context(patch.object(git_status.shutil, 'which', side_effect=
                                        lambda name, *args, **kwargs: str(wrapper) if name == 'git' else original(name, *args, **kwargs)))
        stack.enter_context(local_github_transport(fixture.bare_upstream))
        # The existing authorized-delivery discovery boundary supplies an exact
        # second repo; all Git reads, aggregation and HTTP serialization are real.
        candidate = {'id': 'delivery:proof-control', 'kind': 'delivery', 'label': 'Fictional linked evidence',
                     'path': str(fixture.slow), 'source': {'asset_id': 'fixture-asset', 'run_id': 'fixture-run',
                                                        'task_id': 'fixture-task', 'result_sha256': 'a'*64}}
        stack.enter_context(patch.object(fixture.service, '_code_delivery_workspaces', return_value=([candidate], [])))
        return fixture

    def request(self, fixture, path):
        connection = http.client.HTTPConnection('127.0.0.1', fixture.port, timeout=20)
        try:
            connection.request('GET', path, headers={'X-LAC-Token': fixture.server.token})
            response = connection.getresponse()
            value = json.loads(response.read())
            self.assertEqual(response.status, 200, value)
            return value
        finally:
            connection.close()

    def assert_settled(self, fixture):
        for line in fixture.trace.read_text().splitlines():
            with self.assertRaises(ProcessLookupError):
                os.kill(json.loads(line)['pid'], 0)
            with self.assertRaises(ProcessLookupError):
                os.killpg(json.loads(line)['pid'], 0)
        self.assertEqual(fixture.invocations, [])
        self.assertFalse((fixture.source / '.git/FETCH_HEAD').exists())
        self.assertFalse((fixture.slow / '.git/FETCH_HEAD').exists())
        self.assertEqual(fixture.git(fixture.source, 'rev-parse', 'HEAD'), fixture.local_commit)
        self.assertEqual(fixture.git(fixture.slow, 'rev-parse', 'HEAD'), fixture.local_commit)

    def test_one_slow_root_keeps_other_git_facts_and_room_reads_responsive(self):
        fixture = self.fixture()
        # A pure report reference is intentionally excluded from delivery.
        # Explicitly select its details to exercise a slow read without
        # turning that reference into a required publication repository.
        path = f'/api/rooms/{fixture.room_id}/code-status?refresh_remote=1&workspace=delivery%3Aproof-control'
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.request, fixture, path)
            deadline = time.monotonic() + 3
            while not (fixture.root / 'slow-status-started').exists() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue((fixture.root / 'slow-status-started').exists())
            read_started = time.monotonic()
            snapshot = self.request(fixture, '/api/rooms/' + fixture.room_id)
            self.assertEqual(snapshot['room']['id'], fixture.room_id)
            self.assertLess(time.monotonic() - read_started, 1)
            value = pending.result(timeout=18)
        self.assertLess(time.monotonic() - started, 15)
        summary = value['delivery_summary']
        self.assertEqual((value['state'], value['reason']), ('unavailable', 'git_timeout'))
        self.assertEqual(summary['state'], 'confirmed')
        self.assertTrue(any(check['workspace_id'] == 'project' and check['state'] == 'confirmed'
                            for check in summary['main']['checks']))
        self.assertTrue(any(check['workspace_id'] == 'project' and check['state'] == 'confirmed'
                            for check in summary['remote']['checks']))
        self.assertFalse(any(check['workspace_id'] == 'delivery:proof-control'
                             for check in summary['workspace']['checks']))
        self.assert_settled(fixture)
        fixture.delay.write_text('0')
        recovered = self.request(fixture, path)['delivery_summary']
        self.assertEqual(recovered['state'], 'confirmed')
        self.assert_settled(fixture)

    def test_overall_budget_returns_typed_unknown_before_frontend_timeout(self):
        fixture = self.fixture()
        with patch.object(git_status, 'CODE_STATUS_SECONDS', .4):
            started = time.monotonic()
            value = self.request(fixture, f'/api/rooms/{fixture.room_id}/code-status?refresh_remote=1')
        self.assertLess(time.monotonic() - started, 2)
        summary = value['delivery_summary']
        self.assertNotEqual(summary['state'], 'confirmed')
        self.assertTrue(any(check['reason'] == 'git_timeout' for check in summary['workspace']['checks']))
        self.assert_settled(fixture)


if __name__ == '__main__':
    unittest.main()
