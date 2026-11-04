"""Exact post-prompt context failure recovery over real HTTP/SQLite/fake ACP."""
import json
from pathlib import Path
import sys
import textwrap
import unittest
from uuid import uuid4

from local_agent_coordinator import kimi_acp
from test_internal_replan_http import InternalReplanFixture
from test_kimi_acp import FAKE_ACP
from test_native_room_service import native_reply
from test_team_store import proposal


CONTEXT_FAILURE = textwrap.dedent('''
    if case in {"context_limit", "other_auth403"} and method == "session/prompt":
        append_wire({"type": "turn.prompt", "origin": {"kind": "user"}, "input": params["prompt"]})
        append_wire({"type": "full_compaction.begin", "source": "auto"})
        append_wire({"type": "llm.request", "kind": "compaction", "model": "fictional-0", "modelAlias": "kimi-code/fictional-0", "droppedCount": 0})
        append_wire({"type": "full_compaction.cancel"})
        message = ("Current plan supports context up to 256K; higher context requires an upgrade."
                   if case == "context_limit" else "Fictional account credential is not authorized.")
        append_wire({"type": "turn.ended", "turnId": 23, "reason": "failed", "error": {
            "code": "provider.auth_error", "name": "APIStatusError", "retryable": False,
            "message": message, "details": {"statusCode": 403}}})
        send({"id": row["id"], "error": {"code": -32000, "message": "PRIVATE_FAKE_ERROR"}})
        continue
''')


class ContextRecoveryFixture(InternalReplanFixture):
    def __init__(self, failure='context_limit', *, legacy_label=False):
        super().__init__('permission_required')
        self.failure, self.fail_plan, self.recovered = failure, False, False
        self.legacy_label = legacy_label
        self.failed_plan = None
        marker = '    trace(summary)\n'
        if FAKE_ACP.count(marker) != 1:
            raise AssertionError('The fixed fictional ACP driver boundary changed')
        self.executable.write_text(f'#!{sys.executable}\n' + FAKE_ACP.replace(marker, marker + textwrap.indent(CONTEXT_FAILURE, '    ')))
        self.observe_available_peer_quota()
        code, value = self.request('POST', '/api/partners/kimi/runtime/refresh', {})
        if code != 200:
            raise AssertionError(value)

    def create(self):
        code, value = self.request('POST', '/api/rooms', {
            'goal': 'Preserve accepted fictional work and review a necessary regression task.',
            'target': 'kimi', 'request_id': str(uuid4()), 'mode': 'native_project',
            'project_id': self.project_id, 'participants': ['kimi', 'claude'], 'work_mode': 'build',
            'runtime_settings': {'permission': 'full_access', 'members': {
                'kimi': {'model': 'kimi-code/fictional-0', 'effort': 'high'}}},
            'execution_policy': {'mode': 'autonomous'}})
        if code != 200:
            raise AssertionError(value)
        self.room_id = value['room']['id']
        return value

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        selected = agent == 'kimi' and self.fail_plan and context['job']['job_phase'] in {'plan', 'coordinate'}
        if selected:
            self.failed_plan = {'job_id': context['job']['id'], 'cwd': str(cwd), 'input_sid': session_id}
            previous = self.native_case
            self.native_case = self.failure
            try:
                result = await super().invoke(agent, prompt, cwd, session_id=session_id, **options)
                if self.legacy_label:
                    # Minimal retired classification simulation, not a production snapshot.
                    result['failure_code'] = 'native_auth_unconfirmed'
                    result['native_execution'].pop('failure_diagnostic', None)
                return result
            finally:
                self.native_case = previous
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)

    def reply(self, agent, context, cwd):
        if self.recovered and context['job']['job_phase'] in {'plan', 'coordinate'}:
            commands = []
            if agent == 'kimi' and not context['team']['tasks']:
                commands = [proposal('T4', paths=['README.md']),
                            proposal('T3', paths=['final.md'], dependencies=['T4']),
                            {'type': 'claim', 'task_id': 'T4'}, {'type': 'claim', 'task_id': 'T3'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        return super().reply(agent, context, cwd)

    def stopped_context_run(self):
        self.create()
        self.original = self.settled()
        if self.original['objective']['stop_reason'] != 'objective_decision_required':
            raise AssertionError(self.original['objective'])
        self.fail_plan = True
        code, value = self.request('POST', f'/api/rooms/{self.room_id}/messages', {
            'text': '请在原目标内重做计划，保留已验收成果并增加必要回归分工。', 'target': 'all',
            'request_id': str(uuid4()), 'intent': 'continue', 'binding': self.original['continuation']['binding']})
        if code != 200:
            raise AssertionError((code, value))
        stopped = self.settled()
        if not self.failed_plan:
            raise AssertionError('The synthetic native context failure did not run')
        self.fail_plan = False
        self.source_sid = next(item['result']['session_id'] for item in self.results if item['job_id'] == self.failed_plan['job_id'])
        self.failed_wire = kimi_acp.session_wire(self.failed_plan['cwd'], self.source_sid)
        return stopped

    def continue_http(self, snapshot):
        self.recovered = True
        return self.request('POST', f'/api/rooms/{self.room_id}/messages', {
            'text': '继续原任务，保留旧会话历史和已验收成果。', 'target': 'all', 'request_id': str(uuid4()),
            'intent': 'continue', 'binding': snapshot['continuation']['binding']})


class KimiContextRecoveryHTTPTests(unittest.TestCase):
    def fixture(self, *args, **options):
        fixture = ContextRecoveryFixture(*args, **options)
        self.addCleanup(fixture.close)
        return fixture

    def test_exact_compaction_context_failure_recovers_with_fresh_session_and_retained_accepted_results(self):
        self.assert_recovery(legacy_label=False)

    def test_retired_auth_label_still_requires_exact_wire_before_fresh_handoff(self):
        self.assert_recovery(legacy_label=True)

    def assert_recovery(self, *, legacy_label):
        fixture = self.fixture(legacy_label=legacy_label)
        stopped = fixture.stopped_context_run()
        old_jobs = {job['id']: (job['status'], job['error']) for job in stopped['jobs']}
        original_tasks = fixture.original['team']['tasks']
        old_wire = fixture.failed_wire.read_bytes()
        prior_calls = len(fixture.calls)
        self.assertEqual(next(job['error'] for job in stopped['jobs'] if job['id'] == fixture.failed_plan['job_id']),
                         'native_auth_unconfirmed' if legacy_label else 'native_provider_error')
        self.assertEqual(stopped['team']['phase'], 'unknown')
        self.assertTrue(stopped['continuation']['can_send'], stopped['continuation'])
        code, value = fixture.continue_http(stopped)
        self.assertEqual(code, 200, value)
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        handoff = final['objective']['continuation']['native_context_handoff']
        self.assertEqual({key: handoff[key] for key in ('source_run_id', 'source_job_id', 'reason', 'sessions',
                                                       'old_sessions_preserved', 'old_calls_replayed')},
                         {'source_run_id': stopped['current_run']['id'], 'source_job_id': fixture.failed_plan['job_id'],
                          'reason': 'provider_context_limit', 'sessions': 'fresh',
                          'old_sessions_preserved': True, 'old_calls_replayed': False})
        self.assertEqual(fixture.new_context['objective']['continuation']['native_context_handoff'], handoff)
        self.assertNotEqual(final['current_run']['id'], stopped['current_run']['id'])
        self.assertEqual(final['objective']['original_goal'], stopped['objective']['original_goal'])
        self.assertEqual({job['id']: (job['status'], job['error']) for job in final['jobs'] if job['id'] in old_jobs}, old_jobs)
        code, archived = fixture.request('GET', f"/api/rooms/{fixture.room_id}/runs?run_id={fixture.original['current_run']['id']}")
        self.assertEqual(code, 200, archived)
        self.assertEqual(archived['team']['tasks'], original_tasks)
        self.assertEqual(fixture.failed_wire.read_bytes(), old_wire)
        import hashlib
        self.assertEqual(hashlib.sha256((fixture.source / 'README.md').read_bytes()).hexdigest(),
                         fixture.original_readme_hash)
        later = fixture.calls[prior_calls:]
        self.assertTrue(later)
        self.assertTrue(all(call['permission'] == 'full_access' for call in later))
        self.assertTrue(all(call['input_sid'] is None for call in later if call['agent'] == 'kimi' and call['phase'] == 'plan'))
        self.assertFalse(any(item['result']['session_id'] == fixture.source_sid for item in fixture.results if item['job_id'] not in old_jobs))
        self.assertEqual(fixture.work_tasks.count('T1'), 1)
        self.assertEqual(fixture.work_tasks.count('T2'), 1)
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))

    def test_other_auth403_is_not_a_context_recovery_authority(self):
        fixture = self.fixture('other_auth403')
        stopped = fixture.stopped_context_run()
        calls = len(fixture.calls)
        code, value = fixture.continue_http(stopped)
        self.assertEqual(code, 409, value)
        self.assertEqual(len(fixture.calls), calls)
        self.assertEqual(fixture.read()['current_run']['id'], stopped['current_run']['id'])

    def test_changed_binding_private_drift_or_side_effects_cannot_reuse_context_attestation(self):
        for fault in ('binding', 'drift', 'effects', 'phase', 'wire_tail'):
            with self.subTest(fault=fault):
                fixture = self.fixture()
                stopped = fixture.stopped_context_run()
                calls = len(fixture.calls)
                if fault == 'binding':
                    rows = [json.loads(line) for line in fixture.failed_wire.read_text().splitlines()]
                    for row in reversed(rows):
                        if row['type'] == 'turn.prompt':
                            row['input'][0]['text'] = row['input'][0]['text'].replace(fixture.failed_plan['job_id'], str(uuid4()))
                            break
                    fixture.failed_wire.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                elif fault == 'wire_tail':
                    with fixture.failed_wire.open('a') as stream:
                        stream.write(json.dumps({'type': 'context.append_message',
                                                 'message': 'Unknown unbound native tail.'}) + '\n')
                elif fault == 'drift':
                    state = fixture.service.store.native_workspace(fixture.room_id, 'kimi')
                    (Path(state['project']) / 'README.md').write_text('Unauthorized drift after the failed call.\n')
                else:
                    with fixture.service.store._connection(write=True) as db:
                        if fault == 'effects':
                            row = db.execute('SELECT metadata_json FROM native_executions WHERE job_id=?', (fixture.failed_plan['job_id'],)).fetchone()
                            meta = json.loads(row[0]); meta['side_effects_possible'] = True
                            db.execute('UPDATE native_executions SET metadata_json=? WHERE job_id=?', (json.dumps(meta), fixture.failed_plan['job_id']))
                        else:
                            db.execute("UPDATE team_jobs SET phase='work' WHERE job_id=?", (fixture.failed_plan['job_id'],))
                code, value = fixture.continue_http(stopped)
                self.assertEqual(code, 409, value)
                self.assertEqual(len(fixture.calls), calls)
                self.assertEqual(fixture.read()['current_run']['id'], stopped['current_run']['id'])
                fixture.close()
