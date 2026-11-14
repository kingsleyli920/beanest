"""Plain response preambles across owned ACP stdio, HTTP and SQLite."""
from __future__ import annotations

import json
from pathlib import Path
import types
import unittest

from local_agent_coordinator import adapters, native_workspace
from test_kimi_fenced_http import FencedACPFixture
from test_kimi_fence_recovery_http import FenceRecoveryFixture


PREAMBLE = '已核对当前请求和现有记录，下面给出这次处理的完整结构化结果。'


def install_preamble_peer(fixture, shape='valid'):
    """Change only the fictional peer's final presentation, not the adapter."""
    expression = {
        'valid': 'prefix + "\\n\\n" + body',
        'quoted_fence': 'prefix + ' + repr(' 已核对"main"。\n\n') + ' + final',
        'array': 'prefix + "\\n\\n[" + body + "]"',
        'multiple': 'prefix + "\\n\\n" + body + "\\n{}"',
        'trailing': 'prefix + "\\n\\n" + body + "\\n补充正文不能被忽略。"',
        'jsonlike': '\'{"pretend":\\n\' + body',
    }[shape]
    marker = '    final = scenario.get("final", final)\n'
    source = fixture.executable.read_text()
    if source.count(marker) != 1:
        raise AssertionError('Fictional ACP final boundary changed')
    source = source.replace(marker, marker +
        '    body = final.removeprefix("```json\\n").removesuffix("\\n```")\n' +
        '    prefix = ' + repr(PREAMBLE) + '\n' +
        '    final = ' + expression + '\n')
    source = source.replace('case = scenario["case"]', 'case = "streamed_activity"')
    fixture.executable.write_text(source)


class PreambleFixture(FencedACPFixture):
    def __init__(self, *, shape='valid'):
        super().__init__()
        install_preamble_peer(self, shape)


class PreambleRecoveryFixture(FenceRecoveryFixture):
    def __init__(self, *, archived=True):
        super().__init__()
        install_preamble_peer(self)
        # The previous released parser classified malformed schema JSON with
        # this precise public code. Reproduce that precheck while retaining
        # the real current ACP process, wire, receipt and cleanup machinery.
        self.old_acp.adapters = types.SimpleNamespace(**vars(adapters))
        strict = adapters._strict_json
        def retired_precheck(text):
            try:
                return strict(text)
            except (ValueError, UnicodeError, RecursionError):
                raise self.old_acp.ACPError('invalid_response_json') from None
        self.old_acp.adapters._strict_json = retired_precheck
        self.permissions = []
        self.archived = archived


    def bootstrap_failed(self):
        before = super().bootstrap_failed()
        if self.archived:
            state = self.service.store.native_workspace(self.room_id, 'claude')
            changes = native_workspace.collect_changes(state['project'], state, ['notes.md'], resolved_conflicts=[])
            observation = self.service._native_metadata_observation(state, changes, ['notes.md'], [])
            peer_job = next(job for job in before['jobs'] if job['provider'] == 'claude' and job['error'] == 'invoke_interrupted')
            peer_native = next(item for item in before['native_executions'] if item['job_id'] == peer_job['id'])
            # Seed an already registered archive, as distinct from allowing
            # arbitrary new D merely because a response format was rejected.
            state['partial_archive'] = {'paths': ['notes.md'], 'observation': observation,
                'source_provider': 'claude', 'source_job_id': peer_job['id'],
                'source_run_id': before['current_run']['id'], 'binding': peer_native['binding'],
                'run_id': before['current_run']['id']}
            self.service.store.save_native_workspace(self.room_id, 'claude', state)
            code, before = self.request('GET', f'/api/rooms/{self.room_id}')
            if code != 200:
                raise AssertionError(before)
        return before

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        self.permissions.append({'permission': options['permission'],
                                 'retained_inspection': bool(context.get('native_partial_inspection'))})
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)


class ResponsePreambleHTTPTests(unittest.TestCase):
    def fixture(self, kind=PreambleFixture, **options):
        fixture = kind(**options)
        self.addCleanup(fixture.close)
        return fixture

    def test_plain_preamble_completes_and_120_stream_updates_store_only_three_states(self):
        fixture = self.fixture()
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertTrue(all(job['error'] is None for job in final['jobs']))
        kimi_jobs = [job for job in final['jobs'] if job['provider'] == 'kimi']
        self.assertEqual(len(kimi_jobs), 3)
        for job in kimi_jobs:
            events = [event for event in final['events']
                      if event['kind'] == 'native_tool_activity' and event.get('job_id') == job['id']]
            self.assertEqual(len(events), 3, events)
            self.assertEqual([event['state'] for event in events], ['started', 'running', 'completed'])
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.native_calls))

    def test_natural_quoted_preamble_and_complete_fence_pass_actual_acp(self):
        fixture = self.fixture(shape='quoted_fence')
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertTrue(all(job['error'] is None for job in final['jobs']))

    def test_unregistered_peer_d_and_unconfirmed_cleanup_refuse_the_format_recovery_but_the_room_restarts(self):
        # Catches the old-format recovery (which keeps the peer's private work
        # as audited evidence) running over peer work nobody registered or a
        # call whose exit was never confirmed, and the room refusing every
        # continue because that recovery is unavailable.
        for archived in (False, True):
            with self.subTest(archived=archived):
                fixture = self.fixture(PreambleRecoveryFixture, archived=archived)
                try:
                    failed = fixture.bootstrap_failed()
                    job = next(job for job in failed['jobs'] if job['error'] == 'invalid_response_json')
                    if archived:
                        self.assertTrue(failed['current_run']['can_recover_native'], failed['current_run'])
                        with fixture.service.store._connection(write=True) as db:
                            row = db.execute('SELECT metadata_json FROM native_executions WHERE job_id=?', (job['id'],)).fetchone()
                            metadata = json.loads(row[0])
                            metadata['cleanup_confirmed'] = False
                            db.execute('UPDATE native_executions SET metadata_json=? WHERE job_id=?', (json.dumps(metadata), job['id']))
                        _, failed = fixture.request('GET', f'/api/rooms/{fixture.room_id}')
                    self.assertFalse(failed['current_run']['can_recover_native'], failed['current_run'])
                    self.assertEqual(failed['current_run']['native_recovery_reason'],
                                     'native_recovery_cleanup_unconfirmed' if archived else 'native_recovery_unpublished_changes')
                    self.assertTrue(failed['continuation']['can_send'], failed['continuation'])
                    old_ids = [item['id'] for item in failed['jobs']]
                    rows, count = fixture.old_rows(old_ids), len(fixture.native_calls)
                    code, response = fixture.recover(failed)
                    self.assertEqual(code, 200, response.get('error'))
                    final = fixture.settled()
                    kinds = [event['kind'] for event in final['events']]
                    self.assertEqual(kinds.count('native_fenced_recovery_attestation'), 0)
                    abandoned = [event for event in final['events'] if event['kind'] == 'native_unconfirmed_calls_abandoned']
                    self.assertEqual(len(abandoned), 1)
                    self.assertEqual((abandoned[0]['errors'], abandoned[0]['old_commands_accepted']),
                                     (['invalid_response_json', 'invoke_interrupted'], False))
                    # A call whose exit was never confirmed is told to the next run
                    # as possibly still running, and its session is not continued.
                    self.assertEqual(abandoned[0]['possibly_running_job_ids'], [job['id']] if archived else [])
                    fresh = next(call for call in fixture.native_calls[count:] if call['agent'] == 'kimi')
                    if archived:
                        self.assertIsNone(fresh['input_session_id'])
                    # The rejected result stays rejected and nothing is inspected as retained work.
                    self.assertEqual(fixture.old_rows(old_ids), rows)
                    self.assertFalse(any(call['retained_inspection'] for call in fixture.permissions))
                    self.assertEqual(final['objective']['status'], 'complete', final['team'])
                    # The peer's file reached the shared copy only through its owner's new work call.
                    files = fixture.service._project(fixture.room_id).files_for_paths(['notes.md'])
                    self.assertEqual([(file['owner'], file['content']) for file in files],
                                     [('claude', 'Retained fictional Claude work.\n')])
                    self.assertEqual(sorted(call['agent'] for call in fixture.native_calls[count:] if call.get('phase') == 'work'), ['claude', 'kimi'])
                finally:
                    fixture.close()

    def test_ambiguous_or_nonobject_outputs_remain_rejected_over_actual_http(self):
        for shape in ('array', 'multiple', 'trailing', 'jsonlike'):
            with self.subTest(shape=shape):
                fixture = self.fixture(shape=shape)
                try:
                    fixture.create()
                    final = fixture.settled()
                    self.assertNotEqual(final['objective']['status'], 'complete')
                    self.assertTrue(any(job['error'] == 'invalid_agent_response' for job in final['jobs']))
                    self.assertFalse(any(task['status'] == 'accepted' for task in final['team']['tasks']))
                finally:
                    fixture.close()

    def test_old_known_format_failure_recovers_without_accepting_old_result_or_losing_peer_d(self):
        fixture = self.fixture(PreambleRecoveryFixture)
        before = fixture.bootstrap_failed()
        failure = next(job for job in before['jobs'] if job['error'] == 'invalid_response_json')
        self.assertTrue(before['current_run']['can_recover_native'], before['current_run'])
        old_ids = [job['id'] for job in before['jobs']]
        old_rows = fixture.old_rows(old_ids)
        peer = fixture.service.store.native_workspace(fixture.room_id, 'claude')
        path = Path(peer['project']) / 'notes.md'
        original = path.read_bytes()
        start = len(fixture.native_calls)
        code, response = fixture.recover(before)
        self.assertEqual(code, 200, response.get('error'))
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(fixture.old_rows(old_ids), old_rows)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(next(job for job in final['jobs'] if job['id'] == failure['id'])['error'], 'invalid_response_json')
        self.assertEqual(final['room']['runtime_settings']['permission'], 'full_access')
        new_kimi = next(call for call in fixture.native_calls[start:] if call['agent'] == 'kimi')
        self.assertIsNone(new_kimi['input_session_id'])
        self.assertFalse(any(call['retained_inspection'] for call in fixture.permissions))
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.permissions))


if __name__ == '__main__':
    unittest.main()
