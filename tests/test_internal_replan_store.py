"""Explicit internal coordination preserves the normal task/permission DAG."""
import unittest
from tests import test_team_store as fixtures
from tests import test_native_room_service as native
proposal = fixtures.proposal


class InternalReplanStoreTests(unittest.TestCase):
    setUp = fixtures.TeamStoreTests.setUp
    tearDown = fixtures.TeamStoreTests.tearDown
    def finish(self, job, commands, **options):
        fixtures.TeamStoreTests.finish(self, job, commands, **options)
        self.store.record_job_cleanup(job["id"], confirmed=True)
        self.store.reconcile_team(job["room_id"])
    plan = fixtures.TeamStoreTests.plan
    task = fixtures.TeamStoreTests.task
    assertCode = fixtures.TeamStoreTests.assertCode

    def room(self, **options):
        return fixtures.TeamStoreTests.room(self, execution_policy={"mode": "autonomous"}, **options)

    def request(self, room):
        main = self.store.claim(room, 'claude')
        self.finish(main, [{'type':'block', 'task_id':'main', 'cause':'coordination_required',
                            'reason':'A prerequisite has no assigned scope in the current plan.'}])

    def planning(self):
        room = self.plan(paths=True)
        revision = self.store.snapshot(room)['team']['revision']
        peer = self.store.claim(room, 'codex')
        self.request(room)
        self.assertEqual(self.store.snapshot(room)['team']['phase'], 'working')
        self.assertEqual(self.store.snapshot(room)['team']['revision'], revision)
        self.finish(peer, [{'type':'complete','task_id':'tests','result':'Concrete independently checked result.'}])
        review = self.store.claim(room, 'claude')
        self.assertEqual(review['kind'], 'team_review')
        self.finish(review, [{'type':'review','task_id':'tests','verdict':'accept','reason':'Exact result checked.'}])
        self.assertEqual(self.store.snapshot(room)['team']['phase'], 'planning')
        return room

    def test_drain_and_replan_preserve_accepted_result_and_reject_dependency_cycle(self):
        room = self.planning()
        accepted = self.task(room, 'tests')
        parent = self.task(room, 'main')
        author = self.store.claim(room, 'claude')
        self.assertIn('Explicit internal replanning', author['text'])
        bad = proposal('fix', dependencies=['main'], paths=['missing.py'])
        self.assertCode('invalid_team_dependencies', lambda:self.store.prevalidate_team_result(author['id'], [bad]))
        self.assertEqual(self.task(room, 'main'), parent)
        self.assertEqual(self.task(room, 'tests'), accepted)
        self.finish(author, [proposal('fix', paths=['missing.py']), {'type':'claim','task_id':'fix'}, {'type':'plan_ready'}])
        for _ in range(8):
            if self.store.snapshot(room)['team']['phase'] == 'working': break
            for member in ('codex', 'claude'):
                if self.store.snapshot(room)['team']['phase'] == 'working': break
                job = self.store.claim(room, member)
                if job:
                    self.assertEqual(job['kind'], 'team_coordinate')
                    self.finish(job, [{'type':'plan_ready'}])
        self.assertEqual(self.store.snapshot(room)['team']['phase'], 'working')
        updated = self.task(room, 'main')
        self.assertEqual(updated['owner'], parent['owner'])
        self.assertEqual(updated['write_paths'], parent['write_paths'])
        self.assertEqual(updated['result_version'], parent['result_version'])
        self.assertIn('fix', updated['dependencies'])
        self.assertEqual(self.task(room, 'tests'), accepted)
        work = self.store.claim(room, 'claude')
        with self.store._connection() as db:
            self.assertEqual(self.store._team.job_context(db, work['id'])['task_id'], 'fix')

    def test_ready_work_keeps_going_while_a_replan_waits_for_running_calls(self):
        # Catches the real room where Codex blocked R3 for coordination and then sat idle with three
        # ready tasks: every dispatch stopped until the other partner's running call ended.
        native.planned_owners(self.store)  # planned owners stay put
        room = self.room()
        author, peer = self.store.claim(room, 'claude'), self.store.claim(room, 'codex')
        self.finish(author, [proposal('main', paths=['main.py']), proposal('side', paths=['side.py']),
                             proposal('tests', paths=['test_main.py']), proposal('final', dependencies=['main'], paths=['final.py']),
                             {'type':'claim','task_id':'main'}, {'type':'claim','task_id':'side'},
                             {'type':'claim','task_id':'final'}, {'type':'plan_ready'}])
        self.finish(peer, [{'type':'claim','task_id':'tests'}, {'type':'plan_ready'}])
        for _ in range(4):
            if self.store.snapshot(room)['team']['phase'] == 'working': break
            job = self.store.claim(room, 'codex')
            if job: self.finish(job, [{'type':'plan_ready'}])
        tests = self.store.claim(room, 'codex')
        self.assertEqual(tests['team']['task_id'], 'tests')
        main = self.store.claim(room, 'claude')
        self.assertEqual(main['team']['task_id'], 'main')
        self.finish(main, [{'type':'block','task_id':'main','cause':'coordination_required',
                            'reason':'Needs ownership of a file no task holds.'}])
        side = self.store.claim(room, 'claude')
        self.assertIsNotNone(side, 'the independent task was held back while the replan waited')
        self.assertEqual((side['kind'], side['team']['task_id']), ('team_work', 'side'))
        self.assertEqual(self.store.snapshot(room)['team']['phase'], 'working')
        # Work that needs the blocked task still waits for it.
        self.finish(side, [{'type':'complete','task_id':'side','result':'Concrete independently checked result.'}])
        self.assertNotEqual(self.task(room, 'final')['status'], 'working')

    def test_ready_without_a_proposal_cannot_resume_parent_or_loop(self):
        room = self.planning()
        for _ in range(8):
            changed = False
            for member in ('claude','codex'):
                job = self.store.claim(room, member)
                if job:
                    self.assertEqual(job['kind'], 'team_coordinate')
                    self.finish(job, [{'type':'plan_ready'}]); changed = True
            if not changed: break
        self.assertEqual(self.task(room, 'main')['status'], 'blocked')
        self.assertEqual(self.store.snapshot(room)['team']['stop_reason'], 'team_plan_stalled')
        count = len(self.store.snapshot(room)['jobs'])
        self.store.reconcile_team(room)
        self.assertEqual(len(self.store.snapshot(room)['jobs']), count)

    def test_completed_call_without_confirmed_cleanup_cannot_change_plan_revision(self):
        room = self.plan(dependency=True)
        before = self.store.snapshot(room)['team']['revision']
        job = self.store.claim(room, 'claude')
        fixtures.TeamStoreTests.finish(self, job, [{'type':'block','task_id':'main',
            'cause':'coordination_required','reason':'Need concrete prerequisite planning.'}])
        self.store.record_job_cleanup(job['id'], confirmed=False)
        self.store.reconcile_team(room)
        self.assertEqual(self.store.snapshot(room)['team']['phase'], 'working')
        self.assertEqual(self.store.snapshot(room)['team']['revision'], before)
        self.assertIsNone(self.store.claim(room, 'claude'))
        self.store.record_job_cleanup(job['id'], confirmed=True)
        self.store.reconcile_team(room)
        self.assertEqual(self.store.snapshot(room)['team']['phase'], 'planning')
        self.assertEqual(self.store.snapshot(room)['team']['revision'], before + 1)

    def test_a_task_waiting_for_the_user_does_not_hold_back_the_teams_replan_and_is_asked_alone(self):
        # Catches: one task waiting for the user's permission froze the whole board, so the replan
        # another task had asked for never started; and the opposite, the replan swallowing the
        # permission question so the user was never asked.
        room = self.plan()
        peer = self.store.claim(room, 'codex')
        self.request(room)
        self.finish(peer, [{'type':'block','task_id':'tests','cause':'permission_required','reason':'User authorization is missing.'}])
        snap = self.store.snapshot(room)
        self.assertEqual(snap['team']['phase'], 'planning')
        self.assertEqual([e['task_ids'] for e in snap['events'] if e['kind']=='internal_replan_started'], [['main']])
        self.assertEqual((self.task(room, 'tests')['status'], self.task(room, 'tests')['block_cause']),
                         ('blocked', 'permission_required'))
        self.finish(self.store.claim(room, 'claude'), [proposal('fix'), {'type':'plan_ready'}])
        for _ in range(30):
            jobs = [job for job in (self.store.claim(room, member) for member in ('codex', 'claude')) if job]
            if not jobs: break
            for job in jobs:
                task_id = job['team']['task_id']
                self.finish(job, [{'type':'plan_ready'}] if job['kind'] == 'team_coordinate' else
                            [{'type':'review','task_id':task_id,'verdict':'accept','reason':'Exact result checked.'}]
                            if job['kind'] == 'team_review' else
                            [{'type':'complete','task_id':task_id,'result':'Concrete independently checked result.'}])
        snap = self.store.snapshot(room)
        # The permission block did not keep the confirmed plan in planning, and the rest was delivered.
        self.assertEqual(snap['team']['phase'], 'working')
        self.assertEqual({task['id']: task['status'] for task in snap['team']['tasks']},
                         {'main': 'accepted', 'tests': 'blocked', 'fix': 'accepted'})
        binding = {'run_id':snap['current_run']['id'], 'revision':0, 'digest':'fixture'}
        self.assertTrue(self.store.require_task_decision(room, binding))
        objective = self.store.snapshot(room)['objective']
        self.assertEqual((objective['status'], objective['next_step']['origin'], objective['next_step']['question']),
                         ('needs_user', 'task_block', 'Deliver tests：User authorization is missing.'))
