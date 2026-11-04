"""Provider admin deletion only in temporary homes, never real accounts."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator.native_session_purge import purge_native_sessions, run_official_cleanup
from local_agent_coordinator import native_session_purge as purge_module


SID = '01a19888-2e3d-7170-9354-f7013b531eff'
OTHER = '01a1986f-46ea-7131-b652-5bf8f8aac7ad'
STAMP = '2026-11-01T19:13:44.020Z'


class NativeSessionPurgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.runtime = self.root / 'runtime'
        self.workspace = self.runtime / 'workspaces'
        self.room = str(uuid4())
        self.cwd = self.runtime / 'native-workspaces' / self.room / 'codex' / 'project'
        self.cwd.mkdir(parents=True)
        self.workspace.mkdir()
        self.codex = self.root / '.codex'
        self.claude = self.root / '.claude'
        self.codex.mkdir(); self.claude.mkdir()
        self.db = self.codex / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,source TEXT,created_at REAL)')
            db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT)')
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def target(self, adapter='codex', sid=SID, **changes):
        return {'adapter': adapter, 'session_id': sid, 'owned_cwds': [str(self.cwd)],
                'created_after': 1793550000, 'created_before': 1793570000,
                'shared_room_ids': [], **changes}

    def codex_file(self, sid=SID, *, cwd=None, day='2026/11/01'):
        path = self.codex / 'sessions' / day / ('rollout-2026-11-01T12-13-44-' + sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {'id': sid, 'cwd': str(cwd or self.cwd), 'source': 'exec', 'timestamp': STAMP}
        path.write_text(json.dumps({'type': 'session_meta', 'payload': meta}) + '\n' +
                        json.dumps({'type': 'response_item', 'payload': {'private': 'DO_NOT_READ_OR_PERSIST'}}) + '\n')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?)', (sid, str(path), meta['cwd'], 'exec', 1793560424))
        return path

    def claude_file(self, sid=None, **metadata):
        sid = sid or str(uuid4())
        import re
        project = self.claude / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(self.cwd))
        project.mkdir(parents=True, exist_ok=True)
        path = project / (sid + '.jsonl')
        path.write_text(json.dumps({'sessionId': sid, 'cwd': str(self.cwd), 'timestamp': STAMP,
                                    'isSidechain': False, 'message': 'PRIVATE_PROMPT', **metadata}) + '\n')
        return sid, path

    def runner(self, argv, environment):
        self.calls.append((argv, environment))
        if argv[1] == 'delete':
            with closing(sqlite3.connect(self.db)) as db, db:
                row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (argv[-1],)).fetchone()
                if row:
                    Path(row[0]).unlink(missing_ok=True)
                db.execute('DELETE FROM threads WHERE id=?', (argv[-1],))
        else:
            import re
            shutil.rmtree(self.claude / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', argv[-1]))
        return {'exit_code': 0, 'cleanup_confirmed': True}

    def purge(self, targets, **kwargs):
        return purge_native_sessions(self.runtime, self.workspace, self.room, targets,
            codex_home=self.codex, claude_home=self.claude, codex_cli='/mock/codex',
            claude_cli='/mock/claude', run_command=kwargs.pop('run_command', self.runner), **kwargs)

    def test_official_codex_delete_removes_exact_index_and_transcript_keeps_neighbor(self):
        owned = self.codex_file()
        neighbor = self.codex_file(OTHER)
        result = self.purge([self.target()])
        self.assertTrue(result['cleanup_confirmed'], result)
        self.assertFalse(owned.exists())
        self.assertTrue(neighbor.exists())
        self.assertEqual(self.calls, [(['/mock/codex', 'delete', '--force', SID], {'CODEX_HOME': str(self.codex)})])
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(db.execute('SELECT id FROM threads').fetchall(), [(OTHER,)])
        again = self.purge([self.target()])
        self.assertEqual(again['already_missing'], [{'adapter': 'codex', 'session_id': SID}])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_shared_cwd_identity_time_or_symlink_mismatch_never_calls_official_delete(self):
        path = self.codex_file()
        for changes in ({'shared_room_ids': [str(uuid4())]}, {'owned_cwds': [str(self.root)]},
                        {'created_before': 1793550001}):
            with self.subTest(changes=changes):
                result = self.purge([self.target(**changes)])
                self.assertFalse(result['cleanup_confirmed'])
                self.assertTrue(path.exists())
        original = path.read_text()
        path.unlink(); path.symlink_to(self.root / 'unrelated')
        self.assertFalse(self.purge([self.target()])['cleanup_confirmed'])
        self.assertEqual(self.calls, [])
        path.unlink(); path.write_text(original)
        data = json.loads(path.read_text().splitlines()[0]); data['payload']['id'] = OTHER
        path.write_text(json.dumps(data) + '\n')
        self.assertFalse(self.purge([self.target()])['cleanup_confirmed'])
        self.assertEqual(self.calls, [])

    def test_index_row_without_rollout_uses_official_delete_instead_of_leaving_ghost(self):
        path = self.codex_file(); path.unlink()
        result = self.purge([self.target()])
        self.assertTrue(result['cleanup_confirmed'], result)
        self.assertEqual(len(self.calls), 1)

    def test_unregistered_descendant_blocks_but_verified_registered_child_can_be_removed(self):
        root = self.codex_file(); child = self.codex_file(OTHER)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', (SID, OTHER, 'complete'))
        result = self.purge([self.target()])
        self.assertEqual(result['pending'][0]['reason'], 'session_descendants_unconfirmed')
        self.assertTrue(root.exists()); self.assertTrue(child.exists()); self.assertEqual(self.calls, [])
        # The fixture runner deletes only an exact ID; both registered targets
        # are independently validated before either command is permitted.
        result = self.purge([self.target(), self.target(sid=OTHER)])
        self.assertTrue(result['cleanup_confirmed'], result)
        self.assertFalse(root.exists()); self.assertFalse(child.exists())

    def test_new_unknown_descendant_after_initial_audit_blocks_official_delete(self):
        root = self.codex_file(); child = self.codex_file(OTHER)
        original = purge_module._codex_plan
        calls = 0
        def changed(home, target):
            nonlocal calls
            result = original(home, target)
            calls += 1
            if calls == 1:
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', (SID, OTHER, 'complete'))
            return result
        with patch.object(purge_module, '_codex_plan', side_effect=changed):
            result = self.purge([self.target()])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertEqual(result['pending'][0]['reason'], 'session_descendants_unconfirmed')
        self.assertEqual(self.calls, [])
        self.assertTrue(root.exists()); self.assertTrue(child.exists())

    def test_success_exit_without_provider_cleanup_is_not_success(self):
        path = self.codex_file()
        result = self.purge([self.target()], run_command=lambda *_: {'exit_code': 0, 'cleanup_confirmed': True})
        self.assertFalse(result['cleanup_confirmed'])
        self.assertTrue(path.exists())
        result = self.purge([self.target()], run_command=lambda *_: {'exit_code': 0, 'cleanup_confirmed': False})
        self.assertFalse(result['cleanup_confirmed'])

    def test_claude_official_project_purge_requires_every_session_registered(self):
        sid, path = self.claude_file()
        other, neighbor = self.claude_file()
        result = self.purge([self.target('claude', sid)])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertTrue(path.exists()); self.assertTrue(neighbor.exists()); self.assertEqual(self.calls, [])
        result = self.purge([self.target('claude', sid), self.target('claude', other)])
        self.assertTrue(result['cleanup_confirmed'], result)
        self.assertEqual(self.calls, [(['/mock/claude', 'purge', '--yes', str(self.cwd)],
                                       {'CLAUDE_CONFIG_DIR': str(self.claude)})])
        self.assertFalse(path.exists()); self.assertFalse(neighbor.exists())
        self.assertNotIn('PRIVATE_PROMPT', json.dumps(result))

    def test_claude_known_main_companion_tool_results_and_linked_sidechain_are_owned(self):
        sid, path = self.claude_file()
        companion = path.parent / sid
        outputs = companion / 'tool-results'; outputs.mkdir(parents=True)
        (outputs / 'tool.txt').write_text('PRIVATE_TOOL_RESULT_NOT_READ')
        agents = companion / 'subagents'; agents.mkdir()
        (agents / 'agent-a.jsonl').write_text(json.dumps({'sessionId': sid, 'cwd': str(self.cwd),
            'timestamp': STAMP, 'isSidechain': True, 'message': 'PRIVATE_THINKING_NOT_PERSISTED'}) + '\n')
        result = self.purge([self.target('claude', sid)])
        self.assertTrue(result['cleanup_confirmed'], result)
        self.assertFalse(companion.exists())
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_claude_foreign_or_symlink_companion_does_not_gain_ownership_from_directory_name(self):
        sid, path = self.claude_file()
        companion = path.parent / sid
        outputs = companion / 'tool-results'; outputs.mkdir(parents=True)
        original = self.root / 'original.txt'; original.write_text('keep')
        (outputs / 'outside.txt').symlink_to(original)
        self.assertFalse(self.purge([self.target('claude', sid)])['cleanup_confirmed'])
        self.assertEqual(self.calls, []); self.assertTrue(original.exists())
        (outputs / 'outside.txt').unlink()
        agents = companion / 'subagents'; agents.mkdir()
        (agents / 'agent-a.jsonl').write_text(json.dumps({'sessionId': str(uuid4()),
            'cwd': '/Users/person/original-project', 'timestamp': STAMP, 'isSidechain': True}) + '\n')
        self.assertFalse(self.purge([self.target('claude', sid)])['cleanup_confirmed'])
        self.assertEqual(self.calls, [])

    def test_claude_unknown_session_companion_or_project_subagents_is_not_owned(self):
        sid, path = self.claude_file()
        (path.parent / str(uuid4())).mkdir()
        self.assertFalse(self.purge([self.target('claude', sid)])['cleanup_confirmed'])
        self.assertEqual(self.calls, [])

    def test_legacy_static_room_cwd_is_allowed_but_original_project_is_not(self):
        self.cwd = self.workspace / self.room / 'claude'
        self.cwd.mkdir(parents=True)
        sid, _ = self.claude_file()
        self.assertTrue(self.purge([self.target('claude', sid)])['cleanup_confirmed'])
        self.assertFalse(self.purge([self.target('claude', str(uuid4()), owned_cwds=['/Users/person/game'])])['cleanup_confirmed'])

    def test_provider_command_failure_stays_pending_without_raw_diagnostics(self):
        self.codex_file()
        result = self.purge([self.target()], run_command=lambda *_: {'exit_code': 1, 'cleanup_confirmed': True,
                                                                     'stderr': 'PRIVATE_DIAGNOSTIC'})
        self.assertEqual(result['pending'][0]['reason'], 'official_delete_unconfirmed')
        self.assertNotIn('PRIVATE_DIAGNOSTIC', json.dumps(result))

    def test_claude_unknown_index_entry_or_missing_transcript_never_reports_complete(self):
        sid, path = self.claude_file()
        index = path.parent / 'sessions-index.json'
        index.write_text(json.dumps({'entries': [{'sessionId': str(uuid4())}]}))
        result = self.purge([self.target('claude', sid)])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertTrue(path.exists()); self.assertEqual(self.calls, [])
        path.unlink()
        self.assertFalse(self.purge([self.target('claude', sid)])['cleanup_confirmed'])

    def test_claude_unrecognized_project_entries_prevent_whole_project_purge(self):
        sid, path = self.claude_file()
        for kind in ('file', 'directory', 'symlink'):
            with self.subTest(kind=kind):
                unknown = path.parent / 'unregistered-notes'
                if kind == 'file':
                    unknown.write_text('unowned data')
                elif kind == 'directory':
                    unknown.mkdir()
                else:
                    unknown.symlink_to(self.root / 'unrelated')
                result = self.purge([self.target('claude', sid)])
                self.assertFalse(result['cleanup_confirmed'], result)
                self.assertEqual(result['pending'][0]['reason'], 'session_project_contents_unconfirmed')
                self.assertEqual(self.calls, [])
                self.assertTrue(path.exists())
                unknown.rmdir() if kind == 'directory' else unknown.unlink()

    def test_claude_index_must_be_a_bounded_unshared_regular_file(self):
        sid, path = self.claude_file()
        index = path.parent / 'sessions-index.json'
        for kind in ('fifo', 'directory', 'hardlink', 'oversized'):
            with self.subTest(kind=kind):
                if kind == 'fifo':
                    os.mkfifo(index)
                elif kind == 'directory':
                    index.mkdir()
                elif kind == 'hardlink':
                    os.link(path, index)
                else:
                    with index.open('wb') as stream:
                        stream.truncate(2 * 1024 * 1024 + 1)
                result = self.purge([self.target('claude', sid)])
                self.assertFalse(result['cleanup_confirmed'], result)
                self.assertEqual(self.calls, [])
                self.assertTrue(path.exists())
                index.rmdir() if kind == 'directory' else index.unlink()

    def test_codex_timezone_boundary_day_is_scoped_by_exact_uuid(self):
        path = self.codex_file(day='2026/11/02')
        result = self.purge([self.target()])
        self.assertTrue(result['cleanup_confirmed'], result)
        self.assertFalse(path.exists())

    def test_codex_index_second_precision_still_checks_exact_rollout_time(self):
        path = self.codex_file()
        data = json.loads(path.read_text().splitlines()[0])
        data['payload']['timestamp'] = '2026-11-01T19:13:44.700Z'
        path.write_text(json.dumps(data) + '\n')
        target = self.target(created_after=1793560424.5)
        self.assertTrue(self.purge([target])['cleanup_confirmed'])
        path = self.codex_file()
        result = self.purge([target])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertEqual(result['pending'][0]['reason'], 'session_time_mismatch')
        self.assertTrue(path.exists())
        self.assertEqual(len(self.calls), 1)

    def test_conflicting_duplicate_ownership_evidence_cannot_delete_the_session(self):
        path = self.codex_file()
        result = self.purge([self.target(), self.target(shared_room_ids=[str(uuid4())]), self.target()])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertTrue(path.exists())
        self.assertEqual(self.calls, [])

    def test_owned_cwd_symlink_cannot_redirect_official_project_purge(self):
        sid, path = self.claude_file()
        original = self.root / 'original-user-project'
        original.mkdir()
        marker = original / 'keep.txt'; marker.write_text('Original project stays untouched.')
        self.cwd.rmdir(); self.cwd.symlink_to(original, target_is_directory=True)
        result = self.purge([self.target('claude', sid)])
        self.assertFalse(result['cleanup_confirmed'])
        self.assertEqual(self.calls, [])
        self.assertTrue(marker.exists()); self.assertTrue(path.exists())

    def test_managed_admin_runner_executes_only_fixed_subcommand_and_filters_environment(self):
        stub = self.root / 'mock-codex'
        record = self.codex / 'admin-call.json'
        stub.write_text(f'#!{sys.executable}\nimport os,sys,json\nfrom pathlib import Path\n'
                        'Path(os.environ["CODEX_HOME"],"admin-call.json").write_text(json.dumps(sys.argv[1:]))\n')
        stub.chmod(0o700)
        outcome = run_official_cleanup([str(stub), 'delete', '--force', SID], {'CODEX_HOME': str(self.codex)})
        self.assertEqual(outcome, {'exit_code': 0, 'cleanup_confirmed': True})
        self.assertEqual(json.loads(record.read_text()), ['delete', '--force', SID])
        for argv, env in (([str(stub), 'exec', '-p', 'model prompt'], {'CODEX_HOME': str(self.codex)}),
                          ([str(stub), 'delete', '--force', SID], {'HOME': str(self.root)})):
            with self.assertRaises(ValueError):
                run_official_cleanup(argv, env)
