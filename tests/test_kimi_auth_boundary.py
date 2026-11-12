"""Provider auth errors before and after insertion over actual owned ACP stdio."""
import json
import sys
import textwrap
import unittest

import test_kimi_acp as acp_fixture
from test_kimi_acp import FAKE_ACP, PRIVATE, SID


ERROR_BOUNDARY = textwrap.dedent('''
    if method == scenario.get("error_at"):
        if method == "session/prompt":
            append_wire({"type": "turn.prompt", "origin": {"kind": "user"}, "input": []})
            append_wire({"type": "context.append_message", "message": {"role": "user", "content": "Fictional prompt already inserted."}})
            append_wire({"type": "full_compaction.begin", "source": "automatic"})
            append_wire({"type": "llm.request", "kind": "compaction", "model": "fictional-0", "thinkingEffort": "high"})
            if scenario.get("after_compaction"):
                append_wire({"type": "full_compaction.complete"})
                append_wire({"type": "llm.request", "kind": "loop", "model": "fictional-0"})
            else:
                append_wire({"type": "full_compaction.cancel"})
            append_wire({"type": "turn.ended", "reason": "failed", "error": {
                "code": "provider.auth_error", "name": "APIStatusError", "retryable": False,
                "message": "FICTIONAL_PROVIDER_ERROR_NEVER_EXPOSE", "details": {"statusCode": 403,
                "requestId": "FICTIONAL_REQUEST_ID_NEVER_EXPOSE"}}})
        send({"id": row["id"], "error": {"code": -32000,
              "message": "FICTIONAL_PROVIDER_ERROR_NEVER_EXPOSE"}})
        continue
''')


class KimiAuthBoundaryTests(unittest.IsolatedAsyncioTestCase):
    # Reuse owned transport setup, not the existing test methods or a copied adapter.
    asyncSetUp = acp_fixture.KimiACPTests.asyncSetUp
    asyncTearDown = acp_fixture.KimiACPTests.asyncTearDown
    scenario = acp_fixture.KimiACPTests.scenario
    invoke = acp_fixture.KimiACPTests.invoke
    trace = acp_fixture.KimiACPTests.trace
    wire_path = acp_fixture.KimiACPTests.wire_path
    receipt = acp_fixture.KimiACPTests.receipt

    def configure_error(self, method, **options):
        marker = '    trace(summary)\n'
        self.assertEqual(FAKE_ACP.count(marker), 1)
        code = FAKE_ACP.replace(marker, marker + textwrap.indent(ERROR_BOUNDARY, '    '))
        self.executable.write_text(f'#!{sys.executable}\n' + code)
        self.scenario('auth_error_boundary', error_at=method, **options)

    async def test_post_prompt_compaction_403_never_replays_inserted_prompt_or_downgrades_permission(self):
        self.configure_error('session/prompt')
        result = await self.invoke(session_id=SID, permission='full_access')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result.get('failure_code'), 'native_provider_error')
        diagnostic = result['native_execution']['failure_diagnostic']
        self.assertEqual({key: diagnostic[key] for key in ('phase', 'rpc_method', 'prompt_sent', 'http_status')},
                         {'phase': 'compaction', 'rpc_method': 'session/prompt', 'prompt_sent': True, 'http_status': 403})
        methods = [row.get('method') for row in self.trace()]
        self.assertEqual(methods.count('session/prompt'), 1)
        self.assertEqual(methods.count('session/resume'), 1)
        modes = [row['modeId'] for row in self.trace() if row.get('method') == 'session/set_mode']
        self.assertEqual(modes, ['auto'])
        self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(result['native_execution']['tool_calls'], 0)
        self.assertIsNone(self.receipt(result)['proof'])
        rows = [json.loads(line) for line in self.wire_path().read_text().splitlines()]
        self.assertEqual(sum(row['type'] == 'turn.prompt' for row in rows), 1)
        self.assertEqual(rows[-1]['reason'], 'failed')
        self.assertEqual(rows[-1]['error']['details']['statusCode'], 403)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn('FICTIONAL_REQUEST_ID_NEVER_EXPOSE', json.dumps(result))

    async def test_pre_prompt_auth_failure_never_submits_model_work(self):
        self.configure_error('authenticate')
        result = await self.invoke(permission='full_access')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'native_auth_unconfirmed')
        diagnostic = result['native_execution']['failure_diagnostic']
        self.assertEqual({key: diagnostic[key] for key in ('phase', 'rpc_method', 'prompt_sent')},
                         {'phase': 'handshake', 'rpc_method': 'authenticate', 'prompt_sent': False})
        self.assertNotIn('session/prompt', [row.get('method') for row in self.trace()])
        self.assertTrue(result['cleanup_confirmed'])
        self.assertFalse(result['native_execution']['side_effects_possible'])
        self.assertEqual(result['native_execution']['tool_calls'], 0)
        self.assertFalse(self.wire_path().exists())
        self.assertNotIn(PRIVATE, json.dumps(result))

    async def test_successful_compaction_then_loop_error_is_not_reported_as_compaction_failure(self):
        self.configure_error('session/prompt', after_compaction=True)
        result = await self.invoke(session_id=SID, permission='full_access')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['native_execution']['failure_diagnostic']['phase'], 'prompt')
        self.assertTrue(result['native_execution']['failure_diagnostic']['prompt_sent'])
        self.assertEqual([row.get('method') for row in self.trace()].count('session/prompt'), 1)
        self.assertTrue(result['cleanup_confirmed'])
