"""Exact native quota proof with owned process-cleanup footers; no real CLI."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator import kimi_acp


class KimiQuotaCleanupFooterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wire = Path(self.temp.name).resolve() / 'wire.jsonl'
        self.started = {'taskId': 'fixture-task', 'description': 'Owned test process', 'status': 'running',
            'detached': True, 'startedAt': 101100, 'endedAt': None, 'kind': 'process',
            'command': 'fictional-command', 'pid': 1234, 'exitCode': None}
        self.footer = {'type': 'task.terminated', 'time': 101950,
            'info': {**self.started, 'status': 'killed', 'endedAt': 101940,
                     'stopReason': 'Session closed', 'terminalNotificationSuppressed': True, 'exitCode': -1},
            'outputTail': 'Fictional output; retained as evidence only.'}
        self.rows = [
            {'type': 'turn.prompt', 'time': 100100, 'input': [{'type': 'text', 'text': 'owned prompt'}]},
            {'type': 'task.started', 'time': 101100, 'info': self.started},
            {'type': 'llm.request', 'time': 101800, 'kind': 'loop', 'modelAlias': 'fictional-model'},
            {'type': 'turn.ended', 'time': 101900, 'turnId': 7, 'reason': 'failed',
             'error': {'code': 'provider.api_error', 'name': 'APIProviderQuotaExhaustedError',
                       'retryable': False, 'details': {'statusCode': 429}}}, self.footer]

    def prove(self, rows=None):
        self.wire.write_text(''.join(json.dumps(row) + '\n' for row in (self.rows if rows is None else rows)))
        with patch.object(kimi_acp, 'session_wire', return_value=self.wire):
            return kimi_acp.quota_failure_recovery_proof(self.wire.parent, 'fictional-session',
                claimed_at=100, finished_at=102, model='fictional-model',
                validate_prompt=lambda text: text == 'owned prompt')

    def test_owned_kill_footer_is_included_in_exact_proof(self):
        proof = self.prove()
        self.assertIsNotNone(proof)
        self.assertEqual(proof['wire_through'], self.wire.stat().st_size)
        self.assertEqual(proof['sha256'], hashlib.sha256(self.wire.read_bytes()).hexdigest())
        self.assertEqual(proof['diagnostic']['reason'], 'provider_quota_exhausted')

    def test_terminal_only_quota_still_has_the_same_proof(self):
        self.assertIsNotNone(self.prove(self.rows[:-1]))

    def test_unowned_changed_or_unsettled_process_metadata_is_rejected(self):
        mutations = {'unknown_task': ('taskId', 'other'), 'changed_pid': ('pid', 9),
            'changed_command': ('command', 'other'), 'still_running': ('status', 'running'),
            'unknown_terminal': ('status', 'lost'), 'pending_notification': ('terminalNotificationSuppressed', False),
            'successful_exit': ('exitCode', 0), 'bool_exit': ('exitCode', True),
            'before_failure': ('endedAt', 101899), 'after_record': ('endedAt', 101951),
            'unknown_field': ('newInstruction', 'ignored?')}
        for name, (key, value) in mutations.items():
            with self.subTest(fault=name):
                rows = copy.deepcopy(self.rows)
                rows[-1]['info'][key] = value
                self.assertIsNone(self.prove(rows))
        self.assertIsNone(self.prove([self.rows[0], *self.rows[2:]]))
        rows = copy.deepcopy(self.rows)
        rows[1]['info']['pid'], rows[-1]['info']['pid'] = True, 1
        self.assertIsNone(self.prove(rows))

    def test_any_new_action_unknown_tail_or_duplicate_footer_still_rejects(self):
        for tail in ({'type': 'turn.prompt', 'time': 101960, 'input': []},
                     {'type': 'tool.call', 'time': 101960}, {'type': 'unknown', 'time': 101960},
                     {'type': 'unknown'}, {'type': 'unknown', 'time': float('nan')},
                     {'type': 'unknown', 'time': 99999}, {'type': 'turn.prompt', 'time': 99999},
                     {**self.footer, 'time': 99999}, {'type': 'unknown', 'time': 101849}, self.footer):
            with self.subTest(kind=tail['type']):
                self.assertIsNone(self.prove([*self.rows, tail]))

    def test_prompt_binding_quota_class_and_cleanup_time_remain_authoritative(self):
        for fault in ('wrong_prompt', 'generic_429', 'late_footer'):
            with self.subTest(fault=fault):
                rows = copy.deepcopy(self.rows)
                if fault == 'wrong_prompt':
                    rows[0]['input'][0]['text'] = 'another prompt'
                elif fault == 'generic_429':
                    rows[-2]['error']['name'] = 'RateLimitError'
                else:
                    rows[-1]['time'] = 102001
                self.assertIsNone(self.prove(rows))
