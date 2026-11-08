"""Explicit delivery-repository authority over real HTTP/SQLite and temporary Git."""
from contextlib import ExitStack, closing
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from uuid import uuid4

from test_code_delivery import local_github_transport
import test_code_status_http as registered_fixture
import test_code_status_delivery_http as delivery_fixture
import test_room_media_http as media_fixture


class CodeRepositoryBindingHTTPTests(unittest.TestCase):
    def registered(self):
        value = registered_fixture.CodeStatusHTTPTests(methodName='runTest')
        value.setUp()
        self.addCleanup(value.tearDown)
        return value

    def delivered(self):
        value = delivery_fixture.CodeStatusDeliveryHTTPTests(methodName='runTest')
        value.setUp()
        self.addCleanup(value.tearDown)
        value.git(value.delivery_repository, 'restore', 'src/app.py')
        value.control = value.repository('linked evidence')
        control_file = value.control / 'control-check.mp4'
        control_file.write_bytes(value.video + b'fictional evidence')
        # A perfect evidence copy must not stand in for the chosen delivery repository.
        value.write(value.control, 'results/report.md', value.document)
        value.commit(value.control, 'Fictional evidence baseline')
        value.result += f' 对照实验的证据文件为 <{control_file}>。'
        value.write(value.control, 'control-check.mp4', 'Uncommitted evidence is not project delivery authority.\n')
        value.bare = value.root / 'selected-origin.git'
        value.bare.mkdir()
        value.git(value.bare, 'init', '--bare', '--initial-branch=main')
        value.git(value.delivery_repository, 'remote', 'add', 'origin', str(value.bare))
        value.git(value.delivery_repository, 'push', '--set-upstream', 'origin', 'main')
        value.bootstrap()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(local_github_transport(value.bare))
        return value

    def status(self, fixture, *, room_id=None, workspace=None):
        room = room_id or fixture.room_id
        query = '?refresh_remote=1' + ('&workspace=' + workspace if workspace else '')
        code, value = fixture.request('GET', f'/api/rooms/{room}/code-status' + query)
        self.assertEqual(code, 200, value)
        return value

    def bind(self, fixture, workspace_id):
        return fixture.request('POST', f'/api/rooms/{fixture.room_id}/delivery-repository', {'workspace_id': workspace_id})

    @staticmethod
    def business_digest(fixture):
        with closing(sqlite3.connect(fixture.root / 'rooms.sqlite3')) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name NOT LIKE 'sqlite_%' AND name!='room_delivery_repositories' ORDER BY name")]
            return {table: hashlib.sha256(json.dumps(sorted(connection.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '"').fetchall(), key=repr),
                ensure_ascii=False, sort_keys=True).encode()).hexdigest() for table in tables}

    def test_registered_git_is_the_default_authority_without_a_binding_write(self):
        fixture = self.registered()
        before = self.business_digest(fixture)
        value = self.status(fixture)
        binding = value['delivery_repository']
        self.assertEqual((binding['state'], binding['workspace_id'], binding['source']),
                         ('bound', 'project', 'registered_project'))
        self.assertEqual({check['workspace_id'] for check in value['delivery_summary']['main']['checks']}, {'project'})
        self.assertEqual(self.business_digest(fixture), before)
        with closing(sqlite3.connect(fixture.root / 'rooms.sqlite3')) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM room_delivery_repositories').fetchone()[0], 0)
        self.assertEqual(fixture.invocations, [])

    def test_user_binding_preserves_business_data_and_excludes_evidence_from_required_sync(self):
        fixture = self.delivered()
        actual = fixture.delivery_id(fixture.delivery_repository)
        evidence = fixture.delivery_id(fixture.control)
        initial = self.status(fixture)
        self.assertEqual((initial['delivery_repository']['state'], initial['delivery_repository']['reason']),
                         ('unbound', 'delivery_repository_unbound'))
        self.assertEqual({item['workspace_id'] for item in initial['delivery_repository']['candidates']}, {actual, evidence})
        self.assertEqual(initial['delivery_summary']['main']['state'], 'unknown')
        self.assertFalse(initial['delivery_summary']['request']['available'])
        self.assertEqual(initial['delivery_summary']['published']['state'], 'unknown')
        detail = self.status(fixture, workspace=evidence)
        self.assertEqual(detail['workspace_id'], evidence)
        self.assertEqual(detail['delivery_repository']['state'], 'unbound')
        calls = len(fixture.native.calls)
        before = self.business_digest(fixture)
        route = f'/api/rooms/{fixture.room_id}/delivery-repository'
        for body in ({'path': str(fixture.delivery_repository)},
                     {'workspace_id': actual, 'path': str(fixture.control)}):
            self.assertEqual(fixture.request('POST', route, body)[0], 400)
        self.assertEqual(self.bind(fixture, 'delivery:' + '0'*64)[0], 409)
        self.assertEqual(self.bind(fixture, 'native:claude')[0], 409)
        code, denied = fixture.request('POST', route, {'workspace_id': actual}, mcp=True)
        # Capability scope rejects this user-only route before dispatch.
        self.assertEqual(code, 403)
        self.assertIn(denied['error'], {'token_scope_rejected', 'user_delivery_repository_required'})
        self.assertEqual(self.business_digest(fixture), before)
        code, bound = self.bind(fixture, actual)
        self.assertEqual(code, 200, bound)
        self.assertEqual((bound['delivery_repository']['state'], bound['delivery_repository']['source']), ('bound', 'user'))
        selected = self.status(fixture)
        self.assertEqual(selected['workspace_id'], actual)
        summary = selected['delivery_summary']
        self.assertEqual((summary['main']['state'], summary['remote']['state'], summary['workspace']['state']),
                         ('confirmed', 'confirmed', 'confirmed'))
        for scope in ('main', 'remote', 'workspace'):
            self.assertEqual({check['workspace_id'] for check in summary[scope]['checks']}, {actual})
        self.assertEqual(summary['request']['repository_roots'], [str(fixture.delivery_repository)])
        self.assertEqual(summary['request']['targets'], [{'repository_root': str(fixture.delivery_repository),
                                                       'target_branch': 'main'}])
        self.assertIn(evidence, {item['id'] for item in selected['workspaces']})
        self.assertEqual((summary['published']['state'], summary['published']['reason']),
                         ('pending', 'published_work_not_on_mainline'))
        self.assertEqual({key: summary['published']['files'][key] for key in ('total', 'matched', 'missing')},
                         {'total': 1, 'matched': 0, 'missing': 1})
        self.assertEqual(self.business_digest(fixture), before)
        self.assertEqual(len(fixture.native.calls), calls)

        # A normal new commit changes HEAD, not the selected repository identity.
        fixture.write(fixture.delivery_repository, 'new.txt', 'Fictional next commit.\n')
        fixture.commit(fixture.delivery_repository, 'Normal HEAD advance')
        self.assertEqual(self.status(fixture)['delivery_repository']['state'], 'bound')
        media_fixture.RoomMediaHTTPTests.reopen(fixture)
        self.assertEqual(self.status(fixture)['delivery_repository']['workspace_id'], actual)
        self.assertEqual(self.business_digest(fixture), before)
        self.assertEqual(len(fixture.native.calls), calls)

        # A normal, explicitly requested synthetic followup keeps the room grant,
        # even when its new accepted text does not repeat any external path.
        previous = fixture.settled(fixture.room_id)
        fixture.result = '本轮只核对既有交付，不添加外部文件引用。'
        fixture.summary = '核对完成，原交付仓选择保留。'
        code, response = fixture.request('POST', f'/api/rooms/{fixture.room_id}/messages', {
            'text': '继续核对既有交付。', 'target': 'all', 'request_id': str(uuid4()),
            'intent': 'continue', 'binding': previous['continuation']['binding']})
        self.assertEqual(code, 200, response)
        current = fixture.settled(fixture.room_id)
        self.assertNotEqual(current['current_run']['id'], previous['current_run']['id'])
        self.assertEqual(current['objective']['status'], 'complete')
        self.assertEqual(self.status(fixture)['delivery_repository']['workspace_id'], actual)
        self.assertGreater(len(fixture.native.calls), calls)
        calls = len(fixture.native.calls)
        before = self.business_digest(fixture)

        state = fixture.service.store.native_workspace(fixture.room_id, 'claude')
        (Path(state['project']) / 'notes.md').write_text('Fictional unpublished private work.\n')
        pending = self.status(fixture)['delivery_summary']
        self.assertEqual(pending['private']['state'], 'pending')
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(pending['main']['state'], 'confirmed')
        self.assertEqual(self.business_digest(fixture), before)

        # Catches: a new room of the same project starting without the
        # repository the user chose before (the 九烛 room f477ad94), so
        # neither delivery status nor outside work could be seen there.
        other, _ = fixture.service.store.create_room('Separate fictional room', 'claude', str(uuid4()),
            mode='native_project', project_id=fixture.project_id, participants=['claude', 'codex'])
        inherited = self.status(fixture, room_id=other)['delivery_repository']
        self.assertEqual((inherited['state'], inherited['source'], inherited['workspace_id']),
                         ('bound', 'project_history', actual))
        self.assertIsNone(fixture.service.store.delivery_repository(other))
        unrelated, _ = fixture.service.store.create_room('Unrelated fictional room', 'claude', str(uuid4()),
            participants=['claude', 'codex'])
        self.assertEqual(self.status(fixture, room_id=unrelated)['delivery_repository']['state'], 'unbound')
        self.assertEqual(len(fixture.native.calls), calls)

        # A temporarily unreadable repository is not proof it was replaced.
        saved_git = fixture.root / 'temporarily-unavailable-git'
        (fixture.delivery_repository / '.git').rename(saved_git)
        unavailable = self.status(fixture)
        self.assertEqual((unavailable['delivery_repository']['state'], unavailable['delivery_repository']['reason']),
                         ('unknown', 'delivery_repository_unavailable'))
        saved_git.rename(fixture.delivery_repository / '.git')
        self.assertEqual(self.status(fixture)['delivery_repository']['state'], 'bound')

        # Catches: after a macOS reboot the APFS st_dev of the same volume
        # changed (16777231 -> 16777232) and the user's binding was lost.
        binding = fixture.service.store.delivery_repository(fixture.room_id)
        fixture.service.store.bind_delivery_repository(fixture.room_id, {
            **binding, 'identity': {**binding['identity'], 'device': binding['identity']['device'] + 1}})
        self.assertEqual(self.status(fixture)['delivery_repository']['state'], 'bound')
        fixture.service.store.bind_delivery_repository(fixture.room_id, binding)

        # Replacing the Git directory at the same path must not inherit trust.
        (fixture.delivery_repository / '.git').rename(fixture.root / 'previous-selected-git')
        fixture.git(fixture.delivery_repository, 'init', '--initial-branch=main')
        fixture.commit(fixture.delivery_repository, 'Replacement repository')
        changed = self.status(fixture)
        self.assertEqual((changed['delivery_repository']['state'], changed['delivery_repository']['reason']),
                         ('unknown', 'delivery_repository_changed'))
        self.assertEqual(changed['delivery_repository']['workspace_id'], actual)
        self.assertEqual(changed['delivery_summary']['main']['state'], 'unknown')
        self.assertFalse(changed['delivery_summary']['request']['available'])
        self.assertEqual(len(fixture.native.calls), calls)


if __name__ == '__main__':
    unittest.main()
