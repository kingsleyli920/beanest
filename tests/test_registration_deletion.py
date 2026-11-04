import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator.agent_registry import AgentRegistry, default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.project_catalog import ProjectCatalog, ProjectCatalogError


async def unused(*args, **kwargs):
    raise AssertionError('Configuration changes must not invoke a model')


class RegistrationDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = default_registry(unused)
        self.manager = PartnerManager(self.root / 'partners', self.base)
        self.catalog = ProjectCatalog(self.root / 'catalog')

    def test_project_remove_deletes_registration_keeps_source_and_neighbor(self):
        source = self.root / 'original'
        source.mkdir()
        asset = source / 'asset.bin'
        asset.write_bytes(b'original asset')
        first = self.catalog.register_folder('First', str(source))
        second = self.catalog.register_folder('Neighbor', str(source))
        result = self.catalog.remove_registration(first['id'])
        self.assertEqual(result, {'id': first['id'], 'deleted': True})
        self.assertEqual(self.catalog.list(), [second])
        self.assertEqual(asset.read_bytes(), b'original asset')
        self.assertNotIn(first['id'], (self.root / 'catalog' / 'catalog.json').read_text())
        self.assertEqual(self.catalog.remove_registration(first['id']), result)
        with self.assertRaisesRegex(ProjectCatalogError, 'project_not_found'):
            self.catalog.set_lifecycle(first['id'], 'restore')

    def test_project_archive_remains_reversible_and_old_remove_stays_compatible(self):
        source = self.root / 'original'
        source.mkdir()
        project = self.catalog.register_folder('Project', str(source))
        self.assertEqual(self.catalog.set_lifecycle(project['id'], 'archive')['lifecycle_state'], 'archived')
        self.assertEqual(self.catalog.set_lifecycle(project['id'], 'restore')['lifecycle_state'], 'active')
        self.assertEqual(self.catalog.set_lifecycle(project['id'], 'remove')['lifecycle_state'], 'removed')
        self.assertEqual(self.catalog.set_lifecycle(project['id'], 'restore')['lifecycle_state'], 'active')

    def test_profile_remove_erases_custom_configuration_and_check_no_identity_reuse(self):
        profile = self.manager.add(id='custom', name='Private Custom Name', adapter='claude')
        health = {**self.manager.get('custom')['health'], 'checked_at': 42}
        with self.manager._locked():
            data = self.manager._load()
            data['checks']['custom'] = health
            self.manager._save(data)
        self.assertEqual(self.manager.remove_profile('custom'), {'id': 'custom', 'deleted': True})
        self.assertEqual(self.manager.remove_profile('custom'), {'id': 'custom', 'deleted': True})
        stored = json.loads((self.manager.root / 'profiles.json').read_text())
        self.assertNotIn('Private Custom Name', json.dumps(stored))
        self.assertNotIn('custom', stored['checks'])
        self.assertEqual(stored['retired_profile_ids'], ['custom'])
        self.assertIsNone(self.manager.registry().get('custom'))
        with self.assertRaisesRegex(PartnerError, 'partner_identity_conflict'):
            self.manager.add(id='custom', name='Different Identity', adapter='codex')
        new = self.manager.add(id='another', name='Another', adapter='claude')
        self.assertEqual(new['adapter'], profile['adapter'])

    def test_deleted_builtin_not_resurrected_on_restart_or_scan_can_explicitly_restore(self):
        self.manager.remove_profile('claude')
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/mock/claude'), \
                patch('local_agent_coordinator.partner_manager.adapters.find_codex', return_value='/mock/codex'):
            reopened = PartnerManager(self.manager.root, self.base, startup_discovery=True)
            reopened.refresh_discovery()
        self.assertEqual([item['id'] for item in reopened.profiles()], ['codex'])
        self.assertIsNone(reopened.registry().get('claude'))
        restored = reopened.restore_builtin('claude')
        self.assertTrue(restored['builtin'])
        self.assertEqual(restored['id'], 'claude')
        self.assertEqual(reopened.restore_builtin('claude'), restored)
        self.assertEqual(json.loads((reopened.root / 'profiles.json').read_text())['ignored_builtin_ids'], [])

    def test_all_profiles_can_be_deleted_and_registry_remains_inspectable(self):
        for identifier in self.base.ids():
            self.manager.remove_profile(identifier)
        reopened = PartnerManager(self.manager.root, self.base)
        self.assertEqual(reopened.profiles(), [])
        self.assertEqual(reopened.registry().ids(), ())
        self.assertEqual(reopened.available_ids(), ())
        with self.assertRaisesRegex(ValueError, 'invalid_agent_registry'):
            AgentRegistry([])
        self.assertEqual(AgentRegistry([], allow_empty=True).describe(), [])
        self.assertEqual(reopened.restore_builtin('codex')['id'], 'codex')

    def test_legacy_storage_read_does_not_rewrite_and_next_delete_upgrades_preferences(self):
        self.manager.set_enabled('claude', False)
        config = self.manager.root / 'profiles.json'
        old = json.loads(config.read_text())
        old.pop('ignored_builtin_ids')
        old.pop('retired_profile_ids')
        config.write_text(json.dumps(old))
        raw = config.read_bytes()
        self.manager.profiles()
        self.assertEqual(config.read_bytes(), raw)
        self.manager.remove_profile('claude')
        self.assertEqual(json.loads(config.read_text())['ignored_builtin_ids'], ['claude'])

    def test_builtin_archive_and_old_soft_remove_remain_reversible(self):
        for action, state in [('archive', 'archived'), ('remove', 'removed')]:
            self.assertEqual(self.manager.set_lifecycle('claude', action)['lifecycle_state'], state)
            self.assertEqual(self.manager.set_lifecycle('claude', 'restore')['lifecycle_state'], 'active')

    def test_connector_configuration_erased_late_check_cannot_resurrect(self):
        connection = {'protocol': 'beanest-json-v1', 'executable': '/nonexistent/private-connector',
                      'args': [], 'billing': 'local'}
        profile = self.manager.add(id='connector', name='Private Connector', adapter='local_json', connection=connection)
        health = {**profile['health'], 'checked_at': 42}
        self.manager.remove_profile('connector')
        config = self.manager.root / 'profiles.json'
        before = config.read_bytes()
        with self.assertRaisesRegex(PartnerError, 'unknown_partner'):
            self.manager._save_check('connector', health)
        self.assertEqual(config.read_bytes(), before)
        self.assertNotIn('private-connector', before.decode())

    def test_deleting_last_adapter_profile_clears_only_its_in_memory_account_observation(self):
        self.manager.add(id='alias', name='Alias', adapter='claude')
        self.manager._account_cache['claude'] = {'test': 'private observation'}
        self.manager._account_cache['codex'] = {'test': 'neighbor observation'}
        self.manager.remove_profile('claude')
        self.assertIn('claude', self.manager._account_cache)
        self.manager.remove_profile('alias')
        self.assertNotIn('claude', self.manager._account_cache)
        self.assertIn('codex', self.manager._account_cache)

    def test_invalid_delete_ids_do_not_modify_storage(self):
        self.manager.set_enabled('claude', False)
        before = (self.manager.root / 'profiles.json').read_bytes()
        for identifier in ('../escape', '', 'all', None):
            with self.assertRaises(PartnerError):
                self.manager.remove_profile(identifier)
            with self.assertRaises(ProjectCatalogError):
                self.catalog.remove_registration(identifier)
        self.assertEqual((self.manager.root / 'profiles.json').read_bytes(), before)
