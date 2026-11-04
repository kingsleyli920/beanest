"""Partner appearance updates through HTTP preserve execution and identity."""
import json
import sqlite3
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters
from local_agent_coordinator.partner_manager import PartnerManager
from test_kimi_setup_http import SetupFixture
from test_kimi_native_setup import configuration


STYLE_IDS = {'sprout', 'peach', 'sky', 'lavender', 'sunshine', 'cocoa', 'mint', 'coral', 'ocean', 'rose'}


class BeanAppearanceHTTPTests(unittest.TestCase):
    def fixture(self):
        fixture = SetupFixture(discover=False)
        self.addCleanup(fixture.close)
        fixture.config.write_text(configuration(), encoding='utf-8')
        return fixture

    def profile(self, fixture):
        code, value = fixture.request('GET', '/api/partners')
        self.assertEqual(code, 200)
        return next(item for item in value['partners'] if item['id'] == 'kimi')

    @staticmethod
    def stored_profiles(fixture):
        return json.loads((fixture.partners.root / 'profiles.json').read_text())

    @staticmethod
    def business_rows(fixture):
        with sqlite3.connect(fixture.root / 'rooms.sqlite3') as db:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {name: sorted(db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr)
                    for name in tables}

    def test_all_ten_styles_persist_and_invalid_bodies_do_not_change_any_profile(self):
        fixture = self.fixture()
        original = self.profile(fixture)
        code, catalogue = fixture.request('GET', '/api/partners')
        self.assertEqual(code, 200)
        self.assertEqual({item['id'] for item in catalogue['bean_styles']}, STYLE_IDS)
        with patch.object(adapters, 'account_status', side_effect=AssertionError('appearance must not query accounts')):
            for style in sorted(STYLE_IDS):
                code, value = fixture.request('POST', '/api/partners/kimi/appearance', {'bean_style': style})
                self.assertEqual(code, 200)
                actual = next(item for item in value['partners'] if item['id'] == 'kimi')
                self.assertEqual(actual['bean_style'], style)
                self.assertEqual({key: item for key, item in actual.items() if key != 'bean_style'},
                                 {key: item for key, item in original.items() if key != 'bean_style'})
                self.assertEqual(self.profile(fixture)['bean_style'], style)
                reloaded = PartnerManager(fixture.partners.root, fixture.partners.base_registry)
                self.assertEqual(reloaded.get('kimi')['bean_style'], style)
            saved = self.stored_profiles(fixture)
            for body in ({'bean_style': 'not-a-style'}, {'bean_style': None}, {'bean_style': True}, {},
                         {'bean_style': 'mint', 'enabled': False}, {'bean_style': 'rose', 'adapter': 'codex'}):
                with self.subTest(body=body):
                    code, _ = fixture.request('POST', '/api/partners/kimi/appearance', body)
                    self.assertEqual(code, 400)
                    self.assertEqual(self.stored_profiles(fixture), saved)
            self.assertEqual(fixture.request('POST', '/api/partners/absent/appearance', {'bean_style': 'mint'})[0], 404)
            self.assertEqual(self.stored_profiles(fixture), saved)
        self.assertEqual(fixture.calls, [])
        self.assertFalse((fixture.root / 'login-starts').exists())

    def test_changing_skin_during_a_real_held_job_does_not_write_room_history_or_restart_it(self):
        fixture = self.fixture()
        code, project = fixture.request('POST', '/api/projects', {
            'name': 'Disposable appearance fixture', 'path': str(fixture.source), 'selection_kind': 'folder'})
        self.assertEqual(code, 200)
        code, value = fixture.request('POST', '/api/rooms', {
            'goal': 'Hold one fictional invocation while presentation changes.', 'request_id': str(uuid4()),
            'target': 'kimi', 'participants': ['kimi'], 'mode': 'native_project',
            'project_id': project['project']['id'], 'work_mode': 'build',
            'runtime_settings': {'permission': 'full_access', 'members': {}},
            'execution_policy': {'mode': 'autonomous'}})
        self.assertEqual(code, 200)
        room_id = value['room']['id']
        deadline = time.monotonic() + 5
        while not fixture.calls and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(fixture.calls), 1)
        before_rows = self.business_rows(fixture)
        before_calls = list(fixture.calls)
        before_profiles = self.stored_profiles(fixture)
        with patch.object(adapters, 'account_status', side_effect=AssertionError('appearance must not query accounts')):
            code, changed = fixture.request('POST', '/api/partners/kimi/appearance', {'bean_style': 'ocean'})
            self.assertEqual(code, 200)
            self.assertEqual(next(item for item in changed['partners'] if item['id'] == 'kimi')['bean_style'], 'ocean')
            self.assertEqual(self.profile(fixture)['bean_style'], 'ocean')
        after_profiles = self.stored_profiles(fixture)
        for profile in after_profiles['profiles']:
            if profile['id'] == 'kimi':
                profile['bean_style'] = next(item['bean_style'] for item in before_profiles['profiles'] if item['id'] == 'kimi')
        self.assertEqual(after_profiles, before_profiles)
        self.assertEqual(self.business_rows(fixture), before_rows)
        self.assertEqual(fixture.calls, before_calls)
        self.assertFalse((fixture.root / 'login-starts').exists())
        code, snapshot = fixture.request('GET', '/api/rooms/' + room_id)
        self.assertEqual(code, 200)
        self.assertTrue(any(job['status'] == 'working' for job in snapshot['jobs']))
        self.assertEqual(fixture.request('POST', '/api/rooms/' + room_id + '/cancel', {})[0], 200)


if __name__ == '__main__':
    unittest.main()
