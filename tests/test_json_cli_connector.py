"""Real local subprocess boundary for the opt-in connector protocol; no models."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError


async def unused(*args, **kwargs):
    raise AssertionError('No provider CLI may be invoked')


class JsonConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.program = self.root / 'fixture-connector'
        self.connection = {'protocol': 'beanest-json-v1', 'executable': str(self.program),
                           'args': [], 'billing': 'local'}
        self.write_program()

    def write_program(self, *, check_override=None, result_override=None, sleep=False):
        check = {'protocol': 'beanest-json-v1', 'kind': 'check', 'ready': True,
                 'version': '1.2.3', 'billing': 'local',
                 'capabilities': {'text_only': True, 'tools': False}}
        check.update(check_override or {})
        result = {'protocol': 'beanest-json-v1', 'kind': 'result', 'status': 'completed',
                  'session_id': 'fixture-session:1', 'text': 'Fixture reply',
                  'usage': {'input_tokens': 5, 'output_tokens': 7, 'cost': 99}, 'tools_used': []}
        result.update(result_override or {})
        self.program.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time
value = json.load(sys.stdin)
assert not any(key in os.environ for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'DEEPSEEK_API_KEY'))
with pathlib.Path('requests.jsonl').open('a') as stream:
    stream.write(json.dumps({{'request': value, 'argv': sys.argv[1:]}}) + '\\n')
if value['kind'] == 'check':
    print({json.dumps(check)!r})
else:
    pathlib.Path('connector.pid').write_text(str(os.getpid()))
    if {sleep!r}:
        time.sleep(60)
    print('stderr private detail', file=sys.stderr)
    print({json.dumps(result)!r})
''')
        self.program.chmod(0o700)

    def requests(self, cwd=None):
        return [json.loads(line) for line in ((cwd or self.root) / 'requests.jsonl').read_text().splitlines()]

    async def test_real_handshake_then_invoke_literal_argv_and_filtered_environment(self):
        marker = self.root / 'must-not-exist'
        connection = {**self.connection, 'args': ['--literal', '$(touch ' + str(marker) + ')']}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'SECRET', 'ANTHROPIC_API_KEY': 'SECRET',
                                     'DEEPSEEK_API_KEY': 'SECRET'}):
            result = await adapters.invoke_json_cli(connection, 'A bounded prompt', self.root,
                                                    response_schema={'type': 'object'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['session_id'], 'fixture-session:1')
        self.assertEqual(result['usage'], {'input_tokens': 5, 'output_tokens': 7})
        self.assertNotIn('stderr', json.dumps(result))
        requests = self.requests()
        self.assertEqual([row['request']['kind'] for row in requests], ['check', 'invoke'])
        self.assertEqual(requests[0]['request'], {'protocol': 'beanest-json-v1', 'kind': 'check'})
        self.assertEqual(requests[1]['argv'], connection['args'])
        self.assertEqual(requests[1]['request']['capabilities'], {'text_only': True, 'tools': False})
        self.assertEqual(requests[1]['request']['response_schema'], {'type': 'object'})
        self.assertFalse(marker.exists())

    async def test_manager_requires_real_check_and_never_certifies_provider_subscription(self):
        manager = PartnerManager(self.root / 'partners', default_registry(unused))
        manager.add(id='helper', name='Fixture Helper', adapter='local_json', connection=self.connection)
        self.assertNotIn('helper', manager.available_ids())
        checked = await manager.check('helper')
        self.assertEqual(checked['health']['state'], 'ready')
        self.assertEqual(checked['health']['auth'], {'method': 'local', 'subscription_confirmed': None})
        self.assertTrue(checked['selectable'])
        self.assertIn('helper', manager.available_ids())
        self.assertEqual([row['request']['kind'] for row in self.requests(manager.root)], ['check'])
        self.assertFalse((manager.root / 'connector.pid').exists())
        self.program.unlink()
        self.assertNotIn('helper', manager.available_ids())
        missing = await manager.check('helper')
        self.assertEqual(missing['health']['reason'], 'cli_missing')

    async def test_unconfigured_template_check_never_launches_a_process(self):
        manager = PartnerManager(self.root / 'partners', default_registry(unused))
        # Kimi now has a native adapter; use a genuine unconfigured connector
        # template to verify that a template alone never launches a process.
        manager.add(id='helper', name='Fixture DeepSeek', adapter='deepseek')
        with patch.object(adapters, '_run', side_effect=AssertionError):
            checked = await manager.check('helper')
        self.assertEqual(checked['health']['state'], 'unavailable')
        self.assertEqual(checked['health']['reason'], 'connection_not_configured')
        self.assertFalse(checked['selectable'])

    async def test_selected_configured_connector_completes_through_real_room_boundary(self):
        self.write_program(result_override={
            'text': json.dumps({'message': 'A fixture response, no model.', 'state': 'done', 'actions': []})})
        manager = PartnerManager(self.root / 'partners', default_registry(unused))
        manager.add(id='helper', name='Fixture Helper', adapter='local_json', connection=self.connection)
        service = RoomService(self.root / 'rooms.sqlite3', self.root / 'workspaces',
                              partners=manager, registry=manager.registry())
        try:
            with self.assertRaises(RoomError):
                await service.create_room('Do not dispatch before checking.', 'helper', 'not-ready',
                                          participants=['helper'])
            self.assertFalse((self.root / 'requests.jsonl').exists())
            await manager.check('helper')
            created = await service.create_room('Reply as fixture.', 'helper', 'ready', participants=['helper'])
            while any(not task.done() for task in service._workers.values()):
                await asyncio.wait_for(asyncio.gather(*list(service._workers.values())), 3)
            final = await service.read_room(created['room']['id'])
            self.assertEqual(final['room']['status'], 'completed')
            self.assertEqual(final['members'][0]['adapter'], 'local_json')
            self.assertEqual(final['members'][0]['session_id'], 'fixture-session:1')
            self.assertEqual(final['jobs'][0]['status'], 'completed')
            self.assertIn('A fixture response, no model.', json.dumps(final))
            cwd = self.root / 'workspaces' / created['room']['id'] / 'helper'
            self.assertEqual([row['request']['kind'] for row in self.requests(cwd)], ['check', 'invoke'])
        finally:
            await service.shutdown()

    async def test_failed_or_untrusted_handshake_never_reaches_invoke(self):
        for change in ({'ready': False}, {'billing': 'paid_api'},
                       {'capabilities': {'text_only': True, 'tools': True}}, {'unknown': 'secret'}):
            self.write_program(check_override=change)
            result = await adapters.invoke_json_cli(self.connection, 'A prompt', self.root)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(self.requests()[-1]['request']['kind'], 'check')
        self.assertFalse((self.root / 'connector.pid').exists())

    async def test_subscription_declaration_does_not_become_subscription_confirmation(self):
        self.write_program(check_override={'billing': 'existing_subscription'})
        manager = PartnerManager(self.root / 'partners', default_registry(unused))
        manager.add(id='helper', name='Fixture Helper', adapter='kimi',
                    connection={**self.connection, 'billing': 'existing_subscription'})
        checked = await manager.check('helper')
        self.assertEqual(checked['health']['state'], 'ready')
        self.assertEqual(checked['health']['auth'], {'method': 'configured_cli', 'subscription_confirmed': None})
        self.assertIsNone(checked['health']['cost'])
        self.assertIsNone(checked['health']['subscription_remaining'])

    async def test_shell_symlink_is_not_launched(self):
        shell = self.root / 'shell-alias'
        shell.symlink_to('/bin/sh')
        with patch.object(adapters, '_run', side_effect=AssertionError):
            result = await adapters.invoke_json_cli({**self.connection, 'executable': str(shell)},
                                                    'A prompt', self.root)
        self.assertEqual(result['status'], 'failed')

    async def test_result_tools_identity_and_usage_shape_are_validated(self):
        for change in ({'tools_used': ['shell']}, {'session_id': 'changed'},
                       {'usage': []}, {'extra': 'secret'}):
            self.write_program(result_override=change)
            result = await adapters.invoke_json_cli(self.connection, 'A prompt', self.root,
                                                    session_id='fixture-session:1')
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['failure_code'], 'connector_protocol_failure')
            self.assertNotIn('secret', json.dumps(result))

    async def test_cancellation_waits_until_owned_connector_has_exited(self):
        self.write_program(sleep=True)
        task = asyncio.create_task(adapters.invoke_json_cli(self.connection, 'A prompt', self.root))
        pidfile = self.root / 'connector.pid'
        async def entered():
            while not pidfile.exists():
                if task.done():
                    self.fail('connector stopped before the cancellation boundary: ' + str(task.result()))
                await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(entered(), 5)
            pid = int(pidfile.read_text())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_invalid_configuration_rejects_shell_environment_and_command_expansion(self):
        for change in ({'executable': 'relative'}, {'executable': '/bin/sh'},
                       {'args': '--json'}, {'args': ['line\nbreak']}, {'billing': 'paid_api'},
                       {'billing': {}}, {'env': {'OPENAI_API_KEY': 'SECRET'}}, {'protocol': 'unknown'}):
            with self.assertRaisesRegex(ValueError, 'invalid_partner_connection'):
                adapters.json_cli_connection({**self.connection, **change})


if __name__ == '__main__':
    unittest.main()
