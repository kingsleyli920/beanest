"""Remote observations over real HTTP/Git; every remote transport stays local."""
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from local_agent_coordinator import git_status
import test_code_status_http as fixture_module


class RemoteCheckRetentionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.CodeStatusHTTPTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        # Push only to disposable local paths, before the advertised canonical
        # URL is installed. No GitHub URL can reach a real transport.
        f.git(f.source, 'push', str(f.bare_upstream), 'main')
        self.url = 'https://github.com/beanest-fixture/remote-one.git'
        self.second_url = 'https://github.com/beanest-fixture/remote-two.git'
        self.remotes = {self.url: f.bare_upstream}
        f.git(f.source, 'remote', 'set-url', 'origin', self.url)
        self.queries = []
        self.clock = 1000000.0
        run = git_status._run
        read = git_status.read_github_main

        def local_transport(command, directory, arguments, *args, **kwargs):
            if 'ls-remote' in arguments:
                index = arguments.index('ls-remote')
                remote = arguments[index + 3]
                if remote not in self.remotes:
                    raise AssertionError('A fixture attempted a nonlocal remote query')
                self.queries.append(remote)
                # Real Git reads the actual bare refs. Interception only routes
                # the fake canonical HTTPS identity onto local transport.
                arguments = ['ls-remote', '--refs', '--exit-code', str(self.remotes[remote]),
                             *arguments[index + 4:]]
            return run(command, directory, arguments, *args, **kwargs)

        def observed_time(*args, **kwargs):
            kwargs['now'] = lambda: self.clock
            return read(*args, **kwargs)

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(git_status, '_run', side_effect=local_transport))
        stack.enter_context(patch.object(git_status, 'read_github_main', side_effect=observed_time))

    def status(self, *, refresh=False):
        suffix = '?refresh_remote=1' if refresh else ''
        code, value = self.fixture.request('GET', f'/api/rooms/{self.fixture.room_id}/code-status{suffix}')
        self.assertEqual(code, 200, value)
        self.assertEqual(self.fixture.invocations, [])
        remote = value['delivery_summary']['remote']
        self.assertEqual(len(remote['checks']), 1)
        self.assertEqual(remote['checks'][0]['workspace_id'], 'project')
        for field in ('state', 'reason', 'checked_at', 'cached'):
            self.assertEqual(remote.get(field), remote['checks'][0].get(field), field)
        return remote

    def test_explicit_remote_check_survives_ordinary_reads_with_original_time_and_no_remote_query(self):
        first = self.status(refresh=True)
        self.assertEqual(first['state'], 'confirmed')
        self.assertEqual(first.get('checked_at'), self.clock)
        self.assertIs(first.get('cached'), False)
        self.assertEqual(self.queries, [self.url])
        self.clock += 20
        for _ in range(2):
            recent = self.status()
            self.assertEqual(recent['state'], 'confirmed')
            self.assertEqual(recent['checked_at'], first['checked_at'])
            self.assertIs(recent['cached'], True)
        self.assertEqual(self.queries, [self.url])
        self.assertFalse((self.fixture.source / '.git/FETCH_HEAD').exists())

    def test_new_local_main_head_does_not_borrow_old_remote_success(self):
        self.assertEqual(self.status(refresh=True)['state'], 'confirmed')
        f = self.fixture
        f.write(f.source, 'next.txt', 'Fictional new main commit.\n')
        head = f.commit(f.source, 'New local main')
        self.assertNotEqual(head, f.local_commit)
        self.clock += 10
        local = self.status()
        self.assertEqual((local['state'], local['reason'], local['checked_at']), ('unknown', 'not_checked', None))
        self.assertEqual(self.queries, [self.url])
        fresh = self.status(refresh=True)
        self.assertEqual((fresh['state'], fresh['reason']), ('pending', 'local_mainline_not_on_remote'))
        self.assertEqual(fresh['checked_at'], self.clock)
        self.assertEqual(self.queries, [self.url, self.url])
        retained = self.status()
        self.assertEqual((retained['state'], retained['checked_at'], retained['cached']),
                         ('pending', self.clock, True))
        self.assertEqual(len(self.queries), 2)

    def test_remote_identity_expiry_and_latest_failure_never_reuse_prior_success(self):
        self.assertEqual(self.status(refresh=True)['state'], 'confirmed')
        f = self.fixture
        second = f.root / 'second-upstream.git'
        second.mkdir()
        f.git(second, 'init', '--bare', '--initial-branch=main', '--object-format=sha1')
        f.git(f.source, 'push', str(second), 'main')
        self.remotes[self.second_url] = second
        f.git(f.source, 'remote', 'set-url', 'origin', self.second_url)
        self.assertEqual(self.status()['reason'], 'not_checked')
        self.assertEqual(self.queries, [self.url])
        self.clock += 10
        self.assertEqual(self.status(refresh=True)['state'], 'confirmed')
        checked = self.clock
        self.clock += 301
        expired = self.status()
        self.assertEqual((expired['state'], expired['reason'], expired['checked_at']),
                         ('unknown', 'not_checked', None))
        self.assertEqual(self.queries, [self.url, self.second_url])
        self.assertEqual(self.status(refresh=True)['state'], 'confirmed')
        # A real local transport failure replaces the last good observation.
        moved = second.with_name('unavailable-upstream.git')
        second.rename(moved)
        self.clock += 5
        failed = self.status(refresh=True)
        self.assertEqual(failed['state'], 'unknown')
        self.assertNotEqual(failed['reason'], 'not_checked')
        self.assertGreater(failed['checked_at'], checked)
        self.assertIs(failed['cached'], False)
        count = len(self.queries)
        self.clock += 1
        retained = self.status()
        self.assertEqual((retained['state'], retained['reason'], retained['checked_at'], retained['cached']),
                         ('unknown', failed['reason'], failed['checked_at'], True))
        self.assertEqual(len(self.queries), count)


if __name__ == '__main__':
    unittest.main()
