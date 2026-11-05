"""A reviewer's typed quota failure after tool calls; fictional CLI/HTTP only."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from local_agent_coordinator import room_service
from local_agent_coordinator.room_service import RoomService
from test_kimi_work_quota_http import WorkQuotaFixture


class ReviewQuotaFixture(WorkQuotaFixture):
    """Kimi runs out of quota midway through its first review, after real tool calls."""
    def __init__(self, *, dirty=False):
        super().__init__(legacy=False)
        self.dirty = dirty
        self.failed_review = None

    async def invoke(self, agent, prompt, cwd, session_id=None, **options):
        context = json.loads(prompt.split('CONTEXT DATA:\n', 1)[1])
        selected = agent == 'kimi' and context['job']['job_phase'] == 'review' and self.failed_review is None
        if not selected:
            return await super().invoke(agent, prompt, cwd, session_id=session_id, **options)
        self.failed_review = {'job_id': context['job']['id'], 'task_id': context['task']['id']}
        self.native_case = 'work_quota'
        try:
            if self.dirty:
                # A review never publishes files; a changed private tree is kept, not discarded.
                (Path(cwd) / 'README.md').write_text('Reviewer drift.\n')
            return await super(WorkQuotaFixture, self).invoke(agent, prompt, cwd, session_id=session_id, **options)
        finally:
            self.native_case = None


class ReviewQuotaHTTPTests(unittest.TestCase):
    def setUp(self):
        delay = patch.object(room_service, '_AUTO_CONTINUE_DELAY_SECONDS', 0)
        delay.start()
        self.addCleanup(delay.stop)

    def fixture(self, **kwargs):
        value = ReviewQuotaFixture(**kwargs)
        self.addCleanup(value.close)
        return value

    def test_reviewer_quota_after_tools_with_an_unchanged_tree_retires_it_and_another_partner_reviews(self):
        # Catches: a reviewer's quota failure after tool calls was treated as
        # unconfirmed side effects and stopped the whole objective.
        fixture = self.fixture()
        fixture.create()
        final = fixture.settled()
        self.assertIsNotNone(fixture.failed_review)
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertFalse(any(event['kind'] == 'objective_stopped' for event in final['events']))
        job = next(job for job in final['jobs'] if job['id'] == fixture.failed_review['job_id'])
        self.assertEqual(job['error'], 'subscription_limit_reached')
        self.assertTrue(any(event['kind'] == 'partner_retired' and event.get('provider') == 'kimi'
                            for event in final['events']))
        tasks = {task['id']: task for task in final['team']['tasks']}
        self.assertEqual(tasks[fixture.failed_review['task_id']]['status'], 'accepted')
        self.assertNotEqual(tasks[fixture.failed_review['task_id']]['reviewer'], 'kimi')

    def test_room_an_older_release_stopped_on_a_clean_reviewer_quota_continues_on_its_own(self):
        # Catches: a room already stopped as unconfirmed side effects by an
        # older release stayed stopped with continue unavailable.
        fixture = self.fixture()
        audit = RoomService._quota_left_tree_unchanged
        audits = []

        def older_release_then_real(service, job):
            audits.append(job['id'])
            return False if len(audits) == 1 else audit(service, job)
        with patch.object(RoomService, '_quota_left_tree_unchanged', older_release_then_real):
            fixture.create()
            final = fixture.wait(lambda snapshot: snapshot['objective']['status'] == 'complete'
                                 and not any(job['status'] in {'queued', 'working'} for job in snapshot['jobs']))
        events = [event['kind'] for event in final['events']]
        self.assertIn('objective_stopped', events)
        self.assertIn('native_quota_stop_settled', events)
        job = next(job for job in final['jobs'] if job['id'] == fixture.failed_review['job_id'])
        self.assertEqual(job['error'], 'subscription_limit_reached')
        tasks = {task['id']: task for task in final['team']['tasks']}
        self.assertTrue(all(task['status'] in {'accepted', 'self_checked'} for task in tasks.values()), tasks)
        self.assertTrue(any(event['kind'] == 'partner_retired' and event.get('provider') == 'kimi'
                            and event.get('code') == 'subscription_limit_reached' for event in final['events']))

    def test_reviewer_quota_with_a_changed_private_tree_keeps_it_instead_of_retiring(self):
        # Catches: retiring on a quota failure without checking the private
        # tree, which would silently abandon a reviewer's unexplained edits.
        fixture = self.fixture(dirty=True)
        fixture.create()
        final = fixture.settled()
        job = next(job for job in final['jobs'] if job['id'] == fixture.failed_review['job_id'])
        self.assertEqual(job['error'], 'native_side_effects_unconfirmed')
        self.assertIn(final['team']['phase'], {'stopped', 'unknown'})
        state = fixture.service.store.native_workspace(fixture.room_id, 'kimi')
        self.assertEqual((Path(state['project']) / 'README.md').read_text(), 'Reviewer drift.\n')


if __name__ == '__main__':
    unittest.main()
