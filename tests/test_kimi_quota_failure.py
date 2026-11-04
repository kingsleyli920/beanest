"""Kimi may end ACP normally after a typed native quota failure, never completion."""
import json
import unittest
from tests import test_kimi_acp as fixture


class KimiQuotaFailureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.KimiACPTests.asyncSetUp
    asyncTearDown=fixture.KimiACPTests.asyncTearDown
    scenario=fixture.KimiACPTests.scenario
    trace=fixture.KimiACPTests.trace
    invoke=fixture.KimiACPTests.invoke
    wire_path=fixture.KimiACPTests.wire_path

    def inject(self, *, name='APIProviderQuotaExhaustedError', status=429, retryable=False):
        self.scenario('wire_failed')
        code=self.executable.read_text()
        old='terminal["error"] = {"message": "FICTIONAL_PROVIDER_ERROR_NEVER_EXPOSE"}'
        error={'code':'provider.api_error','name':name,'retryable':retryable,
               'message':fixture.PRIVATE, 'details':{'statusCode':status,'requestId':fixture.PRIVATE}}
        self.assertIn(old,code)
        self.executable.write_text(code.replace(old,'terminal["error"] = '+repr(error)).replace('"kind": "read"','"kind": "execute"'))

    async def test_paired_tools_exit_zero_with_native_quota_failure_remains_failed_and_never_replays(self):
        self.inject()
        result=await self.invoke(permission='full_access')
        self.assertEqual((result['status'],result['failure_code']),('failed','subscription_limit_reached'))
        observation=result['native_execution']
        self.assertEqual((observation['tool_calls'],observation['tool_results'],observation['exit_code']),(1,1,0))
        self.assertTrue(observation['side_effects_possible'])
        self.assertTrue(observation['cleanup_confirmed'])
        self.assertEqual(observation['failure_diagnostic']['reason'],'provider_quota_exhausted')
        self.assertEqual(sum(row.get('method')=='session/prompt' for row in self.trace()),1)
        self.assertNotIn(fixture.PRIVATE,json.dumps(result))

    async def test_quota_failure_wrapped_by_automatic_compaction_is_exhausted_quota(self):
        # Catches: Kimi 2.x reports a 5-hour limit hit during auto compaction as
        # compaction.failed with the typed refusal in cause; it was classified as a
        # generic provider_error, so one exhausted peer stopped the whole objective.
        self.scenario('wire_failed')
        code=self.executable.read_text()
        old='terminal["error"] = {"message": "FICTIONAL_PROVIDER_ERROR_NEVER_EXPOSE"}'
        cause={'code':'provider.api_error','name':'APIProviderQuotaExhaustedError','retryable':False,
               'message':fixture.PRIVATE,'details':{'statusCode':429,'requestId':None,'traceId':fixture.PRIVATE}}
        wrapper={'code':'compaction.failed','name':'Error2','retryable':False,'message':fixture.PRIVATE,'cause':cause}
        compaction=('append_wire({"type": "full_compaction.begin", "source": "auto"}); '
                    'append_wire({"type": "llm.request", "kind": "compaction", "modelAlias": "kimi-code/fictional-0"}); '
                    'append_wire({"type": "full_compaction.cancel"}); ')
        self.assertIn(old,code)
        self.executable.write_text(code.replace(old,compaction+'terminal["error"] = '+repr(wrapper)))
        result=await self.invoke(permission='full_access')
        self.assertEqual((result['status'],result['failure_code']),('failed','subscription_limit_reached'))
        diagnostic=result['native_execution']['failure_diagnostic']
        self.assertEqual((diagnostic['phase'],diagnostic['reason'],diagnostic['http_status']),
                         ('compaction','provider_quota_exhausted',429))
        self.assertEqual(sum(row.get('method')=='session/prompt' for row in self.trace()),1)
        self.assertNotIn(fixture.PRIVATE,json.dumps(result))

    async def test_other_429_wrong_status_or_retryable_failure_is_not_exhausted_quota(self):
        for name,status,retryable in [('APIProviderRateLimitError',429,True),('APIStatusError',429,False),
                                     ('APIProviderQuotaExhaustedError',403,False),
                                     ('APIProviderQuotaExhaustedError',429,True)]:
            with self.subTest(name=name,status=status,retryable=retryable):
                # Reinstall the original owned fixture before each distinct producer.
                self.executable.write_text('#!'+fixture.sys.executable+'\n'+fixture.FAKE_ACP)
                self.wire_path().unlink(missing_ok=True)
                self.inject(name=name,status=status,retryable=retryable)
                result=await self.invoke(permission='full_access')
                self.assertEqual(result['status'],'failed')
                self.assertNotEqual(result['failure_code'],'subscription_limit_reached')
                self.assertNotEqual(result['native_execution']['failure_diagnostic']['reason'],'provider_quota_exhausted')
                self.assertTrue(result['cleanup_confirmed'])


class QuotaRecoveryProofTests(unittest.TestCase):
    def test_only_exact_current_failed_quota_turn_can_authorize_handoff(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from local_agent_coordinator import kimi_acp
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'wire.jsonl'
            rows = [
                {'time': 1100, 'type': 'turn.prompt', 'input': [{'type': 'text', 'text': 'fictional exact job binding'}]},
                {'time': 1200, 'type': 'llm.request', 'kind': 'loop', 'modelAlias': 'kimi-code/fictional'},
                {'time': 1300, 'type': 'turn.ended', 'turnId': 7, 'reason': 'failed', 'error': {
                    'code': 'provider.api_error', 'name': 'APIProviderQuotaExhaustedError', 'retryable': False,
                    'details': {'statusCode': 429}}},
            ]
            def prove():
                return kimi_acp.quota_failure_recovery_proof(path.parent, fixture.SID,
                    claimed_at=1, finished_at=2, model='kimi-code/fictional',
                    validate_prompt=lambda prompt: prompt == 'fictional exact job binding')
            def write(value):
                path.write_text(''.join(json.dumps(row) + '\n' for row in value))
            with patch.object(kimi_acp, 'session_wire', return_value=path):
                write(rows)
                proof = prove()
                self.assertEqual(proof['diagnostic'], {'phase': 'prompt', 'reason': 'provider_quota_exhausted', 'http_status': 429})
                self.assertNotIn('fictional exact job binding', json.dumps(proof))
                for tail in ({'type': 'unknown'}, {'time': 1300, 'type': 'session.config'}, {'time': 1400, 'type': 'turn.prompt'}):
                    with self.subTest(tail=tail['type']):
                        write(rows + [tail])
                        self.assertIsNone(prove())
                write([rows[0], {**rows[1], 'modelAlias': 'kimi-code/other'}, rows[2]])
                self.assertIsNone(prove())
                write([{**rows[0], 'input': [{'type': 'text', 'text': 'different job'}]}, *rows[1:]])
                self.assertIsNone(prove())
                write([*rows[:2], {**rows[2], 'error': {**rows[2]['error'], 'name': 'RateLimitError'}}])
                self.assertIsNone(prove())
