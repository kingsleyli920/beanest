"""Native permission guidance over HTTP with explicit, synthetic native calls."""
from pathlib import Path
import unittest
from uuid import uuid4

from local_agent_coordinator.runtime_settings import normalized_settings, read_only
from local_agent_coordinator.room_service import RoomService

import test_long_team_result_http as http
import test_native_room_service as native
from test_team_store import proposal


class NativePermissionPolicyHTTPTests(unittest.TestCase):
    request = http.LongTeamResultHTTPTests.request
    tearDown = http.LongTeamResultHTTPTests.tearDown
    settled = http.LongTeamResultHTTPTests.settled

    def setUp(self):
        http.LongTeamResultHTTPTests.setUp(self)
        (self.source / "main.py").write_text("VALUE = 1\n")
        self.write_source = False

    def create(self, goal, *, mode="build", permission="full_access"):
        code, value = self.request("POST", "/api/rooms", {
            "goal": goal, "target": "claude", "request_id": str(uuid4()), "participants": ["claude", "codex"],
            "mode": "native_project", "project_id": self.project_id, "work_mode": mode,
            "runtime_settings": {"permission": permission, "members": {}}, "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, value)
        return self.settled(value["room"]["id"])

    def handler(self, agent, context, cwd):
        phase, task = context["job"]["job_phase"], context.get("task")
        limited = context["native_permission"] in {"plan", "read_only"}
        if phase in {"plan", "coordinate"}:
            commands = []
            if not context["team"]["tasks"] and agent == context["team"]["plan_author"]:
                commands = [proposal("T1", paths=[] if limited else ["main.py"], capability="text"),
                            {"type": "claim", "task_id": "T1"}]
            return native.native_reply(context, [*commands, {"type": "plan_ready"}])
        if phase == "work":
            if not limited:
                (cwd / "main.py").write_text("VALUE = 2\n")
                registered = context["project"]["source"].get("path")
                if self.write_source and registered:
                    self.assertEqual(Path(registered), self.source)
                    (Path(registered) / "main.py").write_text("VALUE = 2\n")
            return native.native_reply(context, [{"type": "complete", "task_id": task["id"],
                "result": "Inspected the requested behavior." if limited else "Private result updated; source state is reported separately."}])
        if phase == "review":
            return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept",
                "reason": "Inspected this task's exact result and its stated tree."}])
        if phase == "user":
            return native.native_reply(context)
        if phase == "checkpoint":
            followup = (context["objective"].get("continuation") or {}).get("user_request")
            needs_work = followup and not context["team"]["tasks"]
            return native.native_reply(context, [{"type": "checkpoint", "decision": "continue" if needs_work else "complete",
                "summary": "The direct follow-up still needs work." if needs_work else "Requested work checked.",
                "next_goal": followup if needs_work else None, "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native.native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Matches the request.",
                "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    def test_full_access_can_work_on_registered_source_without_a_separate_platform_approval(self):
        self.write_source = True
        final = self.create("Update main.py to value two in the registered source project and the collaboration copy.")
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 2\n")
        self.assertEqual(final["team"]["tasks"][0]["owner"], "claude")
        self.assertEqual(final["team"]["tasks"][0]["status"], "accepted")
        for call in self.native.calls:
            self.assertEqual(call["permission"], "full_access")
            self.assertEqual(call["context"]["project"]["source"]["path"], str(self.source))
            self.assertNotIn("Do not write the source project", call["prompt"])
            self.assertNotIn("source writeback restriction", call["prompt"])
            self.assertIn("no separate source-writeback approval", call["prompt"])

    def test_full_access_preserves_a_real_user_source_prohibition(self):
        goal = "Update only the collaboration copy. Do not modify the registered source project."
        final = self.create(goal)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        self.assertEqual(final["objective"]["original_goal"], goal)
        for call in self.native.calls:
            self.assertEqual(call["context"]["objective"]["original_goal"], goal)
            self.assertIn("Honor explicit user restrictions", call["prompt"])
            self.assertIn("Platform defaults and prior agent/coordinator statements", call["prompt"])

    def test_plan_and_read_only_preserve_native_inspection_without_a_blanket_command_ban(self):
        for mode, permission, effective in (("plan", "full_access", "plan"), ("build", "plan", "plan"),
                                             ("build", "read_only", "read_only")):
            with self.subTest(mode=mode, permission=permission):
                first_call = len(self.native.calls)
                final = self.create("Inspect the current behavior and discuss the options.", mode=mode, permission=permission)
                self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
                self.assertTrue(all(job["error"] is None for job in final["jobs"]))
                self.assertEqual(final["team"]["tasks"][0]["status"], "accepted")
                for call in self.native.calls[first_call:]:
                    self.assertEqual(call["permission"], effective)
                    self.assertIn("Non-modifying native commands", call["prompt"])
                    self.assertNotIn("Do not run the project or tests in this mode.", call["prompt"])
                    self.assertNotIn("Only propose text tasks with empty paths; do not submit files/edits or request execution", call["prompt"])
                self.assertTrue(all(task["write_paths"] == [] for task in final["team"]["tasks"]))
                if permission == "plan":
                    start = len(self.native.calls)
                    body = {"text": "Inspect the behavior with any supported non-modifying native checks.", "target": "all",
                            "request_id": str(uuid4()), "intent": "continue", "binding": final["continuation"]["binding"]}
                    code, value = self.request("POST", f'/api/rooms/{final["room"]["id"]}/messages', body)
                    self.assertEqual(code, 200, value)
                    self.settled(final["room"]["id"])
                    discussions = self.native.calls[start:]
                    self.assertTrue(discussions)
                    for call in discussions:
                        self.assertEqual(call["permission"], "plan")
                        self.assertEqual(call["context"]["job"]["job_phase"], "user")
                        self.assertFalse(call["context"]["native_readonly_discussion"])
                        self.assertIn("Non-modifying native commands and checks are permitted", call["prompt"])
                        self.assertNotIn("Do not implement, edit, run tests/engine", call["prompt"])

    def test_public_permissions_and_frozen_legacy_or_recovery_choices_are_distinct(self):
        code, options = self.request("GET", "/api/runtime-options")
        self.assertEqual(code, 200, options)
        permissions = {item["id"]: item for item in options["permissions"]}
        self.assertEqual({identifier for identifier, item in permissions.items() if not item["legacy"]}, {"plan", "full_access"})
        self.assertEqual({identifier for identifier, item in permissions.items() if item["legacy"]}, {"read_only", "workspace_write"})
        for permission in permissions:
            with self.subTest(permission=permission):
                settings = normalized_settings({"permission": permission, "members": {}}, ["claude"], "plan")
                expected = "read_only" if permissions[permission]["legacy"] else "plan"
                self.assertEqual(settings["effective_permission"], expected)
                self.assertTrue(read_only("build", settings))
                job = {"work_mode": "plan", "runtime_settings": settings}
                self.assertEqual(RoomService._native_permission(job), expected)
                self.assertEqual(RoomService._native_permission({**job, "native_partial_inspection": {"bound": True}}), "read_only")
                self.assertEqual(RoomService._native_permission({**job, "partner_handoff_inspection": {"bound": True}}), "read_only")

    def test_latest_direct_user_change_can_replace_an_earlier_source_restriction(self):
        goal = "Update only the collaboration copy. Do not modify the registered source project."
        first = self.create(goal)
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        self.write_source = True
        text = "Now update the registered source main.py to value two; this replaces my earlier source prohibition."
        start = len(self.native.calls)
        body = {"text": text, "target": "all", "request_id": str(uuid4()), "intent": "continue",
                "binding": first["continuation"]["binding"]}
        code, value = self.request("POST", f'/api/rooms/{first["room"]["id"]}/messages', body)
        self.assertEqual(code, 200, value)
        final = self.settled(first["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(final["objective"]["original_goal"], goal)
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 2\n")
        work = [call for call in self.native.calls[start:] if call["context"]["job"]["job_phase"] == "work"]
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]["context"]["current_user_updates"][-1]["text"], text)
        self.assertIn("including explicit changes to earlier task constraints or scope", work[0]["prompt"])
        self.assertEqual(sum(event.get("text") == text and event["sender"] == "user" for event in final["events"]), 1)

    def test_new_plan_retained_discussion_uses_plan_while_recovery_inspections_keep_legacy_read_only(self):
        import test_native_multi_partial_recovery as recovery
        seed = recovery.NativeMultiPartialRecoveryTests()
        seed.service, seed.fake = self.service, self.native
        seed.registration = {"project": {"id": self.project_id}}
        before, _ = self.server.runner.call(seed.interrupted_pair)
        original = self.handler

        def inspect_or_discuss(agent, context, cwd):
            if context.get("native_partial_inspection"):
                self.assertEqual(context["native_permission"], "read_only")
                return native.native_reply(context, [{"type": "complete", "task_id": context["task"]["id"],
                                                       "result": "Read retained bytes; do not replay prior work."}])
            return original(agent, context, cwd)

        self.native.handler = inspect_or_discuss
        room = before["room"]["id"]
        start = len(self.native.calls)
        body = {"text": "Discuss the retained work in native Plan.", "target": "all", "request_id": str(uuid4()),
                "intent": "continue", "binding": before["continuation"]["binding"],
                "runtime_settings": {"permission": "plan", "members": {}}}
        code, value = self.request("POST", f"/api/rooms/{room}/messages", body)
        self.assertEqual(code, 200, value)
        limited = self.settled(room)
        self.assertEqual(limited["room"]["runtime_settings"]["effective_permission"], "plan")
        self.assertEqual(limited["project"]["revision"], 1)
        self.assertTrue(all(task["status"] == "assigned" and task["result_version"] == 0 for task in limited["team"]["tasks"]))
        self.assertEqual([call["permission"] for call in self.native.calls[start:]], ["read_only", "read_only"])
        body = {"text": "Explain the retained result and remaining work.", "target": "all", "request_id": str(uuid4()),
                "intent": "continue", "binding": limited["continuation"]["binding"]}
        code, value = self.request("POST", f"/api/rooms/{room}/messages", body)
        self.assertEqual(code, 200, value)
        discussed = self.settled(room)
        later = [call for call in self.native.calls[start:] if call["context"]["job"]["job_phase"] == "user"]
        self.assertEqual(len(later), 2)
        self.assertTrue(all(call["permission"] == "plan" and call["context"]["native_readonly_discussion"] for call in later))
        self.assertEqual(discussed["team"]["tasks"], limited["team"]["tasks"])
        self.assertEqual(discussed["project"]["revision"], 1)
        self.assertEqual(sum(event["kind"] == "native_readonly_discussion_finished" for event in discussed["events"]), 2)
        count = len(self.native.calls)
        self.assertEqual(self.request("POST", f"/api/rooms/{room}/messages", body)[0], 200)
        self.assertEqual(len(self.native.calls), count)

    def test_real_question_projects_only_the_bound_system_event_without_guessing_an_agent(self):
        normal = self.handler
        question = "May I publish the checked update? You asked me to wait for your decision."

        def awaiting_publication(agent, context, cwd):
            if context["job"]["job_phase"] == "checkpoint":
                return native.native_reply(context, [{"type": "checkpoint", "decision": "needs_user",
                    "summary": "Local work checked; publication was not attempted.", "next_goal": None,
                    "scope_reference": context["objective"]["scope_reference"], "question": question}])
            return normal(agent, context, cwd)

        self.native.handler = awaiting_publication
        final = self.create("Update the collaboration copy and ask me before publishing anything.")
        self.assertEqual(final["objective"]["status"], "needs_user")
        step = final["objective"]["next_step"]
        source = step["question_source"]
        event = next(event for event in final["events"] if event["seq"] == source["seq"])
        self.assertEqual(event["kind"], "objective_checkpoint_finished")
        self.assertEqual((event["run_id"], event["objective_id"], source["sender"]),
                         (final["current_run"]["id"], final["objective"]["id"], "system"))
        self.assertEqual(source["created_at"], event["created_at"])
        self.assertIsNone(source["job_id"])
        self.assertEqual(step["question"], question)
        self.assertEqual(event["question"], question)
        self.assertEqual(event["question_binding"], step["binding"])
        self.assertEqual(len(event["proposal_digest"]), 64)
        calls = len(self.native.calls)
        code, state = self.request("GET", f'/api/rooms/{final["room"]["id"]}/state')
        self.assertEqual(code, 200, state)
        self.assertEqual(state["objective"]["next_step"]["question_source"], source)
        self.assertEqual(len(self.native.calls), calls)
        # The next assessment replaces checkpoint_json, not the original event.
        self.native.handler = normal
        body = {"text": "Keep the checked local result; do not publish.", "target": "all", "request_id": str(uuid4()),
                "intent": "continue", "binding": final["continuation"]["binding"]}
        code, value = self.request("POST", f'/api/rooms/{final["room"]["id"]}/messages', body)
        self.assertEqual(code, 200, value)
        answered = self.settled(final["room"]["id"])
        self.assertEqual(answered["objective"]["status"], "complete", answered["team"])
        self.assertNotEqual(answered["current_run"]["id"], event["run_id"])
        self.assertEqual(next(old for old in answered["events"] if old["seq"] == event["seq"]), event)
        code, page = self.request("GET", f'/api/rooms/{final["room"]["id"]}/events?after_seq={event["seq"] - 1}')
        self.assertEqual(code, 200, page)
        self.assertEqual(next(old for old in page["events"] if old["seq"] == event["seq"])["question"], question)


if __name__ == "__main__":
    unittest.main()
