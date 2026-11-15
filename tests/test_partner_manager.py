import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator.agent_registry import AgentRegistry, AgentSpec, default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager


async def unused(*args, **kwargs):
    raise AssertionError('Inspection must not invoke a model')


class PartnerManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'partners'
        self.base = default_registry(unused)
        self.manager = PartnerManager(self.root, self.base, clock=lambda: 42)

    def test_each_builtin_tool_starts_with_its_own_look(self):
        # Kimi Code used to fall back to Codex's green sprout, so two partners looked alike.
        kimi = self.manager.add(id='kimi', name='Kimi Code', adapter='kimi')
        styles = {profile['id']: profile['bean_style'] for profile in self.manager.profiles()}
        self.assertEqual((styles['claude'], styles['codex'], kimi['bean_style']), ('peach', 'sprout', 'sky'))
        chosen = self.manager.add(id='kimi_cocoa', name='Kimi Code 2', adapter='kimi', bean_style='cocoa')
        self.assertEqual(chosen['bean_style'], 'cocoa', 'an explicit choice still wins')

    def test_defaults_are_inspection_only_and_alias_keeps_real_adapter(self):
        initial = self.manager.profiles()
        self.assertEqual([item['id'] for item in initial], ['claude', 'codex'])
        self.assertTrue(all(item['health']['state'] == 'unknown' for item in initial))
        self.assertFalse((self.root / 'profiles.json').exists())
        created = self.manager.add(id='bean', name='豆豆', adapter='claude', avatar='B')
        self.assertEqual((created['adapter'], created['provider']), ('claude', 'anthropic'))
        self.assertFalse(created['builtin'])
        alias = self.manager.registry().get('bean')
        self.assertIs(alias.invoke, self.base.get('claude').invoke)
        self.assertEqual(alias.normalize_session_id, self.base.get('claude').normalize_session_id)
        self.assertEqual(alias.describe()['capabilities'], {'text_only': True, 'tools': False})
        created['health']['state'] = 'ready'
        self.assertEqual(self.manager.get('bean')['health']['state'], 'unknown')

    def test_atomic_private_persistence_and_disable_preserves_identity(self):
        original = self.manager.add(id='bean', name='豆豆', adapter='codex')
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / 'profiles.json').stat().st_mode), 0o600)
        self.manager.set_enabled('bean', False)
        restored = PartnerManager(self.root, self.base)
        self.assertNotIn('bean', restored.enabled_ids())
        self.assertIsNotNone(restored.registry().get('bean'))
        self.assertEqual(restored.get('bean')['adapter'], original['adapter'])
        self.assertEqual(restored.add(id='bean', name='豆豆', adapter='codex')['enabled'], False)
        restored.set_enabled('bean', True)
        self.assertIn('bean', restored.enabled_ids())
        self.assertEqual(list(self.root.glob('.profiles-*')), [])

    def test_idempotency_no_rename_reuse_adapter_switch_or_untrusted_launch(self):
        original = self.manager.add(id='bean', name='豆豆', adapter='claude')
        self.assertEqual(original, self.manager.add(id='bean', name='豆豆', adapter='claude'))
        for kwargs in ({'name': 'Else', 'adapter': 'claude'}, {'name': '豆豆', 'adapter': 'codex'}):
            with self.assertRaisesRegex(PartnerError, 'partner_identity_conflict'):
                self.manager.add(id='bean', **kwargs)
        for adapter in ('unregistered', '/bin/sh', '', None, {'command': 'x'}):
            with self.assertRaisesRegex(PartnerError, 'unsupported_partner_adapter'):
                self.manager.add(id='new', name='New', adapter=adapter)
        for partner_id in ('../outside', 'all', 'user', 'A'):
            with self.assertRaisesRegex(PartnerError, 'invalid_partner_profile'):
                self.manager.add(id=partner_id, name='New', adapter='claude')
        with self.assertRaisesRegex(PartnerError, 'invalid_partner_enabled'):
            self.manager.set_enabled('bean', 1)
        with self.assertRaisesRegex(PartnerError, 'unknown_partner'):
            self.manager.get('missing')

    def test_custom_base_registry_preserved_without_claiming_verified_adapter(self):
        manager = PartnerManager(Path(self.temp.name).resolve() / 'fake',
                                 AgentRegistry([AgentSpec('helper', 'Helper', unused)]))
        self.assertEqual(manager.registry().ids(), ('helper',))
        self.assertEqual(manager.profiles()[0]['provider'], '')
        with self.assertRaises(PartnerError):
            manager.add(id='new', name='New', adapter='claude')

    def test_bad_storage_and_symlinks_fail_closed_without_reset(self):
        config = self.root / 'profiles.json'
        for bad in ('{', '{}', '{"schema":1,"schema":1,"profiles":[],"checks":{}}'):
            config.write_text(bad)
            config.chmod(0o600)
            with self.assertRaisesRegex(PartnerError, 'invalid_partner_storage'):
                self.manager.profiles()
            self.assertEqual(config.read_text(), bad)
        config.unlink()
        elsewhere = Path(self.temp.name).resolve() / 'elsewhere'
        elsewhere.write_text('private')
        config.symlink_to(elsewhere)
        with self.assertRaisesRegex(PartnerError, 'unsafe_partner_storage'):
            self.manager.profiles()
        config.unlink()
        self.root.rmdir() if not list(self.root.iterdir()) else None
        symlink_root = Path(self.temp.name).resolve() / 'link'
        symlink_root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(PartnerError, 'unsafe_partner_storage'):
            PartnerManager(symlink_root, self.base)

    def test_storage_permissions_and_immutable_builtin_validation(self):
        self.manager.set_enabled('claude', False)
        config = self.root / 'profiles.json'
        config.chmod(0o644)
        with self.assertRaisesRegex(PartnerError, 'unsafe_partner_storage'):
            self.manager.profiles()
        config.chmod(0o600)
        data = json.loads(config.read_text())
        data['profiles'][0]['name'] = 'Changed'
        config.write_text(json.dumps(data))
        with self.assertRaisesRegex(PartnerError, 'invalid_partner_storage'):
            self.manager.registry()

    def test_separate_instances_merge_locked_updates(self):
        other = PartnerManager(self.root, self.base)
        self.manager.add(id='bean', name='Bean', adapter='claude')
        other.add(id='pea', name='Pea', adapter='codex')
        self.assertEqual(self.manager.registry().ids(), ('claude', 'codex', 'bean', 'pea'))

    def test_bean_style_persists_and_conflicts_without_changing_adapter_or_invocation(self):
        created = self.manager.add(id='bean', name='Bean', adapter='claude', bean_style='lavender')
        self.assertEqual(created['bean_style'], 'lavender')
        self.assertEqual((created['adapter'], created['provider']), ('claude', 'anthropic'))
        restored = PartnerManager(self.root, self.base)
        self.assertEqual(restored.registry().get('bean').bean_style, 'lavender')
        self.assertIs(restored.registry().get('bean').invoke, self.base.get('claude').invoke)
        self.assertEqual(restored.add(id='bean', name='Bean', adapter='claude', bean_style='lavender'), created)
        with self.assertRaisesRegex(PartnerError, 'partner_identity_conflict'):
            restored.add(id='bean', name='Bean', adapter='claude', bean_style='cocoa')
        for value in ('unknown', '', None, [], 3):
            with self.assertRaisesRegex(PartnerError, 'invalid_partner_profile'):
                restored.add(id='new', name='New', adapter='claude', bean_style=value)
        self.assertEqual(restored.get('bean')['bean_style'], 'lavender')

    def test_legacy_profiles_gain_style_only_in_memory_until_an_explicit_save(self):
        self.manager.add(id='bean', name='Bean', adapter='claude')
        config = self.root / 'profiles.json'
        old = json.loads(config.read_text())
        for item in old['profiles']:
            del item['bean_style']
        config.write_text(json.dumps(old))
        before = config.read_bytes()
        restored = PartnerManager(self.root, self.base)
        self.assertEqual({item['id']: item['bean_style'] for item in restored.profiles()},
                         {'claude': 'peach', 'codex': 'sprout', 'bean': 'peach'})
        self.assertEqual(config.read_bytes(), before)
        restored.set_enabled('bean', False)
        self.assertTrue(all('bean_style' in item for item in json.loads(config.read_text())['profiles']))

    def test_discovery_is_opt_in_and_unavailable_does_not_probe_local_tools(self):
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', side_effect=AssertionError), \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', side_effect=AssertionError):
            manager = PartnerManager(self.root, self.base)
            observed = manager.discovery()
            self.assertEqual(observed['state'], 'unavailable')
            self.assertIsNone(observed['observed_at'])
            self.assertTrue(all(item['installed'] is None for item in observed['tools']))
            self.assertTrue(all(item['installation']['state'] == 'unknown' for item in manager.profiles()))
            self.assertEqual(manager.available_ids(), ('claude', 'codex'))
            observed['tools'][0]['installed'] = True
            self.assertIsNone(manager.discovery()['tools'][0]['installed'])

    def test_startup_discovery_is_a_cached_installation_only_snapshot_without_persistence(self):
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/virtual/claude') as claude, \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', side_effect=RuntimeError('missing')) as codex, \
             patch('local_agent_coordinator.partner_manager.adapters.find_kimi', side_effect=RuntimeError('missing')) as kimi, \
             patch('local_agent_coordinator.partner_manager.adapters.find_dsh', side_effect=RuntimeError('missing')) as dsh, \
             patch('local_agent_coordinator.partner_manager.adapters._run', side_effect=AssertionError('no CLI execution')), \
             patch('local_agent_coordinator.partner_manager.adapters.doctor', side_effect=AssertionError('no auth checks')):
            manager = PartnerManager(self.root, self.base, clock=lambda: 42, startup_discovery=True)
            self.assertEqual(manager.discovery(), {
                'state': 'completed', 'observed_at': 42, 'scope': 'supported_cli_locations',
                'tools': [{'adapter': 'claude', 'name': 'Claude Code', 'installed': True},
                          {'adapter': 'codex', 'name': 'Codex', 'installed': False},
                          {'adapter': 'kimi', 'name': 'Kimi Code', 'installed': False},
                          {'adapter': 'dsh', 'name': 'DeepSeek Harness', 'installed': False}],
            })
            self.assertEqual(manager.available_ids(), ('claude',))
            self.assertEqual(manager.enabled_ids(), ('claude', 'codex'))
            manager.registry(); manager.profiles(); manager.discovery(); manager.available_ids()
            self.assertEqual(claude.call_count, 1)
            self.assertEqual(codex.call_count, 1)
            self.assertEqual(kimi.call_count, 1)
            self.assertEqual(dsh.call_count, 1)
            profiles = {item['id']: item for item in manager.profiles()}
            self.assertEqual(profiles['claude']['installation'], {'state': 'found'})
            self.assertEqual(profiles['codex']['installation'], {'state': 'missing'})
            self.assertEqual(profiles['claude']['health']['state'], 'unknown')
            self.assertIsNone(profiles['claude']['health']['auth']['subscription_confirmed'])
            self.assertNotIn('/virtual', json.dumps(profiles))
            self.assertEqual(len(profiles), 2)
            self.assertFalse((self.root / 'profiles.json').exists())

    def test_discovery_preserves_disabled_profiles_names_styles_and_existing_config_bytes(self):
        self.manager.add(id='bean', name='My Reviewer', adapter='claude', bean_style='lavender')
        self.manager.set_enabled('claude', False)
        self.manager.set_enabled('bean', False)
        config = self.root / 'profiles.json'
        before = config.read_bytes()
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/virtual/claude'), \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', return_value='/virtual/codex'):
            manager = PartnerManager(self.root, self.base, startup_discovery=True)
        profiles = {item['id']: item for item in manager.profiles()}
        self.assertEqual(set(profiles), {'claude', 'codex', 'bean'})
        self.assertEqual(manager.available_ids(), ('codex',))
        self.assertFalse(profiles['claude']['enabled'])
        self.assertEqual((profiles['bean']['name'], profiles['bean']['bean_style'], profiles['bean']['adapter']),
                         ('My Reviewer', 'lavender', 'claude'))
        self.assertFalse(profiles['bean']['enabled'])
        self.assertEqual(profiles['bean']['installation']['state'], 'found')
        self.assertEqual(config.read_bytes(), before)

    def test_new_startup_not_get_detects_new_installation_without_duplicate_profiles(self):
        self.manager.add(id='bean', name='Bean', adapter='claude')
        before = (self.root / 'profiles.json').read_bytes()
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', side_effect=RuntimeError('missing')) as claude, \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', return_value='/virtual/codex') as codex:
            first = PartnerManager(self.root, self.base, startup_discovery=True)
            self.assertEqual(first.available_ids(), ('codex',))
            claude.side_effect = None
            claude.return_value = '/virtual/new-claude'
            self.assertEqual(first.get('claude')['installation']['state'], 'missing')
            self.assertEqual(claude.call_count, 1)
            second = PartnerManager(self.root, self.base, startup_discovery=True)
            self.assertEqual(second.available_ids(), ('claude', 'codex', 'bean'))
            self.assertEqual(claude.call_count, 2)
            self.assertEqual(codex.call_count, 2)
            self.assertEqual(len(second.profiles()), 3)
            self.assertEqual(first.get('claude')['installation']['state'], 'missing')
        self.assertEqual((self.root / 'profiles.json').read_bytes(), before)

    def test_explicit_open_refresh_detects_installation_preserving_disabled_bytes(self):
        self.manager.set_enabled('claude', False)
        config = self.root / 'profiles.json'
        before = config.read_bytes()
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/virtual/claude'), \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', side_effect=RuntimeError), \
             patch('local_agent_coordinator.partner_manager.adapters._run', side_effect=AssertionError):
            self.manager.refresh_discovery()
            self.assertEqual(self.manager.available_ids(), ())
            self.assertFalse(self.manager.get('claude')['enabled'])
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/virtual/claude'), \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', return_value='/virtual/codex'):
            refreshed = self.manager.refresh_discovery()
            self.assertEqual(self.manager.available_ids(), ('codex',))
            self.assertEqual(refreshed['state'], 'completed')
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(len(self.manager.profiles()), 2)

    def test_connector_templates_stay_unconfigured_while_kimi_restores_as_native(self):
        for adapter in ('deepseek', 'local_json'):
            item = self.manager.add(id=adapter, name=adapter.title(), adapter=adapter)
            self.assertEqual(item['connection_state'], 'not_configured')
            self.assertEqual(item['health']['reason'], 'connection_not_configured')
            self.assertFalse(item['selectable'])
            self.assertNotIn(adapter, self.manager.available_ids())
        native = self.manager.add(id='kimi', name='Kimi', adapter='kimi')
        self.assertEqual(native['connection_state'], 'builtin')
        restored = PartnerManager(self.root, self.base)
        self.assertEqual(restored.get('kimi')['connection_state'], 'builtin')
        self.assertIsNone(restored.get('kimi').get('connection'))
        self.assertIsNotNone(restored.registry().get('kimi'))

    def test_connector_configuration_is_literal_private_and_identity_immutable(self):
        connection = {'protocol': 'beanest-json-v1', 'executable': '/nonexistent/connector',
                      'args': ['--json'], 'billing': 'local'}
        self.manager.add(id='helper', name='Helper', adapter='local_json')
        item = self.manager.configure('helper', connection)
        self.assertEqual(item['connection_state'], 'configured')
        self.assertEqual(item['installation']['state'], 'missing')
        self.assertFalse(item['selectable'])
        self.assertEqual(self.manager.configure('helper', connection), item)
        self.assertEqual(self.manager.add(id='helper', name='Helper', adapter='local_json', connection=connection), item)
        with self.assertRaisesRegex(PartnerError, 'partner_identity_conflict'):
            self.manager.configure('helper', {**connection, 'args': ['--other']})
        with self.assertRaisesRegex(PartnerError, 'invalid_partner_connection'):
            self.manager.add(id='nope', name='Nope', adapter='claude', connection=connection)
        self.manager.set_enabled('helper', False)
        restored = PartnerManager(self.root, self.base)
        self.assertFalse(restored.get('helper')['enabled'])
        self.assertEqual(restored.get('helper')['connection'], connection)

    def test_unexpected_discovery_error_is_unknown_and_does_not_block_other_tools(self):
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', side_effect=PermissionError('private path')), \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', return_value='/virtual/codex'):
            manager = PartnerManager(self.root, self.base, startup_discovery=True)
        observed = manager.discovery()
        self.assertEqual(observed['state'], 'unavailable')
        self.assertIsNone(observed['tools'][0]['installed'])
        self.assertEqual(manager.get('claude')['installation'], {'state': 'unknown'})
        self.assertEqual(manager.get('codex')['installation'], {'state': 'found'})
        self.assertEqual(manager.available_ids(), ('codex',))
        self.assertNotIn('private path', json.dumps(observed))


class PartnerHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'partners'
        self.manager = PartnerManager(self.root, default_registry(unused), clock=lambda: 100.5)

    async def test_codex_real_identity_status_only_and_persisted_sanitized_health(self):
        runner = AsyncMock(side_effect=[([b'codex-cli 0.99.0'], 0),
                                        ([b'Logged in using ChatGPT'], 0)])
        with patch('local_agent_coordinator.partner_manager.adapters.find_codex', return_value='/trusted/codex'), \
             patch('local_agent_coordinator.partner_manager.adapters._run', runner):
            result = await self.manager.check('codex')
        health = result['health']
        self.assertEqual(health['state'], 'ready')
        self.assertEqual(health['version'], '0.99.0')
        self.assertEqual(health['checked_at'], 100.5)
        self.assertEqual(health['auth'], {'method': 'chatgpt', 'subscription_confirmed': True})
        self.assertIsNone(health['cost'])
        self.assertIsNone(health['subscription_remaining'])
        commands = [call.args[0] for call in runner.await_args_list]
        self.assertEqual(commands, [['/trusted/codex', '--version'], ['/trusted/codex', 'login', 'status']])
        stored = (self.root / 'profiles.json').read_text()
        self.assertNotIn('/trusted', stored)
        self.assertNotIn('Logged in', stored)
        with patch('local_agent_coordinator.partner_manager.adapters._run', side_effect=AssertionError):
            self.assertEqual(self.manager.get('codex')['health'], health)

    async def test_claude_auth_whitelist_drops_account_and_secret_fields(self):
        auth = json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty',
                           'subscriptionType': 'max', 'email': 'private@example.invalid', 'token': 'SECRET'}).encode()
        runner = AsyncMock(side_effect=[([b'2.1.0 (Claude Code)'], 0), ([auth], 0)])
        self.manager.add(id='reviewer', name='Reviewer', adapter='claude')
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/trusted/claude'), \
             patch('local_agent_coordinator.partner_manager.adapters._run', runner):
            result = await self.manager.check('reviewer')
        self.assertEqual(result['health']['state'], 'ready')
        self.assertEqual(result['health']['auth']['method'], 'claude.ai')
        self.assertEqual(self.manager.get('claude')['health']['state'], 'unknown')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('private@', (self.root / 'profiles.json').read_text())
        self.assertEqual(runner.await_args_list[-1].args[0][-3:], ['auth', 'status', '--json'])

    async def test_missing_auth_failure_timeout_and_bad_output_are_distinct(self):
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', side_effect=RuntimeError('missing')):
            health = (await self.manager.check('claude'))['health']
        self.assertEqual((health['state'], health['reason'], health['cli_available']),
                         ('unavailable', 'cli_missing', False))
        for outputs, state, reason in (
            ([([b'2.0.0'], 0), ([b'{}'], 0)], 'unavailable', 'auth_unconfirmed'),
            ([([b'not a version'], 0)], 'unknown', 'cli_check_failed'),
            (asyncio.TimeoutError(), 'unknown', 'check_timeout'),
            (OSError('hidden path'), 'unknown', 'check_failed'),
        ):
            runner = AsyncMock(side_effect=outputs)
            with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/trusted/claude'), \
                 patch('local_agent_coordinator.partner_manager.adapters._run', runner):
                health = (await self.manager.check('claude'))['health']
            self.assertEqual((health['state'], health['reason']), (state, reason))

    async def test_cancel_does_not_persist_false_result(self):
        entered = asyncio.Event()
        finished = asyncio.Event()
        async def run(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/trusted/claude'), \
             patch('local_agent_coordinator.partner_manager.adapters._run', run):
            job = asyncio.create_task(self.manager.check('claude'))
            await entered.wait()
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
        self.assertTrue(finished.is_set())
        self.assertEqual(self.manager.get('claude')['health']['state'], 'unknown')
        self.assertFalse((self.root / 'profiles.json').exists())

    async def test_checks_have_bounded_concurrency(self):
        self.manager.add(id='one', name='One', adapter='claude')
        self.manager.add(id='two', name='Two', adapter='claude')
        active = 0
        maximum = 0
        async def run(command, *args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ([b'2.0.0'], 0) if command[-1] == '--version' else ([b'{}'], 0)
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/trusted/claude'), \
             patch('local_agent_coordinator.partner_manager.adapters._run', run):
            await asyncio.gather(*(self.manager.check(key) for key in ('claude', 'one', 'two')))
        self.assertEqual(maximum, 2)


if __name__ == '__main__':
    unittest.main()
