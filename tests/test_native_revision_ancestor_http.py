"""A public continuation chain preserves current D and old results; no real CLI."""
import asyncio
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_native_room_service as native
import test_native_revision_binding_http as binding


class NativeRevisionAncestorHTTPTests(unittest.TestCase):
    setUp, tearDown = binding.NativeRevisionBindingHTTPTests.setUp, binding.NativeRevisionBindingHTTPTests.tearDown
    request, create = binding.NativeRevisionBindingHTTPTests.request, binding.NativeRevisionBindingHTTPTests.create
    old_hint_failure = binding.NativeRevisionBindingHTTPTests.old_hint_failure
    continue_body = staticmethod(binding.NativeRevisionBindingHTTPTests.continue_body)

    def settled(self, room_id):
        binding.NativeRevisionBindingHTTPTests.settled(self, room_id)
        # The first HTTP snapshot can precede the worker's final return even
        # when it is done by the time the client receives that response.
        code, fresh = self.request('GET', f'/api/rooms/{room_id}')
        self.assertEqual(code, 200, fresh)
        return fresh

    def handler(self, agent, context, cwd):
        call = next(call for call in reversed(self.native.calls) if call['context']['job']['id'] == context['job']['id'])
        if context['job']['job_phase'] == 'work' and call['permission'] == 'read_only' and hasattr(self, 'current_report'):
            self.assertEqual((cwd / 'results/report.md').read_text(), self.current_report)
            self.assertTrue(context['native_partial_inspection']['historical_immutability_unconfirmed'])
            self.inspections.append(call)
            if hasattr(self, 'inspection_started'):
                self.inspection_started.set()
                async def held():
                    while not self.inspection_release.is_set():
                        await asyncio.sleep(0.005)
                    return native.native_reply(context, [{'type':'complete','task_id':'M1','result':'Current S/D and matching evidence inspected; old completion not accepted.'}])
                return held()
            return native.native_reply(context, [{'type':'complete','task_id':'M1','result':'Current S/D and matching evidence inspected; old completion not accepted.'}])
        return binding.NativeRevisionBindingHTTPTests.handler(self, agent, context, cwd)

    def empty_plan_failure(self):
        self.phases, self.inspections = [], []
        failed = self.old_hint_failure()
        room = failed['room']['id']
        if getattr(self, 'legacy_block', False):
            with self.service.store._connection(write=True) as db:
                db.execute("UPDATE shared_tasks SET block_cause=NULL WHERE room_id=? AND run_id=? AND id='M2'",
                           (room, failed['current_run']['id']))
        # Only this temporary legacy host lacks the new hint provenance bridge.
        with patch.object(self.service.store, '_native_revision_hint_source', return_value=False):
            code, carried = self.request('POST', f'/api/rooms/{room}/messages', self.continue_body(failed))
            self.assertEqual(code, 200, carried)
            carried = self.settled(room)
        self.assertEqual(carried['objective']['status'], 'needs_user')
        self.assertEqual(carried['objective']['budget']['used'], 0)
        self.assertEqual([task['assignment_epoch'] for task in carried['team']['tasks']], [1, 1, 1])
        self.current_report = 'The later plan amended this retained report. Check current S/D; earlier checks are historical.\n'
        started = threading.Event()

        async def empty_planners(agent, context, cwd):
            self.assertEqual(context['job']['job_phase'], 'plan')
            self.assertEqual(context['team']['tasks'], [])
            if agent == 'codex':
                call = next(call for call in reversed(self.native.calls) if call['context']['job']['id'] == context['job']['id'])
                directory = self.root / 'native-receipts' / room
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                receipt = directory / (str(uuid4()) + '.jsonl')
                receipt.write_text(json.dumps({'channel':'stdout','data':{'type':'thread.started','thread_id':call['session_id']}}) + '\n')
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    error.native_execution = {'permission':'full_access','tool_calls':1,'tool_results':1,
                        'cleanup_confirmed':True,'side_effects_possible':True,'exit_code':-15,'receipt_path':str(receipt)}
                    raise
            while not started.is_set():
                await asyncio.sleep(0.005)
            (cwd / 'results/report.md').write_text(self.current_report)
            return native.native_reply(context, [{'type':'plan_ready'}])

        self.native.handler = empty_planners
        code, empty = self.request('POST', f'/api/rooms/{room}/messages', self.continue_body(carried))
        self.assertEqual(code, 200, empty)
        empty = self.settled(room)
        self.native.handler = self.handler
        self.assertEqual(empty['team']['stop_reason'], 'native_changes_not_imported', empty['team'])
        self.assertEqual(empty['team']['tasks'], [])
        # Cancellation leaves execution unresolved, but the owned receipt
        # establishes identity independently of that failure.
        self.assertFalse(next(member for member in empty['members'] if member['id'] == 'codex')['identity_uncertain'])
        self.assertTrue(self.service.store.native_continuation_requires_recovery(room))
        return failed, carried, empty

    def test_current_binding_recovers_the_carried_board_after_empty_plan_with_changed_D(self):
        failed, carried, empty = self.empty_plan_failure()
        room = empty['room']['id']
        self.assertTrue(empty['current_run']['can_recover_native'], empty['current_run'])
        old_rows = [(job['id'], job['status'], job['error']) for job in empty['jobs']]
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        prior = len(self.native.calls)
        try:
            code, accepted = self.request('POST', f'/api/rooms/{room}/messages', self.continue_body(empty))
            self.assertEqual(code, 200, accepted)
            self.assertTrue(self.inspection_started.wait(3))
            held = self.service.store.snapshot(room)
            self.assertEqual(len(self.native.calls[prior:]), 1)
            self.assertEqual(self.native.calls[-1]['permission'], 'read_only')
            self.assertIsNone(self.native.calls[-1]['session_id'])
            self.assertEqual(self.service._project(room).snapshot()['revision'], 1)
            tasks = {task['id']:task for task in held['team']['tasks']}
            self.assertEqual(tasks['M2']['result'], carried['team']['tasks'][1]['result'])
            self.assertEqual((tasks['M2']['result_version'], tasks['M2']['status']), (1, 'review'))
            self.assertEqual(tasks['M3']['dependencies'], ['M1','M2'])
            self.assertEqual([tasks[name]['assignment_epoch'] for name in ('M1','M2','M3')], [2,2,2])
            proof = next(event for event in reversed(held['events']) if event['kind'] == 'native_partial_recovery_attestation')
            self.assertEqual(proof['recovery_ancestry']['source_run_id'], failed['current_run']['id'])
            self.assertEqual(proof['recovery_ancestry']['board_run_id'], carried['current_run']['id'])
            self.assertEqual(proof['observation']['files'][0]['sha256'], hashlib.sha256(self.current_report.encode()).hexdigest())
        finally:
            self.inspection_release.set()
        final = self.settled(room)
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual([(job['id'],job['status'],job['error'])for job in final['jobs'][:len(old_rows)]],old_rows)
        self.assertEqual(self.writes, 1)
        self.assertEqual(len(self.inspections), 1)
        self.assertEqual(sum(phase[:2] == ('work','M2') for phase in self.phases), 1)
        self.assertEqual(self.service._project(room).files_for_paths(['results/report.md'])[0]['content'], self.current_report)

    def test_ancestry_missing_events_changed_board_and_unknown_cleanup_are_not_authority(self):
        failed, carried, empty = self.empty_plan_failure()
        room, board = empty['room']['id'], carried['current_run']['id']
        actor = next(job['id'] for job in empty['jobs'] if job['error'] == 'native_changes_not_imported')
        mutations = [
            ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='objective_work_carried' AND json_extract(value_json,'$.run_id')=?",(room,board)),
            ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='objective_decision' AND json_extract(value_json,'$.run_id')=?",(room,empty['current_run']['id'])),
            ("UPDATE shared_tasks SET owner='codex' WHERE run_id=? AND id='M1'",(board,)),
            ("UPDATE shared_tasks SET result_version=result_version+1 WHERE run_id=? AND id='M2'",(board,)),
            ("UPDATE shared_tasks SET dependencies_json='[]' WHERE run_id=? AND id='M3'",(board,)),
            ("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.cleanup_confirmed',json('false')) WHERE job_id=?",(actor,)),
            ("UPDATE team_jobs SET phase='work' WHERE job_id=?",(actor,)),
            ("UPDATE objectives SET continuation_json=json_set(continuation_json,'$.source_run_id',?) WHERE id=?",
             (empty['current_run']['id'],empty['objective']['id'])),
            ("UPDATE shared_tasks SET write_paths_json='[]' WHERE run_id=? AND id='M1'",(board,)),
            ("UPDATE shared_tasks SET block_cause='permission_required' WHERE run_id=? AND id='M1'",(failed['current_run']['id'],)),
        ]
        with self.service.store._connection(write=True) as db:
            for sql, parameters in mutations:
                with self.subTest(sql=sql):
                    db.execute('SAVEPOINT bad_chain');db.execute(sql,parameters)
                    self.assertEqual(self.service.store._native_revision_hint_ancestor(db,room),[])
                    db.execute('ROLLBACK TO bad_chain');db.execute('RELEASE bad_chain')
        self.assertEqual(len(self.inspections),0)

    def test_real_legacy_block_survives_the_ancestor_board_recovery(self):
        self.model_block = self.legacy_block = True
        _, _, empty = self.empty_plan_failure()
        room = empty['room']['id']
        code, response = self.request('POST', f'/api/rooms/{room}/messages', self.continue_body(empty))
        self.assertEqual(code,200,response)
        final = self.settled(room)
        blocked = next(task for task in final['team']['tasks'] if task['id']=='M2')
        self.assertEqual((blocked['status'],blocked['blocked_reason'],blocked['block_cause']),
                         ('blocked','project_revision_conflict',None))
        self.assertNotEqual(final['objective']['status'],'complete')
        self.assertEqual(sum(phase[:2]==('work','M2')for phase in self.phases),1)

    def test_later_canonical_commit_cannot_be_authorized_by_an_ancestor(self):
        _, _, empty = self.empty_plan_failure()
        room = empty['room']['id']
        project = self.service._project(room)
        project.apply('codex','later-independent-result',1,[{'path':'later.md','previous_sha256':None,'content':'A newer canonical version.\n'}])
        calls = len(self.native.calls)
        code, snapshot = self.request('GET', f'/api/rooms/{room}')
        self.assertEqual(code,200,snapshot)
        self.assertFalse(snapshot['current_run']['can_recover_native'])
        self.assertEqual(snapshot['current_run']['native_recovery_reason'],'native_recovery_binding_changed')
        code, response = self.request('POST',f'/api/rooms/{room}/messages',self.continue_body(snapshot))
        self.assertEqual(code,409,response)
        self.assertEqual(len(self.native.calls),calls)

    def test_frozen_current_D_cannot_drift_during_the_new_readonly_inspection(self):
        _, _, empty = self.empty_plan_failure()
        room = empty['room']['id']
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        try:
            code, response = self.request('POST', f'/api/rooms/{room}/messages', self.continue_body(empty))
            self.assertEqual(code,200,response)
            self.assertTrue(self.inspection_started.wait(3))
            state = self.service.store.native_workspace(room,'claude')
            (Path(state['project'])/'results/report.md').write_text('Changed after the host froze this recovery.\n')
        finally:
            self.inspection_release.set()
        final = self.settled(room)
        self.assertEqual(final['team']['stop_reason'],'native_recovery_unpublished_changes')
        self.assertEqual(self.service._project(room).snapshot()['revision'],1)
        self.assertFalse(any(event['kind']=='native_partial_inspected' for event in final['events']))


if __name__ == '__main__':
    unittest.main()
