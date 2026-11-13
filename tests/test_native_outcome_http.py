"""Actual HTTP/SQLite and owned JSON-RPC stdio lifecycle, fictional peers only."""
from __future__ import annotations

import asyncio
import json
import hashlib
from pathlib import Path
import threading
import sys
import time
import unittest
from uuid import uuid4

from local_agent_coordinator import native_adapters
from test_kimi_fenced_http import FencedACPFixture
from test_native_room_service import native_reply, planned_owners
from test_team_store import proposal
from wait_support import poll


CORPUS = json.loads((Path(__file__).parent / 'fixtures/native_protocol_v1.json').read_text())


class OutcomeFixture(FencedACPFixture):
    def __init__(self, case, *, bad_phase='plan', repeated=False, native_case=None):
        super().__init__()
        # These outcome and recovery cases are built on the owners each partner claims while planning.
        planned_owners(self.service)
        self.presentation = case
        self.bad_phase, self.repeated, self.native_case = bad_phase, repeated, native_case
        self.calls = []
        self.results = []
        self.peer_started, self.release_peer = threading.Event(), threading.Event()
        self.cancelled = []
        self.occurrences = 0
        self.peer.handler = self.peer_reply

    def close(self):
        if hasattr(self, 'release_peer'):
            self.release_peer.set()
        super().close()

    async def peer_reply(self, agent, context, cwd):
        if context['job']['job_phase'] == 'plan':
            self.peer_started.set()
            try:
                while not self.release_peer.is_set():
                    await asyncio.sleep(.005)
            except asyncio.CancelledError:
                self.cancelled.append(context['job']['id'])
                raise
        return self.reply(agent, context, cwd)

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        phase = context['job']['job_phase']
        call = {'agent': agent, 'phase': phase, 'job_id': context['job']['id'],
                'input_sid': session_id, 'permission': options['permission']}
        self.calls.append(call)
        if agent != 'kimi':
            return await self.peer(agent, prompt, cwd, session_id=session_id, **options)
        # Establish the independently running peer before completing a malformed
        # plan. No host scheduling/parser/identity/cleanup function is patched.
        if phase == 'plan':
            while not self.peer_started.is_set():
                await asyncio.sleep(.005)
        selected = phase == self.bad_phase and (self.repeated or self.occurrences == 0)
        case = self.presentation if selected else {'native_case': 'completed'}
        if selected:
            self.occurrences += 1
        if 'response_file' in case:
            # Hand-authored golden bytes do not use native_reply/command_shape.
            final = (Path(__file__).parent / 'fixtures' / case['response_file']).read_text()
        else:
            body = json.dumps(self.reply(agent, context, cwd), ensure_ascii=False)
            final = case.get('text', case.get('prefix', '') + body + case.get('suffix', ''))
        sid = session_id or getattr(self, 'fixed_sid', None) or 'session_' + str(uuid4())
        (self.root / 'scenario.json').write_text(json.dumps({
            'session_id': sid, 'case': self.native_case or case['native_case'], 'final': final,
        }, ensure_ascii=False))
        result = await native_adapters.invoke_native(agent, prompt, cwd, session_id=session_id, **options)
        self.results.append({'job_id': context['job']['id'], 'result': result, 'selected': selected})
        return result

    def read(self):
        code, value = self.request('GET', f'/api/rooms/{self.room_id}')
        if code != 200:
            raise AssertionError(value)
        return value

    def wait(self, predicate):
        return poll(self.read, predicate, timeout=12, describe=lambda value: {
            'team': value['team'], 'jobs': [(j['provider'], j['status'], j['error']) for j in value['jobs']]})


class DependencyFixture(OutcomeFixture):
    def __init__(self, case, **options):
        super().__init__(case, bad_phase='work', **options)
        self.private_bytes = []
        self.release_peer.set()

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == 'kimi' and not context['team']['tasks']:
                commands = [proposal('T1', paths=['result.md']),
                            proposal('T2', paths=['dependent.md'], dependencies=['T1']),
                            {'type': 'claim', 'task_id': 'T1'}]
            elif agent == 'claude' and any(t['id'] == 'T2' and t['owner'] is None for t in context['team']['tasks']):
                commands = [{'type': 'claim', 'task_id': 'T2'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if phase == 'work':
            path = Path(cwd) / ('result.md' if task['id'] == 'T1' else 'dependent.md')
            content = b'Fictional exact private implementation.\n'
            if not path.exists():
                path.write_bytes(content)
            if task['id'] == 'T1':
                self.private_bytes.append(hashlib.sha256(path.read_bytes()).hexdigest())
            return native_reply(context, [{'type': 'complete', 'task_id': task['id'],
                                           'result': 'Checked the actual fictional private file.'}])
        return super().reply(agent, context, cwd)


class NativeOutcomeHTTPTests(unittest.TestCase):
    def fixture(self, case, **options):
        fixture = OutcomeFixture(case, **options)
        self.addCleanup(fixture.close)
        return fixture

    def assert_native_completed(self, result):
        self.assertEqual((result['status'], result.get('failure_code')), ('completed', None))
        native = result['native_execution']
        self.assertEqual(native['exit_code'], 0)
        self.assertTrue(native['cleanup_confirmed'])
        self.assertEqual(native['tool_calls'], native['tool_results'])
        self.assertGreater(native['tool_calls'], 0)
        receipt = json.loads(Path(native['receipt_path']).read_text().splitlines()[0])
        self.assertEqual(receipt['proof']['reason'], 'completed')
        self.assertGreater(receipt['proof']['wire_through'], receipt['proof']['wire_offset'])

    def test_versioned_presentations_keep_native_completion_separate_from_business_correction(self):
        self.assertEqual(CORPUS['version'], 1)
        for case in CORPUS['cases']:
            with self.subTest(case=case['id']):
                fixture = self.fixture(case)
                try:
                    fixture.create()
                    held = fixture.wait(lambda value: any(j['provider'] == 'kimi' and j['status'] == 'completed'
                                                         for j in value['jobs']))
                    self.assertEqual(fixture.cancelled, [])
                    self.assertTrue(any(j['provider'] == 'claude' and j['status'] == 'working' for j in held['jobs']))
                    self.assertFalse(any(c['phase'] == 'work' for c in fixture.calls))
                    selected = next(item for item in fixture.results if item['selected'])
                    self.assert_native_completed(selected['result'])
                    rejected = [j for j in held['jobs'] if j['provider'] == 'kimi' and j['status'] == 'failed']
                    if case['business_valid']:
                        self.assertEqual(rejected, [])
                    else:
                        self.assertEqual([(j['id'], j['error']) for j in rejected], [(selected['job_id'], 'invalid_agent_response')])
                        self.assertTrue(any(e['kind'] == 'format_correction_queued' and e.get('job_id') == selected['job_id']
                                            for e in held['events']))
                        native = next(n for n in held['native_executions'] if n['job_id'] == selected['job_id'])
                        self.assertEqual((native['status'], native['exit_code']), ('completed', 0))
                    self.assertFalse(any(m['identity_uncertain'] for m in held['members']))
                    fixture.release_peer.set()
                    final = fixture.settled()
                    self.assertEqual(final['objective']['status'], 'complete', final['team'])
                    self.assertTrue(all(c['permission'] == 'full_access' for c in fixture.calls))
                    self.assertEqual(fixture.cancelled, [])
                    if rejected:
                        old = next(j for j in final['jobs'] if j['id'] == selected['job_id'])
                        self.assertEqual((old['status'], old['error']), ('failed', 'invalid_agent_response'))
                        correction_id = next(e['job_id'] for e in final['events']
                                             if e['kind'] == 'format_correction_finished' and e.get('correction_of') == selected['job_id'])
                        correction = next(j for j in final['jobs'] if j['id'] == correction_id)
                        self.assertEqual(correction['status'], 'completed')
                        call = next(c for c in fixture.calls if c['job_id'] == correction_id)
                        self.assertEqual(call['input_sid'], selected['result']['session_id'])
                    trace = [json.loads(line) for line in (fixture.root / 'trace.jsonl').read_text().splitlines()]
                    methods = [row.get('method') for row in trace]
                    self.assertEqual(methods.count('session/close'), len(fixture.results))
                    self.assertNotIn('session/cancel', methods)
                finally:
                    fixture.close()

    def test_work_correction_preserves_exact_private_bytes_and_waits_for_dependency_review(self):
        fixture = DependencyFixture(next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json'))
        self.addCleanup(fixture.close)
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(len(fixture.private_bytes), 2)
        self.assertEqual(len(set(fixture.private_bytes)), 1)
        files = fixture.service._project(fixture.room_id).files_for_paths(['result.md'])
        self.assertEqual(hashlib.sha256(files[0]['content'].encode()).hexdigest(), fixture.private_bytes[0])
        rejected = next(j for j in final['jobs'] if j['error'] == 'invalid_agent_response')
        self.assertEqual(rejected['status'], 'failed')
        self.assert_native_completed(next(r['result'] for r in fixture.results if r['job_id'] == rejected['id']))
        t1_accept = next(e['seq'] for e in final['events'] if e['kind'] == 'team_command'
                         and e.get('command') == 'review' and e.get('task_id') == 'T1')
        t2_work = next(c for c in fixture.calls if c['phase'] == 'work' and c['agent'] == 'claude')
        t2_claim = next(e['seq'] for e in final['events'] if e['kind'] == 'job_claimed' and e.get('job_id') == t2_work['job_id'])
        self.assertLess(t1_accept, t2_claim)
        self.assertTrue(all(c['permission'] == 'full_access' for c in fixture.calls))

    def test_repeated_same_work_rejection_blocks_locally_without_releasing_dependencies_or_looping(self):
        fixture = DependencyFixture(next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json'), repeated=True)
        self.addCleanup(fixture.close)
        fixture.create()
        final = fixture.settled()
        self.assertNotEqual(final['objective']['status'], 'complete')
        self.assertEqual(final['room']['status'], 'needs_user')
        self.assertEqual(len(fixture.private_bytes), 2)
        self.assertEqual(len(set(fixture.private_bytes)), 1)
        tasks = {t['id']: t for t in final['team']['tasks']}
        self.assertEqual(tasks['T1']['status'], 'blocked')
        self.assertNotEqual(tasks['T2']['status'], 'accepted')
        self.assertFalse(any(c['phase'] == 'work' and c['agent'] == 'claude' for c in fixture.calls))
        self.assertTrue(any(e['kind'] == 'native_response_blocked' for e in final['events']))
        self.assertFalse(any(m['identity_uncertain'] for m in final['members']))
        self.assertEqual(fixture.cancelled, [])
        self.assertFalse(any(j['status'] in {'queued', 'working'} for j in final['jobs']))
        self.assertTrue(all(c['permission'] == 'full_access' for c in fixture.calls))


    def test_bad_business_text_cannot_hide_missing_wire_nonzero_or_unpaired_tools(self):
        case = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
        for native_case in ('missing_wire', 'nonzero', 'unclosed_tool'):
            with self.subTest(native_case=native_case):
                fixture = self.fixture(case, native_case=native_case)
                try:
                    fixture.create()
                    final = fixture.settled()
                    self.assertNotEqual(final['objective']['status'], 'complete')
                    self.assertFalse(any(e['kind'] == 'format_correction_queued' for e in final['events']))
                    selected = next(r['result'] for r in fixture.results if r['selected'])
                    self.assertEqual(selected['status'], 'failed')
                    self.assertTrue(any(j['status'] == 'unknown' for j in final['jobs']))
                finally:
                    fixture.close()

    def test_completed_native_call_with_historical_sid_collision_is_not_format_corrected(self):
        fixture = self.fixture(CORPUS['cases'][0])
        fixture.release_peer.set()
        fixture.fixed_sid = 'session_' + str(uuid4())
        fixture.create()
        first = fixture.settled()
        self.assertEqual(first['objective']['status'], 'complete')
        fixture.presentation = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
        fixture.occurrences = 0
        offset = len(fixture.results)
        fixture.create()
        second = fixture.settled()
        self.assertNotEqual(second['objective']['status'], 'complete')
        self.assertTrue(any(j['error'] == 'session_identity_changed' for j in second['jobs']))
        self.assertFalse(any(e['kind'] == 'format_correction_queued' for e in second['events']))
        self.assert_native_completed(fixture.results[offset]['result'])


def fixture_main():
    """Interactive disposable runner for the root's independent browser check."""
    case = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
    fixture = OutcomeFixture(case)
    try:
        fixture.create()
        fixture.wait(lambda value: any(j['provider'] == 'kimi' and j['status'] == 'completed'
                                      for j in value['jobs']))
        print(json.dumps({'url': fixture.server.url, 'room_id': fixture.room_id,
                          'root': str(fixture.root), 'real_model_calls': 0,
                          'held_peer': 'claude', 'commands': ['release', 'status', 'stop']}), flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == 'stop':
                break
            if command == 'release':
                fixture.release_peer.set()
            if command in {'release', 'status'}:
                value = fixture.read()
                print(json.dumps({'objective': value['objective']['status'],
                                  'jobs': [(j['provider'], j['status'], j['error']) for j in value['jobs']],
                                  'cancelled': len(fixture.cancelled)}), flush=True)
    finally:
        fixture.close()
        print(json.dumps({'closed': True, 'temporary_removed': not fixture.root.exists()}), flush=True)


if __name__ == '__main__':
    if '--fixture' in sys.argv:
        fixture_main()
    else:
        unittest.main()
