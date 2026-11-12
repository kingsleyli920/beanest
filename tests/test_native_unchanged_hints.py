"""Optional hints through real service/private trees and synthetic native transport."""
import hashlib
from pathlib import Path
import unittest

import test_native_room_service as native
import test_native_workspace as workspace
from local_agent_coordinator.native_workspace import collect_changes


class OptionalNativeHintsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    create = native.NativeRoomServiceTests.create
    handler = native.NativeRoomServiceTests.handler

    async def run_work(self, mutate):
        original = self.handler
        def handler(agent, context, cwd):
            if context['job']['job_phase'] != 'work':
                return original(agent, context, cwd)
            reply = native.native_reply(context, [{'type': 'complete', 'task_id': 'T1',
                'result': 'Checked the actual owned files.'}])
            mutate(cwd, reply)
            return reply
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        return await self.service.read_room(created['room']['id'])

    def assert_not_published(self, result):
        self.assertNotEqual(result['objective']['status'], 'complete')
        self.assertEqual(self.service._project(result['room']['id']).snapshot()['revision'], 1)
        self.assertEqual((self.source / 'main.py').read_text(), 'VALUE = 1\n')

    async def test_complete_imports_real_change_without_notes_for_other_allowed_paths(self):
        result = await self.run_work(lambda cwd, reply: (cwd / 'main.py').write_text('VALUE = 2\n'))
        self.assertEqual(result['objective']['status'], 'complete')
        self.assertEqual(result['team']['tasks'][0]['status'], 'accepted')
        self.assertEqual(len([c for c in self.fake.calls if c['context']['job']['job_phase'] == 'work']), 1)
        self.assertFalse(any(e['kind'] == 'format_correction_queued' for e in result['events']))
        self.assertFalse(any(j['error'] == 'native_unchanged_reason_required' for j in result['jobs']))
        actual = self.service._project(result['room']['id']).files_for_paths(['main.py', 'notes.md'])
        self.assertEqual({f['path']: f['content'] for f in actual},
                         {'main.py': 'VALUE = 2\n', 'notes.md': 'Original notes.\n'})
        self.assertEqual((self.source / 'main.py').read_text(), 'VALUE = 1\n')
        self.assertIn('unchanged_paths may be empty', next(c['prompt'] for c in self.fake.calls
            if c['context']['job']['job_phase'] == 'work'))

    async def test_no_changes_can_be_reviewed_without_fabricated_hints(self):
        original = self.handler
        def handler(agent, context, cwd):
            if context['job']['job_phase'] == 'review':
                self.assertEqual((cwd / 'main.py').read_text(), 'VALUE = 1\n')
                return native.native_reply(context, [{'type': 'review', 'task_id': 'T1',
                    'verdict': 'accept', 'reason': 'The requested preservation is already satisfied.'}])
            return original(agent, context, cwd)
        self.handler = handler
        result = await self.run_work(lambda cwd, reply: None)
        self.assertEqual(result['objective']['status'], 'complete')
        self.assertEqual(result['team']['tasks'][0]['status'], 'accepted')
        self.assertEqual(self.service._project(result['room']['id']).snapshot()['revision'], 1)

    async def test_hint_cannot_hide_real_owned_change(self):
        def mutate(cwd, reply):
            (cwd / 'main.py').write_text('VALUE = 2\n')
            reply['unchanged_paths'] = [{'path': 'main.py', 'reason': 'Incorrect hint.'}]
        result = await self.run_work(mutate)
        self.assertEqual(result['objective']['status'], 'complete')
        file = next(f for f in result['objective']['delivery']['files'] if f['path'] == 'main.py')
        self.assertEqual(file['sha256'], hashlib.sha256(b'VALUE = 2\n').hexdigest())
        self.assertTrue(file['verified'])

    async def test_hint_outside_owned_scope_is_dropped_without_stopping_the_room(self):
        # Catches: a harmless "left this other file alone" note stopping the whole objective,
        # while the owned change still publishes and the outside path gains nothing.
        def mutate(cwd, reply):
            (cwd / 'main.py').write_text('VALUE = 2\n')
            reply['unchanged_paths'] = [{'path': 'outside.txt', 'reason': 'Not owned.'}]
        result = await self.run_work(mutate)
        self.assertEqual(result['objective']['status'], 'complete', result['team'])
        self.assertIsNone(result['team']['stop_reason'])
        paths = [f['path'] for f in result['objective']['delivery']['files']]
        self.assertIn('main.py', paths)
        self.assertNotIn('outside.txt', paths)

    async def test_malformed_supplied_hint_still_rejects(self):
        def mutate(cwd, reply):
            (cwd / 'main.py').write_text('VALUE = 2\n')
            reply['unchanged_paths'] = [{'path': 'notes.md', 'reason': ''}]
        result = await self.run_work(mutate)
        self.assert_not_published(result)
        self.assertTrue(any(j['error'] == 'invalid_agent_response' for j in result['jobs']))

    async def test_omitting_required_schema_key_still_rejects(self):
        def mutate(cwd, reply):
            (cwd / 'main.py').write_text('VALUE = 2\n')
            del reply['unchanged_paths']
        result = await self.run_work(mutate)
        self.assert_not_published(result)
        self.assertTrue(any(j['error'] == 'invalid_agent_response' for j in result['jobs']))

    async def test_actual_unowned_change_stays_private(self):
        # Catches an ownership breach being imported now that deletions and binaries are.
        def mutate(cwd, reply):
            (cwd / 'main.py').write_text('VALUE = 2\n')
            (cwd / 'outside.txt').write_text('unauthorized')
        result = await self.run_work(mutate)
        self.assert_not_published(result)
        reasons = {b['reason'] for state in result['native_workspaces'] for b in state['blocked']}
        self.assertIn('native_path_not_owned', reasons)

    async def test_owned_deletion_and_binary_changes_are_imported(self):
        # Catches an owned file's deletion or binary rewrite still stopping the room.
        for change in ('deletion', 'binary'):
            with self.subTest(change=change):
                def mutate(cwd, reply):
                    (cwd / 'main.py').write_text('VALUE = 2\n')
                    if change == 'deletion':
                        (cwd / 'notes.md').unlink()
                    else:
                        (cwd / 'notes.md').write_bytes(b'\x00\x01')
                result = await self.run_work(mutate)
                self.assertEqual(result['objective']['status'], 'complete', result['team'])
                self.assertEqual({b['reason'] for state in result['native_workspaces'] for b in state['blocked']}, set())
                project = self.service._project(result['room']['id'])
                notes = project.metadata_for_paths(['notes.md'])
                if change == 'deletion':
                    self.assertEqual(notes, [])
                else:
                    self.assertEqual([(f['text'], f['sha256']) for f in notes],
                                     [(False, hashlib.sha256(b'\x00\x01').hexdigest())])
                self.assertEqual((self.source / 'notes.md').read_text(), 'Original notes.\n')


class NativeHintConflictTests(unittest.TestCase):
    setUp = workspace.NativeWorkspaceTests.setUp
    export = workspace.NativeWorkspaceTests.export
    prepare = workspace.NativeWorkspaceTests.prepare

    def test_retaining_divergent_candidate_still_needs_explicit_resolution(self):
        baseline = self.export('base', {'one.py': 'base\n'})
        candidate = self.export('candidate', {'one.py': 'candidate\n'})
        incoming = self.export('incoming', {'one.py': 'incoming\n'})
        state = self.prepare(incoming, baseline, candidate)
        root = Path(state['project'])
        (root / 'one.py').write_text('candidate\n')
        blocked = collect_changes(root, state, ['one.py'], resolved_conflicts=[])
        self.assertEqual(blocked['blocked'], [{'path': 'one.py', 'reason': 'native_unresolved_conflict'}])
        resolved = collect_changes(root, state, ['one.py'], resolved_conflicts=['one.py'])
        self.assertEqual(resolved['blocked'], [])
        self.assertEqual(resolved['files'][0]['content'], 'candidate\n')
