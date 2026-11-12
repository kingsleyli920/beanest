"""Workbench integration with real private SQLite/files and fake model adapters.

Only explicitly selected profiles participate. Project inputs remain text data;
none of these tests launches a provider, project command, or execution runner.
"""
import asyncio
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.agent_registry import AgentRegistry, default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.project_catalog import ProjectCatalogError
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError
from wait_support import poll_async, within


class NoRunner:
    async def ready(self):
        return {'available': False, 'reason': 'test_not_configured'}

    async def run(self, *args, **kwargs):
        raise AssertionError('Imported projects must not execute')


def reply(context, *, message='A direct response.', files=(), action=None):
    value = {'message': message, 'state': 'done', 'actions': [action] if action else []}
    if 'project' in context:
        value.update(base_revision=context['proposal_base_revision'], files=list(files))
    return value


class FakeProfiles:
    def __init__(self):
        self.calls = []
        self.gates = {}
        self.identities = {}
        self.handler = lambda actor, context: reply(context)

    async def __call__(self, adapter, prompt, cwd, session_id=None, timeout=120):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        actor = context['self']
        self.calls.append({'actor': actor, 'adapter': adapter, 'context': context,
                           'cwd': cwd, 'session_id': session_id, 'prompt': prompt})
        if actor in self.gates:
            await self.gates[actor].wait()
        value = self.handler(actor, context)
        if inspect.isawaitable(value):
            value = await value
        return {'session_id': self.identities.get(actor) or session_id or str(uuid5(NAMESPACE_URL, str(cwd))),
                'status': 'completed', 'events': [],
                'usage': {'input_tokens': len(actor), 'output_tokens': 7},
                'text': value if isinstance(value, str) else json.dumps(value)}


class WorkbenchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.fake = FakeProfiles()
        self.manager = PartnerManager(self.root / 'partners', default_registry(self.fake), clock=lambda: 1000.0)
        self.manager.add(id='clover', name='Clover', adapter='claude', avatar='L')
        self.manager.add(id='sprout', name='Sprout', adapter='codex', avatar='S')
        self.service = RoomService(self.root / 'rooms.sqlite3', self.root / 'workspaces',
                                   registry=self.manager.registry(), partners=self.manager,
                                   runner=NoRunner(), clock=lambda: 1000.0)

    async def asyncTearDown(self):
        for event in self.fake.gates.values():
            event.set()
        try:
            await self.service.shutdown()
            if self.service._cleanup_task:
                await self.service._cleanup_task
        finally:
            self.directory.cleanup()

    async def settle(self):
        async def drain():
            while True:
                tasks = [task for task in self.service._workers.values() if not task.done()]
                if not tasks:
                    return
                await asyncio.gather(*tasks)
        await within(drain(), 3)

    async def wait_for_calls(self, count):
        await poll_async(lambda: len(self.fake.calls) >= count, timeout=2, interval=0.001)

    async def fixture_project(self, *, name='Fictional project', content=None):
        source = self.root / ('source-' + str(len(list(self.root.glob('source-*')))))
        source.mkdir()
        # Execution would immediately fail; import/review must only read bytes.
        (source / 'main.py').write_text(content or "raise AssertionError('source must never execute')\nVALUE = 1\n")
        (source / 'README.md').write_text('A fictional isolated fixture.\n')
        registered = await self.service.register_project(name, str(source), ['main.py', 'README.md'])
        return source, registered['project']

    async def test_four_registered_two_selected_all_only_starts_selected_sessions(self):
        room = await self.service.create_room('Say hello directly.', 'all', 'selected-two',
                                              participants=['clover', 'sprout'])
        room_id = room['room']['id']
        await self.settle()
        saved = await self.service.read_room(room_id)
        self.assertEqual({member['id'] for member in saved['members']}, {'clover', 'sprout'})
        self.assertEqual({call['actor'] for call in self.fake.calls}, {'clover', 'sprout'})
        self.assertEqual({(call['actor'], call['adapter']) for call in self.fake.calls},
                         {('clover', 'claude'), ('sprout', 'codex')})
        self.assertEqual(len(self.fake.calls), 2)
        for call in self.fake.calls:
            self.assertEqual({member['id'] for member in call['context']['members']}, {'clover', 'sprout'})
            self.assertEqual(call['cwd'], self.root / 'workspaces' / room_id / call['actor'])
        self.assertFalse((self.root / 'workspaces' / room_id / 'claude').exists())
        self.assertFalse((self.root / 'workspaces' / room_id / 'codex').exists())
        profiles = {item['id']: item for item in (await self.service.list_partners())['partners']}
        for actor in ('claude', 'codex'):
            self.assertEqual(profiles[actor]['monitor']['session_count'], 0)
            self.assertEqual(profiles[actor]['monitor']['status'], 'idle')
            self.assertIsNone(profiles[actor]['monitor']['latest_usage'])
        for actor in ('clover', 'sprout'):
            self.assertEqual(profiles[actor]['monitor']['session_count'], 1)
            self.assertEqual(profiles[actor]['monitor']['latest_usage']['values']['input_tokens'], len(actor))

    async def test_large_directory_requires_explicit_selection_before_dispatch(self):
        for number in range(30):
            self.manager.add(id=f'extra_{number}', name=f'Extra {number}', adapter='codex')
        with self.assertRaises(RoomError) as error:
            await self.service.create_room('No implicit large group.', 'claude', 'too-many-defaults')
        self.assertEqual(error.exception.code, 'invalid_participants')
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((await self.service.list_rooms())['rooms'], [])

        room = await self.service.create_room('Only these two.', 'clover', 'explicit-small-group',
                                              participants=['clover', 'sprout'])
        await self.settle()
        self.assertEqual({member['id'] for member in room['members']}, {'clover', 'sprout'})
        self.assertEqual([call['actor'] for call in self.fake.calls], ['clover'])

    async def test_unselected_user_recipient_and_peer_action_never_dispatch(self):
        self.fake.handler = lambda actor, context: reply(context, action={
            'type': 'request', 'to': 'codex', 'kind': 'help', 'text': 'Must not reach an unselected profile.'})
        room = await self.service.create_room('A bounded goal.', 'clover', 'peer-not-selected',
                                              participants=['clover', 'sprout'])
        await self.settle()
        saved = await self.service.read_room(room['room']['id'])
        self.assertEqual(len(saved['jobs']), 1)
        self.assertEqual(saved['jobs'][0]['error'], 'invalid_agent_action')
        self.assertEqual([call['actor'] for call in self.fake.calls], ['clover'])
        with self.assertRaises(RoomError) as error:
            await self.service.send_message(room['room']['id'], 'No outsider.', 'codex', 'outsider-message')
        self.assertEqual(error.exception.code, 'invalid_target')
        with self.assertRaises(RoomError):
            await self.service.create_room('No outsider.', 'codex', 'outsider-target',
                                           participants=['clover', 'sprout'])
        self.assertEqual(len((await self.service.list_rooms())['rooms']), 1)

    async def test_selection_is_idempotent_as_a_set_and_changed_members_conflict(self):
        first = await self.service.create_room('A direct goal.', 'all', 'same-members',
                                               participants=['clover', 'sprout'])
        await self.settle()
        repeated = await self.service.create_room('A direct goal.', 'all', 'same-members',
                                                  participants=['sprout', 'clover'])
        self.assertEqual(first['room']['id'], repeated['room']['id'])
        with self.assertRaises(RoomError) as error:
            await self.service.create_room('A direct goal.', 'all', 'same-members',
                                           participants=['claude', 'codex'])
        self.assertEqual(error.exception.code, 'request_conflict')
        self.assertEqual(len(self.fake.calls), 2)
        for members in ([], ['clover', 'clover'], ['missing'], ['clover'] * 9, 'clover'):
            with self.assertRaises(RoomError):
                await self.service.create_room('Invalid selection.', 'all', 'invalid-' + repr(members),
                                               participants=members)
        self.assertEqual(len((await self.service.list_rooms())['rooms']), 1)

    async def test_project_snapshot_peer_review_diff_and_owners_leave_sources_untouched(self):
        source, registered = await self.fixture_project()
        before = {path.name: (path.read_bytes(), path.stat().st_mode) for path in source.iterdir()}
        def collaborate(actor, context):
            if context['job']['kind'] == 'task':
                item = next(item for item in context['project']['files'] if item['path'] == 'main.py')
                return reply(context, files=[{'path': 'main.py', 'previous_sha256': item['sha256'],
                                               'content': item['content'].replace('VALUE = 1', 'VALUE = 2')}],
                             action={'type': 'request', 'to': 'sprout', 'kind': 'review', 'text': 'Review the exact draft.'})
            if context['job']['kind'] == 'review':
                return reply(context, files=[{'path': 'REVIEW.md', 'previous_sha256': None,
                                               'content': 'Reviewed statically. No execution performed.\n'}])
            return reply(context, message='Accepted the peer review.')
        self.fake.handler = collaborate
        created = await self.service.create_room('Update VALUE and review the change.', 'clover', 'project-roundtrip',
                                                 mode='project_files', project_id=registered['id'],
                                                 participants=['clover', 'sprout'])
        await self.settle()
        saved = await self.service.read_room(created['room']['id'])
        self.assertEqual(saved['room']['status'], 'completed')
        self.assertEqual(saved['project']['revision'], 3)
        self.assertEqual(saved['project']['execution'], 'disabled')
        self.assertEqual(saved['executions'], [])
        files = {item['path']: item for item in saved['project']['files']}
        self.assertEqual(files['main.py']['owner'], 'clover')
        self.assertEqual(files['README.md']['owner'], 'clover')
        self.assertEqual(files['REVIEW.md']['owner'], 'sprout')
        self.assertIn('VALUE = 2', files['main.py']['content'])
        diffs = {item['path']: item for item in saved['project']['changes']}
        self.assertEqual(set(diffs), {'main.py', 'REVIEW.md'})
        self.assertIn('-VALUE = 1', diffs['main.py']['diff'])
        self.assertIn('+VALUE = 2', diffs['main.py']['diff'])
        self.assertEqual(diffs['main.py']['previous_sha256'], hashlib.sha256(before['main.py'][0]).hexdigest())
        self.assertEqual([call['context']['project']['revision'] for call in self.fake.calls], [1, 2, 3])
        self.assertEqual([call['context']['job']['kind'] for call in self.fake.calls], ['task', 'review', 'feedback'])
        for call in self.fake.calls:
            self.assertNotIn('path', call['context']['project']['source'])
            self.assertNotIn('changes', call['context']['project'])
            self.assertNotEqual(call['cwd'], source)
            self.assertIn('The source directory is NEVER modified.', call['prompt'])
            self.assertIn('There is NO execution runner', call['prompt'])
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mode) for path in source.iterdir()}, before)
        self.assertFalse((source / 'REVIEW.md').exists())

    async def test_project_replay_does_not_recapture_or_dispatch_and_changed_project_conflicts(self):
        source, project = await self.fixture_project()
        _, other = await self.fixture_project(name='Other fictional project')
        room = await self.service.create_room('Review imported text.', 'clover', 'same-project-request',
                                              mode='project_files', project_id=project['id'],
                                              participants=['clover', 'sprout'])
        await self.settle()
        baseline = await self.service.read_room(room['room']['id'])
        (source / 'main.py').write_text('raise AssertionError("changed after capture")\n')
        with patch.object(self.service.catalog, 'capture', side_effect=AssertionError('Replay must not recapture')):
            repeated = await self.service.create_room('Review imported text.', 'clover', 'same-project-request',
                                                      mode='project_files', project_id=project['id'],
                                                      participants=['sprout', 'clover'])
            self.assertEqual(repeated['project'], baseline['project'])
            for updates in ({'project_id': other['id']}, {'participants': ['claude', 'codex']}):
                args = {'project_id': project['id'], 'participants': ['clover', 'sprout'], **updates}
                with self.assertRaises(RoomError) as error:
                    await self.service.create_room('Review imported text.', 'clover', 'same-project-request',
                                                   mode='project_files', **args)
                self.assertEqual(error.exception.code, 'request_conflict')
        self.assertEqual(len(self.fake.calls), 1)

    async def test_project_initialization_failure_is_recorded_and_replay_does_not_retry_source(self):
        source, project = await self.fixture_project()
        (source / 'main.py').write_bytes(b'\xff invalid utf-8')
        with self.assertRaises(RoomError) as error:
            await self.service.create_room('Review imported text.', 'clover', 'bad-source-request',
                                           mode='project_files', project_id=project['id'],
                                           participants=['clover', 'sprout'])
        self.assertEqual(error.exception.code, 'project_initialization_unconfirmed')
        with patch.object(self.service.catalog, 'capture', side_effect=AssertionError('Never retry implicitly')):
            replay = await self.service.create_room('Review imported text.', 'clover', 'bad-source-request',
                                                    mode='project_files', project_id=project['id'],
                                                    participants=['clover', 'sprout'])
        self.assertEqual(replay['room']['status'], 'failed')
        self.assertEqual(replay['jobs'][0]['status'], 'cancelled')
        self.assertEqual(len(self.fake.calls), 0)

    async def test_project_registration_rejects_bytes_limit_and_source_symlink_without_models(self):
        source, _ = await self.fixture_project()
        (source / 'huge.md').write_text('界' * 6000)
        with self.assertRaises(ProjectCatalogError) as error:
            await self.service.register_project('Too many bytes', str(source), ['huge.md'])
        self.assertEqual(error.exception.code, 'project_limit_exceeded')
        (source / 'link.md').symlink_to(source / 'README.md')
        with self.assertRaises(ProjectCatalogError) as error:
            await self.service.register_project('No links', str(source), ['link.md'])
        self.assertEqual(error.exception.code, 'project_unsafe_path')
        self.assertEqual(len(self.fake.calls), 0)
        self.assertEqual(len((await self.service.list_projects())['projects']), 1)

    async def test_project_response_strict_json_rejects_duplicate_or_extra_keys_before_writes(self):
        source, project = await self.fixture_project()
        before = (source / 'main.py').read_bytes()
        for number, malformed in enumerate(('duplicate', 'command')):
            def invalid(actor, context):
                raw = json.dumps(reply(context, files=[{
                    'path': 'unsafe.md', 'previous_sha256': None, 'content': 'Must not be committed.'}]))
                suffix = ', "base_revision": 1}' if malformed == 'duplicate' else ', "command": "run-project"}'
                return raw[:-1] + suffix
            self.fake.handler = invalid
            created = await self.service.create_room('Reject malformed proposal.', 'clover', 'strict-json-' + str(number),
                                                     mode='project_files', project_id=project['id'],
                                                     participants=['clover', 'sprout'])
            await self.settle()
            saved = await self.service.read_room(created['room']['id'])
            self.assertEqual(saved['jobs'][0]['error'], 'invalid_agent_response')
            self.assertEqual(saved['project']['revision'], 1)
            self.assertEqual(saved['project']['changes'], [])
            self.assertNotIn('Must not be committed', json.dumps(saved['events']))
        self.assertEqual((source / 'main.py').read_bytes(), before)
        self.assertFalse((source / 'unsafe.md').exists())

    async def test_monitor_separates_active_queue_sessions_and_usage_by_profile(self):
        gate = self.fake.gates['clover'] = asyncio.Event()
        room = await self.service.create_room('First direct request.', 'clover', 'monitor-first',
                                              participants=['clover', 'sprout'])
        await self.wait_for_calls(1)
        await self.service.send_message(room['room']['id'], 'Second direct request.', 'clover', 'monitor-second')
        monitored = {item['id']: item['monitor'] for item in (await self.service.list_partners())['partners']}
        self.assertEqual(monitored['clover']['status'], 'working')
        self.assertEqual([task['status'] for task in monitored['clover']['current_tasks']], ['working', 'queued'])
        self.assertEqual({task['room_id'] for task in monitored['clover']['current_tasks']}, {room['room']['id']})
        self.assertEqual(monitored['clover']['session_count'], 0)
        self.assertIsNone(monitored['clover']['latest_usage'])
        for actor in ('claude', 'codex', 'sprout'):
            self.assertEqual(monitored[actor]['current_tasks'], [])
            self.assertEqual(monitored[actor]['status'], 'idle')
        gate.set()
        await self.settle()
        monitored = {item['id']: item['monitor'] for item in (await self.service.list_partners())['partners']}
        self.assertEqual(monitored['clover']['status'], 'idle')
        self.assertEqual(monitored['clover']['session_count'], 1)
        self.assertEqual(monitored['clover']['completed_jobs'], 2)
        self.assertEqual(monitored['clover']['latest_usage']['source'], 'claude_cli')
        self.assertEqual(monitored['clover']['latest_usage']['values']['input_tokens'], 6)
        self.assertEqual(monitored['clover']['latest_activity'], 1000.0)
        self.assertIsNone(monitored['claude']['latest_usage'])

    async def test_add_and_get_partner_are_no_inference_and_status_does_not_health_check(self):
        with patch('local_agent_coordinator.adapters._run', side_effect=AssertionError('GET must not inspect CLIs')):
            result = await self.service.add_partner(id='bean', name='Bean', adapter='claude', avatar='B')
            added = next(item for item in result['partners'] if item['id'] == 'bean')
            self.assertEqual(added['health']['state'], 'unknown')
            self.assertEqual(added['monitor']['session_count'], 0)
            self.assertTrue(added['enabled'])
            self.assertEqual(len((await self.service.list_partners())['partners']), 5)
            self.assertEqual((await self.service.status())['providers'], ['claude', 'codex', 'clover', 'sprout', 'bean'])
            await self.service.list_projects()
            await self.service.list_rooms()
        self.assertEqual(self.fake.calls, [])

    async def test_only_default_production_service_performs_one_installation_discovery(self):
        runtime = self.root / 'production'
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', return_value='/virtual/claude') as claude, \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', side_effect=RuntimeError('missing')) as codex, \
             patch('local_agent_coordinator.adapters._run', side_effect=AssertionError('discovery must not execute CLI')):
            production = RoomService(runtime / 'rooms.sqlite3', runtime / 'workspaces',
                                     runner=NoRunner(), clock=lambda: 1000.0)
            try:
                self.assertEqual((await production.status())['providers'], ['claude'])
                directory = await production.list_partners()
                self.assertEqual(directory['discovery']['state'], 'completed')
                self.assertEqual([item['id'] for item in directory['partners']], ['claude', 'codex'])
                self.assertEqual([item['installation']['state'] for item in directory['partners']], ['found', 'missing'])
                self.assertTrue(all(item['health']['state'] == 'unknown' for item in directory['partners']))
                await production.status(); await production.list_partners(); await production.list_rooms()
                self.assertEqual((claude.call_count, codex.call_count), (1, 1))
                self.assertFalse((runtime / 'partners' / 'profiles.json').exists())
                self.assertEqual((await production.list_rooms())['rooms'], [])
            finally:
                await production.shutdown()
        self.assertEqual(self.fake.calls, [])

    async def test_injected_manager_and_registry_do_not_implicitly_discover_local_installations(self):
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', side_effect=AssertionError('no discovery')), \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', side_effect=AssertionError('no discovery')):
            directory = await self.service.list_partners()
            self.assertEqual(directory['discovery']['state'], 'unavailable')
            self.assertTrue(all(item['installation']['state'] == 'unknown' for item in directory['partners']))
            explicit = RoomService(self.root / 'explicit' / 'rooms.sqlite3', self.root / 'explicit' / 'workspaces',
                                   registry=self.manager.registry(), runner=NoRunner())
            try:
                directory = await explicit.list_partners()
                self.assertEqual(directory['discovery']['state'], 'unavailable')
                self.assertTrue(all(item['installation']['state'] == 'unknown' for item in directory['partners']))
                self.assertEqual((await explicit.status())['providers'], ['claude', 'codex', 'clover', 'sprout'])
            finally:
                await explicit.shutdown()
        self.assertEqual(self.fake.calls, [])

    async def test_startup_missing_installation_filters_all_new_goal_paths_but_keeps_history(self):
        room = await self.service.create_room('A historical direct task.', 'clover', 'before-installation-discovery',
                                              participants=['clover', 'sprout'])
        await self.settle()
        before = await self.service.read_room(room['room']['id'])
        self.manager.set_enabled('codex', False)
        config = self.root / 'partners' / 'profiles.json'
        config_before = config.read_bytes()
        await self.service.shutdown()
        with patch('local_agent_coordinator.partner_manager.adapters.find_claude', side_effect=RuntimeError('missing')) as claude, \
             patch('local_agent_coordinator.partner_manager.adapters.find_codex', return_value='/virtual/codex') as codex, \
             patch('local_agent_coordinator.adapters._run', side_effect=AssertionError('no CLI execution')):
            manager = PartnerManager(self.root / 'partners', default_registry(self.fake), startup_discovery=True)
            self.service = RoomService(self.root / 'rooms.sqlite3', self.root / 'workspaces',
                                       registry=manager.registry(), partners=manager, runner=NoRunner(),
                                       clock=lambda: 1000.0)
            restored = await self.service.read_room(room['room']['id'])
            expected_members = [dict(member, configuration_dispatchable=member['id'] == 'sprout',
                configuration_dispatch_reason=None if member['id'] == 'sprout' else 'partner_unavailable')
                for member in before['members']]
            self.assertEqual(restored['members'], expected_members)
            self.assertEqual(restored['events'], before['events'])
            self.assertEqual((await self.service.status())['providers'], ['sprout'])
            for target, selected in (('clover', ['clover', 'sprout']), ('all', ['clover', 'sprout']),
                                     ('codex', ['codex']), ('claude', None)):
                with self.assertRaises(RoomError):
                    await self.service.create_room('Unavailable member must not start.', target,
                                                   'blocked-' + target + str(selected), participants=selected)
            fresh = await self.service.create_room('Only installed enabled profiles.', 'all', 'installed-all')
            await self.settle()
            self.assertEqual([item['id'] for item in fresh['members']], ['sprout'])
            self.assertEqual([call['actor'] for call in self.fake.calls], ['clover', 'sprout'])
            self.assertEqual([call['adapter'] for call in self.fake.calls], ['claude', 'codex'])
            await self.service.list_partners(); await self.service.status()
            self.assertEqual((claude.call_count, codex.call_count), (1, 1))
            self.assertEqual(config.read_bytes(), config_before)
            self.assertFalse(manager.get('codex')['enabled'])
            self.assertEqual(len(manager.profiles()), 4)
            # Repeating the historical request reads its original frozen members
            # even though one participant is now missing from the new-goal list.
            repeated = await self.service.create_room('A historical direct task.', 'clover',
                                                      'before-installation-discovery', participants=['clover', 'sprout'])
            self.assertEqual(repeated['room']['id'], room['room']['id'])

    async def test_duplicate_profile_conflict_exposes_stable_api_error_code(self):
        with self.assertRaises(PartnerError) as error:
            await self.service.add_partner(id='clover', name='Changed', adapter='claude', avatar='L')
        self.assertEqual(error.exception.code, 'partner_identity_conflict')
        self.assertEqual(str(error.exception), error.exception.code)
        self.assertEqual(self.manager.get('clover')['name'], 'Clover')
        self.assertEqual(len(self.manager.profiles()), 4)
        self.assertEqual(self.fake.calls, [])

    async def test_disable_blocks_existing_and_new_calls_until_explicit_enable(self):
        original = await self.service.create_room('Say hello directly.', 'clover', 'before-disable',
                                                  participants=['clover', 'sprout'])
        await self.settle()
        room_id = original['room']['id']
        prior = await self.service.read_room(room_id)
        old_session = next(item['session_id'] for item in prior['members'] if item['id'] == 'clover')
        await self.service.set_partner_enabled('clover', False)
        with self.assertRaises(RoomError) as error:
            await self.service.create_room('A new goal.', 'clover', 'disabled-new',
                                           participants=['clover', 'sprout'])
        self.assertEqual(error.exception.code, 'invalid_participants')
        with self.assertRaisesRegex(RoomError, 'partner_disabled'):
            await self.service.send_message(room_id, 'Continue this existing room.', 'clover', 'disabled-continue')
        saved = await self.service.read_room(room_id)
        self.assertEqual(saved['jobs'], prior['jobs'])
        self.assertEqual({item['id'] for item in saved['members']}, {'clover', 'sprout'})
        self.assertEqual(next(item['session_id'] for item in saved['members'] if item['id'] == 'clover'), old_session)
        self.assertEqual(len(self.fake.calls), 1)
        await self.service.set_partner_enabled('clover', True)
        enabled = await self.service.read_room(room_id)
        self.assertIsNone(next(item['session_id'] for item in enabled['members'] if item['id'] == 'clover'))
        await self.service.send_message(room_id, 'Continue this existing room.', 'clover', 'enabled-continue')
        await self.settle()
        self.assertIsNone(self.fake.calls[-1]['session_id'])
        replay = await self.service.create_room('Say hello directly.', 'clover', 'before-disable',
                                                participants=['clover', 'sprout'])
        self.assertEqual(replay['room']['id'], room_id)
        self.assertEqual(len(self.fake.calls), 2)

    async def test_same_adapter_profiles_cannot_adopt_the_same_native_session(self):
        for separate_room in (False, True):
            with self.subTest(separate_room=separate_room):
                identity = str(uuid5(NAMESPACE_URL, 'collision-case-' + str(separate_room)))
                self.fake.identities = {'claude': identity, 'clover': identity}
                first = await self.service.create_room('First owner of a native session.', 'claude',
                                                       'first-session-' + str(separate_room),
                                                       participants=['claude', 'clover'])
                await self.settle()
                original = await self.service.read_room(first['room']['id'])
                self.assertEqual(next(item['session_id'] for item in original['members']
                                      if item['id'] == 'claude'), identity)
                if separate_room:
                    collision = await self.service.create_room('A different profile must get a fresh session.', 'clover',
                                                               'second-session-' + str(separate_room),
                                                               participants=['clover'])
                    second_room_id = collision['room']['id']
                else:
                    second_room_id = first['room']['id']
                    await self.service.send_message(second_room_id, 'A different profile needs its own session.',
                                                    'clover', 'same-room-collision')
                await self.settle()
                rejected = await self.service.read_room(second_room_id)
                member = next(item for item in rejected['members'] if item['id'] == 'clover')
                self.assertIsNone(member['session_id'])
                self.assertEqual(member['adapter'], 'claude')
                self.assertTrue(member['identity_uncertain'])
                self.assertEqual(member['status'], 'unknown')
                self.assertEqual(rejected['jobs'][-1]['error'], 'session_identity_changed')
                preserved = await self.service.read_room(first['room']['id'])
                self.assertEqual(next(item['session_id'] for item in preserved['members']
                                      if item['id'] == 'claude'), identity)
                count = len(self.fake.calls)
                with self.assertRaises(RoomError) as error:
                    await self.service.send_message(second_room_id, 'Do not resume an ambiguous session.', 'clover',
                                                    'unsafe-retry-' + str(separate_room))
                self.assertEqual(error.exception.code, 'room_not_active')
                self.assertEqual(len(self.fake.calls), count)

    async def test_changed_alias_adapter_cannot_resume_a_frozen_room_with_another_cli(self):
        original = await self.service.create_room('Establish a dedicated alias session.', 'clover', 'frozen-adapter',
                                                  participants=['clover', 'sprout'])
        await self.settle()
        before = await self.service.read_room(original['room']['id'])
        frozen = next(item for item in before['members'] if item['id'] == 'clover')
        self.assertEqual(frozen['adapter'], 'claude')
        old_session = frozen['session_id']
        prior = self.manager.registry()
        replacement = replace(prior.get('codex'), id='clover', name='Clover', avatar='L')
        changed = AgentRegistry(replacement if agent_id == 'clover' else prior.get(agent_id)
                                for agent_id in prior.ids())
        # Simulate a local configuration change without touching real profiles.
        # This also remains valid if service begins syncing on send_message.
        with patch.object(self.manager, 'registry', return_value=changed):
            self.service.registry = changed
            await self.service.send_message(original['room']['id'], 'Continue the historical session.', 'clover',
                                            'must-not-switch-cli')
            await self.settle()
        after = await self.service.read_room(original['room']['id'])
        member = next(item for item in after['members'] if item['id'] == 'clover')
        self.assertEqual(member['adapter'], 'claude')
        self.assertEqual(member['session_id'], old_session)
        self.assertEqual(after['jobs'][-1]['error'], 'agent_unavailable')
        self.assertEqual([(call['actor'], call['adapter']) for call in self.fake.calls], [('clover', 'claude')])

    async def test_bean_style_is_exposed_and_frozen_separately_from_current_profile(self):
        await self.service.add_partner(id='bean', name='Bean', adapter='claude', bean_style='lavender')
        status = await self.service.status()
        directory = await self.service.list_partners()
        self.assertEqual(status['bean_styles'], directory['bean_styles'])
        self.assertEqual(len(status['bean_styles']), 10)
        self.assertEqual(next(item for item in status['agents'] if item['id'] == 'bean')['bean_style'], 'lavender')
        room = await self.service.create_room('Keep a profile style in this room.', 'bean', 'freeze-style',
                                              participants=['bean'])
        await self.settle()
        before = await self.service.read_room(room['room']['id'])
        self.assertEqual(before['members'][0]['bean_style'], 'lavender')
        prior = self.manager.registry()
        changed = AgentRegistry(replace(prior.get(key), bean_style='cocoa') if key == 'bean' else prior.get(key)
                                for key in prior.ids())
        with patch.object(self.manager, 'registry', return_value=changed):
            current = await self.service.status()
            self.assertEqual(next(item for item in current['agents'] if item['id'] == 'bean')['bean_style'], 'cocoa')
            saved = await self.service.read_room(room['room']['id'])
        self.assertEqual(saved['members'][0], before['members'][0])
        self.assertEqual([(call['actor'], call['adapter']) for call in self.fake.calls], [('bean', 'claude')])

    async def test_legacy_member_style_migration_preserves_names_adapters_and_sessions(self):
        room = await self.service.create_room('A fictional legacy room.', 'all', 'legacy-style',
                                              participants=['claude', 'codex', 'clover'])
        await self.settle()
        before = await self.service.read_room(room['room']['id'])
        count = len(self.fake.calls)
        with self.service.store._connection(write=True) as database:
            database.execute('ALTER TABLE members DROP COLUMN bean_style')
        await self.service.shutdown()
        self.service = RoomService(self.root / 'rooms.sqlite3', self.root / 'workspaces',
                                   registry=self.manager.registry(), partners=self.manager,
                                   runner=NoRunner(), clock=lambda: 1000.0)
        migrated = await self.service.read_room(room['room']['id'])
        styles = {item['id']: item['bean_style'] for item in migrated['members']}
        self.assertEqual(styles, {'claude': 'peach', 'codex': 'sprout', 'clover': 'peach'})
        for item in migrated['members']:
            previous = next(member for member in before['members'] if member['id'] == item['id'])
            self.assertEqual({key: item[key] for key in item if key != 'bean_style'},
                             {key: previous[key] for key in previous if key != 'bean_style'})
        self.assertEqual(len(self.fake.calls), count)

    async def test_peer_cannot_overwrite_imported_author_file(self):
        _, project = await self.fixture_project()
        def overwrite(actor, context):
            item = next(item for item in context['project']['files'] if item['path'] == 'main.py')
            if context['job']['kind'] == 'task':
                return reply(context, action={'type': 'request', 'to': 'sprout', 'kind': 'review', 'text': 'Review only.'})
            return reply(context, files=[{'path': 'main.py', 'previous_sha256': item['sha256'], 'content': 'VALUE = 99\n'}])
        self.fake.handler = overwrite
        room = await self.service.create_room('Review imported code.', 'clover', 'ownership-boundary',
                                              mode='project_files', project_id=project['id'],
                                              participants=['clover', 'sprout'])
        await self.settle()
        saved = await self.service.read_room(room['room']['id'])
        self.assertEqual(saved['project']['revision'], 1)
        self.assertEqual(saved['project']['changes'], [])
        self.assertEqual(saved['jobs'][-1]['error'], 'project_owner_conflict')
        self.assertEqual(saved['project']['files'][1]['owner'], 'clover')
        self.assertNotIn('VALUE = 99', json.dumps(saved['project']))


if __name__ == '__main__':
    unittest.main()
