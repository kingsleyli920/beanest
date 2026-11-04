"""Transferred write grants belong to owners, not independent reviewers."""
import copy
import json
import unittest

from local_agent_coordinator.room_store import RoomError
import test_team_store as fixtures


class SuccessorReviewGrantTests(unittest.TestCase):
    setUp = fixtures.TeamStoreTests.setUp
    tearDown = fixtures.TeamStoreTests.tearDown
    room = fixtures.TeamStoreTests.room
    finish = fixtures.TeamStoreTests.finish
    task = fixtures.TeamStoreTests.task
    plan = fixtures.TeamStoreTests.plan

    def prepared(self):
        room = self.plan(dependency=True, paths=True)
        work = self.store.claim(room, 'claude')
        self.grant = {'main.py': {'old_owner': 'codex', 'new_owner': 'claude',
            'sha256': 'a'*64, 'revision': 1, 'assignment_epoch': work['team']['assignment_epoch']}}
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET ownership_grant_json=? WHERE room_id=? AND id='main'",
                       (json.dumps(self.grant), room))
        return room, work

    def review(self, room, work):
        self.finish(work, [{'type': 'complete', 'task_id': 'main', 'result': 'Changed owned content.'}],
            artifact_revision=2, artifact_digest='d'*64, artifact_files=[{'path': 'main.py', 'sha256': 'b'*64}])
        self.store.record_job_cleanup(work['id'], confirmed=True)
        return self.store.claim(room, 'codex')

    def test_owner_retains_write_grant_and_peer_review_does_not_inherit_it(self):
        room, work = self.prepared()
        self.assertEqual(self.store.ownership_grant(work['id']), self.grant)
        review = self.review(room, work)
        self.assertEqual(review['team']['job_phase'], 'review')
        self.assertEqual(self.store.ownership_grant(review['id']), {})
        self.finish(review, [{'type': 'review', 'task_id': 'main', 'verdict': 'accept', 'reason': 'Read changed version.'}])
        self.assertEqual(self.task(room, 'main')['status'], 'accepted')
        self.assertEqual(self.task(room, 'main')['result_files'], [{'path': 'main.py', 'sha256': 'b'*64}])

    def test_owner_epoch_path_and_grant_destination_drift_remain_rejected(self):
        for fault in ('owner', 'epoch', 'path', 'destination'):
            with self.subTest(fault=fault):
                room, work = self.prepared()
                grant = copy.deepcopy(self.grant)
                with self.store._connection(write=True) as db:
                    if fault == 'owner':
                        db.execute("UPDATE shared_tasks SET owner='codex' WHERE room_id=? AND id='main'", (room,))
                    elif fault == 'epoch':
                        db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND id='main'", (room,))
                    else:
                        if fault == 'path': grant['outside.py'] = grant.pop('main.py')
                        else: grant['main.py']['new_owner'] = 'codex'
                        db.execute("UPDATE shared_tasks SET ownership_grant_json=? WHERE room_id=? AND id='main'", (json.dumps(grant), room))
                with self.assertRaisesRegex(RoomError, 'team_assignment_changed'):
                    self.store.ownership_grant(work['id'])

    def test_review_recipient_result_and_epoch_drift_remain_rejected(self):
        for field, value in (('reviewer', 'claude'), ('result_version', 9), ('assignment_epoch', 9)):
            with self.subTest(field=field):
                room, work = self.prepared()
                review = self.review(room, work)
                with self.store._connection(write=True) as db:
                    db.execute(f"UPDATE shared_tasks SET {field}=? WHERE room_id=? AND id='main'", (value, room))
                with self.assertRaisesRegex(RoomError, 'team_assignment_changed'):
                    self.store.ownership_grant(review['id'])

    def legacy_rejection(self):
        room, work = self.prepared()
        review = self.review(room, work)
        self.store.finish_job(review['id'], error='team_assignment_changed', uncertain=False)
        metadata = dict(never_started=True, cleanup_confirmed=True,
                        side_effects_possible=False, tool_calls=0, tool_results=0)
        self.store.record_native_execution(review['id'], metadata, 'not_started')
        with self.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET mode='native_project' WHERE id=?", (room,))
            # This isolated legacy board has one unfinished submitted result.
            db.execute("UPDATE shared_tasks SET status='accepted',blocked_reason=NULL WHERE room_id=? AND id='tests'", (room,))
            claimed = db.execute("SELECT id FROM jobs WHERE room_id=? AND status!='queued'", (room,)).fetchall()
        for job in claimed:
            self.store.record_job_cleanup(job['id'], confirmed=True)
        return room, review

    def source(self, room):
        with self.store._connection() as db:
            run = db.execute('SELECT current_run_id FROM teams WHERE room_id=?', (room,)).fetchone()[0]
            return self.store._team._host_text_failure_source(db, room, run)

    def test_exact_unstarted_legacy_review_is_resumable_but_malformed_metadata_is_not(self):
        room, review = self.legacy_rejection()
        self.assertTrue(self.source(room)['review_only'])
        for metadata in ('[1]', '{bad', json.dumps({'status':'completed', 'never_started':False}),
                         json.dumps({'status':'not_started','never_started':True,'cleanup_confirmed':False})):
            with self.subTest(metadata=metadata):
                with self.store._connection(write=True) as db:
                    db.execute('UPDATE native_executions SET metadata_json=? WHERE job_id=?', (metadata, review['id']))
                self.assertIsNone(self.source(room))

    def test_newer_started_failure_cannot_borrow_older_unstarted_proof(self):
        room, review = self.legacy_rejection()
        self.assertTrue(self.source(room)['review_only'])
        from uuid import uuid4
        newer = str(uuid4())
        with self.store._connection(write=True) as db:
            for table, key in (('jobs','id'), ('team_jobs','job_id'), ('native_executions','job_id')):
                row = dict(db.execute(f'SELECT * FROM {table} WHERE {key}=?', (review['id'],)).fetchone())
                row[key] = newer
                if table == 'native_executions':
                    row['metadata_json'] = json.dumps(dict(status='failed', never_started=False,
                        cleanup_confirmed=True, side_effects_possible=False, tool_calls=0, tool_results=0))
                db.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
        self.store.record_job_cleanup(newer, confirmed=True)
        self.assertIsNone(self.source(room))

    def test_retry_baseline_uses_published_pending_bytes_without_accepting_the_task(self):
        room, review = self.legacy_rejection()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET result_files_json=? WHERE room_id=? AND id='tests'",
                       (json.dumps([{'path':'main.py','sha256':'a'*64}]), room))
            run = db.execute('SELECT current_run_id FROM teams WHERE room_id=?', (room,)).fetchone()[0]
        self.assertEqual(self.store.run_result_files(room, run), {'main.py':'a'*64})
        current = self.store.run_result_files(room, run, include_provenance=True, include_retry_review=True)
        self.assertEqual(current, [{'path':'main.py','sha256':'b'*64,'run_id':run,
                                  'task_id':'main','review_kind':'pending_review'}])
        self.assertEqual(self.task(room, 'main')['status'], 'blocked')
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND id='main'", (room,))
        with self.assertRaisesRegex(RoomError, 'objective_binding_changed'):
            self.store.run_result_files(room, run, include_retry_review=True)

    def test_uninvoked_dependent_collateral_stop_does_not_require_a_fake_review_receipt(self):
        room, review = self.legacy_rejection()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET status='blocked',blocked_reason='team_assignment_changed' "
                       "WHERE room_id=? AND id='tests'", (room,))
        self.assertTrue(self.source(room)['review_only'])
        # A collateral published result without its own exact proof is not the
        # uninvoked waiting-task case and cannot borrow main's old receipt.
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET result_version=1 WHERE room_id=? AND id='tests'", (room,))
        self.assertIsNone(self.source(room))

    def test_stopped_public_projection_only_exposes_exact_review_retry(self):
        room, review = self.legacy_rejection()
        with self.store._connection(write=True) as db:
            self.store._team.objectives.create(db, room, 'Retry the current submitted result', {'mode':'autonomous'})
            db.execute("UPDATE objectives SET status='stopped',stop_reason='team_assignment_changed' WHERE room_id=?", (room,))
        self.assertTrue(self.store.host_text_failure(room)['review_only'])
        with self.store._connection(write=True) as db:
            db.execute("UPDATE native_executions SET metadata_json='[]' WHERE job_id=?", (review['id'],))
        self.assertIsNone(self.store.host_text_failure(room))
