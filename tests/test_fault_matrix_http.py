"""Partner × phase × native failure: none of these may wait for the user.

Real HTTP/SQLite with fictional CLIs; each case fails exactly one call.
"""
import unittest
from unittest.mock import patch

from local_agent_coordinator import room_service
from test_native_outcome_http import DependencyFixture

PHASES = ("plan", "work", "review")
FAILURES = ("nonzero", "unclosed_tool", "missing_wire")


class OneFailureFixture(DependencyFixture):
    def __init__(self, phase, failure):
        super().__init__({'native_case': failure})
        self.bad_phase = phase


class FaultMatrixHTTPTests(unittest.TestCase):
    def setUp(self):
        delay = patch.object(room_service, '_AUTO_CONTINUE_DELAY_SECONDS', 0)
        delay.start()
        self.addCleanup(delay.stop)

    def test_one_native_failure_in_any_phase_finishes_without_the_user(self):
        # Catches: any single crash, unpaired tool or missing wire leaving the
        # objective stopped or asking the user instead of continuing itself.
        for phase in PHASES:
            for failure in FAILURES:
                with self.subTest(phase=phase, failure=failure):
                    fixture = OneFailureFixture(phase, failure)
                    try:
                        fixture.create()
                        final = fixture.wait(lambda value: value['objective']['status'] == 'complete'
                                             and not any(job['status'] in {'queued', 'working'} for job in value['jobs']))
                        self.assertEqual(fixture.occurrences, 1)
                        self.assertTrue(all(task['status'] == 'accepted' for task in final['team']['tasks']))
                        self.assertLessEqual(sum(event['kind'] == 'objective_auto_continued' for event in final['events']), 1)
                    finally:
                        fixture.close()


if __name__ == '__main__':
    unittest.main()
