"""Field-specific work correction through actual HTTP/SQLite/owned ACP stdio."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import threading
import unittest
from uuid import uuid4

from test_native_outcome_http import OutcomeFixture
from test_native_room_service import native_reply
from test_team_store import proposal


LITERALS = json.loads((Path(__file__).parent / 'fixtures/complete_command_v1.json').read_text())
PRIVATE_BYTES = b'Fictional work retained across response-only corrections.\n'


class CommandFixture(OutcomeFixture):
    def __init__(self, sequence, *, dependent_start=False):
        super().__init__({'native_case': 'completed'}, bad_phase='work', repeated=True)
        self.sequence = sequence
        self.dependent_start = dependent_start
        self.work_calls = []
        self.private_hashes = []
        self.peer_work_started, self.release_peer_work = threading.Event(), threading.Event()
        self.release_peer.set()

    def close(self):
        if hasattr(self, 'release_peer_work'):
            self.release_peer_work.set()
        super().close()

    async def peer_reply(self, agent, context, cwd):
        if context['job']['job_phase'] == 'plan':
            self.peer_started.set()
        if context['job']['job_phase'] == 'work':
            self.peer_work_started.set()
            try:
                while not self.release_peer_work.is_set():
                    await asyncio.sleep(.005)
            except asyncio.CancelledError:
                self.cancelled.append(context['job']['id'])
                raise
        return self.reply(agent, context, cwd)

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == 'kimi' and not context['team']['tasks']:
                commands = [proposal('T1', paths=['result.md'], dependencies=['T2'] if self.dependent_start else []),
                            proposal('T2', paths=['peer.md']),
                            {'type': 'claim', 'task_id': 'T1'}]
            elif agent == 'claude' and any(t['id'] == 'T2' and t['owner'] is None for t in context['team']['tasks']):
                commands = [{'type': 'claim', 'task_id': 'T2'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if phase == 'work':
            path = Path(cwd) / ('result.md' if task['id'] == 'T1' else 'peer.md')
            if not path.exists():
                path.write_bytes(PRIVATE_BYTES)
            if agent == 'kimi':
                self.private_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
            return native_reply(context, [{'type': 'complete', 'task_id': task['id'], 'result': '已核对虚构文件。'}])
        if phase == 'user' and agent == 'kimi':
            return native_reply(context, [{'type': 'claim', 'task_id': 'T1'}])
        return super().reply(agent, context, cwd)

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        if agent == 'kimi' and context['job']['job_phase'] == 'work':
            while not self.peer_work_started.is_set():
                await asyncio.sleep(.005)
            index = len(self.work_calls)
            literal = copy.deepcopy(LITERALS[self.sequence[min(index, len(self.sequence) - 1)]])
            # Revision is a current host binding, not part of the malformed
            # command under test. All command bytes remain independent literals.
            literal['base_revision'] = context['proposal_base_revision']
            literal['commands'][0]['task_id'] = context['task']['id']
            self.presentation = {'native_case': 'completed', 'text': json.dumps(literal, ensure_ascii=False)}
            self.work_calls.append({'id': context['job']['id'], 'task_id': context['task']['id'], 'text': context['job']['text'],
                                    'session_id': session_id, 'permission': options['permission']})
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)


class DualBlockedFixture(CommandFixture):
    """Two rejected owners and their unfinished dependency, matching no real text."""
    def __init__(self):
        super().__init__(['capability', 'capability'])
        self.release_peer_work.set()
        self.claude_work = []
        self.dual_hashes = {'T1': [], 'T2': [], 'T3': []}
        self.resubmitting = False

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == 'kimi' and not context['team']['tasks']:
                commands = [proposal('T1', paths=['claude.md']), proposal('T2', paths=['result.md']),
                            proposal('T3', paths=['dependent.md'], dependencies=['T1', 'T2']),
                            {'type': 'claim', 'task_id': 'T2'}, {'type': 'claim', 'task_id': 'T3'}]
            elif agent == 'claude' and any(t['id'] == 'T1' and t['owner'] is None for t in context['team']['tasks']):
                commands = [{'type': 'claim', 'task_id': 'T1'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if phase == 'work':
            path = Path(cwd) / {'T1': 'claude.md', 'T2': 'result.md', 'T3': 'dependent.md'}[task['id']]
            if not path.exists():
                path.write_bytes(PRIVATE_BYTES)
            self.dual_hashes[task['id']].append(hashlib.sha256(path.read_bytes()).hexdigest())
            if task['id'] == 'T2':
                self.private_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
            return native_reply(context, [{'type': 'complete', 'task_id': task['id'], 'result': '已核对虚构文件。'}])
        return super().reply(agent, context, cwd)

    async def peer_reply(self, agent, context, cwd):
        if context['job']['job_phase'] != 'work':
            return await super().peer_reply(agent, context, cwd)
        self.peer_work_started.set()
        self.reply(agent, context, cwd)
        literal = copy.deepcopy(LITERALS['full' if self.resubmitting else 'capability'])
        literal['base_revision'] = context['proposal_base_revision']
        literal['commands'][0]['task_id'] = context['task']['id']
        self.claude_work.append(context['job']['id'])
        return literal


class CommandFormatHTTPTests(unittest.TestCase):
    def fixture(self, sequence, **options):
        value = CommandFixture(sequence, **options)
        self.addCleanup(value.close)
        return value

    @staticmethod
    def old_rows(fixture, ids):
        placeholders = ','.join('?' for _ in ids)
        with fixture.service.store._connection() as db:
            return {table: [dict(row) for row in db.execute(
                f'SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}', list(ids))]
                for table, key in [('jobs', 'id'), ('team_jobs', 'job_id'), ('native_executions', 'job_id')]}

    def assert_retained(self, fixture):
        self.assertEqual(set(fixture.private_hashes), {hashlib.sha256(PRIVATE_BYTES).hexdigest()})
        self.assertTrue(all(c['permission'] == 'full_access' for c in fixture.calls))
        self.assertEqual(len({c['session_id'] for c in fixture.work_calls}), 1)
        self.assertTrue(fixture.work_calls[0]['session_id'])
        self.assertEqual(fixture.cancelled, [])
        published = fixture.service._project(fixture.room_id).files_for_paths(['result.md'])
        self.assertEqual(published[0]['content'].encode(), PRIVATE_BYTES)
        for item in fixture.results:
            self.assertEqual(item['result']['status'], 'completed')
            self.assertTrue(item['result']['native_execution']['cleanup_confirmed'])

    def test_full_flat_unused_capability_gets_exact_correction_and_preserves_healthy_peer(self):
        fixture = self.fixture(['capability', 'full'])
        fixture.create()
        held = fixture.wait(lambda value: len(fixture.work_calls) >= 2 and any(
            j['id'] == fixture.work_calls[1]['id'] and j['status'] == 'completed' for j in value['jobs']))
        self.assertIn('commands[0].capability', fixture.work_calls[1]['text'])
        self.assertIn('must be null', fixture.work_calls[1]['text'])
        self.assertTrue(any(j['provider'] == 'claude' and j['status'] == 'working' for j in held['jobs']))
        self.assert_retained(fixture)
        fixture.release_peer_work.set()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual([(j['status'], j['error']) for j in final['jobs'] if j['id'] == fixture.work_calls[0]['id']],
                         [('failed', 'invalid_team_commands')])
        self.assert_retained(fixture)

    def test_different_invalid_fields_with_same_code_can_reach_concise_completion(self):
        fixture = self.fixture(['capability', 'to', 'concise'], dependent_start=True)
        fixture.release_peer_work.set()
        fixture.create()
        final = fixture.settled()
        self.assertEqual(len(fixture.work_calls), 3, final['team'])
        self.assertIn('commands[0].capability', fixture.work_calls[1]['text'])
        self.assertIn('commands[0].to', fixture.work_calls[2]['text'])
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertFalse(any(e['kind'] == 'native_response_blocked' for e in final['events']))
        self.assert_retained(fixture)

    def test_identical_invalid_field_blocks_only_after_repeated_same_evidence_then_one_continue_resubmits(self):
        fixture = self.fixture(['capability', 'capability'], dependent_start=True)
        fixture.release_peer_work.set()
        fixture.create()
        blocked = fixture.settled()
        self.assertEqual(len(fixture.work_calls), 2)
        self.assertEqual(blocked['room']['status'], 'needs_user')
        tasks = {t['id']: t for t in blocked['team']['tasks']}
        self.assertEqual(tasks['T1']['status'], 'blocked')
        self.assertEqual(tasks['T2']['status'], 'accepted')
        self.assertTrue(any(e['kind'] == 'native_response_blocked' for e in blocked['events']))
        old_ids = {j['id'] for j in blocked['jobs']}
        old_rows = self.old_rows(fixture, old_ids)
        offset = len(fixture.calls)
        fixture.sequence = ['full']
        code, accepted = fixture.request('POST', f'/api/rooms/{fixture.room_id}/messages', {
            'text': '继续', 'target': 'kimi', 'request_id': str(uuid4()), 'intent': 'continue',
            'binding': blocked['continuation']['binding']})
        self.assertEqual(code, 200, accepted.get('error'))
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        new_calls = fixture.calls[offset:]
        self.assertEqual((new_calls[0]['agent'], new_calls[0]['phase']), ('kimi', 'work'))
        self.assertEqual(final['current_run']['id'], blocked['current_run']['id'])
        self.assertFalse(any(c['phase'] in {'plan', 'coordinate'} for c in new_calls))
        self.assertEqual(len([c for c in new_calls if c['phase'] == 'work']), 1)
        self.assertTrue(all(c['agent'] == 'kimi' for c in new_calls if c['phase'] in {'user', 'work'}))
        self.assertEqual(self.old_rows(fixture, old_ids), old_rows)
        final_tasks = {t['id']: t for t in final['team']['tasks']}
        self.assertEqual(final_tasks['T2']['result_version'], tasks['T2']['result_version'])
        self.assertTrue(all(j['status'] != 'working' for j in final['jobs']))
        self.assertTrue(all(j['status'] == 'failed' for j in final['jobs']
                            if j['id'] in old_ids and j['error'] == 'invalid_team_commands'))
        self.assert_retained(fixture)


    def test_one_continue_resubmits_two_blocked_owners_on_original_board_before_dependency(self):
        fixture = DualBlockedFixture()
        self.addCleanup(fixture.close)
        fixture.create()
        blocked = fixture.settled()
        tasks = {t['id']: t for t in blocked['team']['tasks']}
        self.assertEqual((tasks['T1']['status'], tasks['T2']['status']), ('blocked', 'blocked'))
        self.assertEqual(len(fixture.work_calls), 2)
        self.assertEqual(len(fixture.claude_work), 2)
        self.assertEqual(fixture.dual_hashes['T3'], [])
        old_failed = {j['id'] for j in blocked['jobs'] if j['error'] == 'invalid_team_commands'}
        self.assertEqual(len(old_failed), 4)
        old_rows = self.old_rows(fixture, old_failed)
        offset = len(fixture.calls)
        fixture.resubmitting = True
        fixture.sequence = ['full']
        code, accepted = fixture.request('POST', f'/api/rooms/{fixture.room_id}/messages', {
            'text': '继续', 'target': 'all', 'request_id': str(uuid4()), 'intent': 'continue',
            'binding': blocked['continuation']['binding']})
        self.assertEqual(code, 200, accepted.get('error'))
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(final['current_run']['id'], blocked['current_run']['id'])
        self.assertEqual(final['objective']['id'], blocked['objective']['id'])
        self.assertEqual(self.old_rows(fixture, old_failed), old_rows)
        final_tasks = {t['id']: t for t in final['team']['tasks']}
        for task_id in tasks:
            self.assertEqual(final_tasks[task_id]['owner'], tasks[task_id]['owner'])
            self.assertEqual(final_tasks[task_id]['dependencies'], tasks[task_id]['dependencies'])
            self.assertEqual(final_tasks[task_id]['status'], 'accepted')
        new_calls = fixture.calls[offset:]
        self.assertFalse(any(c['phase'] in {'plan', 'coordinate', 'user'} for c in new_calls))
        self.assertEqual(len([c for c in new_calls if c['phase'] == 'work']), 3)
        self.assertEqual(len(fixture.claude_work), 3)
        self.assertEqual(len(fixture.dual_hashes['T3']), 1)
        expected = {hashlib.sha256(PRIVATE_BYTES).hexdigest()}
        self.assertTrue(all(set(values) == expected for values in fixture.dual_hashes.values()))
        self.assertTrue(all(j['status'] == 'failed' for j in final['jobs'] if j['id'] in old_failed))
        self.assertTrue(all(c['permission'] == 'full_access' for c in fixture.calls))
        self.assertEqual(fixture.cancelled, [])


if __name__ == '__main__':
    unittest.main()
