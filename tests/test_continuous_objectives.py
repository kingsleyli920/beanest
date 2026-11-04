"""Continuous policy at real store/service boundaries; provider transport is fake."""
import asyncio
import copy
from pathlib import Path
import unittest
from uuid import uuid4

from local_agent_coordinator.room_service import RoomService, RoomError
import test_autonomous_objectives as objective_fixtures
from test_folder_service import response, request
from test_team_service import reply, changed_file
from test_team_store import proposal


class ContinuousObjectiveTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = objective_fixtures.ObjectiveIntegrationTests.asyncSetUp
    asyncTearDown = objective_fixtures.ObjectiveIntegrationTests.asyncTearDown
    settle = objective_fixtures.ObjectiveIntegrationTests.settle
    handler = objective_fixtures.ObjectiveIntegrationTests.handler

    async def create(self, policy=None, **extra):
        self.fake.handler = self.handler
        return await self.service.create_room('Deliver all requested acceptance items.', 'claude', str(uuid4()),
            mode='python_artifacts', work_mode='build', execution_policy=policy or {'mode':'autonomous'}, **extra)

    async def test_useful_work_continues_past_32_calls_and_8_stages_without_resetting_objective(self):
        self.rounds = 10
        created = await self.create()
        self.assertIsNone(created['budget']['limit'])
        self.assertIsNone(created['budget']['deadline'])
        self.assertIsNone(created['objective']['deadline'])
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['id'], created['objective']['id'])
        self.assertEqual(result['objective']['status'], 'complete')
        self.assertEqual(result['objective']['stage'], 10)
        self.assertGreater(result['objective']['budget']['used'], 32)
        self.assertIsNone(result['objective']['budget']['remaining'])
        self.assertTrue(all(run['budget']['limit'] is None for run in result['runs']))
        self.assertFalse(any(event.get('code') in {'budget_exhausted','objective_stage_limit'} for event in result['events']))
        self.assertTrue(all(run['accepted_at'] is None for run in result['runs']))
        for call in self.fake.calls:
            budget = call['context']['runtime_budget']
            self.assertIsNone(budget['limit'])
            self.assertIsNone(budget['seconds_remaining'])
            self.assertFalse(budget['conserve'])

    async def test_explicit_total_calls_and_deadline_still_stop(self):
        self.rounds = 10
        created = await self.create({'mode':'autonomous','max_calls':5,'max_seconds':7200})
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['budget']['used'], 5)
        self.assertEqual(result['objective']['stop_reason'], 'objective_calls_exhausted')
        self.assertEqual(result['objective']['deadline'], 8200)

    async def test_exact_repeated_help_stops_but_new_help_does_not_spend_a_total_cap(self):
        original = self.handler
        work_calls = 0
        def handler(agent, context):
            nonlocal work_calls
            if context['job']['job_phase'] == 'work':
                work_calls += 1
                return reply(context, commands=[{'type':'request_help','task_id':context['task']['id'],
                    'to':'codex' if agent == 'claude' else 'claude','capability':'text',
                    'text':'Confirm the same unchanged edge case.'}])
            if context['job']['job_phase'] == 'help':
                return reply(context, message='Use 1; this is the same answer.')
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room('Finish the requested file.', 'claude', str(uuid4()),
            mode='python_artifacts', work_mode='build', execution_policy={'mode':'autonomous'})
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['stop_reason'], 'objective_no_progress')
        self.assertEqual(work_calls, 3)
        self.assertFalse((await self.service.status())['autonomous_work_active'])

    async def test_restart_carries_exact_unfinished_board_without_replanning_or_replaying_accepted_work(self):
        original = self.handler
        # Stop after planning/claim, before any task delivery.
        created = await self.create({'mode':'autonomous','max_calls':3,'max_seconds':7200})
        await self.settle()
        before = await self.service.read_room(created['room']['id'])
        old_id = before['current_run']['id']
        old_task = copy.deepcopy(before['team']['tasks'][0])
        old_objective = before['objective']['id']
        self.assertTrue(before['current_run']['can_restart_objective'])
        binding = {key:before['current_run'][key] for key in ('id','revision','digest')}
        binding['run_id'] = binding.pop('id')
        count = len(self.fake.calls)
        result = await self.service.objective_action(created['room']['id'], 'restart-objective', 'restart',
            **binding, execution_policy={'mode':'autonomous'})
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertNotEqual(result['objective']['id'], old_objective)
        self.assertEqual(result['objective']['original_goal'], before['objective']['original_goal'])
        self.assertEqual(result['objective']['status'], 'complete')
        task = result['team']['tasks'][0]
        for key in ('id','title','owner','write_paths','dependencies'):
            self.assertEqual(task[key], old_task[key])
        self.assertGreater(task['assignment_epoch'], old_task['assignment_epoch'])
        self.assertFalse(any(call['context']['job']['job_phase'] in {'plan','coordinate'} for call in self.fake.calls[count:]))
        archived = await self.service.read_run(created['room']['id'], old_id)
        self.assertEqual(archived['run']['budget'], before['current_run']['budget'])
        self.assertEqual(archived['team']['tasks'][0]['status'], old_task['status'])
        replay = await self.service.objective_action(created['room']['id'], 'restart-objective', 'restart',
            **binding, execution_policy={'mode':'autonomous'})
        self.assertEqual(replay['objective']['id'], result['objective']['id'])
        with self.assertRaises(RoomError):
            await self.service.objective_action(created['room']['id'], 'restart-objective', 'stale-restart',
                **binding, execution_policy={'mode':'autonomous'})

    async def test_restart_reopens_tasks_blocked_by_a_reply_mistake_instead_of_asking(self):
        # Catches: a task blocked by a correctable reply mistake such as
        # team_task_permission was carried still blocked, so continuing the
        # objective immediately turned into objective_decision_required.
        created = await self.create({'mode':'autonomous','max_calls':3,'max_seconds':7200})
        await self.settle()
        before = await self.service.read_room(created['room']['id'])
        task_id = before['team']['tasks'][0]['id']
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET status='blocked',blocked_reason='team_task_permission' "
                       "WHERE room_id=? AND run_id=? AND id=?", (created['room']['id'], before['current_run']['id'], task_id))
        binding = {key:before['current_run'][key] for key in ('id','revision','digest')}
        binding['run_id'] = binding.pop('id')
        await self.service.objective_action(created['room']['id'], 'restart-objective', 'restart',
            **binding, execution_policy={'mode':'autonomous'})
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['status'], 'complete', result['objective'].get('stop_reason'))
        self.assertEqual(result['team']['tasks'][0]['id'], task_id)
        self.assertNotEqual(result['team']['tasks'][0]['status'], 'blocked')

    async def test_new_reads_reset_the_repeat_episode_but_old_ranges_cannot_ping_pong_forever(self):
        created = await self.create({'mode':'autonomous','max_calls':3})
        await self.settle()
        # A new explicit objective is not needed to check the host's persisted
        # evidence rule; use the same transaction API that inspection calls use.
        room_id = created['room']['id']
        with self.service.store._connection(write=True) as db:
            guard = self.service.store._team.objectives
            for evidence in ('A','B','A','C','A'):
                self.assertFalse(guard.repeated_without_progress(db, room_id, 'range-scope', evidence))
            self.assertFalse(guard.repeated_without_progress(db, room_id, 'range-scope', 'B'))
            self.assertFalse(guard.repeated_without_progress(db, room_id, 'range-scope', 'A'))
            self.assertFalse(guard.repeated_without_progress(db, room_id, 'range-scope', 'B'))
            self.assertTrue(guard.repeated_without_progress(db, room_id, 'range-scope', 'A'))

    async def test_autonomous_review_can_request_multiple_real_revisions(self):
        original = self.handler
        work = 0
        def handler(agent, context):
            nonlocal work
            phase = context['job']['job_phase']
            if phase == 'work':
                work += 1
                return reply(context, commands=[{'type':'complete','task_id':context['task']['id'], 'result':'Saved the next concrete revision.'}],
                    files=[changed_file(context, context['task']['write_paths'][0], f'VALUE = {work}\n')])
            if phase == 'review' and work < 4:
                return reply(context, commands=[{'type':'review','task_id':context['task']['id'],
                    'verdict':'changes_requested','reason':'The requested next edge case is still missing.'}])
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room('Complete all four edge cases.', 'claude', str(uuid4()),
            mode='python_artifacts',work_mode='build',execution_policy={'mode':'autonomous'})
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['status'], 'complete')
        self.assertEqual(work, 4)
        self.assertEqual(result['team']['tasks'][0]['revision_attempts'], 3)

    async def test_unchanged_revision_stops_even_when_review_chat_wording_changes(self):
        original = self.handler
        reviews = 0
        def handler(agent, context):
            nonlocal reviews
            if context['job']['job_phase'] == 'review':
                reviews += 1
                return reply(context,commands=[{'type':'review','task_id':context['task']['id'],
                    'verdict':'changes_requested','reason':f'The same bytes still miss the acceptance item; review {reviews}.'}])
            return original(agent,context)
        self.fake.handler = handler
        created = await self.service.create_room('Correct the concrete requested item.', 'claude', str(uuid4()),
            mode='python_artifacts',work_mode='build',execution_policy={'mode':'autonomous'})
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['stop_reason'],'objective_no_progress')
        self.assertEqual(reviews,3)

    async def test_identical_checkpoint_deliveries_cannot_loop_using_new_task_ids(self):
        self.rounds = 100
        original = self.handler
        def handler(agent, context):
            phase = context['job']['job_phase']
            if phase in {'plan','coordinate'}:
                commands=[]
                if not context['team']['tasks']:
                    task_id='stage'+str(context['objective']['stage'])
                    commands=[proposal(task_id),{'type':'claim','task_id':task_id},{'type':'plan_ready'}]
                return reply(context,commands=commands)
            if phase=='work':
                return reply(context,commands=[{'type':'complete','task_id':context['task']['id'],'result':'The unchanged evidence.'}])
            return original(agent,context)
        self.fake.handler=handler
        created=await self.service.create_room('Deliver all original acceptance items.', 'claude', str(uuid4()),
            participants=['claude'],work_mode='build',execution_policy={'mode':'autonomous'})
        await self.settle()
        result=await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['stop_reason'],'objective_no_progress')
        self.assertEqual(result['objective']['stage'],3)

    async def test_same_file_bytes_cannot_loop_checkpoint_by_incrementing_revision(self):
        self.rounds=100
        original=self.handler
        def handler(agent,context):
            phase=context['job']['job_phase']
            if phase in {'plan','coordinate'}:
                commands=[]
                board=context['team']['tasks']
                if not board and agent==context['team']['plan_author']:
                    task_id='stage'+str(context['objective']['stage'])
                    commands=[proposal(task_id,paths=['fixed.py'],capability='python'),{'type':'claim','task_id':task_id}, {'type':'plan_ready'}]
                elif board:
                    commands=[{'type':'plan_ready'}]
                return reply(context,commands=commands)
            if phase=='work':
                return reply(context,commands=[{'type':'complete','task_id':context['task']['id'],'result':'Same concrete bytes.'}],
                    files=[changed_file(context,'fixed.py','VALUE = 1\n')])
            return original(agent,context)
        self.fake.handler=handler
        created=await self.service.create_room('Deliver all original acceptance items.','claude',str(uuid4()),
            mode='python_artifacts',work_mode='build',execution_policy={'mode':'autonomous'})
        await self.settle()
        result=await self.service.read_room(created['room']['id'])
        self.assertEqual(result['objective']['status'],'stopped')
        self.assertEqual(result['objective']['stop_reason'],'objective_no_progress')
        self.assertEqual(result['objective']['stage'],3)
        self.assertGreater(result['project']['revision'],1)

    async def test_no_deadline_does_not_disable_user_cancel_or_per_call_timeout(self):
        held=asyncio.Event()
        entered=asyncio.Event()
        original=self.handler
        observed=[]
        async def handler(agent, context):
            if context['job']['job_phase']=='work':
                entered.set()
                await held.wait()
            return original(agent,context)
        self.fake.handler=handler
        invoke=self.service.invoke
        async def bounded(*args,**kwargs):
            observed.append(kwargs['timeout'])
            return await invoke(*args,**kwargs)
        self.service.invoke=bounded
        created=await self.service.create_room('Deliver the requested item.', 'claude',str(uuid4()),
            mode='python_artifacts',work_mode='build',execution_policy={'mode':'autonomous'})
        await asyncio.wait_for(entered.wait(),2)
        self.now+=1000000
        await self.service.expire_tasks()
        self.assertTrue((await self.service.read_room(created['room']['id']))['objective']['status']=='running')
        result=await self.service.cancel_room(created['room']['id'])
        self.assertTrue(result['cancellation_confirmed'])
        self.assertFalse((await self.service.status())['autonomous_work_active'])
        self.assertTrue(all(0<timeout<=600 for timeout in observed))

    async def test_explicit_deadline_cancels_inflight_work_without_late_files(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.handler
        async def handler(agent, context):
            if context['job']['job_phase'] == 'work':
                entered.set()
                await release.wait()
            return original(agent, context)
        self.fake.handler = handler
        created = await self.service.create_room('Deliver the requested item.', 'claude', str(uuid4()),
            mode='python_artifacts', work_mode='build',
            execution_policy={'mode': 'autonomous', 'max_seconds': 7200})
        await asyncio.wait_for(entered.wait(), 2)
        self.now = created['objective']['deadline']
        await self.service.expire_tasks()
        release.set()
        await self.settle()
        final = await self.service.read_room(created['room']['id'])
        self.assertEqual(final['objective']['stop_reason'], 'objective_deadline_exceeded')
        self.assertTrue(final['cancellation_confirmed'])
        self.assertFalse(any(task['status'] == 'accepted' for task in final['team']['tasks']))
        self.assertFalse(final['project']['files'])

    async def test_restart_accepted_dependency_is_baseline_and_only_unfinished_work_is_dispatched(self):
        original = self.handler
        def handler(agent, context):
            if context['job']['job_phase'] in {'plan','coordinate'}:
                commands = []
                board = {task['id']:task for task in context['team']['tasks']}
                if not board and agent == context['team']['plan_author']:
                    commands += [proposal('first',paths=['first.py'],capability='python'),
                        proposal('second',dependencies=['first'],paths=['second.py'],capability='python')]
                    board = {'first':{'owner':None},'second':{'owner':None}}
                if agent == context['team']['plan_author']:
                    commands += [{'type':'claim','task_id':task_id} for task_id,task in board.items() if task['owner'] is None]
                if board:
                    commands.append({'type':'plan_ready'})
                return reply(context,commands=commands)
            return original(agent,context)
        self.fake.handler = handler
        created = await self.service.create_room('Deliver the two dependent files.', 'claude', str(uuid4()),
            mode='python_artifacts',work_mode='build',execution_policy={'mode':'autonomous','max_calls':5})
        await self.settle()
        before = await self.service.read_room(created['room']['id'])
        self.assertEqual(before['team']['tasks'][0]['status'],'accepted')
        old_run = before['current_run']['id']
        count = len(self.fake.calls)
        binding = {key:before['current_run'][key] for key in ('revision','digest')}
        await self.service.objective_action(created['room']['id'],'restart-objective','baseline-restart',
            run_id=old_run,**binding,execution_policy={'mode':'autonomous'})
        await self.settle()
        after = await self.service.read_room(created['room']['id'])
        self.assertEqual(after['objective']['status'],'complete')
        self.assertEqual([file['path'] for file in after['objective']['baseline']['files']],['first.py'])
        # Delivery includes accepted carry with its original provenance; it is
        # not a list of work repeated by the new objective.
        delivered = {file['path']: file for file in after['objective']['delivery']['files']}
        self.assertEqual(set(delivered), {'first.py', 'second.py'})
        self.assertEqual(delivered['first.py']['run_id'], old_run)
        self.assertEqual(delivered['second.py']['run_id'], after['current_run']['id'])
        self.assertTrue(after['objective']['delivery']['verified'])
        self.assertEqual(after['team']['tasks'][0]['inherited_from_run'],old_run)
        self.assertFalse(any(call['context']['task'] and call['context']['task']['id']=='first'
            for call in self.fake.calls[count:]))

    async def test_useful_single_run_reads_cross_32_and_repeated_same_range_stops(self):
        source = self.root/'selected'
        source.mkdir()
        (source/'notes.txt').write_text(''.join(f'Line {index}\n' for index in range(200)))
        project = await self.service.register_project('Selected project', str(source), selection_kind='folder')
        offsets = []
        def handler(agent, context):
            phase = context['job']['job_phase']
            if phase in {'plan','coordinate'}:
                commands = []
                if not context['team']['tasks']:
                    commands = [{'type':'propose','id':'work','title':'Read the relevant file',
                        'description':'Read concrete evidence and deliver the requested observation.',
                        'acceptance':'Relevant lines are actually read.','dependencies':[], 'write_paths':[], 'capability':'text'},
                        {'type':'claim','task_id':'work'},{'type':'plan_ready'}]
                return response(context, commands=commands)
            if phase == 'work':
                # Forty distinct observed ranges are useful. Three further
                # identical reads are stopped, not forty arbitrary calls.
                offset = len(offsets) if len(offsets) < 40 else 39
                offsets.append(offset)
                return response(context, request={**request('read','notes.txt'), 'offset':offset,'limit':1})
            self.fail('unexpected phase '+phase)
        self.fake.handler = handler
        created = await self.service.create_room('Read all requested lines and summarize.', 'claude', str(uuid4()),
            mode='project_folder',project_id=project['project']['id'],participants=['claude'],
            work_mode='build', execution_policy={'mode':'autonomous'})
        await self.settle()
        result = await self.service.read_room(created['room']['id'])
        self.assertEqual(len(result['runs']), 1)
        self.assertGreater(result['budget']['used'], 32)
        self.assertEqual(offsets[:40], list(range(40)))
        self.assertEqual(result['objective']['stop_reason'], 'objective_no_progress')
        self.assertEqual(offsets[-3:], [39,39,39])
        self.assertIsNone(result['budget']['limit'])
        self.assertIsNone(result['budget']['deadline'])
        self.assertEqual((source/'notes.txt').read_text().splitlines()[0], 'Line 0')
