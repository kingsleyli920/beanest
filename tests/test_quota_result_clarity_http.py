"""Fictional quota observations and completed results over real host HTTP/SQLite."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, kimi_usage
from local_agent_coordinator.partner_manager import ACCOUNT_REFRESH_COALESCE_SECONDS
from test_kimi_setup_http import SetupFixture
from test_kimi_native_setup import configuration
from test_native_room_service import NativeTransport, native_reply
from test_team_store import proposal


LIMITATION = '仍未覆盖：虚构的边缘输入没有验证，不能据此宣称全部问题已经解决。'
SUMMARY = '本轮已完成虚构修复、对应检查和交付核对。' * 22 + '\n\n' + LIMITATION
HISTORICAL_PLAN = '此前计划：等待 T3 核对虚构交付，再决定后续范围。这是一份当时的计划记录。'


class QuotaResultFixture(SetupFixture):
    def __init__(self):
        super().__init__(discover=False)
        self.config.write_text(configuration(), encoding='utf-8')
        self.quota_mode = 'available'
        self.telemetry_now = time.time()
        self.telemetry_calls = 0
        self.partners.clock = lambda: self.telemetry_now
        self.partners.quota_gate.clock = lambda: self.telemetry_now
        previous = adapters.account_status

        async def telemetry(adapter, *args, **options):
            if adapter != 'kimi':
                return await previous(adapter, *args, **options)
            self.telemetry_calls += 1
            used = 100 if self.quota_mode == 'exhausted' else 37
            quota = kimi_usage.parse_quota({'kind': 'ok', 'limits': [
                {'window': {'duration': 5, 'unit': 'hour'}, 'used': used, 'limit': 100,
                 'reset_at': '2030-01-01T00:00:00Z'}]})
            authenticated = self.quota_mode != 'signed-out'
            if self.quota_mode in {'unavailable', 'signed-out'}:
                quota = {**kimi_usage.parse_quota(None),
                         'reason': 'provider_usage_unavailable' if authenticated else 'auth_unconfirmed'}
            return {'state': 'ready' if quota['state'] == 'available' else 'partial' if authenticated else 'unavailable',
                    'email': None, 'plan': None, 'auth_method': 'kimi-code' if authenticated else None,
                    'source': 'kimi_managed_oauth', 'scope': 'default_cli_login',
                    'reason': 'provider_account_details_not_exposed' if authenticated else 'auth_unconfirmed', 'quota': quota}

        self.stack.enter_context(patch.object(adapters, 'account_status', side_effect=telemetry))
        self.native = NativeTransport()
        self.native.handler = self.reply
        self.service.native_invoke = self.native
        code, result = self.request('POST', '/api/partners/discover', {})
        if code != 200:
            raise AssertionError(result)
        code, project = self.request('POST', '/api/projects', {
            'name': 'Fictional result clarity fixture', 'path': str(self.source), 'selection_kind': 'folder'})
        if code != 200:
            raise AssertionError(project)
        self.project_id = project['project']['id']

    def set_quota_mode(self, mode):
        if mode not in {'available', 'unavailable', 'exhausted', 'signed-out'}:
            raise ValueError('unknown fictional observation')
        self.quota_mode = mode
        # A test clock advances beyond refresh coalescing; no production cache,
        # credential or identity proof is changed by the fixture control.
        self.telemetry_now += ACCOUNT_REFRESH_COALESCE_SECONDS + 1

    @staticmethod
    def reply(agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        commands = []
        if phase in {'plan', 'coordinate'}:
            tasks = context['team']['tasks']
            owned = {'kimi': 'T1', 'claude': 'T2', 'codex': 'T3'}[agent]
            if not tasks and agent == 'kimi':
                commands = [proposal('T1', paths=[]), proposal('T2', paths=[]),
                            proposal('T3', paths=[], dependencies=['T1', 'T2']), {'type': 'claim', 'task_id': 'T1'}]
            elif any(item['id'] == owned and item['owner'] is None for item in tasks):
                commands = [{'type': 'claim', 'task_id': owned}]
            commands.append({'type': 'plan_ready'})
        elif phase == 'work':
            commands = [{'type': 'complete', 'task_id': task['id'],
                         'result': HISTORICAL_PLAN if task['id'] == 'T2' else '虚构交付已经核对。'}]
        elif phase == 'review':
            commands = [{'type': 'review', 'task_id': task['id'], 'verdict': 'accept', 'reason': '已检查虚构文本。'}]
        elif phase == 'checkpoint':
            commands = [{'type': 'checkpoint', 'decision': 'complete', 'summary': SUMMARY, 'next_goal': None,
                         'question': None, 'scope_reference': context['objective']['scope_reference']}]
        elif phase == 'checkpoint_review':
            commands = [{'type': 'checkpoint_review', 'verdict': 'accept', 'reason': '符合虚构范围，保留末段限制。',
                         'proposal_digest': context['objective']['checkpoint_proposal']['proposal_digest']}]
        else:
            raise AssertionError('Fixture must never receive an unrequested new phase: ' + phase)
        return native_reply(context, commands)

    def create_completed(self):
        code, created = self.request('POST', '/api/rooms', {
            'goal': 'Complete and independently review three fictional text tasks.', 'target': 'kimi',
            'request_id': str(uuid4()), 'mode': 'native_project', 'project_id': self.project_id,
            'participants': ['kimi', 'claude', 'codex'], 'work_mode': 'build',
            'runtime_settings': {'permission': 'full_access', 'members': {}},
            'execution_policy': {'mode': 'autonomous'}})
        if code != 200:
            raise AssertionError(created)
        self.room_id = created['room']['id']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code, value = self.request('GET', f'/api/rooms/{self.room_id}')
            if code == 200 and value['objective']['status'] == 'complete' and not any(
                    j['status'] in {'queued', 'working'} for j in value['jobs']):
                return value
            time.sleep(.005)
        raise AssertionError('Fictional completion did not settle')

    def refresh_kimi(self):
        code, result = self.request('POST', '/api/partners/kimi/account/refresh', {})
        if code != 200:
            raise AssertionError(result)
        return next(partner for partner in result['partners'] if partner['id'] == 'kimi')


class QuotaResultClarityHTTPTests(unittest.TestCase):
    def fixture(self):
        value = QuotaResultFixture()
        self.addCleanup(value.close)
        return value

    def test_email_less_quota_failure_stays_honest_and_current_exhaustion_is_never_cached_away(self):
        fixture = self.fixture()
        good = fixture.refresh_kimi()
        self.assertIsNone(good['account']['email'])
        self.assertEqual(good['account']['quota']['windows'][0]['remaining_percent'], 63)
        calls = fixture.telemetry_calls
        self.assertEqual(fixture.request('GET', '/api/partners')[0], 200)
        self.assertEqual(fixture.telemetry_calls, calls)
        fixture.set_quota_mode('unavailable')
        failed = fixture.refresh_kimi()
        self.assertEqual(failed['account']['quota']['windows'], [])
        self.assertEqual(failed['account']['quota']['reason'], 'provider_usage_unavailable')
        self.assertEqual(failed['availability']['reason'], 'quota_unknown')
        self.assertGreater(failed['account']['quota']['checked_at'], good['account']['quota']['checked_at'])
        fixture.set_quota_mode('exhausted')
        exhausted = fixture.refresh_kimi()['account']['quota']
        self.assertEqual((exhausted['state'], exhausted['windows'][0]['remaining_percent']), ('available', 0))
        self.assertFalse(exhausted['stale'])
        fixture.set_quota_mode('signed-out')
        signed_out = fixture.refresh_kimi()['account']
        self.assertIsNone(signed_out['auth_method'])
        self.assertEqual(signed_out['quota']['windows'], [])
        fixture.set_quota_mode('available')
        current = fixture.refresh_kimi()['account']['quota']
        self.assertEqual(current['windows'][0]['remaining_percent'], 63)
        self.assertFalse(current['stale'])
        self.assertEqual(fixture.native.calls, [])

    def test_completed_http_snapshot_preserves_late_limitations_and_historical_plan_without_a_next_goal(self):
        fixture = self.fixture()
        completed = fixture.create_completed()
        self.assertGreater(len(SUMMARY), 240)
        self.assertNotIn(LIMITATION, SUMMARY[:240])
        self.assertEqual(completed['objective']['next_step']['summary'], SUMMARY)
        self.assertIsNone(completed['objective']['next_step']['goal'])
        tasks = {task['id']: task for task in completed['team']['tasks']}
        self.assertTrue(all(task['status'] == 'accepted' for task in tasks.values()))
        self.assertEqual(tasks['T2']['result'], HISTORICAL_PLAN)
        ids = [job['id'] for job in completed['jobs']]
        calls = len(fixture.native.calls)
        for _ in range(2):
            code, read = fixture.request('GET', f'/api/rooms/{fixture.room_id}')
            self.assertEqual(code, 200)
            self.assertIsNone(read['objective']['next_step']['goal'])
            self.assertEqual([job['id'] for job in read['jobs']], ids)
        self.assertEqual(len(fixture.native.calls), calls)
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.native.calls))


def fixture_main():
    fixture = QuotaResultFixture()
    try:
        value = fixture.create_completed()
        fixture.set_quota_mode('unavailable')
        fixture.refresh_kimi()
        print(json.dumps({'url': fixture.server.url, 'room_id': fixture.room_id, 'root': str(fixture.root),
                          'objective': value['objective']['status'], 'summary_chars': len(SUMMARY),
                          'next_goal': None, 'real_model_calls': 0,
                          'commands': ['available', 'unavailable', 'exhausted', 'signed-out', 'status', 'stop']}), flush=True)
        for line in sys.stdin:
            mode = line.strip()
            if mode == 'stop':
                break
            if mode in {'available', 'unavailable', 'exhausted', 'signed-out'}:
                fixture.set_quota_mode(mode)
                profile = fixture.refresh_kimi()
                print(json.dumps({'mode': mode, 'quota': profile['account']['quota'], 'availability': profile['availability']},
                                 ensure_ascii=False), flush=True)
            elif mode == 'status':
                print(json.dumps({'native_calls': len(fixture.native.calls), 'telemetry_calls': fixture.telemetry_calls}), flush=True)
    finally:
        fixture.close()
        print(json.dumps({'closed': True, 'temporary_removed': not fixture.root.exists()}), flush=True)


if __name__ == '__main__':
    if '--fixture' in sys.argv:
        fixture_main()
    else:
        unittest.main()
