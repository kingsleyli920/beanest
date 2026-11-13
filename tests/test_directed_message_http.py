"""Directed supplements over HTTP; group workflow and native tools are synthetic."""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_long_team_result_http as http
import test_native_help_repair_http as repair
import test_native_room_service as native
from test_team_store import proposal


class DirectedMessageHTTPTests(unittest.TestCase):
    request = http.LongTeamResultHTTPTests.request
    setUp = http.LongTeamResultHTTPTests.setUp
    settled = http.LongTeamResultHTTPTests.settled

    def tearDown(self):
        for release in getattr(self, "releases", {}).values():
            release.set()
        http.LongTeamResultHTTPTests.tearDown(self)

    def handler(self, agent, context, cwd):
        async def reply():
            phase, task, board = context["job"]["job_phase"], context.get("task"), context["team"]
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands = [proposal("A"), proposal("B"), {"type": "claim", "task_id": "A"}]
                elif agent == "codex" and board["tasks"] and board["tasks"][1]["owner"] is None:
                    commands = [{"type": "claim", "task_id": "B"}]
                return native.native_reply(context, [*commands, {"type": "plan_ready"}])
            if phase == "work":
                self.working[agent].set()
                while not self.releases[agent].is_set():
                    await asyncio.sleep(0.005)
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Owned result for independent review."}])
            if phase == "user":
                self.user_calls.append((agent, context))
                return native.native_reply(context)
            if phase == "review":
                return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Exact owned result checked."}])
            if phase == "checkpoint":
                ask = getattr(self, "ask", False)
                self.ask = False
                return native.native_reply(context, [{"type": "checkpoint", "decision": "needs_user" if ask else "complete",
                    "summary": "Results checked; publication still requires the user." if ask else "Results independently checked.",
                    "next_goal": None, "scope_reference": context["objective"]["scope_reference"],
                    "question": "Do you approve publication?" if ask else None}])
            if phase == "checkpoint_review":
                return native.native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "The bound assessment is supported.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            self.fail(phase)
        return reply()

    def create_held_work(self, *, wait=True):
        """Reusable isolated UI fixture: explicit registry, held synthetic workers."""
        self.working = {name: threading.Event() for name in ("claude", "codex")}
        self.releases = {name: threading.Event() for name in self.working}
        self.user_calls = []
        code, value = self.request("POST", "/api/rooms", {"goal": "Produce and review both owned results.", "target": "claude", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": ["claude", "codex"], "work_mode": "build",
            "runtime_settings": {"permission": "full_access", "members": {}}, "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, value)
        if wait:
            for event in self.working.values():
                self.assertTrue(event.wait(3))
        return self.request("GET", "/api/rooms/" + value["room"]["id"])[1]

    @staticmethod
    def body(snapshot, *, target="claude", text="Please check the specific evidence in your owned result."):
        run = snapshot["current_run"]
        return {"text": text, "target": target, "request_id": str(uuid4()), "intent": "continue",
                "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}}

    def wait_for(self, room, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = self.request("GET", "/api/rooms/" + room)[1]
            if predicate(value):
                return value
            time.sleep(0.005)
        self.fail("Temporary directed-message condition did not settle")

    def test_busy_target_only_records_the_directed_supplement_without_an_extra_peer_call(self):
        snapshot = self.create_held_work()
        room, before = snapshot["room"]["id"], len(self.native.calls)
        body = self.body(snapshot)
        code, accepted = self.request("POST", f"/api/rooms/{room}/messages", body)
        self.assertEqual(code, 200, accepted)
        self.assertEqual(len(self.native.calls), before)
        messages = [event for event in accepted["events"] if event["kind"] == "message" and event.get("text") == body["text"]]
        self.assertEqual([event["recipient"] for event in messages], ["claude"])
        self.assertEqual(self.request("POST", f"/api/rooms/{room}/messages/receipt", body)[0], 200)
        self.assertEqual(self.request("POST", f"/api/rooms/{room}/messages", {**body, "target": "codex"})[0], 409)
        for release in self.releases.values():
            release.set()
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(self.user_calls, [])
        # Shared history remains visible, with the true addressee retained.
        review = next(call for call in self.native.calls if call["agent"] == "codex" and call["context"]["job"]["job_phase"] == "review")
        update = next(update for update in review["context"]["current_user_updates"] if update["text"] == body["text"])
        self.assertEqual(update["recipient"], "claude")

    def test_idle_target_receives_one_call_while_the_other_peer_keeps_its_existing_work(self):
        snapshot = self.create_held_work()
        room = snapshot["room"]["id"]
        self.releases["claude"].set()
        snapshot = self.wait_for(room, lambda value: value["team"]["tasks"][0]["status"] == "review"
            and not any(job["status"] in {"queued", "working"} and job["provider"] == "claude" for job in value["jobs"]))
        before = len(self.native.calls)
        body = self.body(snapshot)
        code, value = self.request("POST", f"/api/rooms/{room}/messages", body)
        self.assertEqual(code, 200, value)
        self.wait_for(room, lambda _: len(self.user_calls) == 1)
        self.assertEqual([(call["agent"], call["context"]["job"]["job_phase"]) for call in self.native.calls[before:]], [("claude", "user")])
        self.assertEqual(self.user_calls[0][1]["job"]["text"], body["text"])
        self.assertTrue(any(job["provider"] == "codex" and job["status"] == "working" for job in value["jobs"]))
        self.releases["codex"].set()
        self.assertEqual(self.settled(room)["objective"]["status"], "complete")

    def test_queued_target_uses_its_existing_job_without_adding_a_broadcast_job(self):
        released, queued = threading.Event(), threading.Event()
        original = self.service._guard_availability
        async def guard(members):
            if asyncio.current_task().get_coro().__name__ == "_work":
                queued.set()
                while not released.is_set():
                    await asyncio.sleep(0.005)
            return await original(members)
        with patch.object(self.service, "_guard_availability", new=guard):
            snapshot = self.create_held_work(wait=False)
            self.assertTrue(queued.wait(3))
            room, count = snapshot["room"]["id"], len(snapshot["jobs"])
            body = self.body(snapshot, target="codex")
            try:
                code, value = self.request("POST", f"/api/rooms/{room}/messages", body)
                self.assertEqual(code, 200, value)
                self.assertEqual(len(value["jobs"]), count)
                self.assertEqual(len(self.native.calls), 0)
                self.assertEqual(next(event for event in reversed(value["events"]) if event["kind"] == "message")["recipient"], "codex")
            finally:
                released.set()
                for release in self.releases.values():
                    release.set()
            self.assertEqual(self.settled(room)["objective"]["status"], "complete")
        self.assertEqual(self.user_calls, [])

    def test_directed_resume_preserves_recipient_but_resumes_the_existing_whole_workflow(self):
        snapshot = self.create_held_work()
        room = snapshot["room"]["id"]
        self.assertEqual(self.request("POST", f"/api/rooms/{room}/pause", {"request_id": str(uuid4()), "objective_id": snapshot["objective"]["id"]})[0], 200)
        for release in self.releases.values():
            release.set()
        paused = self.settled(room)
        self.assertEqual(paused["objective"]["status"], "paused")
        body = self.body(paused, target="codex", text="Continue using the existing owners and evidence.")
        code, accepted = self.request("POST", f"/api/rooms/{room}/messages", body)
        self.assertEqual(code, 200, accepted)
        self.assertEqual(next(event for event in reversed(accepted["events"]) if event.get("text") == body["text"])["recipient"], "codex")
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual([(task["id"], task["owner"], task["assignment_epoch"]) for task in final["team"]["tasks"]],
                         [(task["id"], task["owner"], task["assignment_epoch"]) for task in paused["team"]["tasks"]])
        self.assertEqual(self.user_calls, [])

    def test_directed_answer_keeps_real_decision_recipient_and_does_not_change_permissions(self):
        self.ask = True
        snapshot = self.create_held_work()
        room = snapshot["room"]["id"]
        for release in self.releases.values():
            release.set()
        waiting = self.settled(room)
        self.assertEqual(waiting["objective"]["status"], "needs_user")
        body = self.body(waiting, target="codex", text="Do not publish. Finish with the current local evidence only.")
        code, accepted = self.request("POST", f"/api/rooms/{room}/messages", body)
        self.assertEqual(code, 200, accepted)
        decision = next(event for event in reversed(accepted["events"]) if event["kind"] == "objective_decision")
        self.assertEqual((decision["text"], decision["recipient"]), (body["text"], "codex"))
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(final["room"]["runtime_settings"], waiting["room"]["runtime_settings"])
        # Answering may start the existing planner's next stage; targeting the
        # answer must not add an unrelated user-phase peer reply or permission.
        self.assertEqual(self.user_calls, [])
        self.assertTrue(all(call["permission"] == "full_access" for call in self.native.calls))


class DirectedNativeRecoveryHTTPTests(unittest.TestCase):
    request = http.LongTeamResultHTTPTests.request
    setUp = repair.NativeHelpRepairHTTPTests.setUp
    tearDown = http.LongTeamResultHTTPTests.tearDown
    settled = http.LongTeamResultHTTPTests.settled
    handler = repair.NativeHelpRepairHTTPTests.handler

    def test_directed_recovery_starts_only_the_new_target_and_preserves_old_repair_evidence(self):
        failed = repair.NativeHelpRepairHTTPTests.create(self, legacy=True)
        room = failed["room"]["id"]
        old_run = failed["current_run"]["id"]
        old_rows = [(job["id"], job["status"], job["error"]) for job in failed["jobs"]]
        old_bytes = {path: (self.old_helper_cwd / path).read_bytes() for path in ("src/a.txt", "src/b.txt")}
        offset = len(self.native.calls)

        def directed(agent, context, cwd):
            phase, task = context['job']['job_phase'], context.get('task')
            commands = []
            if phase in {'plan', 'coordinate'}:
                if not context['team']['tasks']:
                    commands = [proposal('DIRECTED', paths=['new-response.md']),
                                {'type': 'claim', 'task_id': 'DIRECTED'}, {'type': 'plan_ready'}]
            elif phase == 'work':
                (cwd / 'new-response.md').write_text('New addressed evidence note.\n')
                commands = [{'type': 'complete', 'task_id': task['id'], 'result': 'Saved the newly requested note.'}]
            elif phase == 'review':
                commands = [{'type': 'review', 'task_id': task['id'], 'verdict': 'accept', 'reason': 'Read the new note.'}]
            elif phase == 'checkpoint':
                commands = [{'type': 'checkpoint', 'decision': 'complete', 'summary': 'New note checked.',
                             'next_goal': None, 'scope_reference': context['objective']['scope_reference'], 'question': None}]
            elif phase == 'checkpoint_review':
                commands = [{'type': 'checkpoint_review', 'verdict': 'accept', 'reason': 'Bound note checked.',
                             'proposal_digest': context['objective']['checkpoint_proposal']['proposal_digest']}]
            return native.native_reply(context, commands)
        self.native.handler = directed
        body = DirectedMessageHTTPTests.body(failed, target='claude', text='Write a separate new-response.md evidence note.')
        code, accepted = self.request('POST', f'/api/rooms/{room}/messages', body)
        self.assertEqual(code, 200, accepted)
        final = self.settled(room)
        self.assertEqual(final['objective']['status'], 'complete', final['team'])
        self.assertEqual(final['objective']['continuation']['kind'], 'directed_user_request')
        self.assertEqual(final['objective']['original_goal'], body['text'])
        self.assertEqual({task['id'] for task in final['team']['tasks']}, {'DIRECTED'})
        self.assertEqual({call['agent'] for call in self.native.calls[offset:]
                          if call['context']['job']['job_phase'] in {'plan', 'coordinate'}}, {'claude'})
        self.assertEqual(self.inspections, [])
        self.assertEqual({path: (self.old_helper_cwd / path).read_bytes() for path in old_bytes}, old_bytes)
        self.assertEqual([(job['id'], job['status'], job['error']) for job in final['jobs'][:len(old_rows)]], old_rows)
        archived = self.service.store.read_run(room, old_run)
        self.assertEqual([(task['id'], task['status'], task['result_version']) for task in archived['team']['tasks']],
                         [(task['id'], task['status'], task['result_version']) for task in failed['team']['tasks']])
