"""Native-format selection at the real service/storage boundary, without inference."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.agent_registry import AgentRegistry, default_registry
from local_agent_coordinator.room_service import RoomService


class StructuredRoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.services = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.shutdown()
        self.directory.cleanup()

    async def settle(self, service, room_id):
        async def drain():
            while any(not task.done() for task in service._workers.values()):
                await asyncio.gather(*list(service._workers.values()))
            return await service.read_room(room_id)
        return await asyncio.wait_for(drain(), 3)

    async def test_native_hook_receives_mode_schema_and_real_members(self):
        for mode in ('text', 'python_artifacts'):
            with self.subTest(mode=mode):
                received = []

                async def legacy(*_args, **_kwargs):
                    raise AssertionError('Native transport should be selected')

                async def structured(prompt, cwd, *, session_id, timeout, response_schema):
                    context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
                    received.append((response_schema, context))
                    value = {'message': '已提交草稿，尚未运行。', 'state': 'done', 'actions': []}
                    if mode == 'python_artifacts':
                        value.update(base_revision=context['proposal_base_revision'], files=[{
                            'path': 'main.py', 'previous_sha256': None, 'content': 'VALUE = 1\n'}])
                    return {'session_id': session_id or str(uuid5(NAMESPACE_URL, str(cwd))),
                            'status': 'completed', 'events': [], 'text': json.dumps(value)}

                defaults = default_registry(legacy)
                registry = AgentRegistry(replace(defaults.get(key), invoke_structured=structured)
                                         for key in defaults.ids())
                service = RoomService(self.root / mode / 'rooms.sqlite3',
                                      self.root / mode / 'workspaces', registry=registry)
                self.services.append(service)
                room = await service.create_room('一次直接草稿，无须互审。', 'claude', mode, mode=mode)
                final = await self.settle(service, room['room']['id'])
                self.assertEqual(final['room']['status'], 'completed')
                self.assertEqual(len(received), 1)
                schema, context = received[0]
                self.assertEqual(context['self'], 'claude')
                self.assertEqual(schema['properties']['actions']['items']['properties']['to']['enum'], ['codex'])
                self.assertEqual(set(schema['required']), {'message', 'actions', 'state'} |
                                 ({'base_revision', 'files'} if mode != 'text' else set()))
                if mode != 'text':
                    self.assertEqual([(file['path'], file['owner']) for file in final['project']['files']],
                                     [('main.py', 'claude')])

    async def test_native_format_does_not_relax_ambiguous_json_or_publish_artifacts(self):
        async def invalid(prompt, cwd, **_kwargs):
            value = {'message': '草稿', 'state': 'done', 'actions': [], 'base_revision': 0,
                     'files': [{'path': 'main.py', 'previous_sha256': None, 'content': 'VALUE = 1\n'}]}
            return {'session_id': str(uuid5(NAMESPACE_URL, str(cwd))), 'status': 'completed',
                    'events': [], 'text': '说明在前\n```json\n' + json.dumps(value) + '\n```\n{}'}

        defaults = default_registry(invalid)
        registry = AgentRegistry(replace(defaults.get(key), invoke_structured=invalid)
                                 for key in defaults.ids())
        service = RoomService(self.root / 'invalid' / 'rooms.sqlite3',
                              self.root / 'invalid' / 'workspaces', registry=registry)
        self.services.append(service)
        room = await service.create_room('只验证格式边界。', 'claude', 'invalid', mode='python_artifacts')
        final = await self.settle(service, room['room']['id'])
        self.assertEqual(final['room']['status'], 'failed')
        self.assertEqual(final['jobs'][0]['error'], 'invalid_agent_response')
        self.assertEqual(final['project']['revision'], 0)
        self.assertEqual(final['project']['files'], [])
        self.assertEqual(len(final['jobs']), 1)
