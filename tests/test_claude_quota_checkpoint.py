"""Official Claude terminal quota refusals are failures, never model replies."""
import json
import unittest
from local_agent_coordinator import adapters, native_adapters

SID = 'b95a0c48-d594-469e-8b4d-4e07d1b39b7c'
NOTICE = "You've hit your 5-hour limit · resets 4:10pm (America/Vancouver)"

class ClaudeQuotaCheckpointTests(unittest.TestCase):
    def result(self, terminal):
        rows = [{'type': 'system', 'subtype': 'init', 'session_id': SID}, terminal]
        return adapters.parse_events('claude', [json.dumps(row).encode() for row in rows], structured=True)

    def test_official_error_terminal_without_assistant_metadata_is_typed_quota(self):
        event = {'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': SID, 'result': NOTICE}
        value = self.result(event)
        self.assertEqual((value['status'], value['failure_code']), ('failed', 'subscription_limit_reached'))
        self.assertEqual(value['text'], '')
        receipt = native_adapters._receipt_event('claude', json.dumps(event).encode())
        self.assertNotIn(NOTICE, json.dumps(receipt))
        self.assertEqual(receipt['quota_failure'], 'provider_quota_exhausted')

    def test_notice_in_model_output_tool_error_or_other_terminal_shape_is_not_quota(self):
        for change in ({'is_error': False}, {'subtype': 'error_during_execution'},
                       {'result': NOTICE + ' Additional arbitrary prose.'}, {'session_id': 'another-session'}):
            with self.subTest(change=change):
                event = {'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': SID, 'result': NOTICE}
                event.update(change)
                self.assertNotEqual(self.result(event).get('failure_code'), 'subscription_limit_reached')

class ClaudeQuotaProofTests(unittest.TestCase):
    def test_known_native_cache_records_do_not_authorize_a_new_turn_or_unknown_tail(self):
        import os
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        user_leaf = '7c497d05-6b26-443d-bca8-62517b150875'
        error_leaf = '5ab3736f-ef96-4f15-959f-4b33656aa56a'
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            cwd = home / 'fictional-project'
            encoded = __import__('re').sub(r'[^a-zA-Z0-9]', '-', str(cwd))
            wire = home / '.claude/projects' / encoded / (SID + '.jsonl')
            wire.parent.mkdir(parents=True)
            receipts = home / 'receipts'
            receipts.mkdir()
            rows = [
                {'type': 'user', 'sessionId': SID, 'uuid': user_leaf, 'timestamp': '1970-01-01T00:00:01.100Z',
                 'message': {'role': 'user', 'content': 'fictional exact host prompt'}},
                {'type': 'last-prompt', 'sessionId': SID, 'leafUuid': user_leaf, 'lastPrompt': 'fictional index'},
                {'type': 'mode', 'sessionId': SID, 'mode': 'normal'},
                {'type': 'session-owner', 'sessionId': SID, 'accountUuid': user_leaf, 'organizationUuid': error_leaf},
                {'type': 'atis-latch', 'sessionId': SID, 'atis': 'fictional metadata'},
                {'type': 'assistant', 'sessionId': SID, 'uuid': error_leaf, 'timestamp': '1970-01-01T00:00:01.200Z',
                 'isApiErrorMessage': True, 'error': 'rate_limit', 'apiError': 'usage_limit_reached',
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': NOTICE}]}},
                {'type': 'last-prompt', 'sessionId': SID, 'leafUuid': error_leaf, 'lastPrompt': 'fictional index'},
                dict.fromkeys(('totalCostUSD', 'totalAPIDuration', 'totalAPIDurationWithoutRetries', 'totalToolDuration',
                               'totalLinesAdded', 'totalLinesRemoved', 'totalDuration', 'startTime'), 0)
                | {'type': 'cost-state', 'sessionId': SID, 'modelUsage': {}, 'hasUnknownModelCost': False},
            ]
            receipt = [
                {'channel': 'stdout', 'data': {'type': 'system', 'subtype': 'init', 'session_id': SID}},
                {'channel': 'stdout', 'data': {'type': 'result', 'subtype': 'success', 'session_id': SID, 'is_error': True},
                 'diagnostics': [{'type': 'native_format_terminal', 'ordinary_tool_calls': 0, 'ordinary_tool_results': 0,
                                  'ordinary_tools_closed': True, 'terminal_index': 1, 'init_count': 1}]},
            ]
            entry = receipts / 'owned.jsonl'
            entry.write_text(''.join(json.dumps(row)+'\n' for row in receipt))
            os.utime(entry, (1.3, 1.3))
            def write(items):
                wire.write_text(''.join(json.dumps(row)+'\n' for row in items))
            def prove():
                return native_adapters.claude_quota_recovery_proof(cwd, SID, claimed_at=1, finished_at=2,
                    validate_prompt=lambda text: text == 'fictional exact host prompt', receipt_dir=receipts)
            with patch.object(Path, 'home', return_value=home):
                write(rows)
                self.assertEqual(prove()['reason'], 'provider_quota_exhausted')
                for tail in ({'type': 'unknown', 'sessionId': SID}, rows[0],
                             {'type': 'last-prompt', 'sessionId': SID, 'leafUuid': SID, 'lastPrompt': 'wrong leaf'},
                             {'type': 'mode', 'sessionId': SID, 'mode': 'normal'}):
                    with self.subTest(tail=tail['type']):
                        write(rows+[tail])
                        self.assertIsNone(prove())
