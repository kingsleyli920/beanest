"""Actual loopback HTTP boundary; only provider inference is replaced by a fake."""
import asyncio
import unittest
import test_objective_http as fixtures
import test_autonomous_objectives as objective_fixtures


class ContinuousObjectiveHTTPTests(unittest.TestCase):
    setUp = fixtures.ObjectiveHTTPTests.setUp
    tearDown = fixtures.ObjectiveHTTPTests.tearDown
    request = fixtures.ObjectiveHTTPTests.request
    wait = fixtures.ObjectiveHTTPTests.wait

    def configure(self, rounds=1, *, checkpoint_latency=0):
        self.rounds = rounds
        async def handler(agent, context):
            if checkpoint_latency and context['job']['job_phase'] == 'checkpoint':
                await asyncio.sleep(checkpoint_latency)
            return objective_fixtures.ObjectiveIntegrationTests.handler(self, agent, context)
        self.provider.handler = handler

    def test_both_null_limits_are_advertised_and_progress_beyond_legacy_stages(self):
        # Nine checkpoints alone exceed the shared single-stage four-second
        # wait. Keep real asynchronous provider latency in this HTTP regression.
        self.configure(rounds=9, checkpoint_latency=0.5)
        code,status = self.request('GET','/api/status')
        self.assertEqual(code,200)
        self.assertTrue(status['capabilities']['continuous_objective'])
        code,created = self.request('POST','/api/rooms',{'goal':'Deliver all nine requested items.', 'target':'claude',
            'request_id':'continuous-http','mode':'python_artifacts','work_mode':'build',
            'execution_policy':{'mode':'autonomous','max_seconds':None,'max_calls':None}})
        self.assertEqual(code,200)
        result = self.wait(created['room']['id'],lambda item:item['objective']['status']=='complete',
                           timeout=2 * self.rounds)
        self.assertEqual(result['objective']['stage'],9)
        self.assertGreater(result['objective']['budget']['used'],32)
        self.assertIsNone(result['budget']['deadline'])
        self.assertIsNone(result['objective']['budget']['limit'])
        self.assertTrue(all(run['accepted_at'] is None for run in result['runs']))

    def test_restart_is_user_only_exactly_bound_idempotent_and_carries_task(self):
        self.configure()
        code,created = self.request('POST','/api/rooms',{'goal':'Deliver the unfinished requested file.','target':'claude',
            'request_id':'limited-http','mode':'python_artifacts','work_mode':'build',
            'execution_policy':{'mode':'autonomous','max_calls':3}})
        self.assertEqual(code,200)
        room_id = created['room']['id']
        before = self.wait(room_id,lambda item:item['current_run'].get('can_restart_objective'))
        payload = {key:before['current_run'][key] for key in ('revision','digest')}
        payload.update(run_id=before['current_run']['id'],request_id='restart-http',execution_policy={'mode':'autonomous'})
        count = len(self.provider.calls)
        code,result = self.request('POST',f'/api/mcp/rooms/{room_id}/restart-objective',payload,mcp=True)
        self.assertEqual((code,result['error']),(403,'user_execution_policy_required'))
        code,result = self.request('POST',f'/api/rooms/{room_id}/restart-objective',{**payload,'digest':'0'*64})
        self.assertEqual(code,409)
        self.assertEqual(len(self.provider.calls),count)
        code,_ = self.request('POST',f'/api/rooms/{room_id}/restart-objective',payload)
        self.assertEqual(code,200)
        after = self.wait(room_id,lambda item:item['objective']['status']=='complete')
        self.assertNotEqual(after['objective']['id'],before['objective']['id'])
        self.assertEqual(after['team']['tasks'][0]['id'],before['team']['tasks'][0]['id'])
        self.assertEqual(after['team']['tasks'][0]['owner'],before['team']['tasks'][0]['owner'])
        self.assertFalse(any(call['context']['job']['job_phase'] in {'plan','coordinate'} for call in self.provider.calls[count:]))
        code,replay = self.request('POST',f'/api/rooms/{room_id}/restart-objective',payload)
        self.assertEqual(code,200)
        self.assertEqual(replay['objective']['id'],after['objective']['id'])
