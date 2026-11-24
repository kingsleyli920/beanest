"""A partner's output of any size reaches Beanest whole, through real fake-CLI processes.

No provider login, model or network: each CLI is a local script that prints
the protocol a real one would, only far longer than Beanest used to allow.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters, native_adapters as native
import test_native_adapters as fixture

MIB = 1024 * 1024


class PartnerOutputSizeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60  # a 50 MiB line is meant to be slow

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stub = self.root / 'stub'

    def write_stub(self, agent, body):
        """A CLI that answers the login check, then runs body to print its protocol."""
        auth = ("print('Logged in using ChatGPT')" if agent == 'codex' else
                "print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', "
                "'apiProvider': 'firstParty', 'subscriptionType': 'max'}))")
        self.stub.write_text(f'#!{sys.executable}\nimport json, sys\n'
                             f"if 'status' in sys.argv:\n    {auth}\n    sys.exit(0)\n"
                             'sys.stdin.buffer.read()\n'
                             'def emit(event):\n    sys.stdout.write(json.dumps(event) + "\\n")\n' + body)
        self.stub.chmod(0o700)
        return patch.object(adapters, 'find_' + agent, return_value=str(self.stub))

    async def test_text_call_with_40_mib_of_stdout_completes_with_the_whole_answer(self):
        # Catches the old 32 MiB MAX_OUTPUT failure ("Agent output exceeded limit") and the 4 MiB MAX_TEXT cut.
        body = ("emit({'type': 'system', 'subtype': 'init', 'session_id': '%s'})\n"
                "emit({'type': 'result', 'subtype': 'success', 'session_id': '%s', 'result': 'a' * (40 * 1024 * 1024)})\n"
                % (fixture.SESSION, fixture.SESSION))
        with self.write_stub('claude', body):
            result = await adapters.invoke('claude', 'Write the long report.', self.root)
        self.assertEqual((result['status'], result.get('failure_code')), ('completed', None))
        self.assertEqual(len(result['text']), 41943040)
        self.assertEqual(result['session_id'], fixture.SESSION)

    async def test_native_call_with_a_50_mib_protocol_line_completes_and_records_it_all(self):
        # Catches the old 32 MiB _MAX_LINE (native_protocol_line_limit), the 64 KiB StreamReader line
        # limit, and the old 64 MiB receipt cap that stopped recording what recovery later reads.
        body = '''
emit({'type': 'thread.started', 'thread_id': '%s'})
emit({'type': 'turn.started'})
for tool, size in (('t1', 50), ('t2', 20)):
    emit({'type': 'item.started', 'item': {'id': tool, 'type': 'command_execution', 'command': 'make test', 'status': 'in_progress'}})
    emit({'type': 'item.completed', 'item': {'id': tool, 'type': 'command_execution',
          'aggregated_output': 'o' * (size * 1024 * 1024), 'exit_code': 0, 'status': 'completed'}})
emit({'type': 'item.completed', 'item': {'id': 'a1', 'type': 'agent_message', 'text': json.dumps(%r)}})
emit({'type': 'turn.completed', 'usage': {'input_tokens': 11, 'output_tokens': 7}})
''' % (fixture.SESSION, fixture.FINAL)
        with self.write_stub('codex', body):
            result = await native.invoke_native('codex', 'Run the long test suite.', self.root, permission='full_access',
                                                response_schema=fixture.SCHEMA, receipt_dir=self.root / 'private')
        self.assertEqual((result['status'], result.get('failure_code')), ('completed', None))
        self.assertEqual(json.loads(result['text']), {'message': 'done', 'action': 'finish'})
        evidence = result['native_execution']
        self.assertEqual((evidence['tool_calls'], evidence['tool_results'], evidence['exit_code']), (2, 2, 0))
        receipt = Path(evidence['receipt_path'])
        self.assertGreater(receipt.stat().st_size, 70 * MIB)
        with receipt.open('rb') as stream:
            rows = [json.loads(line) for line in stream]
        self.assertEqual(rows[-1]['data']['type'], 'turn.completed')
        self.assertFalse(any(row['data'].get('type') == 'native_receipt_truncated' for row in rows))

    async def test_lines_of_any_length_are_read_whole(self):
        # Catches a reader that splits or refuses a line longer than its buffer.
        reader = asyncio.StreamReader(limit=16)
        reader.feed_data(b'short\n' + b'x' * (3 * MIB) + b'\n' + b'tail without newline')
        reader.feed_eof()
        lines = [line async for line in adapters.read_lines(reader)]
        self.assertEqual([len(line) for line in lines], [6, 3145729, 20])
        self.assertEqual(lines[0], b'short\n')
        self.assertEqual(lines[2], b'tail without newline')


if __name__ == '__main__':
    unittest.main()
