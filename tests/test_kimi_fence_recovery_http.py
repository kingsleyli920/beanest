"""Exact old fenced-JSON adapter failure and recovery over real HTTP/ACP."""
from __future__ import annotations

import asyncio
import inspect
import types
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import kimi_acp, native_adapters
from test_kimi_fenced_http import FencedACPFixture
from test_native_room_service import native_reply
from test_team_store import proposal


def legacy_schema_adapter():
    """Reproduce only the retired precheck; keep real current stdio/cleanup.

    The original before-source red run remains verification evidence, while
    this durable test has no dependency on disposable runtime output files.
    """
    source = inspect.getsource(kimi_acp)
    end = '        await connection.request("session/close", {"sessionId": session_id})\n'
    if source.count(end) != 1:
        raise AssertionError('Native schema boundary changed; review legacy regression fixture')
    boundary = source.index(end)
    gate = ('        if response_schema is not None:\n'
            '            value = adapters._strict_json(updates.text)\n'
            '            if not isinstance(value, dict):\n'
            '                raise ACPError("invalid_response_json")\n')
    module = types.ModuleType('local_agent_coordinator._legacy_schema_test')
    module.__package__ = 'local_agent_coordinator'
    exec(compile(source[:boundary] + gate + source[boundary:], '<legacy-schema-regression>', 'exec'), module.__dict__)
    return module


class FenceRecoveryFixture(FencedACPFixture):
    failure_phase = 'work'
    def __init__(self):
        # Validate the retired insertion before the parent installs global mocks.
        old_acp = legacy_schema_adapter()
        super().__init__()
        self.old_acp = old_acp
        self.old_acp.managed_configuration = lambda path=None: __import__(
            'local_agent_coordinator.kimi_acp', fromlist=['managed_configuration']).managed_configuration(path)
        source = self.executable.read_text().replace(
            '"origin": {"kind": "user"}, "input": []',
            '"origin": {"kind": "user"}, "input": params["prompt"]')
        source = source.replace('    message(final)\n',
            '    message(final)\n    append_wire({"type": "context.append_loop_event", "event": {"type": "message.part", "part": {"type": "text", "text": final}}})\n')
        self.executable.write_text(source)
        self.use_old = True
        self.kimi_results = []
        self.peer_work_started = asyncio.Event()
        self.peer.handler = self.peer_reply

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        if agent != 'kimi':
            return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)
        record = {'agent': agent, 'phase': context['job']['job_phase'], 'permission': options.get('permission'),
                  'cwd': str(cwd), 'input_session_id': session_id, 'old_adapter': self.use_old and context['job']['job_phase'] == self.failure_phase}
        self.native_calls.append(record)
        (self.root / 'scenario.json').write_text(json.dumps({
            'session_id': session_id or 'session_' + str(uuid4()), 'case': 'completed', 'no_tools': True,
            'final': '```json\n' + json.dumps(self.reply(agent, context, cwd)) + '\n```'}))
        old = record['old_adapter']
        if old:
            await self.peer_work_started.wait()
        invoke = self.old_acp.invoke_native if old else native_adapters.invoke_native
        args = (prompt, cwd) if old else ('kimi', prompt, cwd)
        result = await invoke(*args, session_id=session_id, **options)
        await self.after_kimi(context, result)
        self.kimi_results.append(result)
        record.update(status=result['status'], error=result.get('failure_code'),
                      cleanup_confirmed=result.get('cleanup_confirmed'), session_id=result.get('session_id'))
        return result

    async def after_kimi(self, context, result):
        pass

    @staticmethod
    def reply(agent, context, cwd):
        phase, board = context['job']['job_phase'], context['team']
        if phase in {'plan', 'coordinate'}:
            commands = []
            if not board['tasks'] and agent == board['plan_author']:
                commands = [proposal('T1', paths=[]), proposal('T2', paths=['notes.md'])]
            own = 'T1' if agent == 'kimi' else 'T2'
            if commands or any(task['id'] == own and task['owner'] is None for task in board['tasks']):
                commands.append({'type': 'claim', 'task_id': own})
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        return FencedACPFixture.reply(agent, context, cwd)

    async def peer_reply(self, agent, context, cwd):
        if context['job']['job_phase'] == 'work':
            (cwd / 'notes.md').write_text('Retained fictional Claude work.\n')
            if self.use_old:
                self.peer_work_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    identity = next(member['session_id'] for member in self.service.store.snapshot(self.room_id)['members']
                                    if member['id'] == agent)
                    receipt = self.root / 'native-receipts' / self.room_id / (str(uuid4()) + '.jsonl')
                    receipt.parent.mkdir(parents=True, exist_ok=True)
                    receipt.write_text(json.dumps({'channel': 'stdout', 'data': {
                        'type': 'system', 'subtype': 'init', 'session_id': identity}}) + '\n')
                    error.native_execution = {'permission': 'full_access', 'tool_calls': 1, 'tool_results': 1,
                        'side_effects_possible': True, 'cleanup_confirmed': True, 'exit_code': -15,
                        'receipt_path': str(receipt)}
                    raise
        return self.reply(agent, context, cwd)

    def settled(self):
        # The first snapshot can precede the worker's final cleanup event even
        # when that worker finishes before the caller checks its done flag.
        super().settled()
        code, snapshot = self.request('GET', f'/api/rooms/{self.room_id}')
        if code != 200:
            raise AssertionError(snapshot)
        return snapshot

    def bootstrap_failed(self):
        self.create()
        failed = self.settled()
        self.use_old = False
        return failed

    def recover(self, snapshot):
        run = snapshot['current_run']
        return self.request('POST', f'/api/rooms/{self.room_id}/recover-native', {
            'request_id': str(uuid4()), 'run_id': run['id'], 'revision': run['revision'], 'digest': run['digest']})

    def old_rows(self, identifiers):
        placeholders = ','.join('?' for _ in identifiers)
        with self.service.store._connection() as db:
            return {table: [dict(row) for row in db.execute(
                f'SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}', identifiers)]
                for table, key in (('jobs', 'id'), ('team_jobs', 'job_id'), ('native_executions', 'job_id'))}


class CoordinateRecoveryFixture(FenceRecoveryFixture):
    """A fresh owned Claude coordinate canceled before first SID registration."""
    extra_coordinate = False

    @staticmethod
    def reply(agent, context, cwd):
        if context['job']['job_phase'] in {'plan', 'coordinate'}:
            commands = []
            if not context['team']['tasks'] and agent == context['team']['plan_author']:
                commands = [proposal('T1', paths=[]), {'type': 'claim', 'task_id': 'T1'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        return FencedACPFixture.reply(agent, context, cwd)

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        if self.use_old and agent == 'kimi' and context['job']['job_phase'] == 'work':
            # Seed only an idle member's fresh-session choice and schedule the
            # real coordinate through the ordinary queue. Its claim, native
            # cancellation receipt and first SID registration are not forged.
            with self.service.store._connection(write=True) as db:
                db.execute("UPDATE members SET session_id=NULL WHERE room_id=? AND provider='claude'", (self.room_id,))
                if not self.service.store._team._queue(db, self.room_id, 'claude', 'coordinate'):
                    raise AssertionError('Could not queue fictional fresh coordinate')
            self.extra_coordinate = True
            self.service._wake(self.room_id)
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)

    async def peer_reply(self, agent, context, cwd):
        if self.use_old and self.extra_coordinate and context['job']['job_phase'] == 'coordinate':
            self.peer_work_started.set()
            sid = str(uuid4())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                receipt = self.root / 'native-receipts' / self.room_id / (str(uuid4()) + '.jsonl')
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text(json.dumps({'channel': 'stdout', 'data': {
                    'type': 'system', 'subtype': 'init', 'session_id': sid}}) + '\n')
                error.native_execution = {'permission': 'full_access', 'tool_calls': 0, 'tool_results': 0,
                    'side_effects_possible': False, 'cleanup_confirmed': True, 'exit_code': -15,
                    'receipt_path': str(receipt)}
                raise
        return await super().peer_reply(agent, context, cwd)


class KimiFenceRecoveryHTTPTests(unittest.TestCase):
    def test_retired_boundary_failure_cannot_install_parent_global_mocks(self):
        configuration = kimi_acp.managed_configuration
        account_status = native_adapters.adapters.account_status
        invoke = native_adapters.adapters.invoke
        with patch(__name__ + '.legacy_schema_adapter', side_effect=AssertionError('boundary changed')):
            with self.assertRaisesRegex(AssertionError, 'boundary changed'):
                FenceRecoveryFixture()
        self.assertIs(kimi_acp.managed_configuration, configuration)
        self.assertIs(native_adapters.adapters.account_status, account_status)
        self.assertIs(native_adapters.adapters.invoke, invoke)

    def fixture(self, kind=FenceRecoveryFixture):
        fixture = kind()
        self.addCleanup(fixture.close)
        return fixture

    def test_tampered_prompt_terminal_and_peer_drift_keep_recovery_closed(self):
        for mutation in ('schema', 'terminal', 'peer_drift', 'receipt_proof', 'newer_turn', 'extra_terminal', 'extra_started', 'wire_tool'):
            with self.subTest(mutation=mutation):
                fixture = self.fixture()
                try:
                    before = fixture.bootstrap_failed()
                    self.assertTrue(before['current_run']['can_recover_native'], before['current_run'])
                    candidate = fixture.service.store.native_fenced_recovery_candidate(fixture.room_id)
                    state = fixture.service.store.native_workspace(fixture.room_id, 'kimi')
                    if mutation == 'peer_drift':
                        peer = fixture.service.store.native_workspace(fixture.room_id, 'claude')
                        (Path(peer['project']) / 'outside-allowed.txt').write_text('Unexpected drift.\n')
                    elif mutation == 'receipt_proof':
                        result = next(result for result in fixture.kimi_results if result.get('failure_code') == 'native_turn_unconfirmed')
                        path = Path(result['native_execution']['receipt_path'])
                        receipt = json.loads(path.read_text())
                        receipt['proof'] = {'turn_id': 23}
                        path.write_text(json.dumps(receipt) + '\n')
                    else:
                        path = fixture.old_acp.session_wire(Path(state['project']), candidate['session_id'])
                        rows = [json.loads(line) for line in path.read_text().splitlines()]
                        matching = next(row for row in rows if row.get('type') == 'turn.prompt'
                            and candidate['job_id'] in json.dumps(row))
                        if mutation == 'schema':
                            matching['input'][0]['text'] += 'TAMPERED_SCHEMA'
                        elif mutation == 'extra_terminal':
                            rows.append({'type': 'turn.ended', 'turnId': 24, 'reason': 'completed'})
                        elif mutation == 'extra_started':
                            rows.append({'type': 'turn.started', 'turnId': 24})
                        elif mutation == 'wire_tool':
                            terminal = next(index for index in reversed(range(len(rows))) if rows[index].get('type') == 'turn.ended')
                            rows.insert(terminal, {'type': 'context.append_loop_event', 'event': {'type': 'tool.called', 'toolCallId': '24:unexpected'}})
                        elif mutation == 'newer_turn':
                            rows.append({'type': 'turn.prompt', 'input': [{'type': 'text', 'text': 'Unrelated later turn'}]})
                        else:
                            next(row for row in reversed(rows) if row.get('type') == 'turn.ended')['reason'] = 'failed'
                        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                    code, current = fixture.request('GET', f'/api/rooms/{fixture.room_id}')
                    self.assertEqual(code, 200)
                    self.assertFalse(current['current_run']['can_recover_native'])
                    count = len(fixture.native_calls)
                    self.assertNotEqual(fixture.recover(current)[0], 200)
                    self.assertEqual(len(fixture.native_calls), count)
                finally:
                    fixture.close()

    def test_changed_owned_execution_or_task_binding_cannot_recover(self):
        fixture = self.fixture()
        before = fixture.bootstrap_failed()
        self.assertTrue(before['current_run']['can_recover_native'], before['current_run'])
        job = next(job for job in before['jobs'] if job['error'] == 'native_turn_unconfirmed')
        with fixture.service.store._connection(write=True) as db:
            original = db.execute('SELECT metadata_json FROM native_executions WHERE job_id=?', (job['id'],)).fetchone()[0]
            task = dict(db.execute("SELECT * FROM shared_tasks WHERE room_id=? AND id='T1'", (fixture.room_id,)).fetchone())
        for mutation in ('permission', 'tool_calls', 'cleanup_confirmed', 'owner', 'assignment_epoch'):
            with self.subTest(mutation=mutation):
                try:
                    with fixture.service.store._connection(write=True) as db:
                        if mutation in {'owner', 'assignment_epoch'}:
                            value = 'claude' if mutation == 'owner' else task['assignment_epoch'] + 1
                            db.execute(f'UPDATE shared_tasks SET {mutation}=? WHERE room_id=? AND id=?',
                                       (value, fixture.room_id, task['id']))
                        else:
                            altered = json.loads(original)
                            altered[mutation] = {'permission': 'plan', 'tool_calls': 1, 'cleanup_confirmed': False}[mutation]
                            db.execute('UPDATE native_executions SET metadata_json=? WHERE job_id=?',
                                       (json.dumps(altered), job['id']))
                    count = len(fixture.native_calls)
                    code, changed = fixture.request('GET', f'/api/rooms/{fixture.room_id}')
                    self.assertEqual(code, 200)
                    if mutation == 'cleanup_confirmed':
                        # A process that may still be running keeps the room waiting.
                        self.assertFalse(changed['current_run']['can_recover_native'])
                        self.assertNotEqual(fixture.recover(changed)[0], 200)
                    else:
                        # Evidence that no longer proves the old call never selects the
                        # recovery that would trust it; the room may still abandon that
                        # call and start fresh, accepting nothing from it.
                        self.assertIsNone(fixture.service.store.native_recovery_requirements(fixture.room_id)['fenced'])
                        self.assertEqual((changed['current_run']['native_recovery_kind'], changed['current_run']['can_recover_native']),
                                         ('unconfirmed_restart', True))
                    self.assertEqual(len(fixture.native_calls), count)
                finally:
                    with fixture.service.store._connection(write=True) as db:
                        db.execute('UPDATE native_executions SET metadata_json=? WHERE job_id=?', (original, job['id']))
                        db.execute('UPDATE shared_tasks SET owner=?,assignment_epoch=? WHERE room_id=? AND id=?',
                                   (task['owner'], task['assignment_epoch'], fixture.room_id, task['id']))

    def test_first_coordinate_owned_sid_recovers_fresh_with_null_member_session(self):
        fixture = self.fixture(CoordinateRecoveryFixture)
        before = fixture.bootstrap_failed()
        peer = next(member for member in before['members'] if member['id'] == 'claude')
        self.assertIsNone(peer['session_id'])
        # The owned init receipt identifies the canceled process even though
        # the member has never committed a reusable session.
        self.assertFalse(peer['identity_uncertain'])
        canceled = next(job for job in before['jobs'] if job['provider'] == 'claude' and job['error'] == 'invoke_interrupted')
        observation = next(item for item in before['native_executions'] if item['job_id'] == canceled['id'])
        self.assertEqual(observation['phase'], 'coordinate')
        self.assertTrue(observation['cleanup_confirmed'])
        self.assertTrue(before['current_run']['can_recover_native'], before['current_run'])
        with fixture.service.store._connection() as db:
            event = db.execute("SELECT seq,value_json FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='native_session_cleanup_identity' AND json_extract(value_json,'$.job_id')=?", (fixture.room_id, canceled['id'])).fetchone()
            event_seq, original_event = event['seq'], event['value_json']
        try:
            changed_event = json.loads(original_event)
            changed_event['session_id'] = str(uuid4())
            with fixture.service.store._connection(write=True) as db:
                db.execute('UPDATE events SET value_json=? WHERE room_id=? AND seq=?', (json.dumps(changed_event), fixture.room_id, event_seq))
            _, changed = fixture.request('GET', f'/api/rooms/{fixture.room_id}')
            # The receipt no longer names this call: the recovery that trusted it
            # is gone, and only abandoning the call for a fresh start remains.
            self.assertIsNone(fixture.service.store.native_recovery_requirements(fixture.room_id)['fenced'])
            self.assertEqual((changed['current_run']['native_recovery_kind'], changed['current_run']['can_recover_native']),
                             ('unconfirmed_restart', True))
        finally:
            with fixture.service.store._connection(write=True) as db:
                db.execute('UPDATE events SET value_json=? WHERE room_id=? AND seq=?', (original_event, fixture.room_id, event_seq))
        old_ids = [job['id'] for job in before['jobs']]
        rows = fixture.old_rows(old_ids)
        start = len(fixture.native_calls)
        self.assertEqual(fixture.recover(before)[0], 200)
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(fixture.old_rows(old_ids), rows)
        fresh = [call for call in fixture.native_calls[start:] if call['agent'] == 'kimi']
        self.assertIsNone(fresh[0]['input_session_id'])
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.native_calls[start:]))

    def test_large_other_receipt_and_usage_tail_do_not_hide_valid_recovery(self):
        fixture = self.fixture()
        before = fixture.bootstrap_failed()
        self.assertTrue(before['current_run']['can_recover_native'], before['current_run'])
        candidate = fixture.service.store.native_fenced_recovery_candidate(fixture.room_id)
        state = fixture.service.store.native_workspace(fixture.room_id, 'kimi')
        wire = fixture.old_acp.session_wire(Path(state['project']), candidate['session_id'])
        with wire.open('a') as stream:
            stream.write(json.dumps({'type': 'context.append_loop_event', 'event': {
                'type': 'usage.update', 'inputTokens': 10, 'outputTokens': 4}}) + '\n')
        directory = fixture.root / 'native-receipts' / fixture.room_id
        other = directory / (str(uuid4()) + '.jsonl')
        other.write_text(json.dumps({'channel': 'stdout', 'data': {
            'type': 'assistant', 'message': 'Fictional peer output ' * 500}}) + '\n')
        timestamp = (candidate['claimed_at'] + candidate['cleanup_at']) / 2
        os.utime(other, (timestamp, timestamp))
        code, checked = fixture.request('GET', f'/api/rooms/{fixture.room_id}')
        self.assertEqual(code, 200)
        self.assertTrue(checked['current_run']['can_recover_native'], checked['current_run'])
        self.assertEqual(fixture.recover(checked)[0], 200)
        self.assertEqual(fixture.settled()['objective']['status'], 'complete')

    def test_directed_followup_keeps_unselected_peer_retained_bytes_linked(self):
        fixture = self.fixture()
        before = fixture.bootstrap_failed()
        self.assertTrue(before['current_run']['can_recover_native'], before['current_run'])
        peer = fixture.service.store.native_workspace(fixture.room_id, 'claude')
        retained = (Path(peer['project']) / 'notes.md').read_bytes()
        old_ids = [job['id'] for job in before['jobs']]
        rows = fixture.old_rows(old_ids)
        code, accepted = fixture.request('POST', f'/api/rooms/{fixture.room_id}/messages', {
            'text': 'Kimi: add only a short follow-up explanation; keep earlier work.', 'target': 'kimi',
            'request_id': str(uuid4()), 'intent': 'continue', 'binding': before['continuation']['binding']})
        self.assertEqual(code, 200, accepted.get('error'))
        continuation = accepted['objective']['continuation']
        self.assertEqual(continuation['initial_recipient'], 'kimi')
        retained_work = continuation.get('retained_native_work', [])
        self.assertTrue(any(item['provider'] == 'claude' for item in retained_work), retained_work)
        self.assertEqual((Path(peer['project']) / 'notes.md').read_bytes(), retained)
        self.assertEqual(fixture.old_rows(old_ids), rows)

    def test_exact_old_fence_failure_can_restart_fresh_without_accepting_old_result(self):
        fixture = self.fixture()
        before = fixture.bootstrap_failed()
        failed_job = next(job for job in before['jobs'] if job['error'] == 'native_turn_unconfirmed')
        old_result = next(result for result in fixture.kimi_results if result.get('failure_code') == 'native_turn_unconfirmed')
        self.assertEqual(old_result['native_execution']['tool_calls'], 0)
        self.assertTrue(old_result['cleanup_confirmed'])
        receipt = json.loads(Path(old_result['native_execution']['receipt_path']).read_text())
        self.assertIsNone(receipt['proof'])
        self.assertTrue(before['current_run']['can_recover_native'], before['current_run'])
        old_ids = [job['id'] for job in before['jobs']]
        rows = fixture.old_rows(old_ids)
        peer_tree = Path(fixture.service.store.native_workspace(fixture.room_id, 'claude')['project'])
        retained = (peer_tree / 'notes.md').read_bytes()
        self.assertEqual(retained, b'Retained fictional Claude work.\n')
        self.assertEqual(fixture.recover(before)[0], 200)
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(fixture.old_rows(old_ids), rows)
        self.assertEqual((peer_tree / 'notes.md').read_bytes(), retained)
        self.assertEqual(next(job for job in final['jobs'] if job['id'] == failed_job['id'])['error'],
                         'native_turn_unconfirmed')
        failed_index = next(index for index, call in enumerate(fixture.native_calls) if call.get('old_adapter'))
        fresh = next(call for call in fixture.native_calls[failed_index + 1:] if call['agent'] == 'kimi')
        self.assertIsNone(fresh['input_session_id'])
        self.assertNotEqual(fresh['session_id'], old_result['session_id'])
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.native_calls))


if __name__ == '__main__':
    unittest.main()
