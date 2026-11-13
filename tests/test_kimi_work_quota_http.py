"""Owned failed quota wire and retained-work handoff; fictional CLI/HTTP only."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import textwrap
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, kimi_acp
from test_kimi_acp import FAKE_ACP
from test_native_outcome_http import OutcomeFixture
from test_native_room_service import native_reply
from test_team_store import proposal


QUOTA_TURN = textwrap.dedent('''
    if case in {"work_quota", "work_rate"} and method == "session/prompt":
        append_wire({"type": "turn.prompt", "origin": {"kind": "user"}, "input": params["prompt"]})
        for index in range(21):
            key = "23:fictional-" + str(index)
            update({"sessionUpdate": "tool_call", "toolCallId": key, "kind": "execute", "status": "in_progress"})
            update({"sessionUpdate": "tool_call_update", "toolCallId": key, "kind": "execute", "status": "completed"})
        append_wire({"type": "llm.request", "kind": "loop", "model": "fictional-0", "modelAlias": "kimi-code/fictional-0"})
        append_wire({"type": "turn.ended", "turnId": 23, "reason": "failed", "error": {
            "code": "provider.api_error", "name": "APIProviderQuotaExhaustedError" if case == "work_quota" else "RateLimitError",
            "retryable": False, "details": {"statusCode": 429}, "message": "Fictional private provider error."}})
        response(row["id"], {"stopReason": "end_turn"})
        continue
''')


class WorkQuotaFixture(OutcomeFixture):
    retained = b'Fictional retained implementation; never replay the original tool sequence.\n'

    def __init__(self, *, legacy=True, quota=True, hold_inspection=False, fault=None, startup_unknown_provider=None, startup_receiver_cache=False, receiver_account_failure=False, peers_exhausted=False):
        super().__init__({'native_case': 'completed'}, bad_phase='never')
        self.release_peer.set()
        self.legacy, self.quota, self.fault = legacy, quota, fault
        self.kimi_model = 'kimi-code/fictional-0'
        self.t2_paths = ['second.md']
        self.failed_work = None
        self.release_inspection = threading.Event()
        if not hold_inspection:
            self.release_inspection.set()
        self.work_tasks, self.inspections = [], []
        self.startup_unknown_provider = startup_unknown_provider
        self.startup_receiver_cache = startup_receiver_cache
        self.receiver_account_failure = receiver_account_failure
        self.receiver_identity_ready = not startup_receiver_cache
        self.peers_exhausted, self.peers_out, self.kimi_used = peers_exhausted, False, None
        self.account_clock_offset = 0
        self.partners.clock = lambda: time.time() + self.account_clock_offset
        self.startup_unknown_active = False
        original_get = self.partners.get
        def startup_observation(provider):
            value = original_get(provider)
            if self.startup_unknown_active and provider == self.startup_unknown_provider:
                # Inject the manager observation at boot, without changing the
                # account gate or pretending Kimi has a stable account identity.
                value = {**value, 'availability': {**value['availability'], 'state': 'blocked',
                         'selectable': False, 'reason': 'account_identity_unknown'}}
            return value
        self.stack.enter_context(patch.object(self.partners, 'get', side_effect=startup_observation))
        marker = '    trace(summary)\n'
        assert FAKE_ACP.count(marker) == 1
        self.executable.write_text(f'#!{sys.executable}\n' + FAKE_ACP.replace(marker, marker + textwrap.indent(QUOTA_TURN, '    ')))
        previous = adapters.account_status

        async def account(adapter, *args, **kwargs):
            if adapter == 'kimi' and self.kimi_used is not None:
                return {'state': 'ready', 'email': None, 'plan': 'fixture', 'auth_method': 'kimi_code',
                        'source': 'fixture', 'scope': 'default_cli_login', 'reason': None, 'stale': False,
                        'quota': {'state': 'available', 'source': 'kimi_usage', 'scope': 'provider_account', 'reason': None,
                                  'stale': False, 'windows': [{'name': 'kimi:five_hour', 'scope': 'provider_account',
                                  'used_percent': self.kimi_used, 'remaining_percent': 100 - self.kimi_used,
                                  'resets_at': time.time() + 3600}]}}
            if adapter not in {'claude', 'codex'}:
                return await previous(adapter, *args, **kwargs)
            if self.receiver_account_failure and self.startup_unknown_active:
                raise OSError('Fictional account telemetry unavailable')
            if adapter == 'claude' and self.startup_unknown_active and not self.receiver_identity_ready:
                return {'state': 'unavailable', 'email': None, 'plan': None, 'auth_method': None,
                        'source': 'claude_auth_status', 'scope': 'default_cli_login', 'reason': 'account_check_failed',
                        'quota': {'state': 'unavailable', 'windows': [], 'source': 'claude_sdk_get_usage',
                                  'scope': 'provider_account', 'reason': 'account_check_failed'}}
            source, name, auth = (('claude_sdk_get_usage', 'claude:five_hour', 'claude.ai') if adapter == 'claude'
                                  else ('codex_app_server_account_rate_limits', 'codex:primary', 'chatgpt'))
            return {'state': 'ready', 'email': adapter + '@example.invalid', 'plan': 'fixture', 'auth_method': auth,
                    'source': 'fixture', 'scope': 'default_cli_login', 'reason': None, 'stale': False, 'quota': {'state': 'available', 'source': source,
                    'scope': 'provider_account', 'reason': None, 'stale': False, 'windows': [{'name': name, 'scope': 'provider_account',
                    'used_percent': 100 if self.peers_out else 20, 'remaining_percent': 0 if self.peers_out else 80,
                    'resets_at': time.time() + 3600}]}}
        self.stack.enter_context(patch.object(adapters, 'account_status', side_effect=account))
        for provider in ('claude', 'codex'):
            code, value = self.request('POST', f'/api/partners/{provider}/account/refresh', {})
            assert code == 200, value
            assert self.partners.get(provider)['availability']['state'] == 'available', (provider, self.partners.get(provider)['availability'])
        code, value = self.request('POST', '/api/partners/kimi/runtime/refresh', {})
        assert code == 200, value

    def close(self):
        if hasattr(self, "release_inspection"):
            self.release_inspection.set()
        super().close()

    def create(self):
        code, value = self.request('POST', '/api/rooms', {
            'goal': 'Finish fictional tasks while preserving accepted and interrupted work.', 'target': 'kimi',
            'request_id': str(uuid4()), 'mode': 'native_project', 'project_id': self.project_id,
            'participants': ['kimi', 'claude', 'codex'], 'work_mode': 'build',
            'runtime_settings': {'permission': 'full_access', 'members': {'kimi': {'model': self.kimi_model, 'effort': None if self.kimi_model is None else 'high'}}},
            'execution_policy': {'mode': 'autonomous', 'quota_failover': True}})
        assert code == 200, value
        self.room_id = value['room']['id']
        return value

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        selected = agent == 'kimi' and context['job']['job_phase'] == 'work' and context['task']['id'] == 'T2' and self.failed_work is None
        if context.get("native_partial_inspection"):
            self.inspection_call = {"agent": agent, "permission": options["permission"], "input_sid": session_id}
            while not self.release_inspection.is_set():
                await asyncio.sleep(.005)
        if selected:
            while not any(t['id'] == 'T1' and t['status'] == 'accepted' for t in self.service.store.snapshot(self.room_id)['team']['tasks']):
                await asyncio.sleep(.005)
            self.failed_work = {'job_id': context['job']['id'], 'cwd': str(cwd), 'context': context}
            self.native_case = 'work_quota' if self.quota else 'work_rate'
        try:
            result = await super().invoke(agent, prompt, cwd, session_id=session_id, **options)
            if selected:
                if self.startup_receiver_cache:
                    account = self.partners.get('claude')['account']
                    identity = self.partners.quota_gate.identity(account)
                    assert self.partners.quota_gate.record_limit('claude', account, bound_identity=identity)
                    self.partners._account_cache.pop('claude', None)
                    self.account_clock_offset += 60
                if self.peers_exhausted:
                    # As in the real room: an earlier call was cut off when the
                    # app quit, and its cleanup was confirmed later from its receipt.
                    store = self.service.store
                    earlier = next(job for job in store.snapshot(self.room_id)['jobs']
                                   if job['provider'] == 'kimi' and job['status'] == 'completed')
                    with store._connection(write=True) as db:
                        row = db.execute("SELECT metadata_json FROM native_executions WHERE job_id=?", (earlier['id'],)).fetchone()
                        metadata = {**json.loads(row[0]), 'status': 'cancelled', 'cleanup_confirmed': None}
                        db.execute("UPDATE native_executions SET metadata_json=? WHERE job_id=?", (json.dumps(metadata), earlier['id']))
                    store.record_job_cleanup(earlier['id'], confirmed=False)
                    store.record_job_cleanup(earlier['id'], confirmed=True)
                    self.peers_out = True
                    if self.kimi_used is not None:
                        self.kimi_used = 100
                    self.account_clock_offset += 60
                    for provider in ('claude', 'codex'):
                        self.partners._account_cache.pop(provider, None)
                        await self.partners.refresh_account(provider)
                self.startup_unknown_active = True
                if self.receiver_account_failure:
                    self.account_clock_offset += 60
                    for provider in ('claude', 'codex'):
                        self.partners._account_cache.pop(provider, None)
                        await self.partners.refresh_account(provider)
                self.failed_work['result'] = result
                if self.legacy:
                    # Simulate only the retired mapping, preserving the real old diagnostic.
                    result['failure_code'] = 'native_turn_unconfirmed'
                    result['native_execution']['failure_diagnostic'] = {
                        'phase': 'prompt', 'reason': 'provider_error', 'http_status': 429, 'prompt_sent': True}
                if self.fault == 'outside_d':
                    (Path(cwd) / 'README.md').write_text('Unauthorized drift.\n')
                elif self.fault == 'cleanup':
                    result['native_execution']['cleanup_confirmed'] = False
                elif self.fault == 'prompt':
                    wire = kimi_acp.session_wire(cwd, result['session_id'])
                    rows = [json.loads(line) for line in wire.read_text().splitlines()]
                    for row in reversed(rows):
                        if row.get('type') == 'turn.prompt':
                            row['input'][0]['text'] = row['input'][0]['text'].replace(context['job']['id'], str(uuid4()))
                            break
                    wire.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            return result
        finally:
            if selected:
                self.native_case = None

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == 'kimi' and not context['team']['tasks']:
                commands = [proposal('T1', paths=['first.md']), proposal('T2', paths=self.t2_paths, dependencies=['T1']),
                            proposal('T3', paths=['final.md'], dependencies=['T1', 'T2']), {'type': 'claim', 'task_id': 'T2'}]
            else:
                target = {'claude': 'T1', 'codex': 'T3'}.get(agent)
                if any(t['id'] == target and t['owner'] is None for t in context['team']['tasks']):
                    commands = [{'type': 'claim', 'task_id': target}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if context.get('native_partial_inspection'):
            second = Path(cwd) / 'second.md'
            self.inspections.append({'agent': agent, 'bytes': second.read_bytes() if second.exists() else None, 'context': context})
            return native_reply(context, [{'type': 'complete', 'task_id': task['id'], 'result': 'Read retained bytes without editing or replay.'}])
        if phase == 'work':
            self.work_tasks.append((agent, task['id']))
            for path in task['write_paths']:
                destination = Path(cwd) / path
                if not destination.exists():
                    destination.write_bytes(self.retained if task['id'] == 'T2' else b'Fictional accepted bytes.\n')
            return native_reply(context, [{'type': 'complete', 'task_id': task['id'], 'result': 'Checked exact fictional work.'}],
                                unchanged=[{'path': path, 'reason': 'Preserved inspected bytes.'} for path in task['write_paths']])
        return super().reply(agent, context, cwd)

    def stopped(self):
        self.create()
        value = self.settled()
        assert self.failed_work, value['team']
        self.wire = kimi_acp.session_wire(self.failed_work['cwd'], self.failed_work['result']['session_id'])
        return value

    def continue_http(self, value):
        return self.request('POST', f'/api/rooms/{self.room_id}/messages', {
            'text': '继续原目标，保留已完成成果与被中断伙伴的成果，由可用伙伴承接。', 'target': 'all',
            'request_id': str(uuid4()), 'intent': 'continue', 'binding': value['continuation']['binding']})


class KimiWorkQuotaHTTPTests(unittest.TestCase):
    def fixture(self, **kwargs):
        value = WorkQuotaFixture(**kwargs)
        self.addCleanup(value.close)
        return value

    def test_legacy_quota_failure_preserves_accepted_work_during_audited_peer_handoff(self):
        self.assert_automatic_recovery(legacy=True)

    def test_precise_future_quota_automatically_inspects_and_finishes_without_user_continue(self):
        self.assert_automatic_recovery(legacy=False)

    def assert_automatic_recovery(self, *, legacy, startup_unknown_provider=None, receiver_account_failure=False, kimi_model='kimi-code/fictional-0'):
        fixture = self.fixture(legacy=legacy, hold_inspection=True, startup_unknown_provider=startup_unknown_provider,
                               receiver_account_failure=receiver_account_failure)
        fixture.kimi_model = kimi_model
        fixture.create()
        held = fixture.wait(lambda value: hasattr(fixture, 'inspection_call'))
        failed_id = fixture.failed_work['job_id']
        result = fixture.failed_work['result']
        self.assertEqual((result['status'], result['failure_code']), ('failed', 'native_turn_unconfirmed' if legacy else 'subscription_limit_reached'))
        self.assertEqual(result['native_execution']['failure_diagnostic']['reason'], 'provider_error' if legacy else 'provider_quota_exhausted')
        self.assertEqual(next(j['error'] for j in held['jobs'] if j['id'] == failed_id), 'native_turn_unconfirmed' if legacy else 'native_side_effects_unconfirmed')
        self.assertEqual(fixture.inspection_call['permission'], 'read_only')
        self.assertIsNone(fixture.inspection_call['input_sid'])
        self.assertNotEqual(fixture.inspection_call['agent'], 'kimi')
        if receiver_account_failure:
            for provider in ('claude', 'codex'):
                observation = fixture.partners.get(provider)['availability']
                self.assertEqual((observation['state'], observation['reason'], observation['selectable']),
                                 ('unknown', 'account_check_failed', True))
        native = next(item for item in held['native_executions'] if item['job_id'] == failed_id)
        self.assertEqual((native['status'], native['exit_code'], native['tool_calls'], native['tool_results']), ('failed', 0, 21, 21))
        self.assertTrue(native['cleanup_confirmed'])
        self.assertTrue(native['side_effects_possible'])
        self.assertEqual(fixture.partners.get('kimi')['availability']['state'], 'blocked' if startup_unknown_provider == 'kimi' else 'unknown')
        first = next(task for task in held['team']['tasks'] if task['id'] == 'T1')
        old_wire = kimi_acp.session_wire(fixture.failed_work['cwd'], result['session_id']).read_bytes()
        before = {j['id']: (j['status'], j['error']) for j in held['jobs'] if j['id'] == failed_id}
        source = Path(fixture.failed_work['cwd']) / 'second.md'
        self.assertEqual(source.read_bytes(), fixture.retained)
        self.assertFalse(any(item['path'] == 'second.md' for item in fixture.service._project(fixture.room_id).snapshot()['files']))
        for _ in range(3):
            fixture.read()
        fixture.release_inspection.set()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(len(fixture.inspections), 1)
        self.assertEqual(fixture.inspections[0]['bytes'], fixture.retained)
        failure_index = next(i for i, call in enumerate(fixture.calls) if call['job_id'] == failed_id)
        self.assertFalse(any(call['agent'] == 'kimi' for call in fixture.calls[failure_index + 1:]))
        self.assertEqual(sum(task == 'T1' for _, task in fixture.work_tasks), 1)
        self.assertEqual({j['id']: (j['status'], j['error']) for j in final['jobs'] if j['id'] in before}, before)
        self.assertEqual(source.read_bytes(), fixture.retained)
        self.assertEqual(kimi_acp.session_wire(fixture.failed_work['cwd'], result['session_id']).read_bytes(), old_wire)
        current_first = next(task for task in final['team']['tasks'] if task['id'] == 'T1')
        for key in ('owner', 'result_version', 'result_digest'):
            self.assertEqual(current_first[key], first[key])
        recovery = [event for event in final['events'] if event['kind'] == 'native_recovery_started']
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]['sender'], 'system')
        self.assertTrue(all(event['sender'] == 'system' for event in final['events']
                            if event['kind'] == 'native_partial_recovery_attestation'))

    def test_quota_handoff_works_when_kimi_follows_its_tool_default_model(self):
        # Catches the handoff never starting because no model was chosen for Kimi ("跟随工具"), which left a real room stopped.
        self.assert_automatic_recovery(legacy=False, kimi_model=None)

    def test_a_task_that_writes_no_files_is_handed_over_when_kimi_runs_out_of_quota(self):
        # Catches the real 九烛 stop: Kimi hit its quota on a measurement task with no write paths, the
        # handoff found no candidate, and the whole objective stopped as native_side_effects_unconfirmed.
        fixture = self.fixture(legacy=False)
        fixture.t2_paths = []
        fixture.create()
        final = fixture.wait(lambda value: value['objective']['status'] in {'complete', 'needs_user'}
                             or value['objective']['status'] == 'stopped' and fixture.service._quota_handoffs.get(fixture.room_id, {}).get('state') != 'in_progress')
        self.assertTrue(fixture.failed_work, final['team'])
        failed_id = fixture.failed_work['job_id']
        self.assertEqual(fixture.failed_work['result']['failure_code'], 'subscription_limit_reached')
        self.assertEqual(final['objective']['status'], 'complete', (final['team'], final['objective']))
        failure_index = next(i for i, call in enumerate(fixture.calls) if call['job_id'] == failed_id)
        self.assertFalse(any(call['agent'] == 'kimi' for call in fixture.calls[failure_index + 1:]))
        self.assertTrue(any(event['kind'] == 'native_recovery_started' and event['sender'] == 'system' for event in final['events']))
        second = next(task for task in final['team']['tasks'] if task['id'] == 'T2')
        self.assertNotEqual(second['owner'], 'kimi')
        self.assertIn(second['status'], {'accepted', 'self_checked'})

    def test_account_query_unavailable_but_selectable_receivers_automatically_take_retained_work(self):
        self.assert_automatic_recovery(legacy=True, receiver_account_failure=True)

    def test_boot_unknown_quota_source_does_not_block_exact_audited_handoff(self):
        self.assert_automatic_recovery(legacy=True, startup_unknown_provider='kimi')

    def test_boot_unknown_other_member_identity_is_not_bypassed(self):
        fixture = self.fixture(startup_unknown_provider='claude')
        fixture.create()
        stopped = fixture.wait(lambda value: (value['current_run'].get('native_quota_handoff') or {}).get('state') == 'blocked')
        self.assertEqual(stopped['current_run']['native_quota_handoff']['reason'], 'agent_quota_unconfirmed')
        calls = len(fixture.calls)
        code, value = fixture.request('POST', f'/api/rooms/{fixture.room_id}/recover-native',
                                      {'request_id': str(uuid4()), **stopped['continuation']['binding']})
        self.assertEqual(code, 409, value.get('error'))
        self.assertEqual(len(fixture.calls), calls)
        self.assertFalse(fixture.inspections)
        self.assertEqual(fixture.read()['current_run']['id'], stopped['current_run']['id'])

    def test_boot_receiver_identity_refresh_retries_automatically_without_user_continue(self):
        fixture = self.fixture(startup_receiver_cache=True, hold_inspection=True)
        fixture.create()
        stopped = fixture.wait(lambda value: (value['current_run'].get('native_quota_handoff') or {}).get('state') == 'blocked')
        self.assertEqual(stopped['current_run']['native_quota_handoff']['reason'], 'agent_quota_unconfirmed')
        self.assertEqual(fixture.partners.get('claude')['availability']['reason'], 'account_identity_unknown')
        original_run = stopped['current_run']['id']
        self.assertFalse(hasattr(fixture, 'inspection_call'))
        # Actual manager refresh reads a new canonical fictional identity/window;
        # only its coalescing clock is advanced, never the wire/lease clock.
        fixture.receiver_identity_ready = True
        fixture.account_clock_offset += 60
        code, value = fixture.request('POST', '/api/partners/claude/account/refresh', {})
        self.assertEqual(code, 200, value.get('error'))
        self.assertEqual(fixture.partners.get('claude')['availability']['state'], 'available')
        held = fixture.wait(lambda value: hasattr(fixture, 'inspection_call'))
        self.assertNotEqual(held['current_run']['id'], original_run)
        self.assertEqual(fixture.inspection_call['permission'], 'read_only')
        fixture.release_inspection.set()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(len(fixture.inspections), 1)
        failure_index = next(i for i, call in enumerate(fixture.calls) if call['job_id'] == fixture.failed_work['job_id'])
        self.assertFalse(any(call['agent'] == 'kimi' for call in fixture.calls[failure_index + 1:]))

    def test_quota_source_takes_its_own_task_back_when_no_peer_has_quota(self):
        # Catches: every partner out of quota after a mid-work quota failure
        # showed "result unconfirmed, will not rerun" and never resumed, though
        # the source's own quota came back.
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_source_takes_task_back(legacy)

    def assert_source_takes_task_back(self, legacy):
        fixture = self.fixture(legacy=legacy, peers_exhausted=True)
        fixture.kimi_used = 10  # Usage read before the call failed.
        code, value = fixture.request('POST', '/api/partners/kimi/account/refresh', {})
        self.assertEqual(code, 200, value.get('error'))
        self.assertTrue(fixture.service._quota_returned(['kimi']))
        fixture.create()
        waiting = fixture.wait(lambda value: (value['current_run'].get('native_quota_handoff') or {}).get('state') == 'blocked')
        self.assertEqual(waiting['current_run']['native_quota_handoff']['reason'], 'agent_quota_exhausted')
        self.assertEqual(waiting['stop_kind'], 'quota_wait')
        code, rooms = fixture.request('GET', '/api/rooms')
        self.assertEqual(code, 200)
        self.assertEqual(next(room for room in rooms['rooms'] if room['id'] == fixture.room_id)['stop_kind'], 'quota_wait')
        calls = len(fixture.calls)
        time.sleep(1)
        self.assertEqual(len(fixture.calls), calls)
        fixture.account_clock_offset += 60
        code, value = fixture.request('POST', '/api/partners/kimi/account/refresh', {})
        self.assertEqual(code, 200, value.get('error'))
        fixture.read()  # The page reads the room as it polls; usage read as full after the refusal.
        time.sleep(1)
        self.assertEqual(len(fixture.calls), calls, 'still exhausted: no new call')
        fixture.kimi_used = 10
        fixture.account_clock_offset += 60
        code, value = fixture.request('POST', '/api/partners/kimi/account/refresh', {})
        self.assertEqual(code, 200, value.get('error'))
        deadline = time.monotonic() + 60  # Kimi alone finishes the rest of the board and its checkpoint.
        while fixture.read()['objective']['status'] != 'complete' and time.monotonic() < deadline:
            time.sleep(.05)
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        failed_id = fixture.failed_work['job_id']
        failure_index = next(i for i, call in enumerate(fixture.calls) if call['job_id'] == failed_id)
        later = fixture.calls[failure_index + 1:]
        self.assertTrue(later)
        self.assertEqual({call['agent'] for call in later}, {'kimi'})
        second = next(task for task in final['team']['tasks'] if task['id'] == 'T2')
        self.assertEqual(second['owner'], 'kimi')
        self.assertEqual((Path(fixture.failed_work['cwd']) / 'second.md').read_bytes(), fixture.retained)
        self.assertEqual(sum(event['kind'] == 'native_recovery_started' for event in final['events']), 1)

    def test_chat_during_automatic_inspection_records_message_without_replaying_and_cancel_wins(self):
        fixture = self.fixture(legacy=False, hold_inspection=True)
        fixture.create()
        held = fixture.wait(lambda value: hasattr(fixture, 'inspection_call'))
        calls = len(fixture.calls)
        code, value = fixture.request('POST', f'/api/rooms/{fixture.room_id}/messages', {
            'text': '请保留现有成果，补充这条虚构说明。', 'target': 'all', 'request_id': str(uuid4())})
        self.assertEqual(code, 200, value)
        self.assertTrue(any(event['kind'] == 'message' and event['sender'] == 'user'
                            and event.get('text') == '请保留现有成果，补充这条虚构说明。' for event in fixture.read()['events']))
        self.assertEqual(len(fixture.calls), calls)
        code, value = fixture.request('POST', f'/api/rooms/{fixture.room_id}/cancel', {})
        self.assertEqual(code, 200, value)
        fixture.release_inspection.set()
        final = fixture.settled()
        self.assertEqual(len(fixture.calls), calls)
        self.assertNotEqual(final['objective']['status'], 'complete')
        self.assertEqual((Path(fixture.failed_work['cwd']) / 'second.md').read_bytes(), fixture.retained)

    def test_generic_429_is_not_quota_retirement_or_recovery_authority(self):
        fixture = self.fixture(quota=False)
        stopped = fixture.stopped()
        calls = len(fixture.calls)
        code, value = fixture.continue_http(stopped)
        self.assertIn(code, (200, 409), value.get('error'))
        code, refused = fixture.request('POST', f'/api/rooms/{fixture.room_id}/recover-native',
                                        {'request_id': str(uuid4()), **stopped['continuation']['binding']})
        self.assertEqual(code, 409, refused.get('error'))
        self.assertEqual(len(fixture.calls), calls)
        self.assertEqual(fixture.read()['current_run']['id'], stopped['current_run']['id'])

    def test_outside_d_cleanup_unknown_and_wrong_prompt_reject_without_new_calls(self):
        for fault in ('outside_d', 'cleanup', 'prompt'):
            with self.subTest(fault=fault):
                fixture = self.fixture(fault=fault)
                stopped = fixture.stopped()
                calls = len(fixture.calls)
                code, value = fixture.continue_http(stopped)
                self.assertIn(code, (200, 409), value.get('error'))
                code, refused = fixture.request('POST', f'/api/rooms/{fixture.room_id}/recover-native',
                                                {'request_id': str(uuid4()), **stopped['continuation']['binding']})
                self.assertEqual(code, 409, refused.get('error'))
                self.assertEqual(len(fixture.calls), calls)
                self.assertEqual(fixture.read()['current_run']['id'], stopped['current_run']['id'])
                fixture.close()
