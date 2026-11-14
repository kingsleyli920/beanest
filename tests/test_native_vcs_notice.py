"""Claude VCS notices through a temporary fake CLI; no native model calls."""
import json
import unittest

from local_agent_coordinator import native_adapters as native
from test_native_adapters import FINAL, SCHEMA, SESSION, NativeTests as _Helpers


class NativeVcsNoticeTests(unittest.IsolatedAsyncioTestCase):
    setUp = _Helpers.setUp
    tearDown = _Helpers.tearDown
    make_stub = _Helpers.make_stub

    def notice(self, **changes):
        event = {'type': 'system', 'subtype': 'vcs_state_changed',
                 'kind': 'future-vcs-state', 'cwd': str(self.root),
                 'uuid': '6f949bd9-77ba-4259-b9b5-43ec0a503338',
                 'session_id': SESSION}
        event.update(changes)
        return event

    def events(self, notice, *, tools=True, terminal=True):
        events = [{'type': 'system', 'subtype': 'init', 'session_id': SESSION}, notice]
        if tools:
            events.extend([
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': 'read', 'name': 'Read',
                     'input': {'file_path': '/synthetic/example.txt'}}]}},
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 'read', 'content': 'synthetic'}]}},
            ])
        if terminal:
            events.append({'type': 'result', 'subtype': 'success',
                           'session_id': SESSION, 'structured_output': FINAL})
        return events

    async def invoke(self, events):
        # Consume the real stdin prompt, rather than bypassing invoke_native's
        # subprocess transport. Authentication is answered by the helper stub.
        with self.make_stub(events, agent='claude',
                            before="assert sys.stdin.read() == 'synthetic vcs probe'"):
            result = await native.invoke_native('claude', 'synthetic vcs probe', self.root,
                permission='full_access', response_schema=SCHEMA,
                receipt_dir=self.root / 'receipts')
        self.assertTrue(result['cleanup_confirmed'])
        self.assertTrue(result['native_execution']['cleanup_confirmed'])
        return result

    async def test_vcs_notice_future_kind_and_optional_branch_are_metadata(self):
        for extra in ({}, {'branch': 'future/topic'}):
            with self.subTest(branch_present=bool(extra)):
                result = await self.invoke(self.events(self.notice(**extra)))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(json.loads(result['text']), FINAL)
                self.assertEqual(result['session_id'], SESSION)
                evidence = result['native_execution']
                self.assertEqual((evidence['tool_calls'], evidence['tool_results']), (1, 1))
                self.assertEqual(evidence['permission'], 'full_access')
                self.assertEqual([event['state'] for event in result['events']
                                  if event['type'] == 'native_tool'], ['started', 'completed'])
                self.assertNotIn('vcs_state_changed', json.dumps(result['events']))

    async def test_vcs_notice_preserves_session_tool_and_terminal_guards(self):
        wrong_sid = '9648e7a6-d99c-474f-8705-39f24d8e62cc'
        mismatch = self.events(self.notice(session_id=wrong_sid))
        unpaired = self.events(self.notice())
        del unpaired[3]
        missing_terminal = self.events(self.notice(), terminal=False)
        duplicate_terminal = self.events(self.notice())
        duplicate_terminal.append(duplicate_terminal[-1].copy())
        for label, events, failure in (
            ('session', mismatch, 'native_session_identity_mismatch'),
            ('tool', unpaired, 'native_tool_result_missing'),
            ('missing terminal', missing_terminal, 'protocol_turn_incomplete'),
            ('duplicate terminal', duplicate_terminal, 'protocol_turn_incomplete'),
        ):
            with self.subTest(guard=label):
                result = await self.invoke(events)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['failure_code'], failure)

    async def test_vcs_notice_with_unreadable_fields_is_skipped_and_the_turn_completes(self):
        # Catches: a repository-cache hint whose fields a CLI release changed
        # ending a turn that ran its tool and finished, or the skip going
        # unrecorded in the receipt.
        for changes in ({'kind': 1}, {'cwd': None}, {'branch': []},
                        {'uuid': 'invalid'}, {'session_id': 1}):
            with self.subTest(field=next(iter(changes))):
                result = await self.invoke(self.events(self.notice(**changes)))
                self.assertEqual((result['status'], result.get('failure_code')), ('completed', None))
                self.assertEqual(json.loads(result['text']), {'message': 'done', 'action': 'finish'})
                evidence = result['native_execution']
                self.assertEqual((evidence['tool_calls'], evidence['tool_results']), (1, 1))
                with open(evidence['receipt_path']) as receipt:
                    rows = [json.loads(line)['data'] for line in receipt]
                self.assertEqual(rows[1], {'type': 'native_protocol_diagnostic', 'ignored': ['native_protocol_invalid'],
                                           'event_type': 'system', 'event_subtype': 'vcs_state_changed'})


# unittest discovers imported TestCase classes too; retain only helper methods.
del _Helpers
