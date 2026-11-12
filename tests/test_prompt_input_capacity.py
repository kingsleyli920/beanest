"""Real fake-CLI stdin buffers; no provider login, model, or external workspace."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator import adapters, native_adapters as native
from local_agent_coordinator.message_capacity import MAX_MESSAGE_UTF8_BYTES, MAX_PROMPT_BYTES
import test_native_adapters as fixture
import test_json_cli_connector as connector


class PromptInputCapacityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stub = self.root / 'stub'

    make_stub = fixture.NativeTests.make_stub

    @staticmethod
    def large_prompt():
        return json.dumps({'goal': '\x01' * MAX_MESSAGE_UTF8_BYTES}, ensure_ascii=False)

    def capture_stdin(self):
        return ("import pathlib, hashlib\n"
                "raw = sys.stdin.buffer.read()\n"
                f"pathlib.Path({str(self.root / 'stdin.sha')!r}).write_text(hashlib.sha256(raw).hexdigest())\n"
                f"pathlib.Path({str(self.root / 'stdin.size')!r}).write_text(str(len(raw)))")

    def assert_stdin(self, prompt):
        self.assertEqual((self.root / 'stdin.sha').read_text(), hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(int((self.root / 'stdin.size').read_text()), len(prompt.encode()))

    async def test_native_stdin_preserves_one_mib_control_text_inside_json_without_output_budget_change(self):
        prompt = self.large_prompt()
        self.assertGreater(len(prompt.encode()), 6 * MAX_MESSAGE_UTF8_BYTES)
        self.assertLess(len(prompt.encode()), MAX_PROMPT_BYTES)
        for agent in ('codex', 'claude'):
            with self.subTest(agent=agent):
                events = fixture.codex_events() if agent == 'codex' else [
                    {'type': 'system', 'subtype': 'init', 'session_id': fixture.SESSION},
                    {'type': 'result', 'subtype': 'success', 'session_id': fixture.SESSION,
                     'structured_output': fixture.FINAL}]
                with self.make_stub(events, agent=agent, before=self.capture_stdin()):
                    result = await native.invoke_native(agent, prompt, self.root,
                        permission='full_access', response_schema=fixture.SCHEMA)
                self.assertEqual(result['status'], 'completed')
                self.assertTrue(result['native_execution']['cleanup_confirmed'])
                self.assertEqual(json.loads(result['text']), fixture.FINAL)
                self.assert_stdin(prompt)

    async def test_text_adapter_stdin_preserves_full_prompt_and_restricted_command(self):
        prompt = self.large_prompt()
        events = [{'type': 'result', 'subtype': 'success', 'session_id': fixture.SESSION,
                   'structured_output': {'ok': True}}]
        with self.make_stub(events, agent='claude', before=self.capture_stdin()):
            result = await adapters.invoke('claude', prompt, self.root, response_schema={'type': 'object'})
            command = adapters._command('claude', None)
        self.assertEqual(result['status'], 'completed')
        self.assert_stdin(prompt)
        self.assertIn('--safe-mode', command)
        self.assertEqual(command[command.index('--tools') + 1], '')

    async def test_all_prompt_entries_reject_over_buffer_before_auth_or_process_start(self):
        prompt = 'x' * (MAX_PROMPT_BYTES + 1)
        connection = {'protocol': 'beanest-json-v1', 'executable': str(self.stub), 'args': [], 'billing': 'local'}
        with patch.object(adapters, '_subscription_status', new=AsyncMock()) as auth, \
             patch.object(adapters, 'check_json_cli', new=AsyncMock()) as health, \
             patch.object(adapters.asyncio, 'create_subprocess_exec', new=AsyncMock()) as spawn:
            for operation in (lambda: adapters.invoke('codex', prompt, self.root),
                              lambda: native.invoke_native('codex', prompt, self.root, permission='read_only'),
                              lambda: adapters.invoke_json_cli(connection, prompt, self.root)):
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    await operation()
            auth.assert_not_called()
            health.assert_not_called()
            spawn.assert_not_called()

    async def test_json_cli_outer_escaping_is_checked_before_handshake(self):
        # Inner raw prompt fits, but JSON wrapping it exceeds the actual stdin bound.
        prompt = '\x01' * (MAX_PROMPT_BYTES // 6)
        self.assertLess(len(prompt.encode()), MAX_PROMPT_BYTES)
        connection = {'protocol': 'beanest-json-v1', 'executable': str(self.stub), 'args': [], 'billing': 'local'}
        with patch.object(adapters, 'check_json_cli', new=AsyncMock()) as health, \
             patch.object(adapters.asyncio, 'create_subprocess_exec', new=AsyncMock()) as spawn:
            with self.assertRaisesRegex(ValueError, 'connector request exceeds size limit'):
                await adapters.invoke_json_cli(connection, prompt, self.root)
            health.assert_not_called()
            spawn.assert_not_called()

    def test_output_and_schema_limits_remain_separate(self):
        self.assertEqual(adapters.MAX_TEXT, 4 * 1024 * 1024)
        self.assertEqual(adapters.MAX_OUTPUT, 32 * 1024 * 1024)
        self.assertEqual(native._MAX_LINE, 32 * 1024 * 1024)
        self.assertEqual(adapters.MAX_PROMPT_BYTES, MAX_PROMPT_BYTES)
        with self.assertRaisesRegex(ValueError, 'structured response exceeds size limit'):
            adapters._object_text({'message': 'x' * adapters.MAX_TEXT})


class ConnectorPromptCapacityTests(unittest.IsolatedAsyncioTestCase):
    setUp = connector.JsonConnectorTests.setUp
    write_program = connector.JsonConnectorTests.write_program
    requests = connector.JsonConnectorTests.requests

    async def test_real_json_connector_stdin_preserves_decoded_large_original(self):
        prompt = '\x01' * MAX_MESSAGE_UTF8_BYTES
        result = await adapters.invoke_json_cli(self.connection, prompt, self.root)
        self.assertEqual(result['status'], 'completed')
        requests = self.requests()
        self.assertEqual([row['request']['kind'] for row in requests], ['check', 'invoke'])
        self.assertEqual(requests[1]['request']['prompt'], prompt)
        self.assertEqual(requests[1]['request']['capabilities'], {'text_only': True, 'tools': False})
