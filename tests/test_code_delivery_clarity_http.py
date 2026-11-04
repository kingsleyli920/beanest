"""Read-only code-card dimensions across actual HTTP/Git/private-tree boundaries."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import unittest
from uuid import uuid4

from local_agent_coordinator import native_workspace
from test_code_delivery import local_github_transport
import test_code_status_http as http_fixture
import test_native_workspace as native_fixture


class CodeDeliveryClarityHTTPTests(unittest.TestCase):
    committed_at = http_fixture.CodeStatusHTTPTests.committed_at
    git = http_fixture.CodeStatusHTTPTests.git
    repository = http_fixture.CodeStatusHTTPTests.repository
    write = http_fixture.CodeStatusHTTPTests.write
    commit = http_fixture.CodeStatusHTTPTests.commit
    request = http_fixture.CodeStatusHTTPTests.request
    _close_server = http_fixture.CodeStatusHTTPTests._close_server
    tearDown = http_fixture.CodeStatusHTTPTests.tearDown

    def setUp(self):
        http_fixture.CodeStatusHTTPTests.setUp(self)
        self.git(self.source, 'restore', '--staged', 'tracked.txt')
        self.git(self.source, 'restore', 'tracked.txt')
        (self.source / '未跟踪 说明.txt').unlink()
        self.git(self.source, 'push', 'origin', 'main')
        self.git_only_room = self.room_id
        self.room_id, _ = self.service.store.create_room('Inspect fictional code delivery clarity', 'claude', str(uuid4()),
            mode='native_project', project_id=self.project_id, participants=['claude', 'codex'])
        self.service._project(self.room_id, create=True)
        self.baseline_note = (self.source / 'notes.md').read_text()
        exported = native_fixture.NativeWorkspaceTests.export(self, 'clarity-export', {'notes.md': self.baseline_note})
        self.private_state = native_workspace.prepare_workspace(self.root / 'clarity-private', *exported)
        self.service.store.save_native_workspace(self.room_id, 'claude', self.private_state)
        self.private = Path(self.private_state['project'])
        self.missing_private = self.private.with_name('temporarily-unavailable-project')
        self.transport = ExitStack()
        self.transport.enter_context(local_github_transport(self.bare_upstream))
        self.addCleanup(self.transport.close)
        self.current_case = 'known'

    def select_case(self, case):
        if not self.private.exists():
            self.missing_private.rename(self.private)
        (self.private / 'notes.md').write_text(self.baseline_note)
        if case == 'unavailable':
            self.private.rename(self.missing_private)
        elif case == 'pending':
            (self.private / 'notes.md').write_text('Fictional unpublished private work.\n')
        elif case != 'known':
            raise ValueError('unknown fixture case')
        self.current_case = case

    def summary(self, room_id=None):
        selected = room_id or self.room_id
        code, response = self.request('GET', f'/api/rooms/{selected}/code-status?refresh_remote=1')
        self.assertEqual(code, 200, response)
        return response['delivery_summary']


    def test_known_main_and_remote_keep_private_unknown_and_untracked_decision_separate(self):
        self.select_case('unavailable')
        self.write(self.source, 'optional-note.txt', 'Fictional untracked item needing a user decision.\n')
        unknown = self.summary()
        self.assertEqual((unknown['main']['state'], unknown['remote']['state']), ('confirmed', 'confirmed'))
        self.assertEqual(unknown['private'], {'state': 'unknown', 'reason': 'private_workspace_unavailable'})
        self.assertEqual(unknown['state'], 'unknown')
        self.assertEqual(unknown['workspace']['untracked'], 1)
        self.assertTrue(any(issue['scope'] == 'private' and issue['reason'] == 'private_workspace_unavailable'
                            and issue.get('workspace_id') == 'native:claude' for issue in unknown['issues']))
        self.select_case('known')
        known = self.summary()
        self.assertEqual((known['main']['state'], known['remote']['state'], known['private']['state']),
                         ('confirmed', 'confirmed', 'confirmed'))
        self.assertEqual((known['state'], known['reason']), ('needs_attention', 'untracked_items_need_decision'))
        (self.source / 'optional-note.txt').unlink()
        git_only = self.summary(self.git_only_room)
        self.assertEqual(git_only['private'], {'state': 'confirmed', 'reason': None})
        self.assertEqual(git_only['coverage']['private_workspaces'], 0)
        self.assertEqual(git_only['state'], 'confirmed')
        self.assertEqual(self.invocations, [])
        self.assertFalse((self.source / '.git/FETCH_HEAD').exists())

    def test_actual_unpublished_private_edit_stays_pending_while_main_and_remote_are_known(self):
        self.select_case('pending')
        pending = self.summary()
        self.assertEqual((pending['main']['state'], pending['remote']['state']), ('confirmed', 'confirmed'))
        self.assertEqual(pending['private'], {'state': 'pending', 'reason': 'private_work_not_published'})
        self.assertEqual((pending['state'], pending['reason']), ('pending', 'private_work_not_published'))
        self.assertEqual({key: pending['workspace'][key] for key in ('tracked', 'untracked')},
                         {'tracked': 0, 'untracked': 0})
        self.assertEqual(pending['workspace']['state'], 'confirmed')
        self.assertTrue(any(issue['scope'] == 'private' and issue['state'] == 'pending'
                            and issue.get('workspace_id') == 'native:claude' for issue in pending['issues']))
        self.assertEqual(self.invocations, [])
        self.assertEqual((self.private / 'notes.md').read_text(), 'Fictional unpublished private work.\n')
        self.assertFalse((self.source / '.git/FETCH_HEAD').exists())


    def test_pending_and_unknown_private_reasons_both_survive_with_untracked_items(self):
        self.select_case('unavailable')
        exported = native_fixture.NativeWorkspaceTests.export(self, 'second-private-export', {'notes.md': self.baseline_note})
        second = native_workspace.prepare_workspace(self.root / 'second-private', *exported)
        (Path(second['project']) / 'notes.md').write_text('Fictional second owner unpublished bytes.\n')
        self.service.store.save_native_workspace(self.room_id, 'codex', second)
        self.write(self.source, 'optional-note.txt', 'Fictional untracked item.\n')
        mixed = self.summary()
        self.assertEqual((mixed['main']['state'], mixed['remote']['state']), ('confirmed', 'confirmed'))
        self.assertEqual((mixed['private']['state'], mixed['state']), ('pending', 'pending'))
        self.assertEqual(mixed['workspace']['untracked'], 1)
        issues = {(issue['scope'], issue['state'], issue['reason'], issue.get('workspace_id')) for issue in mixed['issues']}
        self.assertIn(('private', 'unknown', 'private_workspace_unavailable', 'native:claude'), issues)
        self.assertIn(('private', 'pending', 'private_work_not_published', 'native:codex'), issues)
        self.assertEqual(self.invocations, [])


def fixture_main():
    case = CodeDeliveryClarityHTTPTests()
    case.setUp()
    try:
        case.select_case('unavailable')
        print(json.dumps({'url': case.server.url, 'room_id': case.room_id, 'root': str(case.root),
                          'case': case.current_case, 'summary': case.summary(),
                          'real_model_calls': 0, 'commands': ['known', 'unavailable', 'pending', 'status', 'stop']},
                         ensure_ascii=False), flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == 'stop':
                break
            if command in {'known', 'unavailable', 'pending'}:
                case.select_case(command)
            if command in {'known', 'unavailable', 'pending', 'status'}:
                print(json.dumps({'case': case.current_case, 'summary': case.summary()}, ensure_ascii=False), flush=True)
    finally:
        root = case.root
        case.tearDown()
        print(json.dumps({'closed': True, 'temporary_removed': not root.exists()}), flush=True)


if __name__ == '__main__':
    if '--fixture' in sys.argv:
        fixture_main()
    else:
        unittest.main()
