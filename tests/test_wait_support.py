"""Catches: a scale that stops applying, a typo'd scale passing silently, a poll that burns its budget."""
import asyncio
import os
import time
import unittest
from unittest.mock import patch

from wait_support import SCALE_VARIABLE, poll, poll_async, scaled, wait_scale, within


class WaitScaleTests(unittest.TestCase):
    def test_default_leaves_budgets_alone(self):
        with patch.dict(os.environ):
            os.environ.pop(SCALE_VARIABLE, None)
            self.assertEqual(wait_scale(), 1.0)
            self.assertEqual(scaled(3), 3.0)

    def test_variable_multiplies_budgets(self):
        with patch.dict(os.environ, {SCALE_VARIABLE: "3"}):
            self.assertEqual(scaled(2), 6.0)

    def test_bad_values_fail_loudly(self):
        for raw in ("fast", "0.5", "0", "-2", "nan", "inf", ""):
            with self.subTest(raw=raw), patch.dict(os.environ, {SCALE_VARIABLE: raw}):
                with self.assertRaisesRegex(ValueError, SCALE_VARIABLE):
                    scaled(1)


class PollTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(SCALE_VARIABLE, None)

    def test_returns_the_value_as_soon_as_the_condition_holds(self):
        values = iter([0, 0, 7, 8])
        started = time.monotonic()
        self.assertEqual(poll(lambda: next(values), timeout=30), 7)
        self.assertLess(time.monotonic() - started, 5)

    def test_separate_condition_returns_the_read_value(self):
        self.assertEqual(poll(lambda: {"phase": "done"}, lambda value: value["phase"] == "done"), {"phase": "done"})

    def test_timeout_fails_the_test_and_carries_the_description(self):
        with self.assertRaisesRegex(AssertionError, "stuck at waiting"):
            poll(lambda: "waiting", lambda value: False, timeout=0.02, describe=lambda value: "stuck at " + value)

    def test_checks_at_least_once_even_with_a_zero_budget(self):
        calls = []
        def read():
            calls.append(1)
            return True
        self.assertTrue(poll(read, timeout=0, interval=0))
        self.assertEqual(len(calls), 1)

    def test_a_condition_that_turns_true_during_the_budget_is_seen(self):
        ready = iter([False, True])
        self.assertTrue(poll(lambda: next(ready), timeout=0.05, interval=0.001))

    def test_scale_extends_the_real_budget(self):
        with patch.dict(os.environ, {SCALE_VARIABLE: "4"}):
            started = time.monotonic()
            with self.assertRaises(AssertionError):
                poll(lambda: False, timeout=0.05, interval=0.005)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)


class AsyncPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_async_lets_other_tasks_run(self):
        seen = []
        async def producer():
            await asyncio.sleep(0.01)
            seen.append(1)
        task = asyncio.create_task(producer())
        self.assertEqual(await poll_async(lambda: seen, timeout=5), [1])
        await task

    async def test_poll_async_timeout_fails_the_test(self):
        with self.assertRaises(AssertionError):
            await poll_async(lambda: False, timeout=0.01)

    async def test_within_returns_the_result_and_times_out(self):
        async def value():
            return 5
        self.assertEqual(await within(value(), 1), 5)
        with self.assertRaises(asyncio.TimeoutError):
            await within(asyncio.Event().wait(), 0.01)


if __name__ == "__main__":
    unittest.main()
