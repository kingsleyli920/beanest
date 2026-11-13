"""Planning readiness repair over real HTTP/SQLite and fictional Kimi ACP."""
import asyncio
import hashlib
import json
import threading
import time
import unittest
from uuid import uuid4

from test_kimi_fenced_http import FencedACPFixture
from test_native_room_service import native_reply
from test_team_store import proposal
from wait_support import poll


class ReadinessFixture(FencedACPFixture):
    def __init__(self, *, repair):
        super().__init__()
        self.repair = repair
        # Record only a digest of the fictional instruction received by ACP,
        # proving the actual session/prompt carries the queued job text.
        script = self.executable.read_text()
        marker = '        text = params["prompt"][0]["text"]'
        assert marker in script
        script = script.replace(marker, marker + '\n        context = json.JSONDecoder().raw_decode(text.split("CONTEXT DATA:\\n", 1)[1])[0]'
                                + '\n        summary["job_text_sha256"] = hashlib.sha256(context["job"]["text"].encode()).hexdigest()')
        self.executable.write_text(script)
        self.observed = []
        self.confirmation_held = threading.Event()
        self.confirm = threading.Event()
        self.work_started = set()
        self.skip_ready_job = None

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        phase = context['job']['job_phase']
        record = {'agent': agent, 'phase': phase, 'prompt': prompt, 'context': context,
                  'permission': options['permission']}
        self.observed.append(record)
        if phase == 'plan':
            # The host must launch all planning peers without waiting for the
            # plan author's reply; the fixture assumes no ordering among them.
            while len({item['agent'] for item in self.observed if item['phase'] == 'plan'}) < 3:
                await asyncio.sleep(.005)
        if agent == 'kimi' and phase == 'coordinate':
            text = context['job']['text']
            awaiting_repair = self.repair and 'The shared plan is stalled' not in text
            if awaiting_repair:
                self.skip_ready_job = context['job']['id']
            else:
                self.confirmation_held.set()
                while not self.confirm.is_set():
                    await asyncio.sleep(.005)
        if phase == 'work':
            self.work_started.add(context['task']['id'])
            await asyncio.Event().wait()
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)

    def reply(self, agent, context, cwd):
        phase = context['job']['job_phase']
        if phase in {'plan', 'coordinate'}:
            tasks = context['team']['tasks']
            if agent == 'kimi' and not tasks:
                # The author's first reply proposes without confirming, so a confirmation is still missing.
                commands = [proposal('T1', paths=[]), proposal('T2', paths=[]),
                            proposal('T3', paths=[], dependencies=['T1', 'T2'])]
            elif agent == 'kimi' and context['job']['id'] == self.skip_ready_job:
                commands = []
            else:
                commands = [{'type': 'plan_ready'}]
            return native_reply(context, commands)
        return super().reply(agent, context, cwd)

    def create(self):
        code, value = self.request('POST', '/api/rooms', {
            'goal': 'Coordinate two independent fictional tasks before their dependent task.',
            'target': 'kimi', 'request_id': str(uuid4()), 'mode': 'native_project',
            'project_id': self.project_id, 'participants': ['kimi', 'claude', 'codex'], 'work_mode': 'build',
            'runtime_settings': {'permission': 'full_access', 'members': {}},
            'execution_policy': {'mode': 'autonomous'}})
        if code != 200:
            raise AssertionError(value)
        self.room_id = value['room']['id']
        return value

    def read(self):
        code, value = self.request('GET', '/api/rooms/' + self.room_id)
        if code != 200:
            raise AssertionError(value)
        return value

    def wait(self, predicate):
        return poll(self.read, predicate, timeout=12, describe=lambda value: {'team': value['team'], 'jobs': value['jobs']})


class CoordinationReadinessHTTPTests(unittest.TestCase):
    def check_readiness(self, *, repair):
        fixture = ReadinessFixture(repair=repair)
        self.addCleanup(fixture.close)
        fixture.create()
        held = fixture.wait(lambda value: fixture.confirmation_held.is_set())
        team = held['team']
        self.assertEqual(team['phase'], 'planning')
        # Nobody owns a task while planning; the pool hands them out once the plan is confirmed.
        self.assertEqual({task['id']: task['owner'] for task in team['tasks']}, {'T1': None, 'T2': None, 'T3': None})
        members = {item['id']: item for item in team['members']}
        self.assertFalse(members['kimi']['plan_ready'])
        self.assertTrue(members['claude']['plan_ready'])
        self.assertTrue(members['codex']['plan_ready'])
        self.assertFalse(any(item['phase'] == 'work' for item in fixture.observed))
        correction = [item for item in fixture.observed if item['agent'] == 'kimi' and item['phase'] == 'coordinate'][-1]
        text = correction['context']['job']['text']
        self.assertIn(json.dumps(text, ensure_ascii=False), correction['prompt'])
        self.assertIn('plan_ready', text)
        self.assertIn('Unconfirmed members:', text)
        self.assertIn('Tasks are not claimed', text)
        self.assertNotIn('Unclaimed task IDs', text)
        self.assertIn('Your only missing action is the command {"type":"plan_ready"}', text)
        if repair:
            self.assertIn('The shared plan is stalled', text)
        self.assertEqual(correction['context']['team']['revision'], team['revision'])
        self.assertEqual({item['agent'] for item in fixture.observed if item['phase'] == 'plan'},
                         {'kimi', 'claude', 'codex'})
        if repair:
            self.assertTrue(any(event['kind'] == 'coordination_repair_queued' for event in held['events']))
        fixture.confirm.set()
        ready = fixture.wait(lambda value: fixture.work_started == {'T1', 'T2'})
        self.assertEqual(ready['team']['phase'], 'working')
        self.assertEqual(ready['team']['revision'], team['revision'])
        self.assertTrue(all(item['plan_ready'] for item in ready['team']['members']))
        # Free partners in room order take the two ready tasks; T3 waits for both in the pool.
        self.assertEqual({task['id']: task['owner'] for task in ready['team']['tasks']},
                         {'T1': 'kimi', 'T2': 'claude', 'T3': None})
        work_jobs = [job for job in ready['jobs'] if job['kind'] == 'team_work' and job['status'] in {'queued', 'working'}]
        self.assertEqual({job['provider'] for job in work_jobs}, {'kimi', 'claude'})
        self.assertEqual(len(work_jobs), 2)
        self.assertNotIn('T3', fixture.work_started)
        trace = [json.loads(line) for line in (fixture.root / 'trace.jsonl').read_text().splitlines()]
        self.assertIn(hashlib.sha256(text.encode()).hexdigest(),
                      [row.get('job_text_sha256') for row in trace if row.get('method') == 'session/prompt'])
        self.assertTrue(all(item.get('status') == 'completed' and item.get('cleanup_confirmed')
                            for item in fixture.native_calls if item['agent'] == 'kimi'))
        self.assertTrue(all(item['permission'] == 'full_access' for item in fixture.observed))
        code, cancelled = fixture.request('POST', '/api/rooms/' + fixture.room_id + '/cancel', {})
        self.assertEqual(code, 200)
        self.assertTrue(cancelled['cancellation_confirmed'])

    def test_ordinary_coordination_requests_missing_readiness_before_independent_work(self):
        self.check_readiness(repair=False)

    def test_repair_coordination_requests_missing_readiness_before_independent_work(self):
        self.check_readiness(repair=True)


if __name__ == '__main__':
    unittest.main()
