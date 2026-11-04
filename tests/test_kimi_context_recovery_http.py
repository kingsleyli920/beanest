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
    if case == "compaction_quota" and method == "session/prompt":
        # Kimi wraps a 5-hour limit hit by auto compaction and still ends the turn.
        append_wire({"type": "turn.prompt", "origin": {"kind": "user"}, "input": params["prompt"]})
        append_wire({"type": "full_compaction.begin", "source": "auto"})
        append_wire({"type": "llm.request", "kind": "compaction", "model": "fictional-0", "modelAlias": "kimi-code/fictional-0", "droppedCount": 0})
        append_wire({"type": "full_compaction.cancel"})
        append_wire({"type": "turn.ended", "turnId": 23, "reason": "failed", "error": {
            "code": "compaction.failed", "name": "Error2", "retryable": False, "message": "Fictional private provider error.",
            "cause": {"code": "provider.api_error", "name": "APIProviderQuotaExhaustedError", "retryable": False,
                      "message": "Fictional private provider error.", "details": {"statusCode": 429}}}})
        send({"id": row["id"], "result": {"stopReason": "end_turn"}})
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
                if self.failure == 'compaction_quota':
                    # Rooms stuck before wrapped quota refusals were classified.
                    result['failure_code'] = 'native_turn_unconfirmed'
                    result['native_execution']['failure_diagnostic'] = {
                        'phase': 'compaction', 'reason': 'provider_error', 'prompt_sent': True}
                return result
            finally:
                self.native_case = previous
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)

    def reply(self, agent, context, cwd):
        if self.recovered and context['job']['job_phase'] in {'plan', 'coordinate'}:
            commands = []
            if agent == context['team']['plan_author'] and not context['team']['tasks']:
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

    def test_compaction_quota_stuck_room_continues_by_retiring_kimi_without_replaying_it(self):
        # Catches: a room stopped as native_turn_unconfirmed by a wrapped Kimi
        # compaction quota refusal had no recovery path, so every continue,
        # retry and directed message was refused for good.
        fixture = self.fixture('compaction_quota')
        stopped = fixture.stopped_context_run()
        self.assertEqual(next(job['error'] for job in stopped['jobs'] if job['id'] == fixture.failed_plan['job_id']),
                         'native_turn_unconfirmed')
        self.assertEqual((stopped['team']['phase'], stopped['team']['stop_reason']), ('unknown', 'native_turn_unconfirmed'))
        self.assertTrue(stopped['current_run']['can_recover_native'], stopped['current_run'])
        old_jobs = {job['id']: (job['status'], job['error']) for job in stopped['jobs']}
        old_wire = fixture.failed_wire.read_bytes()
        prior_calls = len(fixture.calls)
        # The script blocks only a first T3 attempt and counts Kimi's; the
        # lone peer's resumed T3 is a later attempt of the same task.
        fixture.t3_calls.append({'job_id': None})
        code, value = fixture.continue_http(stopped)
        self.assertEqual(code, 200, value)
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(final['objective']['retired_partners'].get('kimi', {}).get('reason'), 'subscription_limit_reached')
        self.assertEqual(final['objective']['continuation']['native_context_handoff']['reason'], 'provider_quota_exhausted')
        later = fixture.calls[prior_calls:]
        self.assertTrue(later)
        self.assertFalse(any(call['agent'] == 'kimi' for call in later))
        self.assertEqual({job['id']: (job['status'], job['error']) for job in final['jobs'] if job['id'] in old_jobs}, old_jobs)
        self.assertEqual(fixture.failed_wire.read_bytes(), old_wire)
        self.assertTrue(all(task['status'] in {'accepted', 'self_checked'} for task in final['team']['tasks']))
        self.assertTrue({'T3', 'T4'} <= {task['id'] for task in final['team']['tasks']})

    def test_compaction_quota_recovery_resumes_invalid_target_board_with_owner_private_work(self):
        # Catches: the stuck room's lone peer kept unpublished work for a task
        # blocked by the retired invalid_target rejection; recovery refused it
        # as unpublished changes or replanned from an empty board and lost it.
        fixture = self.fixture('compaction_quota')
        stopped = fixture.stopped_context_run()
        board_run = fixture.original['current_run']['id']
        with fixture.service.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET owner='claude',status='blocked',blocked_reason='invalid_target',block_cause=NULL "
                       "WHERE run_id=? AND id='T3'", (board_run,))
        retained = b'Fictional lone-peer report that was never published.\n'
        state = fixture.service.store.native_workspace(fixture.room_id, 'claude')
        (Path(state['project']) / 'final.md').write_bytes(retained)
        snapshot = fixture.read()
        self.assertTrue(snapshot['current_run']['can_recover_native'], snapshot['current_run'])
        seen, original_reply = [], fixture.reply
        def reply(agent, context, cwd):
            if context['job']['job_phase'] == 'work' and (context.get('task') or {}).get('id') == 'T3':
                seen.append((agent, (Path(cwd) / 'final.md').read_bytes()))
                return native_reply(context, [{'type': 'complete', 'task_id': 'T3', 'result': 'Published the retained report.'}])
            return original_reply(agent, context, cwd)
        fixture.reply = reply
        prior_calls = len(fixture.calls)
        code, value = fixture.continue_http(snapshot)
        self.assertEqual(code, 200, value)
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(final['objective']['retired_partners'].get('kimi', {}).get('reason'), 'subscription_limit_reached')
        self.assertEqual(seen, [('claude', retained)])
        self.assertFalse(any(call['agent'] == 'kimi' for call in fixture.calls[prior_calls:]))
        self.assertEqual({task['id']: task['status'] for task in final['team']['tasks']},
                         {'T1': 'accepted', 'T2': 'accepted', 'T3': 'self_checked'})
        published = fixture.service._project(fixture.room_id).files_for_paths(['final.md'])
        self.assertEqual([(file['owner'], file['content']) for file in published], [('claude', retained.decode())])

    def test_resumed_board_never_retains_private_work_outside_the_owner_task_paths(self):
        # Catches: retaining a resumed owner's private work must not become a
        # way to carry arbitrary unpublished files into the next run.
        fixture = self.fixture('compaction_quota')
        fixture.stopped_context_run()
        with fixture.service.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET owner='claude',status='blocked',blocked_reason='invalid_target',block_cause=NULL "
                       "WHERE run_id=? AND id='T3'", (fixture.original['current_run']['id'],))
        state = fixture.service.store.native_workspace(fixture.room_id, 'claude')
        (Path(state['project']) / 'final.md').write_text('Fictional retained report.\n')
        (Path(state['project']) / 'unowned-notes.md').write_text('Fictional file outside every resumed task.\n')
        snapshot = fixture.read()
        self.assertFalse(snapshot['current_run']['can_recover_native'])
        self.assertEqual(snapshot['current_run']['native_recovery_reason'], 'native_recovery_unpublished_changes')
        calls = len(fixture.calls)
        code, value = fixture.continue_http(snapshot)
        self.assertEqual(code, 409, value)
        self.assertEqual(len(fixture.calls), calls)

    def test_room_list_poll_skips_private_tree_audit_and_keeps_the_outcome(self):
        # Catches: the 3-second room-list poll ran each room's full recovery
        # audit (hashing every private tree) although it shows only outcomes.
        fixture = self.fixture('compaction_quota')
        stopped = fixture.stopped_context_run()
        self.assertTrue(stopped['current_run']['can_recover_native'], stopped['current_run'])
        audits, audit = [], fixture.service._audit_native_recovery
        fixture.service._audit_native_recovery = lambda *args, **kwargs: audits.append(1) or audit(*args, **kwargs)
        code, listed = fixture.request('GET', '/api/rooms?view=compact')
        self.assertEqual(code, 200, listed)
        self.assertEqual(audits, [])
        room = next(item for item in listed['rooms'] if item['id'] == fixture.room_id)
        detail = fixture.read()
        self.assertEqual(len(audits), 1)
        self.assertEqual((room['outcome_state'], room['objective_status'], room['work_activity']),
                         (detail['collaboration']['outcome']['state'], detail['objective']['status'], detail['room']['work_activity']))
        with fixture.service.store._connection() as db:
            indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({'events_room_kind', 'events_job'} <= indexes)

    def test_partner_retired_from_the_objective_cannot_veto_recovery_with_an_unreadable_login(self):
        # Catches: a peer already retired for quota whose login could not be
        # re-read (stale CLI, missing email) refused Continue for every peer.
        from unittest.mock import patch
        fixture = self.fixture('compaction_quota')
        stopped = fixture.stopped_context_run()
        with fixture.service.store._connection(write=True) as db:
            db.execute("UPDATE objectives SET retired_json=? WHERE id=?", (json.dumps({'kimi': {
                'reason': 'subscription_limit_reached', 'at': 1.0, 'cleanup_confirmed': True}}), stopped['objective']['id']))
        original = fixture.partners.get
        def get(provider):
            value = original(provider)
            if provider == 'kimi':
                value = {**value, 'availability': {**value['availability'], 'state': 'blocked',
                         'selectable': False, 'reason': 'account_identity_unknown'}}
            return value
        fixture.t3_calls.append({'job_id': None})  # the lone peer resumes a later T3 attempt
        with patch.object(fixture.partners, 'get', side_effect=get):
            code, value = fixture.continue_http(fixture.read())
            self.assertEqual(code, 200, value)
            final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])

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
