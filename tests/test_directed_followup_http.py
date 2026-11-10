"""Directed user followups over real HTTP/SQLite and a synthetic native transport."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import select
import signal
import sys
import threading
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator import adapters, native_adapters

from test_kimi_native_setup import configuration
from test_kimi_setup_http import SetupFixture
from test_native_room_service import NativeTransport, native_reply, no_rebalance
from test_team_runs_http import TeamRunHTTPTests as _Helpers
from test_team_store import proposal
from wait_support import poll


FOLLOWUP = "请 Claude 单独补充本次交付说明。\n保留此前成果，不重做旧任务。"
RETAINED_BYTES = b"Original authorized owner work, not yet published.\n"
RUNNING_NOTE = "Please add a brief note while Codex keeps its current work."
_FAKE_CLI_RUN = adapters._run


class DirectedFollowupHTTPTests(unittest.TestCase):
    request = _Helpers.request
    def setUp(self):
        self.fixture = SetupFixture(discover=False)
        self.addCleanup(self.fixture.close)
        self.fixture.config.write_text(configuration(), encoding="utf-8")
        self.service, self.server = self.fixture.service, self.fixture.server
        no_rebalance(self.service)
        self.root, self.port, self.source = self.fixture.root, self.fixture.port, self.fixture.source
        (self.source / "notes.md").write_text("An unchanged canonical note.\n", encoding="utf-8")
        self.native = NativeTransport()
        self.native.handler = self.handler
        self.service.native_invoke = self.native
        self.initial_run_id = None
        self.room_id = None
        self.stop_original = False
        self.hold_original = False
        self.old_work_started = threading.Event()
        self.release_original = threading.Event()
        self.followup_started = threading.Event()
        self.release_followup = threading.Event()
        self.cancelled_calls = []
        self.protocol_peer_started = threading.Event()
        self.hold_protocol_peer = False
        self.retain_original_work = False
        code, _ = self.request("POST", "/api/partners/discover", {})
        self.assertEqual(code, 200)
        code, value = self.request("POST", "/api/projects", {
            "name": "Directed followup fixture", "path": str(self.source), "selection_kind": "folder"})
        self.assertEqual(code, 200, {"error": value.get("error")})
        self.project_id = value["project"]["id"]

    def tearDown(self):
        self.release_original.set()
        self.release_followup.set()
        self.doCleanups()

    def read(self):
        code, value = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200, {"error": value.get("error")})
        return value

    def quiescent(self, snapshot):
        return (not any(job["status"] in {"queued", "working"} for job in snapshot["jobs"])
                and not any(room == self.room_id and not worker.done()
                            for (room, _), worker in list(self.service._workers.items())))

    def wait(self, predicate):
        return poll(self.read, predicate, timeout=5, describe=lambda snapshot: "Synthetic directed fixture did not settle: " + json.dumps({
            "phase": snapshot["team"]["phase"], "stop": snapshot["team"]["stop_reason"],
            "objective": snapshot["objective"]["status"],
            "jobs": [{key: job[key] for key in ("provider", "status", "error")} for job in snapshot["jobs"]]}))

    def create(self, participants=None):
        code, value = self.request("POST", "/api/rooms", {
            "goal": "Complete the two original dependent deliverables.", "target": "codex", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": participants or ["codex", "claude"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, {"error": value.get("error")})
        self.room_id = value["room"]["id"]
        return value

    def bootstrap_stopped(self):
        # A completed native plan reply fails the real JSON publication gate and
        # repeats it after the host's correction, so the room stops. Its private
        # tree is unchanged and its native identity/cleanup are known.
        self.stop_original = True
        self.create()
        stopped = self.wait(lambda value: value["team"]["phase"] == "stopped" and self.quiescent(value))
        self.assertEqual(stopped["team"]["stop_reason"], "native_json_files_forbidden")
        self.assertEqual(stopped["objective"]["status"], "stopped")
        self.assertFalse(any(member["identity_uncertain"] for member in stopped["members"]))
        self.assert_old_board(stopped)
        return stopped

    def bootstrap_completed(self):
        self.create()
        completed = self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        self.assertTrue(all(task["status"] == "accepted" for task in completed["team"]["tasks"]))
        self.assert_old_board(completed)
        return completed

    def bootstrap_running(self):
        self.hold_original = True
        self.create()
        return self.wait(lambda _: self.old_work_started.is_set())

    def bootstrap_protocol_unknown(self, *, retained_work=False):
        """Real fake-CLI failure and a real host-cancelled, owned peer call."""
        sid = str(uuid4())
        failure_phase = "work" if retained_work else "plan"
        self.retain_original_work = retained_work
        started, release = self.root / "unknown-started", self.root / "release-unknown"
        executable = self.root / "unknown-claude"
        executable.write_text(f"#!{sys.executable}\n" + f'''
import json, sys, time
from pathlib import Path
if 'status' in sys.argv:
    print(json.dumps({{'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'}}))
    raise SystemExit(0)
prompt = sys.stdin.read()
context = json.loads(prompt.split('CONTEXT DATA:\\n', 1)[1])
assert context['self'] == 'claude' and context['job']['job_phase'] == {failure_phase!r}
sid = sys.argv[sys.argv.index('--resume') + 1] if '--resume' in sys.argv else {sid!r}
if {retained_work!r}:
    assert context['task']['id'] == 'OLD1' and context['task']['write_paths'] == ['old-one.md']
    Path('old-one.md').write_bytes({RETAINED_BYTES!r})
print(json.dumps({{'type': 'system', 'subtype': 'init', 'session_id': sid}}), flush=True)
Path({str(started)!r}).write_text(context['job']['id'])
while not Path({str(release)!r}).exists():
    time.sleep(.01)
# A new system subtype is a tolerated notice; an unknown top-level type is not.
print(json.dumps({{'type': 'synthetic_future_event', 'subtype': 'synthetic_future_notice',
    'session_id': sid, 'uuid': {str(uuid4())!r}}}), flush=True)
''', encoding="utf-8")
        executable.chmod(0o700)
        self.hold_protocol_peer = True

        async def original_transport(agent, prompt, cwd, session_id=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            if agent != "claude" or context["job"]["job_phase"] != failure_phase:
                return await self.native(agent, prompt, cwd, session_id=session_id, **options)
            if self.initial_run_id is None:
                self.initial_run_id = context["job"]["run_id"]
            self.native.calls.append(dict(agent=agent, prompt=prompt, context=context, cwd=Path(cwd),
                                          session_id=session_id, **options))
            self.protocol_result = await native_adapters.invoke_native(agent, prompt, cwd, session_id=session_id, **options)
            return self.protocol_result

        self.service.native_invoke = original_transport
        try:
            with patch.object(adapters, "find_claude", return_value=str(executable)), \
                    patch.object(adapters, "_run", new=_FAKE_CLI_RUN):
                self.create(["codex", "claude", "kimi"])
                planning = self.wait(lambda value: started.exists()
                                     and any(task["id"] == "OLD2" and task["owner"] == "codex" for task in value["team"]["tasks"]))
                if retained_work:
                    state = self.service.store.native_workspace(self.room_id, "claude")
                    self.retained_path = Path(state["project"]) / "old-one.md"
                code, value = self.request("POST", f"/api/rooms/{self.room_id}/messages", {
                    "text": "Keep the original context while the current call settles.",
                    "target": "codex", "request_id": str(uuid4())})
                self.assertEqual(code, 200, {"error": value.get("error")})
                self.wait(lambda _: self.protocol_peer_started.is_set() and started.exists())
                release.write_text("emit the synthetic unknown notification\n")
                stopped = self.wait(lambda value: value["team"]["stop_reason"] == "native_protocol_unknown" and self.quiescent(value))
        finally:
            release.touch()
            self.service.native_invoke = self.native
            self.hold_protocol_peer = False
        self.assertEqual(self.protocol_result["failure_code"], "native_protocol_unknown")
        self.assertTrue(self.protocol_result["native_execution"]["cleanup_confirmed"])
        self.assertTrue(any(job["provider"] == "codex" and job["error"] == "invoke_interrupted" for job in stopped["jobs"]))
        self.assertEqual(len(self.cancelled_calls), 1)
        self.assert_old_board(stopped)
        self.assertEqual(planning["current_run"]["id"], stopped["current_run"]["id"])
        return stopped

    def assert_old_board(self, snapshot):
        tasks = {task["id"]: task for task in snapshot["team"]["tasks"]}
        self.assertEqual(set(tasks), {"OLD1", "OLD2"})
        self.assertEqual([tasks[key]["owner"] for key in ("OLD1", "OLD2")],
                         ["claude" if self.retain_original_work else "codex", "codex"])
        self.assertEqual(tasks["OLD2"]["dependencies"], ["OLD1"])

    def old_rows(self, run_id):
        with self.service.store._connection() as db:
            ids = [row[0] for row in db.execute("SELECT job_id FROM team_jobs WHERE run_id=? ORDER BY job_id", (run_id,))]
            placeholders = ",".join("?" for _ in ids)
            rows = {table: [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}", ids)]
                for table, key in (("jobs", "id"), ("team_jobs", "job_id"), ("native_executions", "job_id"))}
            rows["shared_tasks"] = [dict(row) for row in db.execute(
                "SELECT * FROM shared_tasks WHERE room_id=? AND run_id=? ORDER BY id", (self.room_id, run_id))]
            return rows

    @staticmethod
    def message_body(snapshot, text=FOLLOWUP, target="claude"):
        current = snapshot["current_run"]
        return {"text": text, "target": target, "request_id": str(uuid4()), "intent": "continue",
                "binding": {"run_id": current["id"], "revision": current["revision"], "digest": current["digest"]}}

    def begin_followup(self, snapshot, *, target="claude", text=FOLLOWUP):
        self.call_offset = len(self.native.calls)
        self.body = self.message_body(snapshot, text=text, target=target)
        code, value = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.body)
        self.assertEqual(code, 200, {"error": value.get("error")})
        if target not in {"all", "both"}:
            self.assertEqual(value["objective"]["original_goal"], text)
            self.assertEqual(value["team"]["plan_author"], target)
        return self.wait(lambda _: self.followup_started.is_set())

    def new_calls(self):
        return self.native.calls[self.call_offset:]

    async def hold_owned_protocol_peer(self, context):
        call = next(call for call in reversed(self.native.calls) if call["context"]["job"]["id"] == context["job"]["id"])
        self.assertIsNotNone(call["session_id"])
        receipt = call["receipt_dir"] / (str(uuid4()) + ".jsonl")
        receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"channel": "stdout", "data": {"type": "thread.started", "thread_id": call["session_id"]}}) + "\n")
        receipt.chmod(0o600)
        self.protocol_peer_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            self.cancelled_calls.append(context["job"]["id"])
            error.native_execution = {"permission": call["permission"], "tool_calls": 1, "tool_results": 1,
                "side_effects_possible": False, "cleanup_confirmed": True, "exit_code": -15, "receipt_path": str(receipt)}
            raise

    async def handler(self, agent, context, cwd):
        run_id, phase = context["job"]["run_id"], context["job"]["job_phase"]
        if self.initial_run_id is None:
            self.initial_run_id = run_id
        original = run_id == self.initial_run_id
        if not original:
            self.followup_started.set()
            while not self.release_followup.is_set():
                await asyncio.sleep(.005)
        tasks, task = context["team"]["tasks"], context.get("task")
        commands = []
        if original and self.hold_protocol_peer and agent == "codex" and phase == "user":
            await self.hold_owned_protocol_peer(context)
        if original and self.stop_original and agent == "claude" and phase == "plan":
            while self.room_id is None or not any(task["id"] == "OLD2" and task["owner"] == "codex"
                    for task in self.service.store.snapshot(self.room_id)["team"]["tasks"]):
                await asyncio.sleep(.005)
            reply = native_reply(context, [{"type": "plan_ready"}])
            reply["files"] = [{"path": "forbidden.md", "previous_sha256": None,
                               "content": "A JSON-only publication that the host must reject.\n"}]
            return reply
        if phase in {"plan", "coordinate"}:
            if not tasks and agent == context["team"]["plan_author"]:
                if original:
                    commands = [proposal("OLD1", paths=["old-one.md"]),
                        proposal("OLD2", dependencies=["OLD1"], paths=["old-two.md"]),
                        *([] if self.retain_original_work else [{"type": "claim", "task_id": "OLD1"}]),
                        {"type": "claim", "task_id": "OLD2"}]
                else:
                    commands = [proposal("FOLLOWUP", paths=["followup.md"]), {"type": "claim", "task_id": "FOLLOWUP"}]
            if original and self.retain_original_work and agent == "claude" and any(
                    task["id"] == "OLD1" and task["owner"] is None for task in tasks):
                commands.append({"type": "claim", "task_id": "OLD1"})
            commands.append({"type": "plan_ready"})
        elif phase == "work":
            if original and task["id"] == "OLD1":
                self.old_work_started.set()
                if self.hold_original:
                    try:
                        while not self.release_original.is_set():
                            await asyncio.sleep(.005)
                    except asyncio.CancelledError as error:
                        self.cancelled_calls.append(context["job"]["id"])
                        error.native_execution = {"cleanup_confirmed": True, "side_effects_possible": False}
                        raise
            for path in task["write_paths"]:
                (cwd / path).write_text("Synthetic owned result for " + task["id"] + ".\n", encoding="utf-8")
            commands = [{"type": "complete", "task_id": task["id"], "result": "The requested result for " + task["id"] + " is ready."}]
        elif phase == "review":
            for path in task["write_paths"]:
                self.assertEqual((cwd / path).read_text(), "Synthetic owned result for " + task["id"] + ".\n")
            commands = [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact new result."}]
        elif phase == "checkpoint":
            commands = [{"type": "checkpoint", "decision": "complete", "summary": "The requested scope is complete.",
                "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}]
        elif phase == "checkpoint_review":
            commands = [{"type": "checkpoint_review", "verdict": "accept", "reason": "The requested scope has its result.",
                "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}]
        elif phase == "user":
            commands = []
        else:
            self.fail("Unexpected synthetic phase " + phase)
        return native_reply(context, commands)

    def assert_directed_followup(self, previous, *, reviewer="codex", target="claude", text=FOLLOWUP):
        run_id = previous["current_run"]["id"]
        before = self.old_rows(run_id)
        held = self.begin_followup(previous, target=target, text=text)
        self.held_followup = held
        self.assertNotEqual(held["current_run"]["id"], run_id)
        self.assertEqual(held["objective"]["original_goal"], text)
        self.assertEqual(held["team"]["plan_author"], target)
        continuation = held["objective"]["continuation"]
        self.assertEqual((continuation["kind"], continuation["initial_recipient"], continuation["source_run_id"]),
                         ("directed_user_request", target, run_id))
        self.assertEqual(held["team"]["tasks"], [])
        self.assertEqual([(call["agent"], call["context"]["job"]["job_phase"]) for call in self.new_calls()], [(target, "plan")])
        self.assertEqual(self.new_calls()[0]["context"]["objective"]["original_goal"], text)
        self.assertEqual(self.old_rows(run_id), before)
        code, _ = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.body)
        self.assertEqual(code, 200)
        self.assertEqual(len(self.new_calls()), 1)
        self.release_followup.set()
        final = self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        self.assertEqual({task["id"] for task in final["team"]["tasks"]}, {"FOLLOWUP"})
        self.assertEqual((final["team"]["tasks"][0]["owner"], final["team"]["tasks"][0]["status"]), (target, "accepted"))
        self.assertEqual({call["agent"] for call in self.new_calls() if call["context"]["job"]["job_phase"] in {"plan", "coordinate"}}, {target})
        self.assertTrue(any(call["agent"] == reviewer and call["context"]["job"]["job_phase"] == "review" for call in self.new_calls()))
        self.assertFalse(any(call["context"].get("task", {}).get("id", "").startswith("OLD")
                             for call in self.new_calls() if call["context"].get("task")))
        self.assertEqual(self.old_rows(run_id), before)
        return final

    def test_stopped_directed_request_creates_a_new_recipient_board_without_carry(self):
        self.assert_directed_followup(self.bootstrap_stopped())

    def test_completed_directed_request_uses_exact_new_text_and_peer_review(self):
        completed = self.bootstrap_completed()
        calls = len(self.native.calls)
        code, invited = self.request("POST", f"/api/rooms/{self.room_id}/members", {
            "request_id": str(uuid4()), "participants": ["kimi"]})
        self.assertEqual(code, 200, {"error": invited.get("error")})
        self.assertEqual(invited["jobs"], completed["jobs"])
        self.assertEqual(len(self.native.calls), calls)
        # This invitation belongs to the completed OLD run. It must not widen
        # the next user's newly directed planning/coordination recipients.
        self.assert_directed_followup(invited)

    def test_current_run_explicit_invitation_adds_the_new_partner_to_directed_coordination(self):
        completed = self.bootstrap_completed()
        old_run, rows = completed["current_run"]["id"], self.old_rows(completed["current_run"]["id"])
        held = self.begin_followup(completed)
        worker = self.service._workers[(self.room_id, "claude")]
        code, invited = self.request("POST", f"/api/rooms/{self.room_id}/members", {
            "request_id": str(uuid4()), "participants": ["kimi"]})
        self.assertEqual(code, 200, {"error": invited.get("error")})
        self.wait(lambda _: any(call["agent"] == "kimi" for call in self.new_calls()))
        self.assertEqual(invited["current_run"]["id"], held["current_run"]["id"])
        self.assertIs(self.service._workers[(self.room_id, "claude")], worker)
        self.assertFalse(worker.done())
        self.assertEqual({call["agent"] for call in self.new_calls()}, {"claude", "kimi"})
        kimi = next(call for call in self.new_calls() if call["agent"] == "kimi")
        self.assertEqual(kimi["context"]["job"]["job_phase"], "plan")
        self.assertIsNone(kimi["session_id"])
        self.release_followup.set()
        final = self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        self.assertEqual({call["agent"] for call in self.new_calls()
                          if call["context"]["job"]["job_phase"] in {"plan", "coordinate"}}, {"claude", "kimi"})
        self.assertEqual(final["team"]["tasks"][0]["status"], "accepted")
        self.assertEqual(self.old_rows(old_run), rows)
        self.assertEqual(self.cancelled_calls, [])

    def test_running_directed_message_preserves_the_inflight_peer_and_old_owner(self):
        running = self.bootstrap_running()
        worker = self.service._workers[(self.room_id, "codex")]
        before = {task["id"]: (task["owner"], task["assignment_epoch"], task["dependencies"]) for task in running["team"]["tasks"]}
        offset = len(self.native.calls)
        code, response = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.message_body(running, RUNNING_NOTE))
        self.assertEqual(code, 200, {"error": response.get("error")})
        self.wait(lambda _: any(call["agent"] == "claude" and RUNNING_NOTE in call["prompt"] for call in self.native.calls[offset:]))
        current = self.read()
        self.assertEqual(current["current_run"]["id"], running["current_run"]["id"])
        self.assertIs(self.service._workers[(self.room_id, "codex")], worker)
        self.assertFalse(worker.done())
        self.assertEqual(self.cancelled_calls, [])
        self.assertEqual({task["id"]: (task["owner"], task["assignment_epoch"], task["dependencies"])
                          for task in current["team"]["tasks"]}, before)
        self.release_original.set()
        final = self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(self.cancelled_calls, [])

    def test_all_followup_retains_the_existing_team_followup_contract(self):
        completed = self.bootstrap_completed()
        run_id, before = completed["current_run"]["id"], self.old_rows(completed["current_run"]["id"])
        held = self.begin_followup(completed, target="all")
        self.assertEqual(held["team"]["plan_author"], "codex")
        self.assertEqual(held["objective"]["continuation"]["kind"], "user_followup")
        self.assertNotIn("initial_recipient", held["objective"]["continuation"])
        self.release_followup.set()
        self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        self.assertEqual({call["agent"] for call in self.new_calls()}, {"codex", "claude"})
        self.assertEqual(self.old_rows(run_id), before)

    def test_protocol_unknown_directed_recovery_freshens_only_the_recipient(self):
        unknown = self.bootstrap_protocol_unknown()
        self.assert_real_unpublished_change_rejected(unknown)
        members = {member["id"]: member for member in unknown["members"]}
        # Unknown execution does not erase confirmed owned-session identity.
        self.assertFalse(members["claude"]["identity_uncertain"])
        self.assertFalse(members["codex"]["identity_uncertain"])
        self.assertTrue(self.service.store.native_continuation_requires_recovery(self.room_id))
        self.assertTrue(any(job["status"] == "unknown" for job in unknown["jobs"]))
        with self.service.store._connection() as db:
            untouched = dict(db.execute("SELECT * FROM members WHERE room_id=? AND provider='codex'", (self.room_id,)).fetchone())
        final = self.assert_directed_followup(unknown, reviewer="codex")
        self.assertTrue(self.held_followup["continuation"]["can_send"], self.held_followup["continuation"]["reason"])
        self.assertIsNone(self.new_calls()[0]["session_id"])
        self.assertEqual(self.new_calls()[0]["permission"], "full_access")
        held_peer = next(member for member in self.held_followup["members"] if member["id"] == "codex")
        for key in ("status", "session_id", "identity_uncertain"):
            self.assertEqual(held_peer[key], members["codex"][key])
        # A clean host-cancelled peer can review this new goal under its
        # confirmed SID; none of its old interrupted work is replayed.
        review = next(call for call in self.new_calls() if call["agent"] == "codex" and call["context"]["job"]["job_phase"] == "review")
        self.assertEqual(review["session_id"], members["codex"]["session_id"])
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertTrue(final["continuation"]["recipients"]["claude"]["can_send"])
        self.assertTrue(final["continuation"]["recipients"]["codex"]["can_send"])
        self.assertTrue(final["continuation"]["can_send"])
        calls = len(self.native.calls)
        store = self.service.store
        with store._connection(write=True) as db:
            db.execute("SAVEPOINT cleanup_rejection_probe")
            try:
                # A negative metadata probe only: an actual prior owned call
                # with unconfirmed cleanup must still block a healthy target.
                db.execute("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.cleanup_confirmed',json('false')) "
                           "WHERE job_id=(SELECT job_id FROM native_executions WHERE room_id=? ORDER BY rowid LIMIT 1)",
                           (self.room_id,))
                self.assertEqual(store._directed_objective_reason(db, self.room_id, "claude"),
                                 "team_cleanup_unconfirmed")
            finally:
                db.execute("ROLLBACK TO cleanup_rejection_probe")
                db.execute("RELEASE cleanup_rejection_probe")
        self.assertEqual(len(self.native.calls), calls)

    def assert_real_unpublished_change_rejected(self, unknown):
        state = self.service.store.native_workspace(self.room_id, "claude")
        path = Path(state["project"]) / "notes.md"
        previous = path.read_bytes()
        try:
            path.write_text("Real unauthorized private bytes.\n", encoding="utf-8")
            before, calls = self.old_rows(unknown["current_run"]["id"]), len(self.native.calls)
            code, value = self.request("POST", f"/api/rooms/{self.room_id}/messages", self.message_body(unknown))
            self.assertEqual((code, value.get("error")), (409, "native_recovery_unpublished_changes"))
            self.assertEqual(len(self.native.calls), calls)
            self.assertEqual(self.old_rows(unknown["current_run"]["id"]), before)
        finally:
            path.write_bytes(previous)

    def test_protocol_unknown_directed_recovery_preserves_the_selected_owners_retained_work(self):
        unknown = self.bootstrap_protocol_unknown(retained_work=True)
        before_state = self.service.store.native_workspace(self.room_id, "claude")
        self.assertEqual(self.retained_path.read_bytes(), RETAINED_BYTES)
        final = self.assert_directed_followup(unknown, reviewer="codex")
        first = self.new_calls()[0]
        self.assertIsNone(first["session_id"])
        self.assertEqual(first["permission"], "full_access")
        self.assertEqual(first["cwd"], Path(before_state["project"]))
        self.assertEqual((first["cwd"] / "old-one.md").read_bytes(), RETAINED_BYTES)
        self.assertTrue(self.held_followup["continuation"]["can_send"])
        self.assertEqual(self.retained_path.read_bytes(), RETAINED_BYTES)
        self.assertNotIn("old-one.md", {file["path"] for file in final["project"]["files"]})
        held = self.service.store.directed_retained_work(self.room_id, "claude")
        self.assertEqual([(file["path"], file["sha256"]) for file in held["observation"]["files"]],
                         [("old-one.md", hashlib.sha256(RETAINED_BYTES).hexdigest())])
        store = self.service.store
        with store._connection(write=True) as db:
            db.execute("SAVEPOINT owner_release_probe")
            try:
                # Metadata-only predicate probe, rolled back. This does not
                # represent an actual publication or forge a successful job.
                files = json.dumps([{"path": "old-one.md", "sha256": hashlib.sha256(RETAINED_BYTES).hexdigest()}])
                db.execute("UPDATE shared_tasks SET owner='kimi',result_files_json=? WHERE room_id=? AND run_id=? AND id='FOLLOWUP'",
                           (files, self.room_id, final["current_run"]["id"]))

                @contextmanager
                def borrowed_connection(**_):
                    yield db

                view = type(store).__new__(type(store))
                view._team, view._connection = store._team, borrowed_connection
                retained = view.directed_retained_work(self.room_id, "claude")
                self.assertEqual(retained["observation"]["files"], held["observation"]["files"])
                db.execute("UPDATE shared_tasks SET owner='claude' WHERE room_id=? AND run_id=? AND id='FOLLOWUP'",
                           (self.room_id, final["current_run"]["id"]))
                superseded = view.directed_retained_work(self.room_id, "claude")
                self.assertEqual(superseded["observation"]["files"], [])
            finally:
                db.execute("ROLLBACK TO owner_release_probe")
                db.execute("RELEASE owner_release_probe")
        self.assertEqual(store.directed_retained_work(self.room_id, "claude"), held)
        previous_run = final["current_run"]["id"]
        previous_rows = self.old_rows(previous_run)
        self.followup_started.clear()
        self.release_followup.clear()
        second_text = "请 Claude 再单独补充交付说明，保留尚未发布的原始成果。"
        second = self.begin_followup(final, text=second_text)
        self.assertNotEqual(second["current_run"]["id"], previous_run)
        self.assertEqual([(call["agent"], call["context"]["job"]["job_phase"])
                          for call in self.new_calls()], [("claude", "plan")])
        self.assertEqual(self.new_calls()[0]["permission"], "full_access")
        self.assertTrue(second["continuation"]["recipients"]["claude"]["can_send"])
        for key in ("status", "session_id", "identity_uncertain"):
            self.assertEqual(next(member for member in second["members"] if member["id"] == "codex")[key],
                             next(member for member in final["members"] if member["id"] == "codex")[key])
        self.assertEqual(store.directed_retained_work(self.room_id, "claude"), held)
        self.assertEqual(self.retained_path.read_bytes(), RETAINED_BYTES)
        self.assertEqual(self.old_rows(previous_run), previous_rows)


del _Helpers


def fixture_main():
    case = DirectedFollowupHTTPTests()
    case.setUp()
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else "stopped"
        if mode in {"unknown", "directed-unknown"}:
            state = case.bootstrap_protocol_unknown()
        elif mode in {"completed", "directed-completed"}:
            state = case.bootstrap_completed()
        elif mode == "running":
            state = case.bootstrap_running()
        else:
            state = case.bootstrap_stopped()
        if mode.startswith("directed-"):
            state = case.begin_followup(state)
        print(json.dumps({"url": case.server.url, "port": case.port, "instance_id": case.server.instance_id,
            "room_id": case.room_id, "root": str(case.root), "source": str(case.source), "mode": mode,
            "run_id": state["current_run"]["id"], "objective_status": state["objective"]["status"],
            "plan_author": state["team"]["plan_author"], "real_model_calls": 0}), flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.is_set():
            ready, _, _ = select.select([sys.stdin], [], [], .25)
            if not ready:
                continue
            command = sys.stdin.readline().strip()
            if command in {"", "stop"}:
                break
            if command == "release":
                case.release_original.set()
                case.release_followup.set()
            elif command == "status":
                state = case.read()
                print(json.dumps({"objective_status": state["objective"]["status"],
                    "plan_author": state["team"]["plan_author"], "cancelled_calls": len(case.cancelled_calls),
                    "real_model_calls": 0}), flush=True)
    finally:
        case.tearDown()
        print(json.dumps({"closed": True, "temporary_removed": not case.root.exists(), "real_model_calls": 0}), flush=True)


if __name__ == "__main__":
    unittest.main()
