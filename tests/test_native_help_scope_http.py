"""Request-help scope rejection over real HTTP/SQLite and owned ACP stdio."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError
import threading
import time

from local_agent_coordinator import adapters
import unittest

from test_native_outcome_http import OutcomeFixture
import test_native_outcome_http as outcome
from test_native_room_service import native_reply
from test_team_store import proposal


class HelpScopeFixture(OutcomeFixture):
    def __init__(self, case='unowned', *, hold_correction=False):
        super().__init__({'native_case': 'completed'}, bad_phase='work', repeated=True)
        self.help_case = case
        self.release_peer.set()
        self.release_correction = threading.Event()
        if not hold_correction:
            self.release_correction.set()
        self.t3_calls = []
        self.work_tasks = []
        self.private_hashes = []
        self.retired_seed = False

    def observe_available_peer_quota(self):
        previous = adapters.account_status  # SetupFixture's fictional account source.
        async def account(adapter, *args, **options):
            if adapter != 'claude':
                return await previous(adapter, *args, **options)
            return {'state': 'ready', 'email': 'fictional-claude@example.invalid', 'plan': 'fixture',
                    'auth_method': 'claude.ai', 'source': 'claude_auth_status', 'scope': 'default_cli_login',
                    'reason': None, 'quota': {'state': 'available', 'source': 'claude_sdk_get_usage',
                    'scope': 'provider_account', 'reason': None, 'windows': [{'name': 'claude:five_hour',
                    'scope': 'provider_account', 'used_percent': 20, 'remaining_percent': 80,
                    'resets_at': time.time() + 3600}]}}
        self.stack.enter_context(patch.object(adapters, 'account_status', side_effect=account))
        code, value = self.request('POST', '/api/partners/claude/account/refresh', {})
        if code != 200:
            raise AssertionError(value)
        if self.partners.get('claude')['availability']['state'] != 'available':
            raise AssertionError('Fictional canonical quota did not pass the real availability gate')

    def close(self):
        if hasattr(self, 'release_correction'):
            self.release_correction.set()
        super().close()

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        if agent == 'kimi' and context['job']['job_phase'] == 'work' and context['task']['id'] == 'T3':
            self.t3_calls.append({'job_id': context['job']['id'], 'session_id': session_id,
                                  'prompt': context['job']['text'], 'context': context})
            if len(self.t3_calls) == 2:
                while not self.release_correction.is_set():
                    await asyncio.sleep(.005)
            if self.help_case == 'missing_wire':
                self.native_case = 'missing_wire'
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == 'kimi' and not context['team']['tasks']:
                commands = [proposal('T1', paths=['first.md']), proposal('T2', paths=['peer.md']),
                            proposal('T3', paths=['final.md'], dependencies=['T1', 'T2']),
                            {'type': 'claim', 'task_id': 'T1'}, {'type': 'claim', 'task_id': 'T3'}]
            elif agent == 'claude' and any(item['id'] == 'T2' and item['owner'] is None for item in context['team']['tasks']):
                commands = [{'type': 'claim', 'task_id': 'T2'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if phase == 'help':
            value = native_reply(context)
            value['message'] = 'Fictional text advice only; no file permission was requested.'
            return value
        if phase == 'work':
            self.work_tasks.append(task['id'])
            for path in ([] if self.retired_seed and task['id'] == 'T3' else task['write_paths']):
                destination = Path(cwd) / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    destination.write_text('Fictional retained task bytes.\n')
                elif task['id'].startswith('R'):
                    destination.write_text('Fictional canonical-owner repair.\n')
            if task['id'] == 'T3':
                if (Path(cwd) / 'final.md').exists():
                    self.private_hashes.append(hashlib.sha256((Path(cwd) / 'final.md').read_bytes()).hexdigest())
                turn = len(self.t3_calls)
                if turn == 1 or self.help_case == 'repeat':
                    if self.help_case == 'disk_escape':
                        (Path(cwd) / 'README.md').write_text('Unauthorized private edit must stay rejected.\n')
                    target = 'peer.md' if self.help_case == 'owned' else 'README.md'
                    return native_reply(context, [{'type': 'request_help', 'task_id': 'T3', 'to': 'claude',
                        'text': 'Please advise on this fictional task.', 'capability': 'text', 'repair_paths': [target]}])
                if self.help_case != 'owned' and turn == 2:
                    return native_reply(context, [{'type': 'request_help', 'task_id': 'T3', 'to': 'claude',
                        'text': 'Please give text advice only.', 'capability': 'text'}])
            return native_reply(context, [{'type': 'complete', 'task_id': task['id'], 'result': 'Fictional task result checked.'}],
                                unchanged=[{'path': path, 'reason': 'Exact fictional bytes were preserved.'}
                                           for path in task['write_paths']])
        return super().reply(agent, context, cwd)


class NativeHelpScopeHTTPTests(unittest.TestCase):
    assert_native_completed = outcome.NativeOutcomeHTTPTests.assert_native_completed

    def fixture(self, case='unowned', **options):
        value = HelpScopeFixture(case, **options)
        self.addCleanup(value.close)
        return value

    def test_unowned_repair_is_corrected_to_text_help_without_grant_or_replaying_accepted_work(self):
        fixture = self.fixture(hold_correction=True)
        fixture.create()
        held = fixture.wait(lambda value: bool(fixture.t3_calls) and any(
            job['id'] == fixture.t3_calls[0]['job_id'] and job['status'] == 'failed' for job in value['jobs']))
        rejected_id = fixture.t3_calls[0]['job_id']
        rejected = next(job for job in held['jobs'] if job['id'] == rejected_id)
        self.assertEqual(rejected['error'], 'invalid_team_commands')
        self.assertNotEqual(held['team']['phase'], 'stopped')
        self.assertFalse(any(event['kind'] == 'native_repairs_scheduled' for event in held['events']))
        metadata = fixture.service._project(fixture.room_id).metadata_for_paths(['README.md', 'final.md'])
        self.assertEqual([(entry['path'], entry['owner']) for entry in metadata], [('README.md', None)])
        fixture.wait(lambda value: len(fixture.t3_calls) == 2)
        self.assertIn('commands[0].repair_paths', fixture.t3_calls[1]['prompt'])
        first_result = next(item['result'] for item in fixture.results if item['job_id'] == rejected_id)
        self.assert_native_completed(first_result)
        self.assertEqual(fixture.t3_calls[1]['session_id'], first_result['session_id'])
        fixture.release_correction.set()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))
        self.assertEqual(fixture.work_tasks.count('T1'), 1)
        self.assertEqual(fixture.work_tasks.count('T2'), 1)
        self.assertEqual(len(set(fixture.private_hashes)), 1)
        self.assertEqual(len(fixture.private_hashes), 3)
        self.assertTrue(any(call['phase'] == 'help' and call['agent'] == 'claude' for call in fixture.calls))
        self.assertFalse(any(event['kind'] == 'native_repairs_scheduled' for event in final['events']))
        self.assertEqual(next(job['error'] for job in final['jobs'] if job['id'] == rejected_id), 'invalid_team_commands')
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.calls))
        self.assertFalse(any(member['identity_uncertain'] for member in final['members']))

    def test_same_invalid_request_blocks_only_the_current_task_without_looping(self):
        fixture = self.fixture('repeat')
        fixture.create()
        final = fixture.settled()
        self.assertNotEqual(final['team']['phase'], 'stopped')
        self.assertEqual(final['room']['status'], 'needs_user')
        self.assertEqual(len(fixture.t3_calls), 2)
        self.assertEqual({task['id']: task['status'] for task in final['team']['tasks']},
                         {'T1': 'accepted', 'T2': 'accepted', 'T3': 'blocked'})
        self.assertTrue(any(event['kind'] == 'native_response_blocked' for event in final['events']))
        self.assertFalse(any(event['kind'] == 'native_repairs_scheduled' for event in final['events']))
        self.assertFalse(any(member['identity_uncertain'] for member in final['members']))

    def seed_retired_scope_stop(self, fixture):
        # A minimal retired semantic behavior, not a copied production snapshot:
        # the old host escalated this known-completed invalid request to a stop.
        normal = fixture.service._validate_team_result
        def retired(job, parsed):
            if job['team']['job_phase'] == 'work' and job['team']['task_id'] == 'T3' and any(
                    command['type'] == 'request_help' and command.get('repair_paths') == ['README.md']
                    for command in parsed['commands']):
                raise RoomError('team_scope_conflict')
            return normal(job, parsed)
        fixture.retired_seed = True
        with patch.object(fixture.service, '_validate_team_result', side_effect=retired):
            fixture.create()
            stopped = fixture.settled()
        fixture.retired_seed = False
        self.assertEqual(stopped['team']['stop_reason'], 'team_scope_conflict')
        self.assertEqual({task['id']: task['status'] for task in stopped['team']['tasks']},
                         {'T1': 'accepted', 'T2': 'accepted', 'T3': 'blocked'})
        return stopped

    def test_explicit_continue_carries_only_legacy_rejected_task_and_rejects_new_disk_escape(self):
        for fault, quota in (('none', 'unknown'), ('none', 'available'),
                             ('disk_escape', 'unknown'), ('missing_peer_state', 'unknown')):
            with self.subTest(continuation_fault=fault, peer_quota=quota):
                fixture = self.fixture()
                if quota == 'available':
                    fixture.observe_available_peer_quota()
                stopped = self.seed_retired_scope_stop(fixture)
                calls = len(fixture.calls)
                old_jobs = {job['id']: (job['status'], job['error']) for job in stopped['jobs']}
                with patch.object(fixture.service, '_audit_native_scope_retry',
                                  side_effect=AssertionError('Polling must not audit private filesystem trees')):
                    refreshed = fixture.read()
                self.assertTrue(refreshed['continuation']['can_send'], refreshed['continuation'])
                self.assertTrue(stopped['continuation']['can_send'], stopped['continuation'])
                original = {task['id']: (task['owner'], task['write_paths'], task['result_version'])
                            for task in stopped['team']['tasks']}
                if fault == 'disk_escape':
                    state = fixture.service.store.native_workspace(fixture.room_id, 'kimi')
                    (Path(state['project']) / 'README.md').write_text('New unauthorized drift after stop.\n')
                elif fault == 'missing_peer_state':
                    # Only this disposable DB loses a previously called peer receipt.
                    with fixture.service.store._connection(write=True) as connection:
                        connection.execute('DELETE FROM native_workspaces WHERE room_id=? AND provider=?',
                                           (fixture.room_id, 'claude'))
                code, response = fixture.request('POST', f'/api/rooms/{fixture.room_id}/messages', {
                    'text': '继续原任务，保留已经通过的成果。', 'target': 'all', 'request_id': str(uuid4()),
                    'intent': 'continue', 'binding': stopped['continuation']['binding']})
                if fault != 'none':
                    self.assertEqual(code, 409, response)
                    self.assertEqual(response['error'], 'native_recovery_workspace_unavailable'
                                     if fault == 'missing_peer_state' else 'native_recovery_unpublished_changes')
                    self.assertEqual(len(fixture.calls), calls)
                    current = fixture.read()
                    self.assertEqual(current['current_run']['id'], stopped['current_run']['id'])
                    self.assertEqual(current['team']['stop_reason'], 'team_scope_conflict')
                    self.assertEqual({task['id']: (task['owner'], task['write_paths'], task['result_version'])
                                      for task in current['team']['tasks']}, original)
                    self.assertEqual({job['id']: (job['status'], job['error']) for job in current['jobs']}, old_jobs)
                else:
                    self.assertEqual(code, 200, response)
                    current = fixture.settled()
                    self.assertEqual(current['objective']['status'], 'complete', current['team'])
                    self.assertNotEqual(current['current_run']['id'], stopped['current_run']['id'])
                    self.assertTrue(all(task['status'] == 'accepted' for task in current['team']['tasks']))
                    self.assertEqual({task['id']: (task['owner'], task['write_paths']) for task in current['team']['tasks']},
                                     {key: value[:2] for key, value in original.items()})
                    self.assertEqual(fixture.work_tasks.count('T1'), 1)
                    self.assertEqual(fixture.work_tasks.count('T2'), 1)
                    self.assertEqual(len(set(fixture.private_hashes)), 1)
                    old_epoch = next(task['assignment_epoch'] for task in stopped['team']['tasks'] if task['id'] == 'T3')
                    resumed = next(task for task in current['team']['tasks'] if task['id'] == 'T3')
                    self.assertGreater(resumed['assignment_epoch'], old_epoch)
                    for task in current['team']['tasks']:
                        if task['id'] in {'T1', 'T2'}:
                            self.assertEqual(task['result_version'], original[task['id']][2])
                    self.assertEqual({job['id']: (job['status'], job['error']) for job in current['jobs']
                                      if job['id'] in old_jobs}, old_jobs)
                    approval = next(event for event in current['events'] if event['kind'] == 'native_scope_retry_approved')
                    self.assertEqual(approval['task_ids'], ['T3'])
                    self.assertIs(approval['old_commands_replayed'], False)
                    self.assertTrue(any(job['error'] == 'team_scope_conflict' for job in current['jobs']))
                fixture.close()

    def test_canonical_owner_repair_still_routes_to_its_owner(self):
        fixture = self.fixture('owned')
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        repair = next(event for event in final['events'] if event['kind'] == 'native_repairs_scheduled')
        self.assertEqual([(item['provider'], item['write_paths']) for item in repair['repairs']], [('claude', ['peer.md'])])
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))
        self.assertFalse(any(job['error'] == 'invalid_team_commands' for job in final['jobs']))

    def test_incomplete_native_proof_does_not_become_a_format_correction(self):
        fixture = self.fixture('missing_wire')
        fixture.create()
        final = fixture.settled()
        rejected_id = fixture.t3_calls[0]['job_id']
        self.assertNotEqual(final['objective']['status'], 'complete')
        self.assertFalse(any(event['kind'] == 'format_correction_queued' and event.get('job_id') == rejected_id
                             for event in final['events']))
        self.assertEqual(len(fixture.t3_calls), 1)
        self.assertFalse(any(event['kind'] == 'native_repairs_scheduled' for event in final['events']))
        result = next(item['result'] for item in fixture.results if item['job_id'] == rejected_id)
        self.assertEqual(result['status'], 'failed')

    def test_a_free_file_edited_while_asking_for_help_joins_the_task_instead_of_stopping_the_team(self):
        # Catches the team stopping because the call that asked for help had
        # also edited a file its task had not listed and nobody else holds.
        fixture = self.fixture('disk_escape')
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertFalse(any(job['error'] == 'native_changes_not_imported' for job in final['jobs']))
        tasks = {task['id']: task for task in final['team']['tasks']}
        self.assertEqual(tasks['T3']['write_paths'], ['final.md', 'README.md'])
        self.assertTrue(all(task['status'] == 'accepted' for task in tasks.values()))
        self.assertEqual(fixture.service._project(fixture.room_id).files_for_paths(['README.md'])[0]['content'],
                         'Unauthorized private edit must stay rejected.\n')


if __name__ == '__main__':
    unittest.main()
