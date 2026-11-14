"""Exact clean-scope retries keep native provenance and transactional guards."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_service import RoomError, _project_digest
import test_native_help_repair_http as fixture


class NativeScopeRetryTests(unittest.TestCase):
    setUp = fixture.NativeHelpRepairHTTPTests.setUp
    tearDown = fixture.NativeHelpRepairHTTPTests.tearDown
    request = fixture.NativeHelpRepairHTTPTests.request
    settled = fixture.NativeHelpRepairHTTPTests.settled
    create = fixture.NativeHelpRepairHTTPTests.create

    async def handler(self, agent, context, cwd):
        reply = await fixture.NativeHelpRepairHTTPTests.handler(self, agent, context, cwd)
        if any(command.get('type') == 'request_help' for command in reply['commands']):
            # Retired host fixture models the real incident's clean private D.
            (cwd / 'docs/result.md').unlink(missing_ok=True)
        return reply

    def stopped(self):
        self.repair_paths = ['notes.md']
        original = self.service._validate_team_result
        def retired(job, parsed):
            if any(command['type'] == 'request_help' for command in parsed['commands']):
                raise RoomError('team_scope_conflict')
            return original(job, parsed)
        with patch.object(self.service, '_validate_team_result', side_effect=retired):
            result = self.create()
        self.assertEqual(result['team']['stop_reason'], 'team_scope_conflict')
        return result

    def test_candidate_rejects_changed_epoch_owner_identity_cleanup_and_native_outcome(self):
        snapshot = self.stopped()
        store, room = self.service.store, snapshot['room']['id']
        candidates = store.native_scope_retry_candidates(room)
        self.assertEqual(len(candidates), 1)
        self.assertIs(snapshot['current_run']['native_scope_retry']['existing_partial_has_changes'], False)
        # A successful generic audit of EMPTY D does not need a partial-D
        # inspection/receiver merely because account availability is known.
        snapshot['current_run']['can_recover_native'] = True
        self.assertEqual(self.service._natural_continuation(snapshot)['action'], 'restart-objective')
        source = candidates[0]
        mutations = [
            ("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND id='M3'", (room,)),
            ("UPDATE shared_tasks SET owner='claude' WHERE room_id=? AND id='M3'", (room,)),
            ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='native_session_cleanup_identity' AND json_extract(value_json,'$.job_id')=?", (room, source['job_id'])),
            ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_cleanup' AND json_extract(value_json,'$.job_id')=?", (room, source['job_id'])),
            ("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.status','failed') WHERE job_id=?", (source['job_id'],)),
        ]
        with store._connection(write=True) as db:
            for sql, parameters in mutations:
                with self.subTest(mutation=sql):
                    db.execute('SAVEPOINT negative_control')
                    db.execute(sql, parameters)
                    self.assertEqual(store._native_scope_retry_candidates(db, room), [])
                    db.execute('ROLLBACK TO negative_control')
                    db.execute('RELEASE negative_control')
            self.assertEqual(store._native_scope_retry_candidates(db, room), candidates)

    def test_transaction_rejects_missing_audit_and_state_change_after_audit(self):
        snapshot = self.stopped()
        store, room = self.service.store, snapshot['room']['id']
        candidates = store.native_scope_retry_candidates(room)
        audit = self.service._audit_native_scope_retry(store.snapshot(room), candidates)
        project = self.service._project(room).snapshot()
        binding = {'run_id': snapshot['current_run']['id'], 'revision': project['revision'], 'digest': _project_digest(project)}
        def restart(proof):
            store.objective_action(room, 'restart-objective', str(uuid4()), [binding], binding=binding,
                policy={'mode':'autonomous'}, scope_retry=proof)
        with self.assertRaises(RoomError) as missing:
            restart(None)
        self.assertEqual(missing.exception.code, 'native_recovery_binding_changed')
        with store._connection(write=True) as db:
            row = db.execute('SELECT state_json FROM native_workspaces WHERE room_id=? AND provider=?', (room, candidates[0]['provider'])).fetchone()
            previous = row[0]
            changed = json.loads(previous)
            changed['generated'] = ['state changed after audit']
            db.execute('UPDATE native_workspaces SET state_json=? WHERE room_id=? AND provider=?',
                (json.dumps(changed), room, candidates[0]['provider']))
        try:
            with self.assertRaises(RoomError) as stale:
                restart(audit)
            self.assertEqual(stale.exception.code, 'native_recovery_binding_changed')
        finally:
            with store._connection(write=True) as db:
                db.execute('UPDATE native_workspaces SET state_json=? WHERE room_id=? AND provider=?',
                    (previous, room, candidates[0]['provider']))
        self.assertEqual(store.snapshot(room)['team']['run_id'], binding['run_id'])
        peer = next(member['id'] for member in snapshot['members'] if member['id'] != candidates[0]['provider'])
        with store._connection(write=True) as db:
            previous = db.execute('SELECT state_json FROM native_workspaces WHERE room_id=? AND provider=?', (room, peer)).fetchone()[0]
            db.execute('DELETE FROM native_workspaces WHERE room_id=? AND provider=?', (room, peer))
        try:
            self.assertEqual(store.native_scope_retry_candidates(room), [])
            with self.assertRaises(RoomError) as missing_tree:
                self.service._audit_native_scope_retry(store.snapshot(room), candidates)
            self.assertEqual(missing_tree.exception.code, 'native_recovery_workspace_unavailable')
        finally:
            with store._connection(write=True) as db:
                db.execute('INSERT INTO native_workspaces VALUES (?,?,?)', (room, peer, previous))

    def test_missing_peer_workspace_blocks_the_help_repair_audit_and_the_room_restarts_without_it(self):
        # Catches the audited help repair running although a called peer's
        # private project is not registered (the helper's proposed bytes would
        # be retained or published unaudited), and the room refusing every
        # continue because that audit cannot be made.
        snapshot = self.create(legacy=True)
        room, store = snapshot['room']['id'], self.service.store
        self.assertTrue(snapshot['current_run']['can_recover_native'])
        candidate = store.native_help_repair_candidate(room)
        peer = next(member['id'] for member in snapshot['members'] if member['id'] != candidate['provider'])
        self.assertEqual((candidate['provider'], peer), ('claude', 'codex'))
        calls = len(self.native.calls)
        with store._connection(write=True) as db:
            previous = db.execute('SELECT state_json FROM native_workspaces WHERE room_id=? AND provider=?', (room, peer)).fetchone()[0]
            db.execute('DELETE FROM native_workspaces WHERE room_id=? AND provider=?', (room, peer))
        try:
            code, current = self.request('GET', f'/api/rooms/{room}')
            self.assertEqual(code, 200)
            # The audited help repair is still refused for the missing tree.
            self.assertFalse(current['current_run']['can_recover_native'])
            self.assertEqual(current['current_run']['native_recovery_reason'], 'native_recovery_workspace_unavailable')
            self.assertTrue(current['continuation']['can_send'], current['continuation'])
            run = current['current_run']
            code, result = self.request('POST', f'/api/rooms/{room}/recover-native', {
                'request_id':str(uuid4()), 'run_id':run['id'], 'revision':run['revision'], 'digest':run['digest']})
            self.assertEqual((code, result.get('error')), (200, None))
            # Only the start of the generic restart is asserted here: with the
            # registration removed by hand, what the new run then does is not
            # this test's subject.
            final = self.settled(room)
            kinds = [event['kind'] for event in final['events']]
            self.assertEqual(kinds.count('native_unconfirmed_calls_abandoned'), 1)
            self.assertEqual(kinds.count('native_help_repair_retained'), 0)
            self.assertEqual(kinds.count('native_help_repair_workspace'), 0)
            abandoned = next(event for event in final['events'] if event['kind'] == 'native_unconfirmed_calls_abandoned')
            self.assertEqual((abandoned['job_ids'], abandoned['old_commands_accepted']), ([candidate['job_id']], False))
            self.assertNotEqual(final['current_run']['id'], run['id'])
            self.assertNotIn(candidate['job_id'], [call['context']['job']['id'] for call in self.native.calls[calls:]])
            # The helper's proposed repairs were never published by the restart.
            files = self.service._project(room).files_for_paths(['src/a.txt', 'src/b.txt'])
            self.assertEqual([file['content'] for file in files], ['original src/a.txt\n', 'original src/b.txt\n'])
        finally:
            with store._connection(write=True) as db:
                db.execute('DELETE FROM native_workspaces WHERE room_id=? AND provider=?', (room, peer))
                db.execute('INSERT INTO native_workspaces VALUES (?,?,?)', (room, peer, previous))
