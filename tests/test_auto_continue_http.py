"""The host presses 继续 on stops 继续 would fix; real HTTP/SQLite, fictional CLIs."""
import json
import unittest
from unittest.mock import patch

from local_agent_coordinator import room_service
from test_native_outcome_http import CORPUS, DependencyFixture, OutcomeFixture


class LimitedDependencyFixture(DependencyFixture):
    """The selected work reply is malformed a fixed number of times, then valid."""
    def __init__(self, case, *, bad_times):
        super().__init__(case, repeated=True)
        self.bad_times = bad_times

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        if context['job']['job_phase'] == 'work' and agent == 'kimi':
            self.repeated = self.occurrences < self.bad_times
            if not self.repeated:
                self.occurrences = 1  # Past the first call, an unrepeated case is not selected.
        return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)


class AutoContinueHTTPTests(unittest.TestCase):
    def setUp(self):
        delay = patch.object(room_service, '_AUTO_CONTINUE_DELAY_SECONDS', 0)
        delay.start()
        self.addCleanup(delay.stop)

    def keep(self, fixture):
        self.addCleanup(fixture.close)
        return fixture

    def settled_after_auto(self, fixture, count):
        """Wait until the watchdog has continued `count` times and the room is idle again."""
        return fixture.wait(lambda value: sum(event['kind'] == 'objective_auto_continued' for event in value['events']) >= count
                            and value['objective']['status'] != 'running'
                            and not any(job['status'] in {'queued', 'working'} for job in value['jobs']))

    def test_a_process_crash_mid_work_is_continued_automatically_and_finishes(self):
        # Catches: a native call that exited nonzero stopped the objective until
        # the user pressed 继续, though 继续 alone recovers it.
        fixture = self.keep(OutcomeFixture({'native_case': 'nonzero'}, bad_phase='work'))
        fixture.release_peer.set()
        fixture.create()
        final = self.settled_after_auto(fixture, 1)
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertTrue(any(event['kind'] == 'objective_stopped' for event in final['events']))
        self.assertEqual(sum(event['kind'] == 'objective_auto_continued' for event in final['events']), 1)
        self.assertTrue(all(task['status'] in {'accepted', 'self_checked'} for task in final['team']['tasks']))

    def test_a_crash_that_repeats_without_progress_is_continued_once_then_left_for_the_user(self):
        # Catches: the automatic continue looping on the same failure forever.
        fixture = self.keep(OutcomeFixture({'native_case': 'nonzero'}, bad_phase='work', repeated=True))
        fixture.release_peer.set()
        fixture.create()
        value = self.settled_after_auto(fixture, 1)
        calls = len(fixture.calls)
        later = fixture.wait(lambda snapshot: True)
        for _ in range(3):
            later = fixture.wait(lambda snapshot, seen=later['events'][-1]['seq']: snapshot['events'][-1]['seq'] >= seen)
        import time
        time.sleep(5)  # Two more watchdog passes.
        later = fixture.read()
        self.assertNotEqual(later['objective']['status'], 'complete')
        self.assertEqual(sum(event['kind'] == 'objective_auto_continued' for event in later['events']), 1)
        self.assertEqual(len(fixture.calls), calls)
        self.assertFalse(any(job['status'] in {'queued', 'working'} for job in later['jobs']))

    def test_a_task_blocked_on_a_repeated_bad_reply_is_reopened_and_finishes(self):
        # Catches: the host's own block on a repeated malformed reply was shown
        # as a question for the user.
        case = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
        fixture = self.keep(LimitedDependencyFixture(case, bad_times=2))
        fixture.create()
        final = self.settled_after_auto(fixture, 1)
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertTrue(any(event['kind'] == 'native_response_blocked' for event in final['events']))
        self.assertTrue(any(event['kind'] == 'task_blocks_resumed' for event in final['events']))
        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))

    def test_a_reply_that_stays_bad_is_reopened_once_then_left_for_the_user(self):
        case = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
        fixture = self.keep(DependencyFixture(case, repeated=True))
        fixture.create()
        value = self.settled_after_auto(fixture, 1)
        calls = len(fixture.calls)
        import time
        time.sleep(5)  # Two more watchdog passes.
        later = fixture.read()
        self.assertEqual(later['objective']['status'], 'needs_user')
        self.assertEqual(sum(event['kind'] == 'task_blocks_resumed' for event in later['events']), 1)
        self.assertEqual(len(fixture.calls), calls)


if __name__ == '__main__':
    unittest.main()
