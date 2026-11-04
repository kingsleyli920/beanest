"""Real HTTP/ACP quota handoff with fictional owned process-cleanup footers."""
import json
from pathlib import Path
import sys
import textwrap
import unittest
from uuid import uuid4

from local_agent_coordinator import kimi_acp
from test_kimi_acp import FAKE_ACP
import test_kimi_work_quota_http as work_quota
from test_kimi_work_quota_http import QUOTA_TURN, WorkQuotaFixture


class QuotaFooterFixture(WorkQuotaFixture):
    def __init__(self, *, footer_fault=None, **options):
        start = '''        task = {"taskId": "fictional-background-process", "kind": "process",
                "description": "Fictional owned background check", "status": "running",
                "startedAt": time.time() * 1000, "command": "fictional-check --local",
                "pid": 424242, "detached": False, "timeoutMs": 120000}
        append_wire({"type": "task.started", "info": task})
'''
        end = '''        footer = {"type": "task.terminated", "info": {**task, "status": "killed",
                "endedAt": time.time() * 1000, "exitCode": -15,
                "stopReason": "Session closed", "terminalNotificationSuppressed": True}}
        fault = FOOTER_FAULT
        if fault == "wrong_task":
            footer["info"]["taskId"] = "different-fictional-task"
        elif fault == "drift":
            footer["info"]["pid"] += 1
        elif fault == "unknown":
            footer = {"type": "unrecognized.after_turn"}
        elif fault == "new_prompt":
            footer = {"type": "turn.prompt", "origin": {"kind": "user"}, "input": []}
        append_wire(footer)
'''.replace('FOOTER_FAULT', repr(footer_fault))
        start = textwrap.indent(textwrap.dedent(start), '    ')
        end = textwrap.indent(textwrap.dedent(end), '    ')
        before_request = '    append_wire({"type": "llm.request"'
        before_reply = '    response(row["id"], {"stopReason": "end_turn"})\n'
        assert QUOTA_TURN.count(before_request) == QUOTA_TURN.count(before_reply) == 1
        turn = QUOTA_TURN.replace(before_request, start + before_request).replace(before_reply, end + before_reply)
        marker = '    trace(summary)\n'
        assert FAKE_ACP.count(marker) == 1
        script = f'#!{sys.executable}\n' + FAKE_ACP.replace(marker, marker + textwrap.indent(turn, '    '))
        compile(script, '<fictional-quota-footer-cli>', 'exec')
        super().__init__(**options)
        try:
            self.executable.write_text(script)
        except BaseException:
            self.close()
            raise


class KimiQuotaFooterHTTPTests(unittest.TestCase):
    def fixture(self, **options):
        fixture = QuotaFooterFixture(**options)
        self.addCleanup(fixture.close)
        self.last_fixture = fixture
        return fixture

    def test_owned_cleanup_footer_automatically_preserves_d_and_finishes_without_replay(self):
        # Reuse the existing full HTTP invariants: read-only fresh inspection,
        # exact D/accepted T1/history preservation, one system handoff, no source replay.
        work_quota.KimiWorkQuotaHTTPTests.assert_automatic_recovery(self, legacy=False)
        fixture = self.last_fixture
        result = fixture.failed_work['result']
        wire = kimi_acp.session_wire(fixture.failed_work['cwd'], result['session_id'])
        rows = [json.loads(line) for line in wire.read_text().splitlines()]
        ended = next(index for index, row in enumerate(rows) if row['type'] == 'turn.ended' and row.get('reason') == 'failed')
        self.assertEqual([row['type'] for row in rows[ended + 1:]], ['task.terminated'])
        self.assertTrue(rows[-1]['info']['terminalNotificationSuppressed'])
        self.assertEqual(rows[-1]['info']['status'], 'killed')

    def test_unknown_new_prompt_mismatched_or_drifted_footer_cannot_authorize_handoff(self):
        for fault in ('unknown', 'new_prompt', 'wrong_task', 'drift'):
            with self.subTest(fault=fault):
                fixture = self.fixture(legacy=False, footer_fault=fault)
                try:
                    stopped = fixture.stopped()
                    # Invalid tails may prevent even a structural quota candidate;
                    # neither absent projection nor a blocked handoff grants recovery.
                    blocked = fixture.read()
                    self.assertEqual(fixture.inspections, [])
                    calls = len(fixture.calls)
                    code, rejected = fixture.request('POST', f'/api/rooms/{fixture.room_id}/recover-native',
                        {'request_id': str(uuid4()), **blocked['continuation']['binding']})
                    self.assertEqual(code, 409, rejected)
                    final = fixture.read()
                    self.assertEqual(final['current_run']['id'], stopped['current_run']['id'])
                    self.assertEqual(len(fixture.calls), calls)
                    self.assertEqual((Path(fixture.failed_work['cwd']) / 'second.md').read_bytes(), fixture.retained)
                    self.assertFalse(any(event['kind'] == 'native_recovery_started' for event in final['events']))
                finally:
                    fixture.close()


if __name__ == '__main__':
    unittest.main()
