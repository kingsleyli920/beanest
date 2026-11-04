"""Continuous policy compatibility; no service, provider or filesystem needed."""
import copy
import math
import sqlite3
import unittest

from local_agent_coordinator.execution_policy import (
    ExecutionPolicyError,
    normalized_policy,
    effective_policy,
    stage_available,
    task_finished,
)


class ExecutionPolicyTests(unittest.TestCase):
    def assert_invalid(self, value):
        with self.assertRaises(ExecutionPolicyError) as caught:
            normalized_policy(value)
        self.assertEqual(caught.exception.code, "invalid_execution_policy")

    def test_absent_policy_preserves_legacy_instead_of_enabling_autonomy(self):
        self.assertIsNone(normalized_policy(None))

    def test_legacy_modes_become_continuous_without_mutating_input(self):
        for mode in ("manual", "autonomous"):
            with self.subTest(mode=mode):
                requested = {"mode": mode}
                normalized = normalized_policy(requested)
                self.assertEqual(normalized, {
                    "mode": "autonomous", "max_seconds": None,
                    "max_calls": None,
                    "quota_failover": True,
                })
                self.assertEqual(requested, {"mode": mode})
                self.assertIsNot(normalized, requested)
                normalized["max_calls"] = 1
                self.assertIsNone(normalized_policy(requested)["max_calls"])

    def test_legacy_finite_limits_are_not_carried_into_new_work(self):
        for seconds in (7200, 14400, 28800):
            for calls in (1, 256):
                with self.subTest(seconds=seconds, calls=calls):
                    requested = {"mode": "manual", "max_seconds": seconds,
                                 "max_calls": calls, "quota_failover": False}
                    before = copy.deepcopy(requested)
                    normalized = normalized_policy(requested)
                    self.assertEqual(normalized, {"mode": "autonomous", "max_seconds": None,
                                                  "max_calls": None, "quota_failover": False})
                    self.assertEqual(requested, before)
                    self.assertIsNot(normalized, requested)
                    self.assertIsNone(normalized["max_calls"])
                    self.assertIsNone(normalized["max_seconds"])
                    self.assertIs(normalized["quota_failover"], False)

    def test_effective_policy_does_not_rewrite_exhausted_historical_policy(self):
        historical = {"mode": "manual", "max_seconds": 7200,
                      "max_calls": 64, "quota_failover": False}
        before = copy.deepcopy(historical)
        self.assertEqual(effective_policy(historical), {
            "mode": "autonomous", "max_seconds": None,
            "max_calls": None, "quota_failover": False,
        })
        self.assertEqual(historical, before)
        self.assertIsNone(effective_policy(None))

    def test_explicit_autonomous_limits_preserve_user_stopping_conditions(self):
        requested = {"mode": "autonomous", "max_seconds": 7200,
                     "max_calls": 5, "quota_failover": False}
        before = copy.deepcopy(requested)
        self.assertEqual(normalized_policy(requested), before)
        self.assertEqual(effective_policy(requested), before)
        self.assertEqual(requested, before)

    def test_stage_count_never_blocks_continuation(self):
        for policy in ({"mode": "manual"}, {"mode": "autonomous"}, None):
            for stage in (0, 8, 64, 10000):
                with self.subTest(policy=policy, stage=stage):
                    self.assertTrue(stage_available(policy, stage))

    def test_malformed_container_or_mode_always_returns_the_domain_error(self):
        for value in ([], ["autonomous"], (), "autonomous", b"manual", 1, 1.0, True):
            with self.subTest(container=repr(value)):
                self.assert_invalid(value)
        self.assert_invalid({})
        for mode in (None, True, 1, 1.0, [], {}, set(), ["manual"],
                     "", "Manual", "AUTONOMOUS", "manual ", " autonomous", "auto"):
            with self.subTest(mode=repr(mode)):
                self.assert_invalid({"mode": mode})

    def test_unknown_keys_cannot_smuggle_permissions_or_ignored_settings(self):
        for key in ("permission", "allow_shell", "runtime_settings", "enabled", "max_steps", "", 1):
            with self.subTest(key=repr(key)):
                requested = {"mode": "autonomous", key: True}
                before = copy.deepcopy(requested)
                self.assert_invalid(requested)
                self.assertEqual(requested, before)

    def test_explicit_null_is_not_a_request_for_a_default(self):
        for key in ("mode", "quota_failover"):
            with self.subTest(key=key):
                self.assert_invalid({"mode": "autonomous", key: None})

    def test_autonomous_limits_can_be_omitted_or_explicit_null(self):
        self.assertEqual(normalized_policy({"mode": "autonomous"}), normalized_policy(
            {"mode": "autonomous", "max_seconds": None, "max_calls": None}))
        for key in ("max_seconds", "max_calls"):
            self.assert_invalid({"mode": "manual", key: None})

    def test_duration_must_be_one_of_the_supported_integer_bounds(self):
        for seconds in (True, False, 7200.0, "7200", [], {}, -1, 0, 1, 3600,
                        7199, 7201, 28801, 86400, math.nan, math.inf):
            with self.subTest(seconds=repr(seconds)):
                self.assert_invalid({"mode": "manual", "max_seconds": seconds})

    def test_call_cap_rejects_bool_numeric_coercion_and_out_of_range(self):
        for calls in (True, False, 1.0, 256.0, "64", [], {}, -1, 0, 257,
                      2**100, math.nan, math.inf):
            with self.subTest(calls=repr(calls)):
                self.assert_invalid({"mode": "autonomous", "max_calls": calls})

    def test_failover_is_an_explicit_boolean_not_truthiness(self):
        for failover in (0, 1, "false", "true", "", [], {}, math.nan):
            with self.subTest(failover=repr(failover)):
                self.assert_invalid({"mode": "manual", "quota_failover": failover})


class TaskFinishedTests(unittest.TestCase):
    def test_self_checked_unblocks_without_becoming_an_independent_acceptance(self):
        for status in ("accepted", "self_checked"):
            with self.subTest(status=status):
                task = {"status": status, "owner": "claude", "reviewer": None,
                        "result": {"validation": "not_run"}}
                before = copy.deepcopy(task)
                self.assertTrue(task_finished(task))
                self.assertEqual(task, before)
                self.assertIsNone(task["reviewer"])
                self.assertEqual(task["status"], status)

    def test_only_the_two_finished_states_unblock_dependencies(self):
        for status in ("open", "assigned", "working", "review", "waiting_peer", "blocked",
                       "unknown", "interrupted", "cancelled", "completed", "done",
                       "ready_for_user", "Self_checked", "accepted ", "", None, [], {}):
            with self.subTest(status=repr(status)):
                self.assertFalse(task_finished({"status": status}))

    def test_database_rows_remain_compatible_without_dict_only_methods(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            for status, expected in (("self_checked", True), ("accepted", True), ("review", False)):
                with self.subTest(status=status):
                    row = db.execute("SELECT ? AS status", (status,)).fetchone()
                    self.assertIs(task_finished(row), expected)
                    self.assertEqual(row["status"], status)
