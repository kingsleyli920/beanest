"""Typed internal coordination across real HTTP/SQLite and owned fake ACP."""
import asyncio
import hashlib
from pathlib import Path
import threading
import unittest

from test_native_help_scope_http import HelpScopeFixture
from test_native_room_service import native_reply
from test_team_store import proposal


class InternalReplanFixture(HelpScopeFixture):
    def __init__(self, cause='coordination_required', *, escape=False, repeat=False, hold_new=False):
        super().__init__()
        self.cause, self.escape, self.repeat = cause, escape, repeat
        self.release_new = threading.Event()
        if not hold_new:
            self.release_new.set()
        self.new_work_started = threading.Event()
        self.new_context = None
        self.replan_prompts = []
        self.initial_bytes = b'Fictional retained integration note.\n'
        self.original_readme_hash = hashlib.sha256((self.source / 'README.md').read_bytes()).hexdigest()
        self.before_replan_metadata = None
        self.new_private_baseline_hash = None

    def close(self):
        self.release_new.set()
        super().close()

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        import json
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        if context['job']['job_phase'] == 'work' and context.get('task', {}).get('id') == 'T4':
            self.new_context = context
            self.new_private_baseline_hash = hashlib.sha256((Path(cwd) / 'README.md').read_bytes()).hexdigest()
            self.new_work_started.set()
            while not self.release_new.is_set():
                await asyncio.sleep(.005)
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        board = context['team']['tasks']
        if phase in {'plan', 'coordinate'} and self.t3_calls:
            # The new permission comes only from ordinary author proposal,
            # member claim, and exact-revision confirmation, never this block.
            self.replan_prompts.append(context['job']['text'])
            commands = []
            if agent == 'kimi' and not any(item['id'] == 'T4' for item in board):
                commands = [proposal('T4', paths=['README.md']), {'type': 'claim', 'task_id': 'T4'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if phase == 'work' and task['id'] == 'T3':
            self.work_tasks.append('T3')
            destination = Path(cwd) / 'final.md'
            if not destination.exists():
                destination.write_bytes(self.initial_bytes)
            self.private_hashes.append(hashlib.sha256(destination.read_bytes()).hexdigest())
            if len(self.t3_calls) == 1 or self.repeat:
                if self.before_replan_metadata is None:
                    self.before_replan_metadata = self.service._project(self.room_id).metadata_for_paths(['README.md'])
                if self.escape:
                    (Path(cwd) / 'README.md').write_text('Outside the currently assigned scope.\n')
                return native_reply(context, [{'type': 'block', 'task_id': 'T3', 'cause': self.cause,
                    'reason': 'A separate owned regression task is needed within the existing fictional goal.'}])
            return native_reply(context, [{'type': 'complete', 'task_id': 'T3', 'result': 'Reviewed new regression result and retained integration bytes.'}],
                                unchanged=[{'path': 'final.md', 'reason': 'Preserved exact integration bytes.'}])
        if phase == 'work' and task['id'] == 'T4':
            self.work_tasks.append('T4')
            (Path(cwd) / 'README.md').write_text('Fictional newly authorized regression evidence.\n')
            return native_reply(context, [{'type': 'complete', 'task_id': 'T4', 'result': 'New regression evidence ready for independent review.'}])
        return super().reply(agent, context, cwd)


class InternalReplanHTTPTests(unittest.TestCase):
    def fixture(self, *args, **options):
        fixture = InternalReplanFixture(*args, **options)
        self.addCleanup(fixture.close)
        return fixture

    def test_typed_replan_uses_normal_author_claim_ready_and_retains_accepted_work(self):
        fixture = self.fixture(hold_new=True)
        fixture.create()
        held = fixture.wait(lambda snapshot: fixture.new_work_started.is_set())
        tasks = {task['id']: task for task in held['team']['tasks']}
        self.assertEqual({key: tasks[key]['status'] for key in ('T1', 'T2')}, {'T1': 'accepted', 'T2': 'accepted'})
        accepted = {key: {field: tasks[key][field] for field in ('owner', 'result_version', 'result_digest')}
                    for key in ('T1', 'T2')}
        self.assertEqual(len(fixture.t3_calls), 1)
        self.assertEqual(tasks['T3']['owner'], 'kimi')
        self.assertEqual(tasks['T3']['write_paths'], ['final.md'])
        self.assertIn('T4', tasks['T3']['dependencies'])
        self.assertEqual(fixture.new_context['task']['write_paths'], ['README.md'])
        self.assertEqual(len(fixture.before_replan_metadata), 1)
        self.assertIsNone(fixture.before_replan_metadata[0]['owner'])
        self.assertTrue(fixture.before_replan_metadata[0]['text'])
        self.assertEqual(fixture.before_replan_metadata[0]['sha256'], fixture.original_readme_hash)
        self.assertEqual(fixture.new_private_baseline_hash, fixture.original_readme_hash)
        grants = fixture.service.store.ownership_grant(fixture.new_context['job']['id'])
        # An unowned baseline needs an ordinary claim, not a foreign-owner transfer grant.
        self.assertEqual(grants, {})
        self.assertTrue(any(event['kind'] == 'team_command' and event.get('command') == 'claim'
                            and event.get('task_id') == 'T4' and event.get('sender') == 'kimi'
                            for event in held['events']))
        self.assertNotIn('README.md', fixture.service.store.ownership_grant(fixture.t3_calls[0]['job_id']))
        self.assertTrue(fixture.replan_prompts)
        self.assertFalse(any(event['kind'] == 'objective_decision_required' for event in held['events']))
        fixture.release_new.set()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        tasks = {task['id']: task for task in final['team']['tasks']}
        self.assertTrue(all(task['status'] == 'accepted' for task in tasks.values()))
        for key in ('T1', 'T2'):
            self.assertEqual({field: tasks[key][field] for field in accepted[key]}, accepted[key])
            self.assertEqual(fixture.work_tasks.count(key), 1)
        self.assertEqual(fixture.work_tasks.count('T4'), 1)
        self.assertEqual(len(fixture.t3_calls), 2)
        self.assertEqual(len(set(fixture.private_hashes)), 1)
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.calls))
        self.assertFalse(any(member['identity_uncertain'] for member in final['members']))
        self.assertTrue(all(item['result']['native_execution']['cleanup_confirmed'] for item in fixture.results))

    def test_real_permission_and_user_decision_are_not_relabelled_by_reason_text(self):
        for cause in ('permission_required', 'user_decision'):
            with self.subTest(cause=cause):
                fixture = self.fixture(cause)
                fixture.create()
                final = fixture.settled()
                self.assertEqual(final['objective']['stop_reason'], 'objective_decision_required')
                tasks = {task['id']: task for task in final['team']['tasks']}
                self.assertEqual(tasks['T3']['block_cause'], cause)
                self.assertEqual(tasks['T3']['status'], 'blocked')
                self.assertNotIn('T4', tasks)
                self.assertEqual(len(fixture.t3_calls), 1)
                self.assertFalse(fixture.replan_prompts)

    def test_coordination_label_never_authorizes_current_out_of_scope_disk_edit(self):
        fixture = self.fixture(escape=True)
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final['team']['phase'], 'stopped')
        self.assertEqual(next(job['error'] for job in final['jobs'] if job['id'] == fixture.t3_calls[0]['job_id']),
                         'native_changes_not_imported')
        self.assertFalse(any(task['id'] == 'T4' for task in final['team']['tasks']))
        self.assertEqual(len(fixture.t3_calls), 1)

    def test_same_coordination_request_cannot_cycle_without_new_progress(self):
        fixture = self.fixture(repeat=True)
        fixture.create()
        final = fixture.settled()
        self.assertNotEqual(final['objective']['status'], 'running')
        self.assertLessEqual(len(fixture.t3_calls), 4)
        self.assertLessEqual(fixture.work_tasks.count('T4'), 1)
        self.assertFalse(any(job['status'] in {'queued', 'working'} for job in final['jobs']))
        self.assertIn(final['team']['stop_reason'], {'objective_no_progress', 'team_plan_stalled'})
        self.assertTrue(any(event.get('code') == 'objective_repeated_evidence' for event in final['events']))
