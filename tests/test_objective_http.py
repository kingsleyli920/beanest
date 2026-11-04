"""Real loopback API checks with fake provider calls, never native models."""
import time
import unittest
from urllib.parse import urlencode

import test_autonomous_objectives as objective_fixtures
from test_autonomous_objectives import POLICY
import test_team_runs_http as http_fixtures


class ObjectiveHTTPTests(unittest.TestCase):
    setUp = http_fixtures.TeamRunHTTPTests.setUp
    tearDown = http_fixtures.TeamRunHTTPTests.tearDown
    request = http_fixtures.TeamRunHTTPTests.request

    def configure(self, *, question=False):
        self.rounds = 1
        def handler(agent, context):
            result = objective_fixtures.ObjectiveIntegrationTests.handler(self, agent, context)
            if question and context["job"]["job_phase"] == "checkpoint" and context["objective"]["stage"] == 1:
                result["commands"][0].update(decision="needs_user", question="Choose the remaining value.", next_goal="Finish requested item.")
            return result
        self.provider.handler = handler

    def wait(self, room_id, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            code, snapshot = self.request("GET", "/api/rooms/" + room_id)
            self.assertEqual(code, 200)
            if predicate(snapshot):
                return snapshot
            time.sleep(0.005)
        self.fail("Objective fixture did not reach expected state")

    def create_objective(self):
        code, created = self.request("POST", "/api/rooms", {"goal": "Deliver the bounded checked item.", "target": "claude",
            "request_id": "objective-http", "mode": "python_artifacts", "execution_policy": POLICY})
        self.assertEqual(code, 200)
        return created["room"]["id"]

    def test_capability_user_scope_and_strict_historical_file_binding(self):
        self.configure()
        code, status = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertIs(status["capabilities"]["execution_policy"], True)
        self.assertIs(status["autonomous_work_active"], False)
        code, result = self.request("POST", "/api/mcp/rooms", {"goal": "No model call", "target": "claude",
            "request_id": "forbidden", "execution_policy": POLICY}, mcp=True)
        self.assertEqual((code, result["error"]), (403, "user_execution_policy_required"))
        self.assertEqual(len(self.provider.calls), 0)
        room = self.create_objective()
        snapshot = self.wait(room, lambda item: item["objective"]["status"] == "complete" and item["current_run"]["can_accept"])
        file = snapshot["objective"]["delivery"]["files"][0]
        query = {key: file[key] for key in ("run_id", "revision", "digest", "path", "sha256")}
        count = len(self.provider.calls)
        code, result = self.request("GET", f"/api/rooms/{room}/file?" + urlencode(query))
        self.assertEqual(code, 200)
        self.assertEqual(result["content"], "VALUE = 1\n")
        code, _ = self.request("GET", f"/api/mcp/rooms/{room}/file?" + urlencode(query), mcp=True)
        self.assertEqual(code, 200)
        code, result = self.request("GET", f"/api/rooms/{room}/file?" + urlencode({**query, "sha256": "0" * 64}))
        self.assertEqual((code, result["error"]), (409, "objective_binding_changed"))
        for action in ("objective", "continue", "pause", "resume", "answer"):
            code, result = self.request("POST", f"/api/mcp/rooms/{room}/{action}", {}, mcp=True)
            self.assertEqual((code, result["error"]), (403, "user_execution_policy_required"))
        self.assertEqual(len(self.provider.calls), count)

    def test_answer_keeps_objective_binding_and_rejects_budget_reset_route(self):
        self.configure(question=True)
        room = self.create_objective()
        before = self.wait(room, lambda item: item["objective"]["status"] == "needs_user" and item["objective"]["can_answer"])
        objective = before["objective"]
        binding = objective["next_step"]["binding"]
        code, result = self.request("POST", f"/api/rooms/{room}/runs", {**binding, "text": "Use 2.", "target": "all",
            "request_id": "wrong-reset", "retry": False, "execution_policy": POLICY})
        self.assertEqual((code, result["error"]), (409, "objective_answer_required"))
        payload = {**binding, "request_id": "answer", "objective_id": objective["id"], "text": "Use 2."}
        code, _ = self.request("POST", f"/api/rooms/{room}/answer", payload)
        self.assertEqual(code, 200)
        after = self.wait(room, lambda item: item["objective"]["status"] == "complete" and item["current_run"]["can_accept"])
        code, replay = self.request("POST", f"/api/rooms/{room}/answer", payload)
        self.assertEqual(code, 200)
        self.assertEqual(replay["objective"]["id"], objective["id"])
        self.assertEqual(after["objective"]["deadline"], objective["deadline"])
        self.assertEqual(after["objective"]["stage"], 2)
        self.assertGreater(after["objective"]["budget"]["used"], objective["budget"]["used"])

