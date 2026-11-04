"""HTTP/SQLite and owned ACP coverage of generic engine cache import policy.

Disk changes are fictional fixture inputs; Kimi completion/identity/cleanup uses
real stdio ACP. No installed CLI, model, source project or account is accessed.
"""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import folder_workspace
from test_native_outcome_http import OutcomeFixture
from test_native_room_service import native_reply
from test_team_store import proposal


class GeneratedCacheFixture(OutcomeFixture):
    cache_path = 'code/.godot/fixture-check.lock'
    report = b'Fictional authorized task report.\n'

    def __init__(self, fault=None):
        super().__init__({'native_case': 'completed'})
        self.release_peer.set()
        self.fault = fault
        self.original_readme = (self.source / 'README.md').read_bytes()
        self.work = []
        self.inspections = []
        self.capture_patch = None
        if fault == 'captured':
            # Capture under the actual previous generic policy, then exercise
            # the new collector against that immutable captured baseline.
            self.capture_patch = patch.object(folder_workspace, '_EXCLUDED_DIRS',
                folder_workspace._EXCLUDED_DIRS - {'.godot'})
            self.capture_patch.start()
            path = self.source / self.cache_path
            path.parent.mkdir(parents=True)
            path.write_bytes(b'Previously captured project input.\n')

    def close(self):
        if self.capture_patch:
            self.capture_patch.stop()
            self.capture_patch = None
        super().close()

    def reply(self, agent, context, cwd):
        phase, task = context['job']['job_phase'], context.get('task')
        if phase in {'plan', 'coordinate'}:
            commands = []
            if agent == context['team']['plan_author'] and not context['team']['tasks']:
                commands = [proposal('T1', paths=['result.md']), {'type': 'claim', 'task_id': 'T1'}]
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        if phase == 'work':
            if context.get('native_partial_inspection'):
                self.inspections.append({'job_id': context['job']['id'], 'cwd': str(cwd),
                                         'observation': context['native_partial_inspection']})
                assert (Path(cwd) / 'result.md').read_bytes() == self.report
                return native_reply(context, [{'type': 'complete', 'task_id': task['id'],
                    'result': 'Inspected exact retained report without repeating its original execution.'}])
            if self.capture_patch:
                state = self.service.store.native_workspace(self.room_id, agent)
                assert any(entry['path'] == self.cache_path for entry in state['files'])
                self.capture_patch.stop()
                self.capture_patch = None
            path = Path(cwd) / self.cache_path
            path.parent.mkdir(parents=True, exist_ok=True)
            (Path(cwd) / 'result.md').write_bytes(self.report)
            if self.fault == 'sensitive':
                path = path.parent / 'credentials.json'
                path.write_bytes(b'fictional-not-a-secret')
            elif self.fault == 'symlink':
                path.symlink_to(Path(cwd) / 'README.md')
            elif self.fault == 'ordinary':
                path = Path(cwd) / 'code/unowned.md'
                path.write_bytes(b'Unauthorized source input.\n')
            else:
                path.write_bytes(b'')
            self.work.append({'job_id': context['job']['id'], 'cwd': str(cwd),
                              'path': str(path), 'context': context})
            return native_reply(context, [{'type': 'complete', 'task_id': task['id'],
                                          'result': 'The authorized fictional report is checked.'}])
        return super().reply(agent, context, cwd)

    def continue_http(self, snapshot):
        return self.request('POST', f'/api/rooms/{self.room_id}/messages', {
            'text': 'Continue the original task while retaining its current report.', 'target': 'all',
            'request_id': str(uuid4()), 'intent': 'continue', 'binding': snapshot['continuation']['binding']})


class GeneratedCacheImportHTTPTests(unittest.TestCase):
    def fixture(self, **options):
        value = GeneratedCacheFixture(**options)
        self.addCleanup(value.close)
        return value

    def assert_private_not_published(self, fixture, final):
        project = fixture.service._project(fixture.room_id).snapshot()
        self.assertEqual([(entry['path'], entry['sha256']) for entry in project['files']],
                         [('result.md', hashlib.sha256(fixture.report).hexdigest())])
        self.assertEqual((fixture.source / 'README.md').read_bytes(), fixture.original_readme)
        self.assertFalse(any('.godot' in entry['path'] for task in final['team']['tasks']
                             for entry in task['result_files'] or []))
        self.assertEqual((Path(fixture.work[0]['cwd']) / fixture.cache_path).read_bytes(), b'')
        self.assertFalse((fixture.source / fixture.cache_path).exists())

    def test_new_engine_cache_stays_private_while_authorized_report_is_imported_and_reviewed(self):
        fixture = self.fixture()
        fixture.create()
        final = fixture.settled()
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertTrue(all(job['error'] is None for job in final['jobs']))
        self.assertEqual(len(fixture.work), 1)
        self.assert_private_not_published(fixture, final)
        state = fixture.service.store.native_workspace(fixture.room_id, 'kimi')
        self.assertEqual(state.get('blocked'), [])
        self.assertTrue(any(entry['path'] == fixture.cache_path for entry in state.get('generated', [])))

    def test_previous_cache_rejection_recovers_through_normal_continue_and_fresh_readonly_inspection(self):
        fixture = self.fixture()
        # Replay only the former excluded-directory policy, using the real
        # collector and normal host failure path, not forged ledger rows.
        with patch.object(folder_workspace, '_EXCLUDED_DIRS', folder_workspace._EXCLUDED_DIRS - {'.godot'}):
            fixture.create()
            stopped = fixture.settled()
        self.assertEqual(stopped['team']['stop_reason'], 'native_changes_not_imported')
        old = next(job for job in stopped['jobs'] if job['error'] == 'native_changes_not_imported')
        rows = {job['id']: (job['status'], job['error']) for job in stopped['jobs']}
        self.assertEqual((Path(fixture.work[0]['cwd']) / 'result.md').read_bytes(), fixture.report)
        code, resumed = fixture.continue_http(stopped)
        self.assertEqual(code, 200, resumed)
        fixture.wait(lambda value: value['objective']['status'] == 'complete')
        final = fixture.settled()
        self.assertEqual(len(fixture.inspections), 1)
        inspection_id = fixture.inspections[0]['job_id']
        self.assertEqual(next(call['permission'] for call in fixture.calls if call['job_id'] == inspection_id),
                         'read_only')
        before_task, after_task = stopped['team']['tasks'][0], final['team']['tasks'][0]
        self.assertEqual(after_task['owner'], before_task['owner'])
        self.assertGreater(after_task['assignment_epoch'], before_task['assignment_epoch'])
        self.assertEqual((before_task['result_version'], after_task['result_version']), (0, 1))
        self.assertEqual((Path(fixture.work[0]['cwd']) / 'result.md').read_bytes(), fixture.report)
        self.assertEqual({job['id']: (job['status'], job['error']) for job in final['jobs'] if job['id'] in rows}, rows)
        self.assertEqual(next(job['error'] for job in final['jobs'] if job['id'] == old['id']), 'native_changes_not_imported')
        self.assert_private_not_published(fixture, final)
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))

    def test_sensitive_symlink_captured_and_ordinary_unowned_files_remain_rejected(self):
        for fault in ('sensitive', 'symlink', 'captured', 'ordinary'):
            with self.subTest(fault=fault):
                fixture = self.fixture(fault=fault)
                try:
                    fixture.create()
                    final = fixture.settled()
                    self.assertNotEqual(final['objective']['status'], 'complete')
                    self.assertFalse(any(task['status'] == 'accepted' for task in final['team']['tasks']))
                    self.assertFalse(any(entry['path'] == 'result.md'
                                         for entry in fixture.service._project(fixture.room_id).snapshot()['files']))
                    self.assertEqual(len(fixture.work), 1)
                    failed = next(job for job in final['jobs'] if job['id'] == fixture.work[0]['job_id'])
                    self.assertEqual(failed['error'], 'native_symlink_refused' if fault == 'symlink'
                                     else 'native_changes_not_imported')
                    result = next(item['result'] for item in fixture.results if item['job_id'] == failed['id'])
                    self.assertEqual(result['status'], 'completed')
                    self.assertTrue(result['native_execution']['cleanup_confirmed'])
                    if fault == 'captured':
                        self.assertEqual((fixture.source / fixture.cache_path).read_bytes(),
                                         b'Previously captured project input.\n')
                    self.assertTrue(Path(fixture.work[0]['path']).is_symlink() if fault == 'symlink'
                                    else Path(fixture.work[0]['path']).exists())
                finally:
                    fixture.close()


if __name__ == '__main__':
    unittest.main()
