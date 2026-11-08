"""Per-profile CLI homes: a partner may use its own login instead of the local one.

Every case uses temporary directories and a fake CLI. No real login command
runs, and the real ~/.claude, ~/.codex and ~/.kimi-code are never touched.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, kimi_acp, native_adapters
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.quota_gate import QuotaGate
from local_agent_coordinator.room_service import RoomError, RoomService

SID = '6216d06d-043b-4c66-ad6a-c85a0de23b71'
# The fake reports who it is from the home it was given, and logs that home.
FAKE_CLAUDE = r'''#!{python}
import json, os, sys
home = os.environ.get("CLAUDE_CONFIG_DIR")
with open({log!r}, "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "home": home}}) + "\n")
if "--version" in sys.argv:
    print("2.1.300 (Claude Code)")
elif sys.argv[-3:] == ["auth", "status", "--json"]:
    if home is None:
        print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                          "subscriptionType": "max", "email": "team@example.com"}}))
    elif os.path.exists(os.path.join(home, "signed-in")):
        print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                          "subscriptionType": "pro", "email": "personal@example.com"}}))
    else:
        print(json.dumps({{"loggedIn": False}}))
else:
    sys.exit(3)
'''


async def unused(*args, **kwargs):
    raise AssertionError('inspection must not invoke a model')


class EnvironmentBindingTests(unittest.TestCase):
    """Catches: a profile home leaking to other calls, or the user's own override reaching a child."""

    def test_only_the_bound_profile_home_reaches_the_child(self):
        poison = {'CLAUDE_CONFIG_DIR': '/user/shell/claude', 'CODEX_HOME': '/user/shell/codex',
                  'KIMI_CODE_HOME': '/user/shell/kimi'}
        with patch.dict(os.environ, poison):
            local = adapters._environment()
            with adapters.account_home('claude', '/beanest/homes/bean'):
                bound = adapters._environment()
                with adapters.account_home('claude', None):
                    nested_local = adapters._environment()
            after = adapters._environment()
        for child in (local, nested_local, after):
            self.assertFalse({'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'KIMI_CODE_HOME'} & set(child))
        self.assertEqual(bound['CLAUDE_CONFIG_DIR'], '/beanest/homes/bean')
        self.assertNotIn('CODEX_HOME', bound)
        self.assertNotIn('KIMI_CODE_HOME', bound)

    def test_each_cli_gets_its_own_variable_and_relative_homes_are_refused(self):
        for agent, variable in (('codex', 'CODEX_HOME'), ('kimi', 'KIMI_CODE_HOME')):
            with adapters.account_home(agent, '/beanest/homes/x'):
                self.assertEqual(adapters._environment()[variable], '/beanest/homes/x')
                self.assertEqual(adapters.native_home(agent), Path('/beanest/homes/x'))
        with self.assertRaises(ValueError):
            with adapters.account_home('claude', 'relative/home'):
                pass
        with self.assertRaises(ValueError):
            with adapters.account_home('deepseek', '/beanest/homes/x'):
                pass

    def test_binding_follows_tasks_started_inside_it(self):
        async def child():
            return adapters._environment().get('CODEX_HOME')

        async def main():
            with adapters.account_home('codex', '/beanest/homes/c'):
                inside = await asyncio.create_task(child())
            outside = await asyncio.create_task(child())
            return inside, outside
        self.assertEqual(asyncio.run(main()), ('/beanest/homes/c', None))


class SessionHomeReadTests(unittest.TestCase):
    """Catches: Kimi wires, Kimi config and Claude transcripts still read from the real home."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.root / 'user')}))

    def test_kimi_wire_uses_profile_home_explicit_or_bound(self):
        profile = self.root / 'homes' / 'kimi_two'
        default = kimi_acp.session_wire('/work/project', 'session-1')
        explicit = kimi_acp.session_wire('/work/project', 'session-1', home=profile)
        with adapters.account_home('kimi', profile):
            bound = kimi_acp.session_wire('/work/project', 'session-1')
        self.assertEqual(default.parts[:len((self.root / 'user' / '.kimi-code' / 'sessions').parts)],
                         (self.root / 'user' / '.kimi-code' / 'sessions').parts)
        self.assertEqual(explicit, bound)
        self.assertEqual(explicit.relative_to(profile / 'sessions'), default.relative_to(self.root / 'user' / '.kimi-code' / 'sessions'))

    def test_kimi_managed_configuration_reads_the_bound_home(self):
        profile = self.root / 'homes' / 'kimi_two'
        profile.mkdir(parents=True)
        (profile / 'config.toml').write_text('default_model = "kimi-code/k2"\n'
            '[providers."managed:kimi-code"]\ntype = "kimi"\nbase_url = "https://api.kimi.com/coding/v1"\noauth = { key = "x" }\n'
            '[models."kimi-code/k2"]\nprovider = "managed:kimi-code"\n')
        self.assertEqual(kimi_acp.managed_configuration()['state'], 'unavailable', 'the local home has no config')
        with adapters.account_home('kimi', profile):
            self.assertEqual(kimi_acp.managed_configuration()['default_model'], 'kimi-code/k2')

    def test_claude_transcript_is_read_from_the_profile_home(self):
        profile = self.root / 'homes' / 'bean'
        with self.assertRaises(FileNotFoundError) as caught:
            native_adapters.claude_quota_recovery_proof('/work/project', SID, claimed_at=0, finished_at=1,
                validate_prompt=lambda prompt: True, receipt_dir=self.root / 'receipts', home=profile)
        self.assertTrue(caught.exception.filename.startswith(str(profile / 'projects') + '/'), caught.exception.filename)


class QuotaScopeTests(unittest.TestCase):
    """Catches: a limit on one login blocking (or being cleared by) another profile's login."""

    def account(self, email, used):
        return {'auth_method': 'claude.ai', 'email': email, 'stale': False, 'state': 'ready', 'checked_at': 10,
                'quota': {'state': 'available', 'stale': False, 'source': 'claude_sdk_get_usage', 'checked_at': 10,
                          'windows': [{'name': 'claude:five_hour', 'scope': 'provider_account', 'used_percent': used,
                                       'resets_at': None}]}}

    def test_profile_scope_is_separate_and_survives_restart(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        gate = QuotaGate(root, lambda: 20)
        gate.observe_account('claude', self.account('team@example.com', 100))
        gate.observe_account('claude:bean', self.account('personal@example.com', 30))
        self.assertEqual(gate.snapshot('claude', self.account('team@example.com', 100))['state'], 'blocked')
        personal = gate.snapshot('claude:bean', self.account('personal@example.com', 30))
        self.assertEqual((personal['state'], personal['scope']), ('available', 'profile_cli_home'))
        reloaded = QuotaGate(root, lambda: 20)
        self.assertEqual(reloaded.snapshot('claude', {**self.account('team@example.com', 100), 'stale': True})['state'], 'blocked')
        self.assertTrue(reloaded.record_limit('claude:bean', self.account('personal@example.com', 30),
                                              bound_identity=reloaded.identity(self.account('personal@example.com', 30))))
        self.assertEqual(QuotaGate(root, lambda: 20).snapshot('claude:bean', self.account('personal@example.com', 30))['reason'],
                         'subscription_limit_reached')

    def test_unknown_scope_keys_are_still_rejected(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / 'quota-gates.json').write_text(json.dumps({'version': 1, 'salt': '0' * 64, 'blocks': {
            'claude:../x': {'identity': '0' * 64, 'reason': 'quota_exhausted', 'source': 'claude_shared_pool',
                            'observed_at': 1, 'resets_at': None}}}))
        with self.assertRaisesRegex(ValueError, 'quota_state_corrupt'):
            QuotaGate(root, lambda: 20)


class PartnerHomeTests(unittest.IsolatedAsyncioTestCase):
    """A real subprocess fake CLI shows which home each check actually used."""

    async def asyncSetUp(self):
        # A space and a quote in the runtime path prove the login command is quoted.
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "my runtime's"
        bin_dir = self.base / 'bin'
        bin_dir.mkdir(parents=True)
        self.log = self.base / 'calls.jsonl'
        self.claude = bin_dir / 'claude'
        self.claude.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(self.log)))
        self.claude.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.base / 'user'), 'PATH': str(bin_dir),
                                                  'CLAUDE_CONFIG_DIR': str(self.base / 'user-shell-claude')}))
        self.enterContext(patch.object(adapters, 'CLAUDE_SYSTEM_PATHS', ()))
        self.root = self.base / 'partners'
        self.manager = PartnerManager(self.root, default_registry(unused), clock=lambda: 100)
        self.manager.add(id='bean', name='Claude · 备用', adapter='claude')

    def homes_seen(self):
        return [json.loads(line)['home'] for line in self.log.read_text().splitlines()]

    async def test_profile_home_login_command_and_switch_back(self):
        local = self.manager.get('bean')
        self.assertEqual(local['account_source'], {'kind': 'local', 'home': None, 'variable': None, 'login_command': None})
        own = self.manager.set_account_source('bean', 'profile_home')
        home = self.root / 'homes' / 'bean'
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / 'homes').stat().st_mode), 0o700)
        self.assertEqual(own['account_source']['home'], str(home))
        command = own['account_source']['login_command']
        self.assertEqual(shlex.split(command), ['CLAUDE_CONFIG_DIR=' + str(home), str(self.claude), 'auth', 'login'])
        self.assertEqual(command, "CLAUDE_CONFIG_DIR='" + str(home).replace("'", "'\"'\"'") + "' '"
                         + str(self.claude).replace("'", "'\"'\"'") + "' auth login")
        self.assertEqual(self.manager.get('claude')['account_source']['kind'], 'local', 'other profiles keep the local login')
        self.assertFalse(self.log.exists(), 'switching or describing never runs the CLI, let alone login')
        persisted = json.loads((self.root / 'profiles.json').read_text())
        self.assertEqual([item.get('account_source') for item in persisted['profiles']], [None, None, 'profile_home'])
        back = PartnerManager(self.root, default_registry(unused)).set_account_source('bean', 'local')
        self.assertEqual(back['account_source']['kind'], 'local')
        self.assertTrue(home.is_dir(), 'switching back keeps the separate login')
        self.assertNotIn('account_source', json.loads((self.root / 'profiles.json').read_text())['profiles'][2])
        with self.assertRaisesRegex(PartnerError, 'invalid_account_source'):
            self.manager.set_account_source('bean', '/tmp/elsewhere')

    async def test_account_checks_use_each_profile_home_and_never_the_user_override(self):
        self.manager.set_account_source('bean', 'profile_home')
        home = self.root / 'homes' / 'bean'
        team = await self.manager.refresh_account('claude', force=True)
        fresh = await self.manager.refresh_account('bean', force=True)
        self.assertEqual(team['account']['email'], 'team@example.com')
        self.assertIsNone(fresh['account']['email'], 'an empty profile home is not signed in')
        self.assertEqual(fresh['account']['scope'], 'profile_cli_home')
        self.assertEqual(fresh['account']['shared_profile_ids'], ['bean'])
        self.assertEqual(team['account']['shared_profile_ids'], ['claude'])
        (home / 'signed-in').write_text('')  # stands in for the user's own terminal login
        personal = await self.manager.refresh_account('bean', force=True)
        self.assertEqual((personal['account']['email'], personal['account']['plan']), ('personal@example.com', 'pro'))
        self.assertEqual(self.manager.get('claude')['account']['email'], 'team@example.com')
        checked = await self.manager.check('bean')
        self.assertEqual(checked['health']['state'], 'ready')
        seen = set(self.homes_seen())
        self.assertEqual(seen, {None, str(home)})
        self.assertNotIn(str(self.base / 'user-shell-claude'), seen)

    def test_older_profiles_without_a_source_load_unchanged(self):
        data = json.loads((self.root / 'profiles.json').read_text())
        self.assertNotIn('account_source', data['profiles'][2])
        for item in PartnerManager(self.root, default_registry(unused)).profiles():
            self.assertEqual(item['account_source']['kind'], 'local')
            self.assertEqual(item['account']['scope'], 'default_cli_login')
        data['profiles'][2]['account_source'] = '/tmp/elsewhere'
        (self.root / 'profiles.json').write_text(json.dumps(data))
        with self.assertRaisesRegex(PartnerError, 'invalid_partner_storage'):
            PartnerManager(self.root, default_registry(unused))


class RoomDispatchHomeTests(unittest.IsolatedAsyncioTestCase):
    """Real SQLite rooms with a synthetic transport that reports the home it was given."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/user/shell/claude', 'HOME': str(self.root / 'user')}))
        self.calls, self.hold = [], None

        async def invoke(agent, prompt, cwd, session_id=None, timeout=None, **options):
            self.calls.append({'agent': agent, 'session_id': session_id,
                               'env': adapters._environment().get('CLAUDE_CONFIG_DIR')})
            if self.hold is not None:
                await self.hold.wait()
            return {'session_id': session_id or str(uuid4()), 'status': 'completed', 'events': [],
                    'text': json.dumps({'message': 'Finished.', 'state': 'done', 'actions': []})}
        partners = PartnerManager(self.root / 'partners', default_registry(invoke), clock=lambda: 100)
        partners.add(id='bean', name='Claude · 备用', adapter='claude')
        self.partners = partners
        self.service = RoomService(self.root / 'rooms.sqlite3', self.root / 'workspaces', partners=partners)
        self.home = self.root / 'partners' / 'homes' / 'bean'

    async def asyncTearDown(self):
        await self.service.shutdown()
        if self.service._cleanup_task:
            await self.service._cleanup_task

    async def settle(self):
        await asyncio.gather(*list(self.service._workers.values()))

    async def test_rooms_use_the_profile_home_and_never_resume_across_homes(self):
        await self.service.set_partner_account_source('bean', 'profile_home')
        own = (await self.service.create_room('Use the separate login.', 'bean', 'own-room'))['room']['id']
        team = (await self.service.create_room('Use the local login.', 'claude', 'team-room'))['room']['id']
        await self.settle()
        self.assertEqual([(call['agent'], call['env']) for call in self.calls],
                         [('claude', str(self.home)), ('claude', None)])
        record = self.service.store.native_session_cleanup_records(own)[0]
        self.assertEqual(record['account_home'], str(self.home))
        self.assertIsNone(self.service.store.native_session_cleanup_records(team)[0]['account_home'])

        # The session lives in the profile home; after switching back it is not resumed elsewhere.
        await self.service.set_partner_account_source('bean', 'local')
        await self.service.send_message(own, 'Continue here.', 'bean', 'own-followup')
        await self.settle()
        self.assertEqual(len(self.calls), 2, 'no call is made with a session from the other home')
        jobs = self.service.store.snapshot(own)['jobs']
        self.assertEqual(jobs[-1]['error'], 'partner_account_home_changed')

        # Switching back to the home the session was created in continues it there.
        await self.service.set_partner_account_source('bean', 'profile_home')
        await self.service.send_message(own, 'Continue again.', 'bean', 'own-followup-2')
        await self.settle()
        self.assertEqual(self.calls[-1], {'agent': 'claude', 'session_id': record['session_id'], 'env': str(self.home)})

    async def test_switch_is_refused_while_the_profile_is_working(self):
        self.hold = asyncio.Event()
        await self.service.create_room('Hold this call.', 'bean', 'busy-room')
        while not self.calls:
            await asyncio.sleep(0.01)
        with self.assertRaisesRegex(RoomError, 'partner_account_source_busy'):
            await self.service.set_partner_account_source('bean', 'profile_home')
        self.assertFalse((self.root / 'partners' / 'homes').exists())
        await self.service.set_partner_account_source('claude', 'profile_home')  # another profile is free
        self.hold.set()
        await self.settle()
        await self.service.set_partner_account_source('bean', 'profile_home')
        self.assertEqual(self.partners.get('bean')['account_source']['kind'], 'profile_home')

    async def test_room_removal_purges_each_session_in_its_own_home(self):
        await self.service.set_partner_account_source('bean', 'profile_home')
        room = (await self.service.create_room('Remove me later.', 'bean', 'purge-room'))['room']['id']
        await self.settle()
        seen = []

        def cleanup(runtime, workspace, room_id, targets, **homes):
            seen.append((homes['claude_home'], homes['codex_home'], [target['account_home'] for target in targets]))
            return {'provider_managed_deleted': [{'adapter': t['adapter'], 'session_id': t['session_id']} for t in targets],
                    'already_missing': [], 'pending': [], 'cleanup_confirmed': True}
        with patch('local_agent_coordinator.native_session_purge.purge_native_sessions', side_effect=cleanup):
            await self.service.set_room_lifecycle(room, 'remove', 'purge-request')
            await asyncio.gather(*list(self.service._lifecycle_tasks.values()))
        self.assertEqual(seen, [(self.home, self.home, [str(self.home)])])


if __name__ == '__main__':
    unittest.main()
