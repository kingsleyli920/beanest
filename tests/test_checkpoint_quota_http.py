"""Checkpoint-only quota refusal across HTTP, SQLite and a real fictional CLI."""
import asyncio
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, native_adapters
from test_kimi_work_quota_http import WorkQuotaFixture
from test_native_room_service import native_reply
from test_team_store import proposal


NATIVE_RUN = adapters._run


class CheckpointQuotaFixture(WorkQuotaFixture):
    def __init__(self, *, typed=True, cleanup=True, hold_successor=False, legacy=False, fault=None, late_drift=False):
        super().__init__()
        self.typed, self.clean, self.legacy, self.fault = typed, cleanup, legacy, fault
        self.failure = None
        self.checkpoint_contexts = []
        self.successor_started = threading.Event()
        self.release_successor = threading.Event()
        if not hold_successor:
            self.release_successor.set()
        cli = self.root / 'fictional-claude'
        cli.write_text(f'#!{sys.executable}\n' + r'''import json,sys,os,re
from datetime import datetime,timezone
from pathlib import Path
if 'status' in sys.argv:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'pro'}))
    sys.exit(0)
value=json.loads((Path(__file__).parent/'claude-quota-scenario.json').read_text())
prompt=sys.stdin.read()
if value.get('owned_transcript'):
    sid=value['events'][0]['session_id']
    stamp=datetime.now(timezone.utc).isoformat()
    path=Path(os.environ['HOME'])/'.claude/projects'/re.sub(r'[^a-zA-Z0-9]', '-', os.getcwd())/(sid+'.jsonl')
    path.parent.mkdir(parents=True,exist_ok=True)
    if value.get('wrong_prompt'):
        prompt=prompt.replace(value['job_id'],'00000000-0000-4000-8000-000000000001')
    rows=[{'type':'user','sessionId':sid,'timestamp':stamp,'message':{'content':prompt}},
          {'type':'assistant','sessionId':sid,'timestamp':stamp,'isApiErrorMessage':True,
           'error':'rate_limit','apiError':'usage_limit_reached',
           'message':{'role':'assistant','content':[{'type':'text','text':"You've hit your 5-hour limit · resets 4:10pm (America/Vancouver)"}]}}]
    with path.open('a') as stream:
        for row in rows:
            stream.write(json.dumps(row)+'\n')
for event in value['events']:
    print(json.dumps(event),flush=True)
sys.exit(1)
''')
        cli.chmod(0o700)
        self.stack.enter_context(patch.object(adapters, 'find_claude', return_value=str(cli)))
        self.stack.enter_context(patch.object(adapters, '_run', NATIVE_RUN))
        previous_account = adapters.account_status
        async def account_after_limit(adapter, *args, **options):
            value = await previous_account(adapter, *args, **options)
            if adapter == 'claude' and self.failure is not None and self.typed:
                value = json.loads(json.dumps(value))
                for window in value['quota']['windows']:
                    window.update(used_percent=100, remaining_percent=0)
            return value
        self.stack.enter_context(patch.object(adapters, 'account_status', side_effect=account_after_limit))
        if late_drift:
            original_snapshot = self.service._snapshot
            self.resume_snapshots = 0
            def snapshot_with_late_drift(room_id, *args, **options):
                if sys._getframe(1).f_code.co_name == '_resume_quota_checkpoint':
                    self.resume_snapshots += 1
                    if self.resume_snapshots == 2:
                        state = self.service.store.native_workspace(room_id, 'claude')
                        (Path(state['project']) / 'README.md').write_text('Drift after the first successful full audit.\n')
                return original_snapshot(room_id, *args, **options)
            self.stack.enter_context(patch.object(self.service, '_snapshot', new=snapshot_with_late_drift))

    def close(self):
        if hasattr(self, 'release_successor'):
            self.release_successor.set()
        super().close()

    def create(self):
        code, value = self.request('POST', '/api/rooms', {
            'goal': 'Finish and independently review three fictional files, then summarize the completed work.',
            'target': 'claude', 'request_id': str(uuid4()), 'mode': 'native_project', 'project_id': self.project_id,
            'participants': ['claude', 'codex', 'kimi'], 'work_mode': 'build',
            'runtime_settings': {'permission': 'full_access', 'members': {}},
            'execution_policy': {'mode': 'autonomous', 'quota_failover': True}})
        assert code == 200, value.get('error')
        self.room_id = value['room']['id']
        return value

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        phase = context['job']['job_phase']
        self.calls.append({'agent': agent, 'phase': phase, 'job_id': context['job']['id'],
                           'input_sid': session_id, 'permission': options['permission']})
        if phase == 'checkpoint' and agent == 'claude' and self.failure is None:
            sid = session_id or str(uuid4())
            events = [{'type': 'system', 'subtype': 'init', 'session_id': sid}]
            events.append({'type': 'result', 'subtype': 'success', 'session_id': sid, 'is_error': True,
                           'result': "You've hit your 5-hour limit · resets 4:10pm (America/Vancouver)" if self.typed
                                     else 'A fictional report mentions a 5-hour limit, without an official quota notice.',
                           'usage': {}})
            # Prior room-level retirement is a fictional fixture precondition,
            # not an inference from current unknown account telemetry.
            self.service._retire_quota(self.room_id, 'kimi', 'agent_quota_exhausted')
            (self.root / 'claude-quota-scenario.json').write_text(json.dumps({'events': events, 'owned_transcript': self.legacy, 'wrong_prompt': self.fault == 'prompt', 'job_id': context['job']['id']}))
            self.failure = {'job_id': context['job']['id'], 'context': context,
                            'accepted': [dict(task) for task in context['team']['tasks']]}
            self.partners._account_attempts.pop('claude', None)
            result = await native_adapters.invoke_native(agent, prompt, cwd, session_id=session_id, **options)
            if self.typed:
                await self.partners.refresh_account('claude', force=True)
            if not self.clean:
                result['native_execution']['cleanup_confirmed'] = False
                result['cleanup_confirmed'] = False
            if self.legacy:
                # Retired classification simulation only; official owned CLI
                # transcript and host receipt remain real fixture process output.
                result['failure_code'] = 'cli_nonzero_exit'
                result['native_execution'].pop('failure_diagnostic', None)
            if self.fault == 'outside_d':
                (Path(cwd) / 'README.md').write_text('Unauthorized post-work drift.\n')
            self.failure['result'] = result
            return result
        if phase == 'checkpoint' and self.failure is not None:
            self.checkpoint_contexts.append(context)
            self.successor_started.set()
            try:
                while not self.release_successor.is_set():
                    await asyncio.sleep(.005)
            except asyncio.CancelledError as error:
                error.native_execution = {'cleanup_confirmed': True, 'side_effects_possible': False,
                                          'never_started': True, 'tool_calls': 0, 'tool_results': 0, 'receipt_path': None}
                raise
        return await self.peer(agent, prompt, cwd, session_id=session_id, **options)

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == 'claude' and not context['team']['tasks']:
                commands = [proposal('T1', paths=['first.md']), proposal('T2', paths=['second.md']),
                            proposal('T3', paths=['final.md'], dependencies=['T1', 'T2']),
                            {'type': 'claim', 'task_id': 'T1'}, {'type': 'claim', 'task_id': 'T3'}]
            elif agent == 'codex' and any(t['id'] == 'T2' and t['owner'] is None for t in context['team']['tasks']):
                commands = [{'type': 'claim', 'task_id': 'T2'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        return super().reply(agent, context, cwd)


class CheckpointQuotaHTTPTests(unittest.TestCase):
    def fixture(self, **options):
        value = CheckpointQuotaFixture(**options)
        self.addCleanup(value.close)
        return value

    def test_typed_zero_tool_checkpoint_quota_automatically_finishes_with_peer_without_redoing_work(self):
        self.assert_checkpoint_handoff(legacy=False)

    def test_retired_cli_nonzero_checkpoint_requires_owned_quota_proof_and_preserves_accepted_board(self):
        self.assert_checkpoint_handoff(legacy=True)

    def assert_checkpoint_handoff(self, *, legacy):
        fixture = self.fixture(legacy=legacy)
        fixture.create()
        final = fixture.wait(lambda value: value['objective']['status'] == 'complete')
        self.assertIsNotNone(fixture.failure)
        result = fixture.failure['result']
        self.assertEqual((result['status'], result['failure_code']), ('failed', 'cli_nonzero_exit' if legacy else 'subscription_limit_reached'))
        native = result['native_execution']
        self.assertEqual((native['tool_calls'], native['tool_results'], native['exit_code']), (0, 0, 1))
        self.assertFalse(native['side_effects_possible'])
        self.assertTrue(native['cleanup_confirmed'])
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertTrue(fixture.checkpoint_contexts)
        self.assertTrue(all(context['self'] == 'codex' for context in fixture.checkpoint_contexts))
        for task in final['team']['tasks']:
            prior = next(t for t in fixture.failure['accepted'] if t['id'] == task['id'])
            for field in ('status', 'owner', 'result_version', 'result_digest', 'result_files'):
                self.assertEqual(task[field], prior[field])
        self.assertEqual(sorted(task for _, task in fixture.work_tasks), ['T1', 'T2', 'T3'])
        failure_index = next(i for i, call in enumerate(fixture.calls) if call['job_id'] == fixture.failure['job_id'])
        self.assertTrue(all(call['agent'] == 'codex' and call['phase'] in {'checkpoint', 'checkpoint_review'}
                            for call in fixture.calls[failure_index + 1:]))
        old = next(job for job in final['jobs'] if job['id'] == fixture.failure['job_id'])
        self.assertNotEqual(old['status'], 'completed')
        self.assertEqual(old['error'], 'cli_nonzero_exit' if legacy else 'subscription_limit_reached')
        self.assertEqual(final['current_run']['id'], fixture.failure['context']['job']['run_id'])
        decisions = [event for event in final['events'] if event['kind'] == 'objective_checkpoint_finished']
        self.assertEqual([(event['decision'], event['review_kind']) for event in decisions], [('complete', 'self_check')])
        if legacy:
            handoffs = [event for event in final['events'] if event['kind'] == 'native_quota_checkpoint_handoff']
            self.assertEqual(len(handoffs), 1)
            self.assertEqual(handoffs[0]['sender'], 'system')

    def test_legacy_last_fresh_sd_audit_failure_cannot_resume_on_unchanged_run_binding(self):
        fixture = self.fixture(legacy=True, late_drift=True)
        fixture.create()
        final = fixture.wait(lambda value: value['objective']['status'] == 'complete'
                             or (value['current_run'].get('native_quota_handoff') or {}).get('state') == 'blocked')
        self.assertGreaterEqual(fixture.resume_snapshots, 2)
        self.assertFalse(fixture.successor_started.is_set())
        self.assertNotEqual(final['objective']['status'], 'complete')
        self.assertEqual(final['team']['phase'], 'unknown')
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))

    def test_legacy_wrong_prompt_or_private_drift_cannot_resume_checkpoint(self):
        for fault in ('prompt', 'outside_d'):
            with self.subTest(fault=fault):
                fixture = self.fixture(legacy=True, fault=fault)
                fixture.create()
                stopped = fixture.wait(lambda value: (value['current_run'].get('native_quota_handoff') or {}).get('state') == 'blocked')
                self.assertFalse(fixture.successor_started.is_set())
                self.assertEqual(stopped['team']['phase'], 'unknown')
                self.assertTrue(all(task['status'] == 'accepted' for task in stopped['team']['tasks']))
                self.assertEqual(sorted(task for _, task in fixture.work_tasks), ['T1', 'T2', 'T3'])
                fixture.close()

    def test_untyped_limit_prose_or_unconfirmed_cleanup_cannot_auto_retire_and_resume(self):
        for options in ({'typed': False}, {'cleanup': False}):
            with self.subTest(options=options):
                fixture = self.fixture(**options)
                fixture.create()
                final = fixture.settled()
                self.assertIsNotNone(fixture.failure)
                self.assertFalse(fixture.successor_started.is_set())
                self.assertNotEqual(final['objective']['status'], 'complete')
                self.assertEqual(sorted(task for _, task in fixture.work_tasks), ['T1', 'T2', 'T3'])
                self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))
                fixture.close()

    def test_cancel_during_successor_checkpoint_does_not_restart_work_or_quota_source(self):
        fixture = self.fixture(hold_successor=True)
        fixture.create()
        held = fixture.wait(lambda value: fixture.successor_started.is_set())
        count = len(fixture.calls)
        code, value = fixture.request('POST', f'/api/rooms/{fixture.room_id}/cancel', {})
        self.assertEqual(code, 200, value.get('error'))
        fixture.release_successor.set()
        final = fixture.settled()
        self.assertEqual(len(fixture.calls), count)
        self.assertNotEqual(final['objective']['status'], 'complete')
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))
