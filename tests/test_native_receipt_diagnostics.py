"""Private format-stage observations do not change terminal acceptance."""
import hashlib
import json
from pathlib import Path
import unittest
import test_native_adapters as fixtures

SCHEMA, SESSION, FINAL = fixtures.SCHEMA, fixtures.SESSION, fixtures.FINAL
from local_agent_coordinator import native_adapters as native


def events(*, duplicate_terminal=False):
    first = [{'type': 'system', 'subtype': 'init', 'session_id': SESSION}]
    if duplicate_terminal:
        first += [{'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': SESSION},
                  {'type': 'system', 'subtype': 'init', 'session_id': SESSION}]
    return first + [
        {'type': 'assistant', 'message': {'content': [
            {'type': 'thinking', 'thinking': 'DO_NOT_STORE_THOUGHT'},
            {'type': 'text', 'text': 'DO_NOT_STORE_TEXT'},
            {'type': 'tool_use', 'id': 'format-1', 'name': 'StructuredOutput', 'input': FINAL}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'format-1',
                                                'content': 'DO_NOT_STORE_ACK'}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': SESSION,
         'structured_output': FINAL},
    ]


class NativeReceiptDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    make_stub = fixtures.NativeTests.make_stub

    async def run_case(self, duplicate=False):
        with self.make_stub(events(duplicate_terminal=duplicate), agent='claude'):
            return await native.invoke_native('claude', 'Private prompt', self.root, permission='read_only',
                response_schema=SCHEMA, receipt_dir=self.root / 'private')

    def read_metadata(self, result):
        text = Path(result['native_execution']['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE', text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[0]['data'], {'type': 'system', 'subtype': 'init', 'session_id': SESSION})
        metadata = [item for row in rows for item in (row.get('diagnostics', []) if row['channel'] == 'stdout' else row['data'])]
        return metadata

    async def test_single_terminal_remains_complete_and_retains_only_format_shape(self):
        result = await self.run_case()
        self.assertEqual(result['status'], 'completed')
        meta = self.read_metadata(result)
        self.assertEqual([item['type'] for item in meta],
                         ['native_format_init', 'native_format_call', 'native_format_ack', 'native_format_terminal'])
        call, ack, terminal = meta[1:]
        self.assertEqual(call['input']['keys'], ['action', 'message'])
        self.assertEqual(call['input']['sha256'], terminal['structured_output']['sha256'])
        self.assertEqual(call['input']['sha256'], hashlib.sha256(json.dumps(
            FINAL, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual((ack['paired'], ack['duplicate'], ack['is_error']), (True, False, None))
        self.assertEqual((terminal['terminal_index'], terminal['init_count'], terminal['ordinary_tools_closed']), (1, 1, True))

    async def test_two_success_results_still_fail_and_keep_each_boundary(self):
        result = await self.run_case(duplicate=True)
        self.assertEqual((result['status'], result['failure_code']), ('failed', 'protocol_turn_incomplete'))
        meta = self.read_metadata(result)
        terminals = [item for item in meta if item['type'] == 'native_format_terminal']
        self.assertEqual([item['terminal_index'] for item in terminals], [1, 2])
        self.assertEqual([item['structured_output_present'] for item in terminals], [False, True])
        self.assertEqual([item['init_count'] for item in terminals], [1, 2])
        self.assertEqual(next(item for item in meta if item['type'] == 'native_format_call')['after_terminal_index'], 1)

    def test_unknown_payload_keys_and_malformed_metadata_do_not_leak_values(self):
        observer = native._ClaudeReceiptDiagnostics(SCHEMA)
        stream = native._Stream('claude', True, None)
        event = {'type': 'result', 'subtype': 'DO_NOT_STORE_SUBTYPE', 'session_id': 'DO_NOT_STORE_SESSION',
                 'structured_output': {'DO_NOT_STORE_KEY': 'DO_NOT_STORE_VALUE', 'message': 'DO_NOT_STORE_VALUE'}}
        observed = observer.observe(json.dumps(event).encode(), stream)[0]
        self.assertNotIn('DO_NOT_STORE', json.dumps(observed))
        self.assertEqual(observed['structured_output']['keys'], ['message'])
        self.assertEqual(observed['structured_output']['unknown_key_count'], 1)
        self.assertEqual(observed['subtype'], 'unknown')
        self.assertIsNone(observed['session_id'])
        self.assertEqual(observer.observe(b'{', stream), [])
        self.assertEqual(observer.observe(json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': {}}]}}).encode(), stream), [])


if __name__ == '__main__':
    unittest.main()
