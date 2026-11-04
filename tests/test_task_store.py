from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator.task_store import StoreError, TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private" / "tasks.sqlite3"
        self.store = TaskStore(self.path, "workspace-a")

    def assert_error(self, code, action):
        with self.assertRaises(StoreError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def reserve_idle(self, request_id="request-1", *, store=None, parent=None):
        store = store or self.store
        task, _ = store.reserve("claude", "fixture prompt", request_id, parent)
        return store.finish(task["id"], session_id=str(uuid4()), status="idle")

    def race_reservations(self, *, max_tasks, max_total_turns, same_request=False):
        workers = 8
        barrier = threading.Barrier(workers)

        def reserve(index):
            store = TaskStore(self.path, "race", max_tasks=max_tasks,
                              max_total_turns=max_total_turns)
            barrier.wait(timeout=5)
            try:
                return store.reserve("codex", "parallel fixture", "same" if same_request else str(index))
            except StoreError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(reserve, range(workers)))

    def test_atomic_reservations_enforce_task_and_total_turn_budgets(self):
        for limit, tasks, turns, expected in (
            ("tasks", 3, 20, "task_budget_exceeded"),
            ("turns", 20, 3, "total_turn_budget_exceeded"),
        ):
            with self.subTest(limit=limit):
                results = self.race_reservations(max_tasks=tasks, max_total_turns=turns)
                self.assertEqual(sum(isinstance(value, tuple) for value in results), 3)
                self.assertEqual(results.count(expected), 5)
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*), SUM(turns) FROM tasks WHERE scope='race'"
                    ).fetchone(), (3, 3))
                    connection.execute("DELETE FROM tasks WHERE scope='race'")

    def test_idempotency_is_atomic_conflicts_are_safe_and_prompts_are_not_stored(self):
        results = self.race_reservations(max_tasks=1, max_total_turns=1, same_request=True)
        self.assertTrue(all(isinstance(value, tuple) for value in results))
        self.assertEqual(sum(is_new for _, is_new in results), 1)
        self.assertEqual(len({task["id"] for task, _ in results}), 1)
        store = TaskStore(self.path, "race", max_tasks=1, max_total_turns=1)
        error = self.assert_error("request_conflict", lambda: store.reserve(
            "codex", "private prompt NEVER_SAVE", "same"))
        self.assertNotIn("NEVER_SAVE", str(error))
        self.assertNotIn("parallel fixture", json.dumps(results))
        self.assertNotIn(b"parallel fixture", self.path.read_bytes())
        self.assertNotIn(b"NEVER_SAVE", self.path.read_bytes())
        for agent, timeout in (("claude", 180), ("codex", 181)):
            self.assert_error("request_conflict", lambda: store.reserve(
                agent, "parallel fixture", "same", timeout=timeout))

    def test_per_task_and_total_turn_limits_cover_replies(self):
        store = TaskStore(self.path, "turns", max_tasks=4, max_turns=2, max_total_turns=3)
        first = self.reserve_idle("first", store=store)
        second = self.reserve_idle("second", store=store)
        reply = store.begin_reply(first["id"])
        self.assertEqual((reply["turns"], reply["operation"], reply["status"]), (2, "send", "running"))
        self.assert_error("task_not_idle", lambda: store.begin_reply(first["id"]))
        store.finish(first["id"], status="idle")
        self.assert_error("turn_budget_exceeded", lambda: store.begin_reply(first["id"]))
        self.assert_error("total_turn_budget_exceeded", lambda: store.begin_reply(second["id"]))
        self.assertEqual(store.get(second["id"])["turns"], 1)

    def test_two_connections_cannot_reserve_same_idle_reply_twice(self):
        task = self.reserve_idle()
        barrier = threading.Barrier(2)

        def reply(_):
            store = TaskStore(self.path, "workspace-a")
            barrier.wait(timeout=5)
            try:
                return store.begin_reply(task["id"])
            except StoreError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reply, range(2)))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(results.count("task_not_idle"), 1)
        self.assertEqual(self.store.get(task["id"])["turns"], 2)

    def test_scope_filters_every_access_and_parent_depth_is_one(self):
        root = self.reserve_idle()
        child, _ = self.store.reserve("codex", "child fixture", "child", root["id"])
        self.assertEqual(child["parent_task_id"], root["id"])
        self.assert_error("parent_depth_exceeded", lambda: self.store.reserve(
            "claude", "grandchild", "grandchild", child["id"]))
        other = TaskStore(self.path, "workspace-b")
        self.assertEqual(other.list(), [])
        for action in (
            lambda: other.get(root["id"]),
            lambda: other.begin_reply(root["id"]),
            lambda: other.finish(root["id"], status="failed"),
            lambda: other.request_cancel(root["id"]),
            lambda: other.observe(root["id"], "idle"),
            lambda: other.history(root["id"]),
            lambda: other.record_page(root["id"], {"sessionId": root["session_id"]}),
            lambda: other.reserve("claude", "child fixture", "cross-parent", root["id"]),
            lambda: self.store.get(str(uuid4())),
            lambda: self.store.get("invalid PRIVATE_ID"),
        ):
            self.assert_error("task_not_found", action)
        own, _ = other.reserve("codex", "different scope", "request-1")
        self.assertEqual([row["id"] for row in other.list()], [own["id"]])
        self.assertEqual(len(self.store.list()), 2)

    def test_cancellation_survives_late_completion_and_observation(self):
        task, _ = self.store.reserve("codex", "fixture", "cancel")
        cancelled = self.store.request_cancel(task["id"])
        self.assertEqual(cancelled["status"], "cancel_requested")
        late = self.store.finish(task["id"], session_id="late-session", status="running")
        self.assertEqual(late["session_id"], "late-session")
        self.assertEqual(late["status"], "cancel_requested")
        self.assertIsNone(late["operation"])
        self.assertEqual(self.store.observe(task["id"], "idle")["status"], "cancel_requested")
        self.assert_error("task_cancelled", lambda: self.store.begin_reply(task["id"]))
        self.assert_error("task_cancelled", lambda: self.store.reserve(
            "claude", "child", "cancelled-parent", task["id"]))
        self.store.finish(task["id"], status="cancelled")
        self.store.finish(task["id"], status="idle")
        self.store.observe(task["id"], "running")
        state = self.store.request_cancel(task["id"])
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["cancel_requested"])

    def test_observations_cannot_clear_inflight_operations_or_terminal_states(self):
        task, _ = self.store.reserve("claude", "fixture", "observe")
        state = self.store.observe(task["id"], "idle")
        self.assertEqual((state["status"], state["provider_status"]), ("starting", "idle"))
        self.store.finish(task["id"])
        self.assertEqual(self.store.observe(task["id"], "idle")["status"], "idle")
        self.store.begin_reply(task["id"])
        self.assertEqual(self.store.observe(task["id"], "idle")["status"], "running")
        self.store.finish(task["id"], status="failed", error="provider_failed")
        self.assertEqual(self.store.observe(task["id"], "idle")["status"], "failed")
        self.assertEqual(self.store.finish(task["id"], status="running")["status"], "failed")
        second = self.reserve_idle("unknown")
        self.assertEqual(self.store.observe(second["id"], "offline")["status"], "unknown")
        self.assertEqual(self.store.observe(second["id"], "idle")["status"], "unknown")

    def test_observations_ignore_older_and_equal_sequences_across_connections(self):
        task = self.reserve_idle()
        session = task["session_id"]
        other = TaskStore(self.path, "workspace-a")
        self.store.observe(task["id"], "running", session_id=session, seq=5)
        fresh = other.observe(task["id"], "idle", session_id=session, seq=10)
        self.assertEqual(fresh["status"], "idle")
        for seq in (9, 10):
            stale = self.store.observe(task["id"], "running", session_id=session, seq=seq)
            self.assertEqual((stale["status"], stale["provider_status"]), ("idle", "idle"))
        self.assertEqual(other.observe(task["id"], "awaiting_input", session_id=session, seq=11)["status"],
                         "awaiting_input")
        self.assertNotIn("provider_seq", fresh)
        self.assertNotIn("provider_session", fresh)

    def test_observations_ignore_old_sessions_after_resume_and_restart(self):
        task = self.reserve_idle()
        old_session = task["session_id"]
        self.store.observe(task["id"], "idle", session_id=old_session, seq=50)
        self.store.finish(task["id"], session_id="new-native-session")
        reopened = TaskStore(self.path, "workspace-a")
        state = reopened.observe(task["id"], "running", session_id="new-native-session", seq=1)
        self.assertEqual(state["provider_status"], "running")
        reopened.observe(task["id"], "idle", session_id="new-native-session", seq=2)
        stale = self.store.observe(task["id"], "running", session_id=old_session, seq=1000)
        self.assertEqual((stale["status"], stale["provider_status"]), ("idle", "idle"))
        self.assertEqual(reopened.observe(task["id"], "running", session_id="new-native-session", seq=1)["status"],
                         "idle")

    def test_confirmed_cancel_clears_only_prior_unconfirmed_cancel_error(self):
        task = self.reserve_idle()
        self.store.request_cancel(task["id"])
        self.store.finish(task["id"], status="unknown", error="cancellation_unconfirmed")
        confirmed = self.store.finish(task["id"], status="cancelled")
        self.assertEqual(confirmed["status"], "cancelled")
        self.assertIsNone(confirmed["error"])
        late = self.store.observe(task["id"], "idle", session_id=task["session_id"], seq=20)
        self.assertEqual(late["status"], "cancelled")
        self.assertIsNone(late["error"])
        late_failure = self.store.finish(task["id"], status="unknown", error="cancellation_unconfirmed")
        self.assertEqual(late_failure["status"], "cancelled")
        self.assertIsNone(late_failure["error"])

    def test_uncertain_identity_is_sticky_after_restart_and_cancellation(self):
        task = self.reserve_idle()
        self.assertIs(task["identity_uncertain"], False)
        self.store.begin_reply(task["id"])
        uncertain = self.store.finish(task["id"], status="unknown", error="command_timeout",
                                      identity_uncertain=True)
        self.assertIs(uncertain["identity_uncertain"], True)
        reopened = TaskStore(self.path, "workspace-a")
        self.assertIs(reopened.get(task["id"])["identity_uncertain"], True)
        late = reopened.finish(task["id"], session_id="late-resumed-session")
        self.assertIs(late["identity_uncertain"], True)
        reopened.request_cancel(task["id"])
        closed = reopened.finish(task["id"], status="cancelled", identity_uncertain=False)
        self.assertIs(closed["identity_uncertain"], True)
        reopened.observe(task["id"], "idle")
        self.assertIs(TaskStore(self.path, "workspace-a").list()[0]["identity_uncertain"], True)
        self.assert_error("invalid_result", lambda: self.store.finish(
            task["id"], identity_uncertain="PRIVATE VALUE"))

    def test_legacy_schema_migrates_identity_uncertainty_with_false_default(self):
        task = self.reserve_idle()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE tasks DROP COLUMN identity_uncertain")
        migrated = TaskStore(self.path, "workspace-a")
        self.assertIs(migrated.get(task["id"])["identity_uncertain"], False)
        migrated.finish(task["id"], status="unknown", identity_uncertain=True)
        self.assertIs(TaskStore(self.path, "workspace-a").get(task["id"])["identity_uncertain"], True)

    def test_history_deduplicates_pages_by_session_kind_id_and_largest_seq(self):
        task = self.reserve_idle()
        session = task["session_id"]
        page = {
            "sessionId": session,
            "messages": [{"id": "message-1", "role": "assistant", "text": "partial", "seq": 1,
                          "hidden_reasoning": "DO_NOT_KEEP"}],
            "turns": [{"id": "turn-1", "status": "running", "seq": 2, "usage": "DO_NOT_KEEP"}],
            "tools": [{"id": "tool-1", "toolName": "Read", "status": "running", "seq": 3,
                       "input": "DO_NOT_KEEP", "output": "DO_NOT_KEEP"}],
            "raw": "DO_NOT_KEEP",
        }
        self.store.record_page(task["id"], page)
        page["messages"][0].update(text="complete", seq=6)
        page["turns"][0].update(status="completed", seq=7)
        page["tools"][0].update(status="completed", seq=8)
        self.store.record_page(task["id"], page)
        page["messages"][0].update(text="stale", seq=4)
        page["turns"][0].update(status="running", seq=2)
        self.store.record_page(task["id"], page)
        self.store.record_page(task["id"], page)
        self.store.finish(task["id"], session_id="resumed-session", status="idle")
        self.store.record_page(task["id"], {"sessionId": "resumed-session", "messages": [
            {"id": "message-1", "role": "assistant", "text": "followup", "seq": 1}]})
        history = TaskStore(self.path, "workspace-a").history(task["id"])
        self.assertEqual([row["text"] for row in history["messages"]], ["complete", "followup"])
        self.assertEqual(len(history["turns"]), 1)
        self.assertEqual(history["turns"][0]["status"], "completed")
        self.assertEqual(history["tools"][0]["status"], "completed")
        self.assertEqual({row["sourceSession"] for row in history["messages"]}, {session, "resumed-session"})
        self.assertNotIn("DO_NOT_KEEP", json.dumps(history))
        self.assertNotIn(b"DO_NOT_KEEP", self.path.read_bytes())
        self.assert_error("session_not_owned", lambda: self.store.record_page(
            task["id"], {"sessionId": "unrelated-session", "messages": []}))

    def test_permissions_and_safe_invalid_input_errors(self):
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = self.path.parent / "symlink.sqlite3"
        link.symlink_to(self.path)
        self.assert_error("invalid_store_path", lambda: TaskStore(link, "linked"))
        self.assert_error("invalid_request", lambda: self.store.reserve(
            "claude", "private fixture", "id", timeout=float("nan")))
        self.assert_error("invalid_budget", lambda: TaskStore(self.path, "scope", max_tasks=True))
        error = StoreError("PRIVATE RAW DIAGNOSTIC")
        self.assertEqual(error.as_dict(), {"code": "store_error"})
        self.assertNotIn("PRIVATE", str(error))

    def test_deadline_and_malformed_observations_fail_without_leaking_data(self):
        with patch("local_agent_coordinator.task_store.time.time", return_value=100):
            task = self.reserve_idle()
        with patch("local_agent_coordinator.task_store.time.time", return_value=281):
            self.assert_error("deadline_exceeded", lambda: self.store.begin_reply(task["id"]))
        self.assertEqual(self.store.get(task["id"])["turns"], 1)
        self.assert_error("invalid_result", lambda: self.store.finish(task["id"], status=[]))
        self.store.record_page(task["id"], {"sessionId": task["session_id"], "messages": [
            {"id": "bad-message", "role": [], "text": "DO_NOT_KEEP"}]})
        self.assertEqual(self.store.history(task["id"])["messages"], [])
        self.assert_error("invalid_request", lambda: self.store.reserve(
            "claude", "fixture", "huge-timeout", timeout=10**1000))


if __name__ == "__main__":
    unittest.main()
