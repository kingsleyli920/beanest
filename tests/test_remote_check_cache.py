"""Real disposable Git identities and remote observations, no network calls."""
import unittest
import shutil
from unittest.mock import patch

from local_agent_coordinator import git_status
import test_git_status as fixtures


class RemoteCheckCacheTests(unittest.TestCase):
    committed_at = fixtures.GitStatusTests.committed_at
    setUp = fixtures.GitStatusTests.setUp
    git = fixtures.GitStatusTests.git
    repository = fixtures.GitStatusTests.repository
    write = fixtures.GitStatusTests.write
    commit = fixtures.GitStatusTests.commit

    def prepare(self):
        self.source = self.repository()
        self.write(self.source, 'entry.txt', 'Fictional committed content.\n')
        self.head = self.commit(self.source)
        self.bare = self.root / 'remote.git'
        self.bare.mkdir()
        self.git(self.bare, 'init', '--bare', '--initial-branch=main')
        self.git(self.source, 'push', str(self.bare), 'main')
        self.git(self.source, 'remote', 'add', 'origin', 'https://github.com/example/fictional.git')
        self.cache = git_status.GithubCheckCache()
        self.now = 1000.0
        self.network_calls = 0
        self.network_failure = False
        original = git_status._run
        def transport(command, directory, arguments, deadline, **kwargs):
            if 'ls-remote' in arguments:
                self.network_calls += 1
                if self.network_failure:
                    raise git_status._GitError('git_timeout')
                arguments = ['-c','protocol.file.allow=always', *arguments]
                arguments[-2] = str(self.bare)
            return original(command, directory, arguments, deadline, **kwargs)
        wrapped = patch.object(git_status, '_run', side_effect=transport)
        wrapped.start()
        self.addCleanup(wrapped.stop)

    def read(self, *, refresh=False, path=None, branch='main', head=None, cache=None):
        return git_status.read_github_main(path or self.source, branch, head or self.head,
            now=lambda:self.now, cache=cache or self.cache, refresh=refresh)

    def test_local_reads_retain_original_check_time_without_network_and_are_instance_local(self):
        self.prepare()
        self.assertEqual(self.read()['reason'], 'not_checked')
        fresh = self.read(refresh=True)
        self.assertEqual((fresh['state'], fresh['checked_at'], fresh['cached']), ('confirmed',1000,False))
        self.now += 20
        cached = self.read()
        self.assertEqual((cached['state'], cached['checked_at'], cached['cached']), ('confirmed',1000,True))
        self.assertEqual(self.network_calls, 1)
        self.assertEqual(self.read(cache=git_status.GithubCheckCache())['reason'], 'not_checked')
        self.read(refresh=True)
        self.assertEqual(self.network_calls, 2)
        self.assertFalse((self.source / '.git/FETCH_HEAD').exists())

    def test_new_main_head_remote_branch_and_repository_do_not_reuse_success(self):
        self.prepare()
        self.read(refresh=True)
        self.write(self.source, 'entry.txt', 'Next main commit.\n')
        next_head = self.commit(self.source)
        self.assertEqual(self.read(head=next_head)['reason'], 'not_checked')
        self.assertEqual(self.read(branch='trunk')['reason'], 'not_checked')
        self.git(self.source, 'remote', 'set-url', 'origin', 'https://github.com/example/other.git')
        self.assertEqual(self.read()['reason'], 'not_checked')
        other = self.repository('other')
        self.git(other, 'remote', 'add', 'origin', 'https://github.com/example/fictional.git')
        self.assertEqual(self.read(path=other)['reason'], 'not_checked')
        self.assertEqual(self.network_calls, 1)

    def test_expired_and_future_observations_are_not_checked_and_keep_no_fake_timestamp(self):
        self.prepare()
        self.read(refresh=True)
        for timestamp in (1301.0, 999.0):
            with self.subTest(timestamp=timestamp):
                self.now = 1000
                self.read(refresh=True)
                self.now = timestamp
                result = self.read()
                self.assertEqual((result['reason'], result['checked_at'], result['cached']), ('not_checked',None,False))

    def test_latest_failed_query_is_retained_as_unknown_and_never_falls_back_to_success(self):
        self.prepare()
        self.read(refresh=True)
        self.now = 1010
        self.network_failure = True
        failed = self.read(refresh=True)
        self.assertEqual((failed['state'], failed['reason'], failed['cached']), ('unknown','git_timeout',False))
        self.now += 1
        cached = self.read()
        self.assertEqual((cached['state'],cached['reason'],cached['checked_at'],cached['cached']),
                         ('unknown','git_timeout',1010,True))
        self.assertEqual(self.network_calls, 2)

    def test_remote_rewrite_rejection_invalidates_old_success_even_if_configuration_is_restored(self):
        self.prepare()
        self.read(refresh=True)
        self.git(self.source, 'config', 'url.ext::fictional.insteadOf', 'https://github.com/')
        self.assertEqual(self.read()['reason'], 'github_remote_rewrite')
        self.git(self.source, 'config', '--unset-all', 'url.ext::fictional.insteadOf')
        self.assertEqual(self.read()['reason'], 'not_checked')
        self.assertEqual(self.network_calls, 1)

    def test_older_late_success_cannot_replace_latest_error_or_be_deleted_by_local_poll(self):
        cache = git_status.GithubCheckCache()
        key = (('root','common',1,2),'https://github.com/example/repo.git','main','a'*40)
        old = cache.begin(key)
        latest = cache.begin(key)
        self.assertIsNone(cache.read(key, 1000))
        cache.finish(key, latest, {'state':'unknown','reason':'git_timeout','checked_at':1000,'cached':False})
        cache.finish(key, old, {'state':'confirmed','reason':None,'checked_at':999,'cached':False})
        self.assertEqual(cache.read(key, 1001)['reason'], 'git_timeout')

    def test_replacing_git_directory_at_same_path_does_not_inherit_old_repository_check(self):
        self.prepare()
        self.read(refresh=True)
        original = self.source / '.git'
        preserved = self.root / 'previous-git-directory'
        original.rename(preserved)
        shutil.copytree(preserved, original)
        self.assertEqual(self.read()['reason'], 'not_checked')
        self.assertEqual(self.network_calls, 1)
