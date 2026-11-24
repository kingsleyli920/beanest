"""Real fake-CLI stdin buffers; no provider login, model, or external workspace."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local_agent_coordinator import adapters, native_adapters as native
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
        return json.dumps({'goal': '\x01' * (1024 * 1024)}, ensure_ascii=False)

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
        self.assertEqual(len(prompt.encode()), 6291468)
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

    async def test_prompt_past_the_old_16_mib_limit_reaches_each_cli_whole(self):
        # Catches a prompt size refusal (the old 16 MiB MAX_PROMPT_BYTES) in either transport.
        prompt = 'x' * (16 * 1024 * 1024 + 1)
        events = [{'type': 'result', 'subtype': 'success', 'session_id': fixture.SESSION,
                   'structured_output': {'ok': True}}]
        with self.make_stub(events, agent='claude', before=self.capture_stdin()):
            result = await adapters.invoke('claude', prompt, self.root, response_schema={'type': 'object'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(int((self.root / 'stdin.size').read_text()), 16777217)
        with self.make_stub(fixture.codex_events(), agent='codex', before=self.capture_stdin()):
            result = await native.invoke_native('codex', prompt, self.root,
                permission='read_only', response_schema=fixture.SCHEMA)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(int((self.root / 'stdin.size').read_text()), 16777217)

    def test_structured_answer_past_the_old_4_mib_limit_is_kept_whole(self):
        # Catches the old 4 MiB MAX_TEXT refusal of a long structured answer.
        text = adapters._object_text({'message': 'x' * (5 * 1024 * 1024)})
        self.assertEqual(len(text), 5242894)
        self.assertEqual(json.loads(text)['message'], 'x' * 5242880)
        for name in ('MAX_TEXT', 'MAX_OUTPUT', 'MAX_PROMPT_BYTES'):
            self.assertFalse(hasattr(adapters, name), name)
        self.assertFalse(hasattr(native, '_MAX_LINE'))


class ConnectorPromptCapacityTests(unittest.IsolatedAsyncioTestCase):
    setUp = connector.JsonConnectorTests.setUp
    write_program = connector.JsonConnectorTests.write_program
    requests = connector.JsonConnectorTests.requests

    async def test_real_json_connector_stdin_preserves_decoded_large_original(self):
        prompt = '\x01' * (1024 * 1024)
        result = await adapters.invoke_json_cli(self.connection, prompt, self.root)
        self.assertEqual(result['status'], 'completed')
        requests = self.requests()
        self.assertEqual([row['request']['kind'] for row in requests], ['check', 'invoke'])
        self.assertEqual(requests[1]['request']['prompt'], prompt)
        self.assertEqual(requests[1]['request']['capabilities'], {'text_only': True, 'tools': False})

    async def test_json_connector_request_past_the_old_16_mib_limit_is_sent(self):
        # Catches the old "connector request exceeds size limit" refusal of an escaped prompt.
        prompt = '\x01' * (3 * 1024 * 1024)
        result = await adapters.invoke_json_cli(self.connection, prompt, self.root)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.requests()[1]['request']['prompt'], prompt)
