"""Actual loopback routes with fake native tools and real private workspace IO."""
import time
import unittest

import test_team_runs_http as http_fixtures
import test_native_room_service as native_fixtures
from test_folder_service import FolderProvider, response
from test_team_store import proposal
from wait_support import poll


class NativeRoomHTTPTests(unittest.TestCase):
    setUp = http_fixtures.TeamRunHTTPTests.setUp
    tearDown = http_fixtures.TeamRunHTTPTests.tearDown
    request = http_fixtures.TeamRunHTTPTests.request
    handler = native_fixtures.NativeRoomServiceTests.handler

    def configure(self):
        source = self.root / "native-source"
        source.mkdir()
        (source / "main.py").write_text("VALUE = 1\n")
        (source / "notes.md").write_text("Original notes.\n")
        self.native = native_fixtures.NativeTransport()
        self.native.handler = self.handler
        self.service.native_invoke = self.native
        code, value = self.request("POST", "/api/projects", {"name": "Native example", "path": str(source), "selection_kind": "folder"})
        self.assertEqual(code, 200)
        return source, value["project"]["id"]

    def wait(self, room_id, predicate):
        def read():
            code, result = self.request("GET", "/api/rooms/" + room_id)
            self.assertEqual(code, 200)
            return result
        return poll(read, predicate, timeout=8, describe=lambda result: "Native HTTP room did not settle")

    def test_natural_chat_continuation_is_user_only_bound_atomic_and_idempotent(self):
        _, project_id = self.configure()
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Update main.py and preserve notes.", "target": "claude", "request_id": "natural-http-create",
            "mode": "native_project", "project_id": project_id, "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200)
        room_id = created["room"]["id"]
        before = self.wait(room_id, lambda value: value["current_run"]["can_reassess_objective"])
        self.assertTrue(before["continuation"]["can_send"])
        self.assertEqual(set(before["continuation"]), {"can_send", "reason", "binding", "recipients"})
        update = "Confirm the existing result still satisfies the original request; keep the publication restriction."
        payload = {"text": update, "target": "all", "request_id": "natural-http-send", "intent": "continue",
                   "binding": before["continuation"]["binding"]}
        route = f"/api/rooms/{room_id}/messages"
        self.assertEqual(self.request("POST", f"/api/mcp/rooms/{room_id}/messages", payload, mcp=True)[0], 403)
        self.assertEqual(self.request("POST", route, {**payload, "runtime_settings": {"permission": "full_access"}})[0], 400)
        self.assertEqual(self.request("POST", route, {**payload, "binding": {**payload["binding"], "digest": "0" * 64}})[0], 409)
        count = len(self.native.calls)
        code, started = self.request("POST", route, payload)
        self.assertEqual(code, 200, started)
        final = self.wait(room_id, lambda value: value["objective"]["status"] == "complete"
                          and value["current_run"]["can_reassess_objective"])
        self.assertEqual(final["room"]["id"], room_id)
        self.assertNotEqual(final["objective"]["id"], before["objective"]["id"])
        self.assertEqual(self.native.calls[count]["context"]["current_user_updates"][-1]["text"], update)
        self.assertEqual(self.native.calls[count]["context"]["objective"]["continuation"]["user_request"], update)
        self.assertEqual(sum(event.get("sender") == "user" and event.get("text") == update for event in final["events"]), 1)
        calls = len(self.native.calls)
        self.assertEqual(self.request("POST", route, payload)[0], 200)
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(self.request("POST", route, {**payload, "text": "Different follow-up."})[0], 409)

    def test_native_permission_models_and_efforts_cross_actual_http_dispatch(self):
        source, project_id = self.configure()
        self.service._runtime_catalogs.update({agent: {"state": "ready", "models": [{"id": "example-" + agent,
            "name": "Fixture model", "efforts": ["high", "max"]}]} for agent in ("claude", "codex")})
        settings = {"permission": "full_access", "members": {
            "claude": {"model": "example-claude", "effort": "high"}, "codex": {"model": "example-codex", "effort": "max"}}}
        code, status = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(status["capabilities"]["native_project"])
        payload = {"goal": "Update the existing value and preserve notes.", "target": "claude", "request_id": "native-http",
                   "mode": "native_project", "project_id": project_id, "participants": ["claude", "codex"], "work_mode": "build",
                   "runtime_settings": settings, "execution_policy": {"mode": "autonomous", "max_calls": None, "max_seconds": None}}
        code, denied = self.request("POST", "/api/mcp/rooms", payload, mcp=True)
        self.assertEqual(code, 403)
        self.assertIn(denied["error"], {"user_execution_policy_required", "user_native_permission_required", "user_runtime_settings_required"})
        self.assertEqual(self.native.calls, [])
        code, created = self.request("POST", "/api/rooms", payload)
        self.assertEqual(code, 200)
        result = self.wait(created["room"]["id"], lambda snapshot: snapshot["objective"]["status"] == "complete")
        self.assertEqual(result["collaboration"]["outcome"]["validation"]["scope"], "native_review")
        self.assertTrue(all(call["runtime_settings"] == settings["members"][call["agent"]] for call in self.native.calls))
        self.assertTrue(all(call["timeout"] is None and call["permission"] == "full_access" for call in self.native.calls))
        self.assertEqual((source / "main.py").read_text(), "VALUE = 1\n")

    def test_reassess_objective_is_strict_user_only_and_preserves_same_goal_ledger(self):
        _, project_id = self.configure()
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Update main.py and preserve notes.", "target": "claude", "request_id": "reassess-http-create",
            "mode": "native_project", "project_id": project_id, "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous", "max_calls": None, "max_seconds": None}})
        self.assertEqual(code, 200)
        room_id = created["room"]["id"]
        before = self.wait(room_id, lambda value: value["current_run"]["can_reassess_objective"])
        payload = {"request_id": "reassess-http-request", "run_id": before["current_run"]["id"],
                   "revision": before["current_run"]["revision"], "digest": before["current_run"]["digest"]}
        code, _ = self.request("POST", f"/api/mcp/rooms/{room_id}/reassess-objective", payload, mcp=True)
        self.assertEqual(code, 403)
        code, _ = self.request("POST", f"/api/rooms/{room_id}/reassess-objective", {**payload, "execution_policy": {}})
        self.assertEqual(code, 400)
        code, _ = self.request("POST", f"/api/rooms/{room_id}/reassess-objective", {**payload, "digest": "0" * 64})
        self.assertEqual(code, 409)
        count = len(self.native.calls)
        code, started = self.request("POST", f"/api/rooms/{room_id}/reassess-objective", payload)
        self.assertEqual(code, 200)
        self.assertNotEqual(started["current_run"]["id"], before["current_run"]["id"])
        final = self.wait(room_id, lambda value: value["objective"]["status"] == "complete"
                          and value["current_run"]["can_reassess_objective"])
        self.assertEqual(final["objective"]["id"], before["objective"]["id"])
        self.assertEqual(final["objective"]["deadline"], before["objective"]["deadline"])
        self.assertEqual(final["objective"]["created_at"], before["objective"]["created_at"])
        self.assertEqual(final["room"]["runtime_settings"], before["room"]["runtime_settings"])
        self.assertEqual([call["context"]["job"]["job_phase"] for call in self.native.calls[count:]],
                         ["checkpoint", "checkpoint_review"])
        completed_calls = len(self.native.calls)
        code, replay = self.request("POST", f"/api/rooms/{room_id}/reassess-objective", payload)
        self.assertEqual(code, 200)
        self.assertEqual(replay["current_run"]["id"], final["current_run"]["id"])
        self.assertEqual(len(self.native.calls), completed_calls)

    def test_handoff_is_strict_user_only_exact_bound_and_idempotent(self):
        source, project_id = self.configure()
        old = FolderProvider()
        def handler(agent, context):
            commands = []
            if context["job"]["job_phase"] == "work":
                commands = [{"type": "complete", "task_id": "T1", "result": "The old static attempt omitted its files."}]
            elif not context["team"]["tasks"] and agent == "claude":
                commands = [proposal("T1", paths=["main.py", "notes.md"], capability="python"),
                            {"type": "claim", "task_id": "T1"}, {"type": "plan_ready"}]
            elif context["team"]["tasks"]:
                commands = [{"type": "plan_ready"}]
            return response(context, commands=commands)
        old.handler = handler
        self.service.invoke = old
        code, created = self.request("POST", "/api/rooms", {"goal": "Update the existing value and preserve notes.", "target": "claude",
            "request_id": "old-capped-http", "mode": "project_folder", "project_id": project_id,
            "work_mode": "build", "execution_policy": {"mode": "autonomous", "max_calls": 3}})
        self.assertEqual(code, 200)
        room_id = created["room"]["id"]
        before = self.wait(room_id, lambda snapshot: snapshot["current_run"]["can_native_handoff"])
        current = before["current_run"]
        payload = {"run_id": current["id"], "revision": current["revision"], "digest": current["digest"], "request_id": "native-handoff-http",
            "execution_policy": {"mode": "autonomous"}, "runtime_settings": {"permission": "full_access", "members": {}}}
        code, denied = self.request("POST", f"/api/mcp/rooms/{room_id}/native-handoff", payload, mcp=True)
        self.assertEqual((code, denied["error"]), (403, "user_native_permission_required"))
        for invalid in ({**payload, "goal": "untrusted replacement"}, {**payload, "revision": None}, {**payload, "runtime_settings": None}):
            self.assertEqual(self.request("POST", f"/api/rooms/{room_id}/native-handoff", invalid)[0], 400)
        code, denied = self.request("POST", f"/api/rooms/{room_id}/native-handoff", {**payload, "digest": "0" * 64})
        self.assertEqual((code, denied["error"]), (409, "team_run_conflict"))
        self.assertEqual(self.native.calls, [])
        code, result = self.request("POST", f"/api/rooms/{room_id}/native-handoff", payload)
        self.assertEqual(code, 200)
        final = self.wait(result["room"]["id"], lambda snapshot: snapshot["objective"]["status"] == "complete")
        self.assertNotEqual(final["room"]["id"], room_id)
        self.assertEqual(final["native_handoff"], {"source_room_id": room_id, "source_run_id": current["id"], "revision": current["revision"], "digest": current["digest"]})
        self.assertFalse(any(call["context"]["job"]["job_phase"] in {"plan", "coordinate"} for call in self.native.calls))
        count = len(self.native.calls)
        code, repeated = self.request("POST", f"/api/rooms/{room_id}/native-handoff", payload)
        self.assertEqual(code, 200)
        self.assertEqual(repeated["room"]["id"], final["room"]["id"])
        self.assertEqual(len(self.native.calls), count)
        self.assertEqual((source / "main.py").read_text(), "VALUE = 1\n")

    def test_explicit_recover_native_is_strict_user_only_and_uses_new_session(self):
        source, project_id = self.configure()
        original = self.handler
        def failure(agent, context, cwd):
            result = original(agent, context, cwd)
            if context["job"]["job_phase"] == "review":
                self.native.failure = {"status": "failed", "failure_code": "native_protocol_invalid"}
            return result
        self.native.handler = failure
        code, created = self.request("POST", "/api/rooms", {"goal": "Update the existing value and preserve notes.",
            "target": "claude", "request_id": "recover-http-create", "mode": "native_project", "project_id": project_id,
            "work_mode": "build", "execution_policy": {"mode": "autonomous"}, "runtime_settings": {"permission": "full_access", "members": {}}})
        self.assertEqual(code, 200)
        room_id = created["room"]["id"]
        before = self.wait(room_id, lambda snapshot: snapshot["current_run"]["can_recover_native"])
        payload = {"request_id": "recover-native-http", "run_id": before["current_run"]["id"],
                   "revision": before["current_run"]["revision"], "digest": before["current_run"]["digest"],
                   "user_message": "Continue from the saved result; preserve the original publication restriction."}
        code, denied = self.request("POST", f"/api/mcp/rooms/{room_id}/recover-native", payload, mcp=True)
        self.assertEqual((code, denied["error"]), (403, "user_native_permission_required"))
        self.assertEqual(self.request("POST", f"/api/rooms/{room_id}/recover-native", {**payload, "runtime_settings": {}})[0], 400)
        self.assertEqual(self.request("POST", f"/api/rooms/{room_id}/recover-native", {**payload, "user_message": None})[0], 400)
        self.assertEqual(self.request("POST", f"/api/rooms/{room_id}/recover-native", {**payload, "digest": "0" * 64})[0], 409)
        self.native.failure = None
        self.native.handler = self.handler
        calls = len(self.native.calls)
        code, result = self.request("POST", f"/api/rooms/{room_id}/recover-native", payload)
        self.assertEqual(code, 200)
        final = self.wait(room_id, lambda snapshot: snapshot["objective"]["status"] == "complete")
        self.assertEqual(final["room"]["id"], room_id)
        self.assertNotEqual(final["current_run"]["id"], payload["run_id"])
        resumed = self.native.calls[calls:]
        self.assertEqual(resumed[0]["context"]["job"]["job_phase"], "review")
        self.assertIsNone(resumed[0]["session_id"])
        self.assertEqual(resumed[0]["context"]["current_user_updates"][-1]["text"], payload["user_message"])
        self.assertFalse(any(call["context"]["job"]["job_phase"] in {"plan", "coordinate", "work"} for call in resumed))
        self.assertEqual(self.request("POST", f"/api/rooms/{room_id}/recover-native", payload)[0], 200)
        self.assertEqual(self.request("POST", f"/api/rooms/{room_id}/recover-native", {**payload, "user_message": "Changed requirement"})[0], 409)
        replay = self.request("GET", f"/api/rooms/{room_id}")[1]
        self.assertEqual(sum(event["kind"] == "message" and event.get("text") == payload["user_message"] for event in replay["events"]), 1)
        self.assertEqual((source / "main.py").read_text(), "VALUE = 1\n")
