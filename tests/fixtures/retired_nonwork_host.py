"""Small, fictional reproduction of two retired host decisions (protocol v1).

This fixture models the old hint rejection and forced read-only recovery only.
It deliberately delegates current scheduling, receipts and recovery audits to the
real host; no runtime source snapshot or production module is copied here.
"""
from contextlib import ExitStack, contextmanager
import json
from unittest.mock import patch

from local_agent_coordinator import room_response_schema
from local_agent_coordinator.room_store import RoomError


def reject_nonwork_hints(service):
    current = service._collect_native_changes

    def collect(job, parsed):
        if job['team']['job_phase'] != 'work' and parsed['unchanged_paths']:
            raise RoomError('team_scope_conflict')
        return current(job, parsed)

    return patch.object(service, '_collect_native_changes', collect)


@contextmanager
def forced_readonly_recovery(service):
    """Retired inspection jobs never published work or advanced the board."""
    store, team = service.store, service.store._team
    claim, protocol, permission = store.claim, service._native_protocol, service._native_permission
    finish = service._finish
    schema = room_response_schema.response_schema
    inspections = []

    def inspection_schema(**options):
        if options.get("native") and options.get("team_phase") == "coordinate":
            options["team_phase"] = "user"
        return schema(**options)

    def queue(db, room_id, source, observation):
        if not team._queue(db, room_id, source['provider'], 'coordinate', text=
                           'Retired host inspection: read current state only; do not edit or replay commands.'):
            raise RoomError('native_recovery_unavailable')
        job_id = db.execute("SELECT id FROM jobs WHERE room_id=? AND provider=? AND status='queued' "
                            "ORDER BY rowid DESC LIMIT 1", (room_id, source['provider'])).fetchone()[0]
        inspections.append(job_id)
        store._event(db, room_id, 'native_nonwork_inspection_queued', sender='system', job_id=job_id,
                     source_job_id=source['job_id'], observation=observation)
        # Both legacy peers were queued before the old host froze the group.
        if len(inspections) == 2:
            store._event(db, room_id, 'native_partial_inspection_group', sender='system',
                         run_id=team.team(db, room_id)['current_run_id'], inspection_job_ids=list(inspections),
                         canonical_imported=False)
        return job_id

    def claim_inspection(room_id, provider):
        job = claim(room_id, provider)
        if job:
            queued = next((event for event in reversed(store.snapshot(room_id)['events'])
                           if event['kind'] == 'native_nonwork_inspection_queued'
                           and event.get('job_id') == job['id']), None)
            if queued:
                job['native_nonwork_inspection'] = queued['observation']
        return job

    def inspection_protocol(job, context):
        text = protocol(job, context)
        if job.get('native_nonwork_inspection'):
            context['native_nonwork_inspection'] = job['native_nonwork_inspection']
            return 'Retired host read-only inspection. Do not edit or replay prior work.\n' + text
        return text

    def inspection_permission(job):
        return 'read_only' if job.get('native_nonwork_inspection') else permission(job)

    def finish_inspection(job, result):
        if not job.get('native_nonwork_inspection') or result.get('status') != 'completed':
            return finish(job, result)
        parsed = json.loads(result['text'])
        if any(parsed.get(key) for key in ('commands', 'files', 'edits', 'resolved_conflicts')):
            raise RoomError('native_recovery_binding_changed')
        service._collect_native_changes(job, parsed)
        # A completed legacy inspection records its real native result and
        # identity, but deliberately does not issue ordinary coordinate commands.
        with patch.object(team, 'finish', return_value=None), patch.object(team, 'reconcile', return_value=None):
            store.finish_job(job['id'], session_id=result.get('session_id'), message=parsed['message'],
                             team_commands=[])
        with store._connection(write=True) as db:
            store._event(db, job['room_id'], 'native_partial_inspected', sender=job['provider'],
                         job_id=job['id'], task_id=None, observation=job['native_nonwork_inspection'],
                         outcome='complete', canonical_imported=False, implementation_completed=False)

    with ExitStack() as stack:
        for obj, name, replacement in (
            (team, 'coordinate_native_nonwork_after_recovery', queue),
            (room_response_schema, 'response_schema', inspection_schema),
            (store, 'claim', claim_inspection), (service, '_native_protocol', inspection_protocol),
            (service, '_native_permission', inspection_permission), (service, '_finish', finish_inspection),
        ):
            stack.enter_context(patch.object(obj, name, replacement))
        yield
