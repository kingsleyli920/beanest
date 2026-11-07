"""The host presses 继续 on stops 继续 would fix; real HTTP/SQLite, fictional CLIs."""
import json
import unittest
from unittest.mock import patch

from local_agent_coordinator import room_service
from test_native_outcome_http import CORPUS, DependencyFixture, OutcomeFixture
from test_native_room_service import native_reply


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


class ProgressOnlyFixture(LimitedDependencyFixture):
    """The T1 owner replies "still running" with no command a fixed number of times."""
    def __init__(self, case, *, progress_times):
        super().__init__(case, bad_times=0)
        self.progress_times = progress_times
        self.progress_replies = 0
        self.work_prompts = []

    def reply(self, agent, context, cwd):
        if context['job']['job_phase'] == 'work' and context['task']['id'] == 'T1':
            self.work_prompts.append(context)
            if self.progress_replies < self.progress_times:
                self.progress_replies += 1
                reply = native_reply(context, [])
                reply['message'] = 'The full test run is still going in the background; I will report when it finishes.'
                reply['state'] = 'working'
                return reply
        return super().reply(agent, context, cwd)


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


    def test_a_progress_only_work_reply_goes_back_to_its_owner_and_finishes(self):
        # Catches: a turn ending with "tests still running" blocking the task as
        # team_result_missing and showing the user a decision card.
        case = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
        fixture = self.keep(ProgressOnlyFixture(case, progress_times=1))
        fixture.create()
        final = fixture.settled()
        kinds = [event['kind'] for event in final['events']]
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(kinds.count('unfinished_work_resumed'), 1)
        self.assertNotIn('objective_decision_required', kinds)
        self.assertNotIn('objective_auto_continued', kinds)
        self.assertEqual(len(fixture.work_prompts), 2)
        self.assertIn('Processes started in that call were stopped', fixture.work_prompts[1]['task']['help_result'])

    def test_a_reply_that_never_finishes_is_resumed_a_bounded_number_of_times(self):
        # Catches: resuming a progress-only owner forever when nothing changes.
        case = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
        fixture = self.keep(ProgressOnlyFixture(case, progress_times=99))
        fixture.create()
        self.settled_after_auto(fixture, 1)
        import time
        time.sleep(5)  # Two more watchdog passes.
        later = fixture.read()
        self.assertNotEqual(later['objective']['status'], 'complete')
        self.assertEqual(sum(event['kind'] == 'unfinished_work_resumed' for event in later['events']), 2)
        self.assertEqual(fixture.progress_replies, 4)
        self.assertFalse(any(job['status'] in {'queued', 'working'} for job in later['jobs']))

    def test_cleanup_recorded_too_early_at_shutdown_is_confirmed_from_the_calls_own_receipt(self):
        # Catches: quitting the app mid-call recorded cleanup as unconfirmed
        # before the adapter wrote its receipt, leaving continue unavailable.
        case = next(c for c in CORPUS['cases'] if c['id'] == 'illegal_json')
        fixture = self.keep(LimitedDependencyFixture(case, bad_times=0))
        fixture.create()
        fixture.settled()
        service, room = fixture.service, fixture.room_id
        kimi = [job for job in service.store.snapshot(room)['jobs'] if job['provider'] == 'kimi']
        last, earlier = kimi[-1], kimi[0]

        def interrupt(job):
            with service.store._connection(write=True) as db:
                db.execute("UPDATE jobs SET status='interrupted' WHERE id=?", (job['id'],))
                db.execute("DELETE FROM events WHERE json_extract(value_json,'$.kind')='job_cleanup' "
                           "AND json_extract(value_json,'$.job_id')=?", (job['id'],))
            service.store.record_job_cleanup(job['id'], confirmed=False)

        def confirmed(job):
            service._late_cleanup_checks.clear()
            service._confirm_late_cleanup(room, service.store.snapshot(room))
            return any(event['kind'] == 'job_cleanup' and event['job_id'] == job['id'] and event['cleanup_confirmed'] is True
                       for event in service.store.snapshot(room)['events'])

        interrupt(earlier)
        self.assertFalse(confirmed(earlier), "A reused session's receipts cannot be told apart by session alone.")
        session = next(event['session_id'] for event in service.store.snapshot(room)['events']
                       if event['kind'] == 'native_session_cleanup_identity' and event['job_id'] == last['id'])
        receipts = [path for path in (service.workspace.parent / 'native-receipts' / room).iterdir()
                    if json.loads(path.read_text().splitlines()[0]).get('session_id') == session]
        own = [path for path in receipts if path.stat().st_mtime >= next(
            event['created_at'] for event in service.store.snapshot(room)['events']
            if event['kind'] == 'job_claimed' and event['job_id'] == last['id'])]
        self.assertEqual(len(own), 1)
        original = own[0].read_text()
        # Remove the proof before the call is marked interrupted: the server's
        # own watchdog runs the same check every 2 s and would otherwise
        # confirm cleanup from the intact receipt in between.
        own[0].write_text(original.replace('"cleanup_confirmed": true', '"cleanup_confirmed": false', 1))
        self.assertNotEqual(own[0].read_text(), original)
        interrupt(last)
        self.assertFalse(confirmed(last), "A receipt without cleanup proof keeps the scene.")
        own[0].write_text(original)
        self.assertTrue(confirmed(last))

if __name__ == '__main__':
    unittest.main()
