"""Fictional review presentation over actual host HTTP/SQLite and ACP stdio."""
import json
import unittest

from local_agent_coordinator import native_adapters
from test_kimi_acp import SID
from test_kimi_fenced_http import FencedACPFixture
from test_native_room_service import native_reply
from test_team_store import proposal


LABEL_PREFIX = '审阅完成，按[检查项]核对；嵌套标签[条目[门禁]]的结论为[通过]。\n\n'


class ReviewPreambleFixture(FencedACPFixture):
    def __init__(self, case):
        super().__init__()
        self.case = case
        self.injected = None
        self.review_calls = []

    def reply(self, agent, context, cwd):
        if context['job']['job_phase'] in {'plan', 'coordinate'}:
            tasks = context['team']['tasks']
            if not tasks and agent == 'kimi':
                commands = [proposal('T1', paths=[]), proposal('T2', paths=[]), {'type': 'claim', 'task_id': 'T1'}]
            elif agent == 'claude' and any(item['id'] == 'T2' and item['owner'] is None for item in tasks):
                commands = [{'type': 'claim', 'task_id': 'T2'}]
            else:
                commands = []
            return native_reply(context, commands + [{'type': 'plan_ready'}])
        return super().reply(agent, context, cwd)

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        selected = agent == 'kimi' and context['job']['job_phase'] == 'review'
        if selected:
            self.review_calls.append({'job_id': context['job']['id'], 'input_sid': session_id,
                                      'permission': options['permission']})
        if not selected or self.injected is not None:
            return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)
        response = self.reply(agent, context, cwd)
        prefix = LABEL_PREFIX
        if self.case == 'ambiguous_object':
            prefix = '先前记录 {"note":1}。\n\n'
        if self.case == 'invalid_schema':
            response.pop('state')
        final = prefix + json.dumps(response, ensure_ascii=False)
        self.injected = {'job_id': context['job']['id'], 'raw': final}
        (self.root / 'scenario.json').write_text(json.dumps({
            'session_id': session_id or SID, 'case': 'completed', 'final': final, 'no_tools': True}), encoding='utf-8')
        result = await native_adapters.invoke_native(agent, prompt, cwd, session_id=session_id, **options)
        self.injected['result'] = result
        return result


class ReviewPreambleHTTPTests(unittest.TestCase):
    def check_case(self, case, *, rejected):
        fixture = ReviewPreambleFixture(case)
        self.addCleanup(fixture.close)
        fixture.create()
        final = fixture.settled()
        selected = fixture.injected
        result = selected['result']
        self.assertEqual((result['status'], result.get('failure_code')), ('completed', None))
        self.assertEqual(result['text'], selected['raw'])
        proof = result['native_execution']
        self.assertEqual((proof['exit_code'], proof['cleanup_confirmed']), (0, True))
        self.assertEqual(proof['tool_calls'], proof['tool_results'])
        self.assertEqual(final['objective']['status'], 'complete')
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))
        old = next(job for job in final['jobs'] if job['id'] == selected['job_id'])
        if rejected:
            self.assertEqual((old['status'], old['error']), ('failed', 'invalid_agent_response'))
            event = next(event for event in final['events'] if event['kind'] == 'format_correction_finished'
                         and event.get('correction_of') == old['id'])
            correction = next(job for job in final['jobs'] if job['id'] == event['job_id'])
            self.assertEqual(correction['status'], 'completed')
            self.assertEqual(len(fixture.review_calls), 2)
            self.assertEqual(fixture.review_calls[1]['input_sid'], result['session_id'])
        else:
            self.assertEqual((old['status'], old['error']), ('completed', None))
            self.assertEqual(len(fixture.review_calls), 1)
            self.assertFalse(any(event['kind'] == 'format_correction_queued' for event in final['events']))
        self.assertTrue(all(call['permission'] == 'full_access' for call in fixture.review_calls))
        self.assertFalse(any(member['identity_uncertain'] for member in final['members']))
        trace = [json.loads(line) for line in (fixture.root / 'trace.jsonl').read_text().splitlines()]
        self.assertNotIn('session/cancel', [row.get('method') for row in trace])

    def test_non_json_inline_and_nested_square_labels_allow_the_valid_review(self):
        self.check_case('labels', rejected=False)

    def test_json_object_in_prefix_is_still_ambiguous_and_rejected(self):
        self.check_case('ambiguous_object', rejected=True)

    def test_allowed_presentation_does_not_bypass_business_schema(self):
        self.check_case('invalid_schema', rejected=True)


if __name__ == '__main__':
    unittest.main()
