"""A bound official denial advisory neither grants access nor closes a tool."""
import json
from pathlib import Path
import unittest
import test_native_adapters as fixtures
from local_agent_coordinator import native_adapters as native

SESSION = fixtures.SESSION
DENIAL = {'type': 'system', 'subtype': 'permission_denied', 'tool_name': 'Glob', 'tool_use_id': 'denied-1',
          'message': 'DO_NOT_STORE_DENIAL_PATH', 'uuid': '33333333-3333-4333-8333-333333333333',
          'session_id': SESSION, 'decision_reason_type': 'mode', 'decision_reason': 'DO_NOT_STORE_REASON'}
INIT = {'type': 'system', 'subtype': 'init', 'session_id': SESSION}
CALL = {'type': 'assistant', 'message': {'content': [
    {'type': 'tool_use', 'id': 'denied-1', 'name': 'Glob', 'input': {'pattern': '*.md'}}]}}
RESULT = {'type': 'user', 'message': {'content': [
    {'type': 'tool_result', 'tool_use_id': 'denied-1', 'is_error': True, 'content': 'Access refused.'}]}}
FINAL = {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': SESSION,
         'structured_output': fixtures.FINAL}


class PermissionDeniedTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    make_stub = fixtures.NativeTests.make_stub

    async def invoke(self, rows):
        with self.make_stub(rows, agent='claude'):
            return await native.invoke_native('claude', 'Inspect only', self.root, permission='read_only',
                response_schema=fixtures.SCHEMA, receipt_dir=self.root / 'private')

    async def test_bound_advisory_waits_for_actual_failed_result_then_finishes_without_permission_change(self):
        result = await self.invoke([INIT, CALL, DENIAL, RESULT, FINAL])
        self.assertEqual(result['status'], 'completed')
        observation = result['native_execution']
        self.assertEqual((observation['permission'], observation['tool_calls'], observation['tool_results']), ('read_only', 1, 1))
        self.assertTrue(observation['cleanup_confirmed'])
        receipt = Path(observation['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE', receipt)
        rows = [json.loads(line)['data'] for line in receipt.splitlines()]
        denial = next(row for row in rows if isinstance(row, dict) and row.get('subtype') == 'permission_denied')
        self.assertEqual(set(denial), {'type', 'subtype', 'tool_name', 'tool_use_id', 'session_id', 'uuid'})
        # Command construction is part of the fixture too: CI needs no installed CLI.
        with self.make_stub([], agent='claude'):
            argv = native.native_command('claude', permission='read_only')
        self.assertEqual(argv[0], str(self.stub))
        self.assertIn('dontAsk', argv)
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Glob,Grep')

    async def test_advisory_cannot_replace_the_missing_tool_result(self):
        result = await self.invoke([INIT, CALL, DENIAL, FINAL])
        self.assertEqual((result['status'], result['failure_code']), ('failed', 'native_tool_result_missing'))
        self.assertEqual((result['native_execution']['tool_calls'], result['native_execution']['tool_results']), (1, 0))

    async def test_an_advisory_in_another_shape_is_skipped_and_another_session_still_fails(self):
        # Catches three defects: a denial advisory whose shape or tool binding
        # the parser cannot read ending the call (a CLI release adding a field
        # or reason code did that); such an advisory closing the tool; and an
        # advisory from another session no longer failing the call.
        cases = [({'session_id': '44444444-4444-4444-8444-444444444444'}, 'native_session_identity_mismatch', []),
                 ({'tool_use_id': 'unobserved'}, None, ['native_protocol_invalid']),
                 ({'tool_name': 'Read'}, None, ['native_protocol_invalid']),
                 ({'tool_name': 'UnapprovedTool'}, None, ['native_protocol_invalid']),
                 ({'tool_name': {}}, None, ['native_protocol_invalid']),
                 ({'uuid': 'invalid'}, None, ['native_protocol_invalid']),
                 ({'agent_id': 'subagent'}, None, ['native_protocol_invalid']),
                 ({'extra': 'DO_NOT_STORE'}, None, ['native_protocol_invalid']),
                 ({'message': None}, None, ['native_protocol_invalid']),
                 ({'decision_reason_code': 'unknown'}, None, ['native_protocol_invalid'])]
        for update, failure, ignored in cases:
            with self.subTest(update=update):
                stream = native._Stream('claude', True, None)
                for row in (INIT, CALL, {**DENIAL, **update}):
                    await stream.consume(json.dumps(row).encode())
                self.assertEqual((stream.failure, stream.ignored), (failure, ignored))
                self.assertEqual((stream.calls, stream.results), (1, 0))
        # End to end: the unreadable advisory changes nothing about the turn.
        result = await self.invoke([INIT, CALL, {**DENIAL, 'decision_reason_code': 'unknown'}, RESULT, FINAL])
        self.assertEqual((result['status'], result.get('failure_code')), ('completed', None))
        self.assertEqual((result['native_execution']['tool_calls'], result['native_execution']['tool_results']), (1, 1))
        self.assertNotIn('DO_NOT_STORE', Path(result['native_execution']['receipt_path']).read_text())
        missing = {key: value for key, value in DENIAL.items() if key != 'message'}
        self.assertFalse(native._claude_permission_denied_valid(missing))
        diagnostic = native._protocol_diagnostic(json.dumps({**DENIAL, 'agent_id': 'DO_NOT_STORE'}).encode())
        self.assertNotIn('DO_NOT_STORE', json.dumps(diagnostic))
        self.assertEqual(diagnostic['field_types']['agent_id'], 'str')


if __name__ == '__main__':
    unittest.main()
