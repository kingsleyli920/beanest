"""Quota partial ownership transfer followed by actual peer CLI writes and review."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, native_adapters
from local_agent_coordinator.room_store import RoomError
from test_kimi_work_quota_http import WorkQuotaFixture
from test_native_room_service import native_reply, no_rebalance
from test_team_store import proposal


_NATIVE_RUN = adapters._run
_PEER_CLI = r'''import hashlib,json,os,sys
from pathlib import Path
agent = Path(sys.argv[0]).name.rsplit('-',1)[-1]
if 'status' in sys.argv:
    if agent == 'codex': print('Logged in using ChatGPT')
    else: print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'pro'}))
    sys.exit(0)
root=Path(__file__).parent
value=json.loads((root/(agent+'-submission.json')).read_text())
prompt=sys.stdin.read()
assert value['job_id'] in prompt
path=Path('second.md')
assert hashlib.sha256(path.read_bytes()).hexdigest()==value['before_sha']
if value['write']:
    path.write_text(value['write'])
if value.get('outside'):
    Path('README.md').write_text('Fictional unauthorized review edit.\n')
sha=hashlib.sha256(path.read_bytes()).hexdigest()
with (root/'peer-submission-calls.jsonl').open('a') as log:
    log.write(json.dumps({'agent':agent,'job_id':value['job_id'],'phase':value['phase'],
                         'sha256':sha,'pid':os.getpid()})+'\n')
sid=value['sid']
if agent=='claude':
    events=[{'type':'system','subtype':'init','session_id':sid},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'local-file','name':'Edit' if value['write'] else 'Read','input':{'file_path':'second.md'}}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'local-file','content':sha}]}},
            {'type':'result','subtype':'success','session_id':sid,'structured_output':value['response']}]
else:
    events=[{'type':'thread.started','thread_id':sid},{'type':'turn.started'},
            {'type':'item.started','item':{'id':'local-file','type':'command_execution','command':'fictional-local-file-check','status':'in_progress'}},
            {'type':'item.completed','item':{'id':'local-file','type':'command_execution','aggregated_output':sha,'exit_code':0,'status':'completed'}},
            {'type':'item.completed','item':{'id':'answer','type':'agent_message','text':json.dumps(value['response'])}},
            {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]
for event in events: print(json.dumps(event),flush=True)
'''


class SuccessorSubmissionFixture(WorkQuotaFixture):
    original = b'Fictional registered baseline owned through normal planning.\n'
    owner_delivery = original + b'Initial Kimi-owned delivery accepted normally.\n'
    completed = WorkQuotaFixture.retained + b'Successor completed the remaining implementation.\n'

    def __init__(self, *, hold_review=False, review_outside=False, legacy_review_failure=False):
        compile(_PEER_CLI, '<fictional-peer-cli>', 'exec')
        super().__init__(legacy=False)
        no_rebalance(self.service)
        # Folder capture happens when the room starts: this is an existing
        # registered file, not a new unpublished path with no old owner/grant.
        (self.source / 'second.md').write_bytes(self.original)
        self.review_outside = review_outside
        self.legacy_review_failure = legacy_review_failure
        self.original_readme = (self.source / 'README.md').read_bytes()
        self.review_started = threading.Event()
        self.release_review = threading.Event()
        if not hold_review:
            self.release_review.set()
        self.submissions, self.peer_results, self.invoke_errors = [], [], []
        for agent in ('claude', 'codex'):
            cli = self.root / ('fictional-peer-' + agent)
            cli.write_text(f'#!{sys.executable}\n' + _PEER_CLI)
            cli.chmod(0o700)
            self.stack.enter_context(patch.object(adapters, 'find_' + agent, return_value=str(cli)))
        self.stack.enter_context(patch.object(adapters, '_run', _NATIVE_RUN))
        current_grant = self.service.store.ownership_grant
        def retired_review_guard(job_id):
            value = current_grant(job_id)
            if self.legacy_review_failure:
                with self.service.store._connection() as db:
                    job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                    context = self.service.store._team.job_context(db, job_id)
                    if context and context['task_id'] == 'T2' and context['job_phase'] == 'review' and job['status'] == 'working':
                        task = self.service.store._team.task(db, job['room_id'], 'T2')
                        grants = json.loads(task['ownership_grant_json'] or '{}')
                        # Simulate only the retired writer-vs-reviewer predicate;
                        # the current access checks run first, with no fabricated job ledger.
                        if grants and any(grant['new_owner'] != job['provider'] for grant in grants.values()):
                            self.legacy_review_job = job_id
                            raise RoomError('team_assignment_changed')
            return value
        self.stack.enter_context(patch.object(self.service.store, 'ownership_grant', side_effect=retired_review_guard))


    def close(self):
        if hasattr(self, 'release_review'):
            self.release_review.set()
        super().close()

    def reply(self, agent, context, cwd):
        task = context.get('task')
        phase = context['job']['job_phase']
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == 'kimi' and not context['team']['tasks']:
                commands = [proposal('T0', paths=['second.md']), proposal('T1', paths=['first.md']),
                            proposal('T2', paths=['second.md'], dependencies=['T0', 'T1']),
                            proposal('T3', paths=['final.md'], dependencies=['T1', 'T2']),
                            {'type': 'claim', 'task_id': 'T0'}, {'type': 'claim', 'task_id': 'T2'}]
            else:
                own = {'claude': 'T1', 'codex': 'T3'}.get(agent)
                if any(item['id'] == own and item['owner'] is None for item in context['team']['tasks']):
                    commands = [{'type': 'claim', 'task_id': own}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if phase == 'work' and task and task['id'] == 'T0':
            self.work_tasks.append((agent, task['id']))
            (Path(cwd) / 'second.md').write_bytes(self.owner_delivery)
            return native_reply(context, [{'type': 'complete', 'task_id': 'T0',
                                           'result': 'Initial registered file ownership established normally.'}])
        if (agent == 'kimi' and context['job']['job_phase'] == 'work'
                and task and task['id'] == 'T2'):
            old = next(item for item in self.service._project(self.room_id).snapshot()['files'] if item['path'] == 'second.md')
            assert old['owner'] == 'kimi', old
            self.source_assignment = {'owner': task['owner'], 'epoch': task['assignment_epoch'],
                                      'sha256': old['sha256']}
            (Path(cwd) / 'second.md').write_bytes(self.retained)
        return super().reply(agent, context, cwd)

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        task, phase = context.get('task'), context['job']['job_phase']
        selected = (task and task['id'] == 'T2' and agent != 'kimi'
                    and phase in {'work', 'review'} and not context.get('native_partial_inspection'))
        if not selected:
            try:
                return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)
            except BaseException as error:
                self.invoke_errors.append((agent, phase, type(error).__name__, str(error)))
                raise
        record = {'agent': agent, 'phase': phase, 'job_id': context['job']['id'],
                  'permission': options['permission'], 'input_sid': session_id,
                  'task': json.loads(json.dumps(task)), 'cwd': str(cwd),
                  'transfer_paths': context['host_authorized_transfer_paths']}
        with self.service.store._connection() as db:
            bound = db.execute('SELECT assignment_epoch,result_version FROM team_jobs WHERE job_id=?',
                               (context['job']['id'],)).fetchone()
            row = db.execute("SELECT ownership_grant_json FROM shared_tasks WHERE room_id=? AND run_id=? AND id='T2'",
                             (self.room_id, context['team']['run_id'])).fetchone()
        record['job_binding'] = dict(bound)
        record['grant'] = json.loads(row['ownership_grant_json'] or '{}')
        if phase == 'review':
            self.review_canonical_files = {item['path']: (item['sha256'], item['owner'])
                for item in self.service._project(self.room_id).snapshot()['files']}
        self.submissions.append(record)
        self.calls.append({key: record[key] for key in ('agent', 'phase', 'job_id', 'permission', 'input_sid')})
        if phase == 'review':
            self.review_started.set()
            while not self.release_review.is_set():
                await asyncio.sleep(.005)
        path = Path(cwd) / 'second.md'
        expected = self.retained if phase == 'work' else self.completed
        assert path.read_bytes() == expected
        commands = ([{'type': 'complete', 'task_id': 'T2', 'result': 'Actual successor bytes submitted.'}]
                    if phase == 'work' else [{'type': 'review', 'task_id': 'T2', 'verdict': 'accept',
                                              'reason': 'Read the exact submitted successor bytes.'}])
        scenario = {'job_id': context['job']['id'], 'phase': phase,
                    'sid': session_id or str(uuid4()), 'before_sha': hashlib.sha256(expected).hexdigest(),
                    'write': self.completed.decode() if phase == 'work' else None,
                    'outside': phase == 'review' and self.review_outside,
                    'response': native_reply(context, commands)}
        (self.root / (agent + '-submission.json')).write_text(json.dumps(scenario))
        result = await native_adapters.invoke_native(agent, prompt, cwd, session_id=session_id, **options)
        self.peer_results.append({'phase': phase, 'result': result})
        return result


class QuotaSuccessorSubmissionHTTPTests(unittest.TestCase):
    def fixture(self, **options):
        fixture = SuccessorSubmissionFixture(**options)
        self.addCleanup(fixture.close)
        return fixture

    def test_quota_transfer_actual_successor_submission_receives_distinct_peer_review_and_finishes(self):
        fixture = self.fixture()
        fixture.create()
        fixture.wait(lambda value: value['objective']['status'] == 'complete'
                     or (value['current_run'].get('native_quota_handoff') or {}).get('state') == 'blocked'
                     or any(job['error'] == 'team_assignment_changed' for job in value['jobs']))
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete',
                         (fixture.invoke_errors, final['current_run'], final['team']))
        work, review = fixture.submissions
        self.assertNotEqual(work['agent'], review['agent'])
        self.assertNotIn('kimi', (work['agent'], review['agent']))
        self.assertGreater(work['task']['assignment_epoch'], fixture.source_assignment['epoch'])
        self.assertEqual(review['task']['assignment_epoch'], work['task']['assignment_epoch'])
        self.assertEqual(review['task']['result_version'], work['task']['result_version'] + 1)
        self.assertEqual(review['task']['owner'], work['agent'])
        for call in (work, review):
            self.assertEqual(call['job_binding']['assignment_epoch'], call['task']['assignment_epoch'])
            self.assertEqual(call['job_binding']['result_version'], call['task']['result_version'])
        grant = work['grant']['second.md']
        self.assertEqual((grant['new_owner'], grant['assignment_epoch']), (work['agent'], work['task']['assignment_epoch']))
        self.assertEqual(work['transfer_paths'], ['second.md'])
        self.assertEqual(review['transfer_paths'], [])
        task = next(item for item in final['team']['tasks'] if item['id'] == 'T2')
        self.assertEqual((task['status'], task['owner'], task['reviewer']), ('accepted', work['agent'], review['agent']))
        project = fixture.service._project(fixture.room_id).snapshot()
        file = next(item for item in project['files'] if item['path'] == 'second.md')
        self.assertEqual((file['owner'], file['sha256']), (work['agent'], hashlib.sha256(fixture.completed).hexdigest()))
        self.assertEqual((Path(fixture.failed_work['cwd']) / 'second.md').read_bytes(), fixture.retained)
        self.assertEqual((fixture.source / 'second.md').read_bytes(), fixture.original)
        for accepted in ('T0', 'T1'):
            self.assertEqual(sum(task == accepted for _, task in fixture.work_tasks), 1)
        self.assertEqual(len(fixture.inspections), 1)
        failed_id = fixture.failed_work['job_id']
        index = next(i for i, call in enumerate(fixture.calls) if call['job_id'] == failed_id)
        self.assertFalse(any(call['agent'] == 'kimi' for call in fixture.calls[index + 1:]))
        old = next(job for job in final['jobs'] if job['id'] == failed_id)
        self.assertEqual(old['error'], 'native_side_effects_unconfirmed')
        calls = [json.loads(line) for line in (fixture.root / 'peer-submission-calls.jsonl').read_text().splitlines()]
        self.assertEqual([call['phase'] for call in calls], ['work', 'review'])
        self.assertTrue(all(call['sha256'] == hashlib.sha256(fixture.completed).hexdigest() for call in calls))
        self.assertTrue(all(item['result']['status'] == 'completed' and item['result']['native_execution']['cleanup_confirmed']
                            for item in fixture.peer_results))

    def test_review_claim_cannot_accept_after_assignment_epoch_changes(self):
        fixture = self.fixture(hold_review=True)
        fixture.create()
        before = fixture.wait(lambda value: fixture.review_started.is_set())
        with fixture.service.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND run_id=? AND id='T2'",
                       (fixture.room_id, before['current_run']['id']))
        fixture.release_review.set()
        final = fixture.settled()
        task = next(item for item in final['team']['tasks'] if item['id'] == 'T2')
        self.assertNotEqual(task['status'], 'accepted')
        self.assertTrue(any(job['error'] == 'team_assignment_changed' for job in final['jobs']))
        self.assertNotEqual(final['objective']['status'], 'complete')

    def test_peer_review_cannot_publish_outside_owner_scope(self):
        fixture = self.fixture(review_outside=True)
        fixture.create()
        fixture.wait(lambda value: fixture.review_started.is_set())
        final = fixture.settled()
        self.assertNotEqual(final['objective']['status'], 'complete')
        project = fixture.service._project(fixture.room_id).snapshot()
        self.assertEqual({item['path']: (item['sha256'], item['owner']) for item in project['files']},
                         fixture.review_canonical_files)
        self.assertEqual((fixture.source / 'README.md').read_bytes(), fixture.original_readme)
        self.assertNotEqual(next(item for item in final['team']['tasks'] if item['id'] == 'T2')['status'], 'accepted')


    def legacy_stopped(self, **options):
        fixture = self.fixture(legacy_review_failure=True, **options)
        fixture.create()
        fixture.wait(lambda value: any(job['error'] == 'team_assignment_changed' for job in value['jobs']))
        stopped = fixture.settled()
        fixture.legacy_review_failure = False
        self.assertEqual([call['phase'] for call in fixture.submissions], ['work'])
        task = next(item for item in stopped['team']['tasks'] if item['id'] == 'T2')
        self.assertEqual((task['owner'], task['result_version']), (fixture.submissions[0]['agent'], 1))
        self.assertTrue(task['result_files'])
        return fixture, stopped

    def test_normal_continue_after_legacy_host_rejection_only_retries_review_and_keeps_quota_retirement(self):
        fixture, stopped = self.legacy_stopped()
        rows = {job['id']: (job['status'], job['error']) for job in stopped['jobs']}
        accepted = {task['id']: (task['owner'], task['result_version'], task['result_digest'])
                    for task in stopped['team']['tasks'] if task['status'] == 'accepted'}
        retired = stopped['objective']['retired_partners']
        self.assertIn('kimi', retired)
        code, resumed = fixture.continue_http(stopped)
        self.assertEqual(code, 200, resumed)
        fixture.wait(lambda value: value['objective']['status'] == 'complete')
        final = fixture.settled()
        self.assertEqual([call['phase'] for call in fixture.submissions], ['work', 'review'])
        work, review = fixture.submissions
        self.assertGreater(review['task']['assignment_epoch'], work['task']['assignment_epoch'])
        self.assertEqual(review['job_binding']['assignment_epoch'], review['task']['assignment_epoch'])
        self.assertEqual(review['job_binding']['result_version'], 1)
        self.assertEqual(review['transfer_paths'], [])
        self.assertEqual(review['grant'], {})
        self.assertEqual(final['objective']['retired_partners'], retired)
        self.assertEqual({job['id']: (job['status'], job['error']) for job in final['jobs'] if job['id'] in rows}, rows)
        for task in final['team']['tasks']:
            if task['id'] in accepted:
                self.assertEqual((task['owner'], task['result_version'], task['result_digest']), accepted[task['id']])
        self.assertEqual(sum(task == 'T0' for _, task in fixture.work_tasks), 1)
        self.assertEqual(sum(task == 'T1' for _, task in fixture.work_tasks), 1)
        self.assertEqual(next(task['result_version'] for task in final['team']['tasks'] if task['id'] == 'T2'), 1)
        failed_index = next(i for i, call in enumerate(fixture.calls) if call['job_id'] == fixture.failed_work['job_id'])
        self.assertFalse(any(call['agent'] == 'kimi' for call in fixture.calls[failed_index + 1:]))

    def test_legacy_review_resume_refuses_changed_reviewer_epoch_or_published_file(self):
        for fault in ('reviewer', 'epoch', 'file'):
            with self.subTest(fault=fault):
                fixture, stopped = self.legacy_stopped()
                try:
                    task = next(item for item in stopped['team']['tasks'] if item['id'] == 'T2')
                    if fault == 'file':
                        project = fixture.service._project(fixture.room_id)
                        snapshot = project.snapshot()
                        current = next(item for item in snapshot['files'] if item['path'] == 'second.md')
                        project.apply(task['owner'], str(uuid4()), snapshot['revision'], [{
                            'path': 'second.md', 'previous_sha256': current['sha256'],
                            'content': 'Fictional newer published bytes after the stopped review.\n'}])
                    else:
                        with fixture.service.store._connection(write=True) as db:
                            if fault == 'reviewer':
                                db.execute("UPDATE shared_tasks SET reviewer=owner WHERE room_id=? AND run_id=? AND id='T2'",
                                           (fixture.room_id, stopped['current_run']['id']))
                            else:
                                db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND run_id=? AND id='T2'",
                                           (fixture.room_id, stopped['current_run']['id']))
                    calls = len(fixture.calls)
                    code, rejected = fixture.continue_http(stopped)
                    self.assertEqual(code, 409, rejected)
                    self.assertEqual(len(fixture.calls), calls)
                    self.assertNotEqual(next(item['status'] for item in fixture.read()['team']['tasks'] if item['id'] == 'T2'), 'accepted')
                finally:
                    fixture.close()


if __name__ == '__main__':
    unittest.main()
