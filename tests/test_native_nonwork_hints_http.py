"""Native informational hints over actual HTTP; all providers are synthetic."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import select
import signal
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.native_workspace import collect_changes
from local_agent_coordinator import kimi_acp
from test_kimi_native_setup import configuration
from test_kimi_setup_http import SetupFixture
from test_native_room_service import NativeTransport, native_reply, planned_owners
from test_team_store import proposal
from fixtures.retired_nonwork_host import forced_readonly_recovery, reject_nonwork_hints
from wait_support import poll


DOCS = ("docs/one.md", "docs/two.md", "docs/three.md")
HINTS = [{"path": path, "reason": "Read the existing document; no edit was needed."} for path in DOCS]
KIMI_SCHEMA_SUFFIX = "\nReturn your final answer as one JSON object matching this schema, without Markdown fences:\n"


class JournalTransport(NativeTransport):
    """Synthetic provider receipts derived from the actual invocation, never an audit stub."""
    def __init__(self, journal_root):
        super().__init__()
        self.journal_root = journal_root
        self.journals = {}
        self.intercept = None

    async def __call__(self, agent, prompt, cwd, session_id=None, **options):
        owned_sid = session_id or (("session_" if agent == "kimi" else "") + str(uuid4()))
        try:
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            result = await self.intercept(agent, context, options) if self.intercept else None
            if result is None:
                result = await super().__call__(agent, prompt, cwd, session_id=session_id, **options)
            else:
                self.calls.append(dict(agent=agent, prompt=prompt, context=context, cwd=Path(cwd),
                                       session_id=session_id, **options))
        except asyncio.CancelledError as error:
            # The host recovers an interrupted Claude identity only from its
            # owned, bounded receipt, just as the production adapter does.
            receipt = options["receipt_dir"] / (str(uuid4()) + ".jsonl")
            receipt.parent.mkdir(parents=True, exist_ok=True)
            if agent == "kimi":
                header = {"type": "kimi_native_turn", "session_id": owned_sid}
            else:
                data = ({"type": "thread.started", "thread_id": owned_sid} if agent == "codex" else
                        {"type": "system", "subtype": "init", "session_id": owned_sid})
                header = {"channel": "stdout", "data": data}
            receipt.write_text(json.dumps(header) + "\n", encoding="utf-8")
            error.native_execution = {"permission": options["permission"], "cleanup_confirmed": True,
                "side_effects_possible": False, "tool_calls": 1, "tool_results": 1,
                "exit_code": -15, "duration_seconds": .01, "receipt_path": str(receipt)}
            raise
        result["session_id"] = owned_sid
        if agent == "kimi":
            # kimi_acp adds this schema instruction after receiving the host
            # prompt. Persist that actual wire shape, not a bare context JSON.
            wire_prompt = prompt + KIMI_SCHEMA_SUFFIX + json.dumps(
                options["response_schema"], ensure_ascii=False, allow_nan=False)
            path = self.journal_root / (owned_sid + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = [{"type": "metadata", "protocol_version": "1.5", "sessionId": owned_sid},
                {"type": "turn.prompt", "input": [{"type": "text", "text": wire_prompt}]}]
            if result.get("failure_code") == "native_permission_required":
                interaction = str(uuid4())
                rows.extend([{"type": "plan_mode.enter"}, {"type": "permission.set_mode", "mode": "manual"},
                    {"type": "interaction.request", "id": interaction, "kind": "approval", "text": result["native_question"]},
                    {"type": "interaction.resolved", "id": interaction, "response": {"decision": "cancelled"}}])
            else:
                rows.append({"type": "context.append_loop_event", "event": {"part": {"type": "text", "text": result["text"]}}})
            # ACP may close the turn after a cancelled native interaction;
            # the adapter still reports native_permission_required to the host.
            rows.append({"type": "turn.ended", "turnId": 0, "reason": "completed"})
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            self.journals[(str(Path(cwd)), owned_sid)] = path
        return result

    def session_wire(self, cwd, session_id):
        return self.journals.get((str(Path(cwd)), session_id), self.journal_root / "missing.jsonl")


class NativeNonworkHintsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SetupFixture(discover=False)
        self.service, self.server = self.fixture.service, self.fixture.server
        self.root, self.port, self.source = self.fixture.root, self.fixture.port, self.fixture.source
        self.fixture.config.write_text(configuration(), encoding="utf-8")
        (self.source / "docs").mkdir()
        for path in DOCS:
            (self.source / path).write_text("Existing canonical " + path + ".\n", encoding="utf-8")
        self.native = NativeTransport()
        self.native.handler = self.handler
        self.service.native_invoke = self.native
        # These recovery cases replay a board whose owners were set while planning.
        planned_owners(self.service)
        self.goal_to_room = {}
        self.target_phase = "plan"
        self.violation = None
        self.hold_claude = True
        self.claude_started = threading.Event()
        self.release_claude = threading.Event()
        self.cancelled = []
        self.hinted_jobs = []
        self.inspection_started = {provider: threading.Event() for provider in ("kimi", "claude")}
        self.inspection_release = {provider: threading.Event() for provider in ("kimi", "claude")}
        self.coordinate_started = {provider: threading.Event() for provider in ("kimi", "claude")}
        self.coordinate_release = {provider: threading.Event() for provider in ("kimi", "claude")}
        self.hold_coordinates = False
        self.hold_work = False
        self.work_started, self.release_work = threading.Event(), threading.Event()
        self.room_id = None
        self.addCleanup(self.fixture.close)
        self.request("POST", "/api/partners/discover", {})
        self.project_id = self.request("POST", "/api/projects", {
            "name": "Nonwork hint fixture", "path": str(self.source), "selection_kind": "folder",
        })["project"]["id"]

    def tearDown(self):
        self.release_claude.set()
        for release in self.inspection_release.values():
            release.set()
        for release in self.coordinate_release.values():
            release.set()
        self.release_work.set()
        self.doCleanups()

    def request(self, method, path, value=None):
        code, result = self.fixture.request(method, path, value)
        self.assertEqual(code, 200, {"status": code, "error": result.get("error")})
        return result

    def read(self):
        return self.request("GET", f"/api/rooms/{self.room_id}")

    def quiescent(self, snapshot):
        return (not any(job["status"] in {"queued", "working"} for job in snapshot["jobs"])
                and not any(room == self.room_id and not worker.done()
                            for (room, _), worker in list(self.service._workers.items())))

    def wait(self, predicate):
        return poll(self.read, predicate, timeout=5, describe=lambda snapshot: "Synthetic hint fixture did not settle: " + json.dumps({
            "team": snapshot["team"]["phase"], "stop": snapshot["team"]["stop_reason"],
            "objective": snapshot["objective"]["status"],
            "jobs": [{key: job[key] for key in ("provider", "status", "error")} for job in snapshot["jobs"]],
        }))

    def create(self):
        goal = "Inspect the three existing documents and deliver one result. " + str(uuid4())
        created = self.request("POST", "/api/rooms", {"goal": goal, "target": "codex", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": ["codex", "kimi", "claude"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.room_id = created["room"]["id"]
        self.goal_to_room[goal] = self.room_id
        return created

    def bootstrap_held(self):
        self.create()
        return self.wait(lambda snapshot: bool(self.hinted_jobs) and any(
            job["id"] == self.hinted_jobs[0] and job["status"] not in {"queued", "working"} for job in snapshot["jobs"]))

    def bootstrap_legacy_stopped(self):
        """Actual old-host failure/cancellation, with a real prompt-bound fake journal."""
        self.native = JournalTransport(self.root / "owned-journals")
        self.native.handler = self.handler
        self.service.native_invoke = self.native
        wire = patch.object(kimi_acp, "session_wire", side_effect=self.native.session_wire)
        wire.start()
        self.addCleanup(wire.stop)
        with reject_nonwork_hints(self.service):
            self.create()
            stopped = self.wait(lambda value: value["team"]["stop_reason"] == "team_scope_conflict" and self.quiescent(value))
        self.assertEqual(stopped["objective"]["status"], "stopped")
        self.assertEqual(stopped["project"]["revision"], 1)
        self.assertEqual(len(self.cancelled), 1)
        candidate = self.service.store.native_nonwork_recovery_candidate(self.room_id)
        self.assertIsNotNone(candidate, "Legacy seed must satisfy the actual structural classifier")
        self.assertEqual(candidate["provider"], "kimi")
        self.assertEqual({job["provider"]: job["status"] for job in candidate["jobs"]},
                         {"codex": "completed", "kimi": "failed", "claude": "unknown"})
        self.legacy_candidate = candidate
        return self.read()

    @staticmethod
    def recovery_body(snapshot):
        current = snapshot["current_run"]
        return {"request_id": str(uuid4()), "run_id": current["id"],
                "revision": current["revision"], "digest": current["digest"]}

    def old_job_rows(self, ids):
        placeholders = ",".join("?" for _ in ids)
        with self.service.store._connection() as db:
            return {table: [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}", ids)]
                for table, key in (("jobs", "id"), ("team_jobs", "job_id"), ("native_executions", "job_id"))}

    def bootstrap_recovery_held(self):
        self.legacy_stopped = self.bootstrap_legacy_stopped()
        self.hold_coordinates = self.hold_work = True
        self.recovery_call_offset = len(self.native.calls)
        self.request("POST", f"/api/rooms/{self.room_id}/recover-native", self.recovery_body(self.legacy_stopped))
        return self.wait(lambda _: all(event.is_set() for event in self.coordinate_started.values()))

    def recovery_calls(self):
        return self.native.calls[self.recovery_call_offset:]

    def bootstrap_legacy_permission_failure(self):
        original = self.bootstrap_legacy_stopped()
        self.inspection_release["claude"].set()
        self.native_question = "旧宿主只读模式要求批准：是否允许继续当前协作？\n允许一次 / 拒绝"

        async def permission_failure(agent, context, options):
            if agent != "kimi" or not context.get("native_nonwork_inspection"):
                return None
            self.assertEqual(options["permission"], "read_only")
            self.inspection_started[agent].set()
            # A real completed peer is part of the old group. Do not let the
            # permission failure cancel it and accidentally model another case.
            while True:
                jobs = self.service.store.snapshot(self.room_id)["jobs"]
                if any(job["provider"] == "claude" and job["status"] == "completed"
                       and job["id"] not in {item["id"] for item in original["jobs"]} for job in jobs):
                    break
                await asyncio.sleep(.005)
            return {"status": "failed", "failure_code": "native_permission_required", "native_question": self.native_question,
                "text": "", "usage": None, "events": [], "cleanup_confirmed": True,
                "native_execution": {"permission": options["permission"], "tool_calls": 0, "tool_results": 0,
                    "side_effects_possible": False, "cleanup_confirmed": True, "exit_code": -15, "duration_seconds": .01}}

        self.native.intercept = permission_failure
        try:
            with forced_readonly_recovery(self.service):
                self.request("POST", f"/api/rooms/{self.room_id}/recover-native", self.recovery_body(original))
                failed = self.wait(lambda value: value["objective"]["status"] == "needs_user" and self.quiescent(value))
        finally:
            self.native.intercept = None
        self.assertNotEqual(failed["current_run"]["id"], original["current_run"]["id"])
        self.assertTrue(any(event.get("native_question") is True and event.get("text") == self.native_question
                            for event in failed["events"]))
        current_jobs = [job for job in failed["jobs"] if job["id"] not in {old["id"] for old in original["jobs"]}]
        self.assertEqual({job["provider"]: (job["status"], job["error"]) for job in current_jobs},
                         {"claude": ("completed", None), "kimi": ("failed", "native_permission_required")})
        self.permission_original = original
        self.permission_failed = failed
        return self.read()

    async def handler(self, agent, context, cwd):
        phase, task = context["job"]["job_phase"], context.get("task")
        if context.get("native_nonwork_inspection"):
            self.assertIn(agent, self.inspection_started)
            self.assertEqual(phase, "coordinate")
            self.inspection_started[agent].set()
            while not self.inspection_release[agent].is_set():
                await asyncio.sleep(.005)
            return native_reply(context)
        if phase == "coordinate" and self.hold_coordinates and agent in self.coordinate_started:
            self.coordinate_started[agent].set()
            while not self.coordinate_release[agent].is_set():
                await asyncio.sleep(.005)
        if phase == "plan" and agent == "claude":
            self.claude_started.set()
            if self.hold_claude:
                try:
                    while not self.release_claude.is_set():
                        await asyncio.sleep(.005)
                except asyncio.CancelledError as error:
                    self.cancelled.append(context["job"]["id"])
                    error.native_execution = {"cleanup_confirmed": True, "side_effects_possible": False,
                                              "tool_calls": 1, "tool_results": 1}
                    raise
        if phase == "plan" and agent == "kimi":
            # Order the real submissions: Codex's proposal/claim must already
            # be committed, and Claude must have entered its existing call.
            goal = context["objective"]["original_goal"]
            while True:
                room = self.goal_to_room.get(goal)
                tasks = self.service.store.snapshot(room)["team"]["tasks"] if room else []
                if self.claude_started.is_set() and any(item["id"] == "T1" and item["owner"] == "codex" for item in tasks):
                    break
                await asyncio.sleep(.005)
            reply = native_reply(context)
        elif phase in {"plan", "coordinate"}:
            tasks = context["team"]["tasks"]
            commands = []
            if agent == "codex" and not tasks:
                commands = [proposal("T1", paths=["result.md"]), {"type": "claim", "task_id": "T1"}]
            reply = native_reply(context, [*commands, {"type": "plan_ready"}])
        elif phase == "work":
            if self.hold_work:
                self.work_started.set()
                while not self.release_work.is_set():
                    await asyncio.sleep(.005)
            (cwd / "result.md").write_text("Ordinary owned result.\n", encoding="utf-8")
            reply = native_reply(context, [{"type": "complete", "task_id": task["id"],
                                          "result": "The requested owned result is ready."}])
        elif phase == "review":
            self.assertEqual((cwd / "result.md").read_text(), "Ordinary owned result.\n")
            reply = native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept",
                                          "reason": "Read the exact owned result."}])
        elif phase == "checkpoint":
            reply = native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "The original work is reviewed.",
                "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}])
        elif phase == "checkpoint_review":
            reply = native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Matches the original goal.",
                "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        else:
            self.fail("Unexpected synthetic phase " + phase)
        selected = (phase == self.target_phase and not self.hinted_jobs
                    and (agent == "kimi" or phase in {"work", "checkpoint"}))
        if selected:
            self.hinted_jobs.append(context["job"]["id"])
            reply["unchanged_paths"] = [dict(item) for item in HINTS]
            if self.violation == "private_D":
                (cwd / DOCS[0]).write_text("Unauthorized actual private edit.\n", encoding="utf-8")
            elif self.violation == "json_files":
                reply["files"] = [{"path": DOCS[0], "previous_sha256": None, "content": "Unauthorized JSON file.\n"}]
            elif self.violation == "json_edits":
                reply["edits"] = [{"path": DOCS[0], "old_text": "Existing", "new_text": "Unauthorized"}]
            elif self.violation == "resolved":
                reply["resolved_conflicts"] = [DOCS[0]]
        return reply

    def test_plan_hints_with_no_disk_change_do_not_stop_an_existing_peer_call(self):
        snapshot = self.bootstrap_held()
        hint = next(job for job in snapshot["jobs"] if job["id"] == self.hinted_jobs[0])
        self.assertEqual((hint["status"], hint["error"]), ("completed", None), {
            "team_stop": snapshot["team"]["stop_reason"],
            "claude_jobs": [{key: job[key] for key in ("status", "error")} for job in snapshot["jobs"] if job["provider"] == "claude"],
        })
        claude = next(job for job in snapshot["jobs"] if job["provider"] == "claude")
        self.assertEqual(claude["status"], "working")
        self.assertEqual(self.cancelled, [])
        state = self.service.store.native_workspace(self.room_id, "kimi")
        diff = collect_changes(state["project"], state, [], resolved_conflicts=[])
        self.assertEqual((diff["files"], diff["blocked"]), ([], []))
        self.assertEqual(snapshot["project"]["revision"], 1)
        self.assertEqual(next(task for task in snapshot["team"]["tasks"] if task["id"] == "T1")["owner"], "codex")
        self.release_claude.set()
        final = self.wait(lambda value: value["objective"]["status"] == "complete")
        self.assertFalse(any(job["error"] for job in final["jobs"]))
        self.assertTrue(any(call["agent"] == "kimi" and call["context"]["job"]["job_phase"] == "coordinate" for call in self.native.calls))

    def test_coordinate_review_and_checkpoints_accept_informational_hints_without_publication(self):
        self.hold_claude = False
        for phase in ("coordinate", "review", "checkpoint", "checkpoint_review"):
            with self.subTest(phase=phase):
                self.target_phase, self.hinted_jobs = phase, []
                self.create()
                final = self.wait(lambda value: value["objective"]["status"] == "complete")
                self.assertEqual(len(self.hinted_jobs), 1)
                hinted = next(job for job in final["jobs"] if job["id"] == self.hinted_jobs[0])
                self.assertEqual((hinted["status"], hinted["error"]), ("completed", None))
                self.assertFalse(any(job["error"] for job in final["jobs"]))
                for file in self.service._project(self.room_id).files_for_paths(list(DOCS)):
                    self.assertEqual(file["content"], "Existing canonical " + file["path"] + ".\n")

    def test_nonwork_hints_cannot_hide_disk_json_or_resolution_changes(self):
        self.hold_claude = False
        expected = {"private_D": "native_changes_not_imported", "json_files": "native_json_files_forbidden",
                    "json_edits": "native_json_files_forbidden", "resolved": "team_scope_conflict"}
        for violation, code in expected.items():
            with self.subTest(violation=violation):
                self.violation, self.hinted_jobs = violation, []
                self.create()
                final = self.wait(lambda value: any(job["error"] == code for job in value["jobs"]) and self.quiescent(value))
                if code == "native_json_files_forbidden":
                    # Was: the objective must not complete. JSON file bodies in a
                    # native reply are a correctable reply mistake, so the host
                    # rejects them, asks once for a corrected reply and continues;
                    # nothing from the rejected reply may reach canonical files.
                    self.assertEqual(final["objective"]["status"], "complete")
                    self.assertIn(code, [event["code"] for event in final["events"]
                                         if event["kind"] == "format_correction_queued"])
                    # Only the owned T1 result is published.
                    self.assertEqual(final["project"]["revision"], 2)
                    self.assertEqual(self.service._project(self.room_id).files_for_paths(["result.md"])[0]["content"],
                                     "Ordinary owned result.\n")
                else:
                    self.assertNotEqual(final["objective"]["status"], "complete")
                    self.assertEqual(final["project"]["revision"], 1)
                for file in self.service._project(self.room_id).files_for_paths(list(DOCS)):
                    self.assertEqual(file["content"], "Existing canonical " + file["path"] + ".\n")

    def test_work_phase_hints_outside_owned_paths_are_dropped_without_import(self):
        # Catches: "left these other files alone" notes stopping the objective, or such a
        # note letting an outside path be imported.
        self.hold_claude = False
        self.target_phase = "work"
        self.create()
        final = self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        self.assertFalse(any(job["error"] == "team_scope_conflict" for job in final["jobs"]))
        task = next(task for task in final["team"]["tasks"] if task["id"] == "T1")
        self.assertEqual((task["owner"], task["status"]), ("codex", "accepted"))
        for file in self.service._project(self.room_id).files_for_paths(list(DOCS)):
            self.assertEqual(file["content"], "Existing canonical " + file["path"] + ".\n")

    def test_recovery_requires_exact_journal_context_and_unchanged_private_trees(self):
        stopped = self.bootstrap_legacy_stopped()
        self.assertTrue(stopped["current_run"]["can_recover_native"], stopped["current_run"]["native_recovery_reason"])
        candidate = self.legacy_candidate
        state = self.service.store.native_workspace(self.room_id, "kimi")
        journal = self.native.session_wire(state["project"], candidate["session_id"])
        original = journal.read_bytes()
        calls = len(self.native.calls)
        rows = self.old_job_rows([job["id"] for job in stopped["jobs"]])
        for invalid in ("missing_receipt", "wrong_context", "private_D"):
            with self.subTest(invalid=invalid):
                private = Path(state["project"]) / DOCS[0]
                previous = private.read_bytes()
                try:
                    if invalid == "missing_receipt":
                        journal.unlink()
                    elif invalid == "wrong_context":
                        entries = [json.loads(line) for line in original.splitlines()]
                        text = entries[1]["input"][0]["text"]
                        prefix, payload = text.split("CONTEXT DATA:\n", 1)
                        payload, separator, schema = payload.partition(KIMI_SCHEMA_SUFFIX)
                        context = json.loads(payload)
                        context["job"]["run_id"] = str(uuid4())
                        entries[1]["input"][0]["text"] = prefix + "CONTEXT DATA:\n" + json.dumps(context) + separator + schema
                        journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
                    else:
                        private.write_text("A real unpublished private change.\n")
                    code, response = self.fixture.request("POST", f"/api/rooms/{self.room_id}/recover-native",
                                                          self.recovery_body(stopped))
                    self.assertEqual((code, response.get("error")), (409,
                        "native_recovery_unpublished_changes" if invalid == "private_D"
                        else "native_recovery_diagnostic_unconfirmed"))
                    self.assertEqual(len(self.native.calls), calls)
                    self.assertEqual(self.old_job_rows([job["id"] for job in stopped["jobs"]]), rows)
                finally:
                    journal.write_bytes(original)
                    private.write_bytes(previous)

    def test_recovery_uses_fresh_normal_coordination_at_the_original_full_access(self):
        stopped = self.bootstrap_legacy_stopped()
        ids = [job["id"] for job in stopped["jobs"]]
        old_rows = self.old_job_rows(ids)
        old_task = stopped["team"]["tasks"][0]
        self.hold_coordinates = self.hold_work = True
        self.recovery_call_offset = len(self.native.calls)
        recovered = self.request("POST", f"/api/rooms/{self.room_id}/recover-native", self.recovery_body(stopped))
        held = self.wait(lambda _: all(event.is_set() for event in self.coordinate_started.values())
                         or all(event.is_set() for event in self.inspection_started.values()))
        self.assertNotEqual(recovered["current_run"]["id"], stopped["current_run"]["id"])
        carried = held["team"]["tasks"][0]
        self.assertEqual((carried["id"], carried["owner"], carried["result_version"], carried["write_paths"]),
                         (old_task["id"], old_task["owner"], old_task["result_version"], old_task["write_paths"]))
        self.assertEqual(carried["assignment_epoch"], old_task["assignment_epoch"] + 1)
        calls = [call for call in self.recovery_calls() if call["context"]["job"]["job_phase"] == "coordinate"]
        self.assertEqual({call["agent"] for call in calls}, {"kimi", "claude"})
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual((call["context"]["job"]["job_phase"], call["permission"]), ("coordinate", "full_access"))
            self.assertIsNone(call["session_id"])
            self.assertFalse(call["context"].get("native_nonwork_inspection"))
            for path in DOCS:
                self.assertEqual((call["cwd"] / path).read_text(), "Existing canonical " + path + ".\n")
        self.assertEqual(held["project"]["revision"], 1)
        self.assertEqual(self.old_job_rows(ids), old_rows)
        self.assertFalse(any(event["kind"] in {"native_nonwork_inspection_queued", "native_partial_inspection_group"}
                             and event.get("run_id") == held["current_run"]["id"] for event in held["events"]))
        for release in self.coordinate_release.values():
            release.set()
        self.release_work.set()
        final = self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        task = final["team"]["tasks"][0]
        self.assertEqual((task["id"], task["owner"], task["status"], task["result_version"]), ("T1", "codex", "accepted", 1))
        work = [call for call in self.recovery_calls() if call["context"]["job"]["job_phase"] == "work"]
        self.assertEqual([(call["agent"], call["context"]["task"]["id"]) for call in work], [("codex", "T1")])
        self.assertEqual(work[0]["permission"], "full_access")
        self.assertEqual(self.old_job_rows(ids), old_rows)
        self.assertFalse(any(job["error"] for job in final["jobs"] if job["id"] not in ids))

    def test_recovery_rejects_independent_blocks_unclean_or_unbound_cancellation(self):
        stopped = self.bootstrap_legacy_stopped()
        candidate = self.legacy_candidate
        cancelled = next(job for job in candidate["jobs"] if job["provider"] == "claude")
        calls = len(self.native.calls)
        # These are rejection-only reconstructions of malformed historical
        # metadata. The successful seed above was produced solely over HTTP.
        with self.service.store._connection(write=True) as db:
            for invalid in ("independent_block", "cleanup_false", "wrong_cancel_chain"):
                with self.subTest(invalid=invalid):
                    db.execute("SAVEPOINT invalid_old_metadata")
                    try:
                        if invalid == "independent_block":
                            db.execute("UPDATE shared_tasks SET block_cause='user_decision' WHERE room_id=? AND run_id=?",
                                       (self.room_id, candidate["run_id"]))
                        elif invalid == "cleanup_false":
                            db.execute("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.cleanup_confirmed',json('false')) "
                                       "WHERE job_id=?", (cancelled["job_id"],))
                        else:
                            db.execute("UPDATE events SET value_json=json_set(value_json,'$.session_id',?) WHERE room_id=? "
                                       "AND json_extract(value_json,'$.job_id')=? "
                                       "AND json_extract(value_json,'$.kind')='native_session_cleanup_identity'",
                                       (str(uuid4()), self.room_id, cancelled["job_id"]))
                        self.assertIsNone(self.service.store._native_nonwork_recovery_candidate(db, self.room_id))
                    finally:
                        db.execute("ROLLBACK TO invalid_old_metadata")
                        db.execute("RELEASE invalid_old_metadata")
        self.assertEqual(self.service.store.native_nonwork_recovery_candidate(self.room_id), candidate)
        self.assertEqual(len(self.native.calls), calls)

    def test_permission_restore_keeps_current_run_and_retries_only_kimi_at_full_access(self):
        failed = self.bootstrap_legacy_permission_failure()
        candidate = self.service.store.native_nonwork_permission_candidate(self.room_id)
        self.assertIsNotNone(candidate, "Real legacy permission failure must match the typed bridge")
        self.assertTrue(failed["current_run"]["can_recover_native"], failed["current_run"]["native_recovery_reason"])
        ids = [job["id"] for job in failed["jobs"]]
        old_rows, old_task = self.old_job_rows(ids), failed["team"]["tasks"][0]
        self.hold_coordinates = self.hold_work = True
        self.recovery_call_offset = len(self.native.calls)
        body = self.recovery_body(failed)
        resumed = self.request("POST", f"/api/rooms/{self.room_id}/recover-native", body)
        held = self.wait(lambda _: self.coordinate_started["kimi"].is_set())
        self.assertEqual(resumed["current_run"]["id"], failed["current_run"]["id"])
        coordinate = [call for call in self.recovery_calls() if call["context"]["job"]["job_phase"] == "coordinate"]
        self.assertEqual([call["agent"] for call in coordinate], ["kimi"])
        call = coordinate[0]
        self.assertEqual(call["permission"], "full_access")
        self.assertIsNone(call["session_id"])
        self.assertFalse(call["context"].get("native_nonwork_inspection"))
        task = held["team"]["tasks"][0]
        for key in ("id", "owner", "assignment_epoch", "result_version", "dependencies", "write_paths"):
            self.assertEqual(task[key], old_task[key])
        self.assertEqual(held["project"]["revision"], 1)
        self.assertEqual(self.old_job_rows(ids), old_rows)
        self.request("POST", f"/api/rooms/{self.room_id}/recover-native", body)
        self.assertEqual(len([call for call in self.recovery_calls() if call["agent"] == "kimi"
                              and call["context"]["job"]["job_phase"] == "coordinate"]), 1)
        self.coordinate_release["kimi"].set()
        self.release_work.set()
        final = self.wait(lambda value: value["objective"]["status"] == "complete" and self.quiescent(value))
        self.assertEqual(final["current_run"]["id"], failed["current_run"]["id"])
        self.assertEqual((final["team"]["tasks"][0]["owner"], final["team"]["tasks"][0]["status"]), ("codex", "accepted"))
        work = [call for call in self.recovery_calls() if call["context"]["job"]["job_phase"] == "work"]
        self.assertEqual([(call["agent"], call["context"]["task"]["id"]) for call in work], [("codex", "T1")])
        self.assertEqual(self.old_job_rows(ids), old_rows)
        self.assertFalse(any(call["context"].get("native_nonwork_inspection") for call in self.recovery_calls()))

    def test_permission_restore_requires_host_plan_proof_and_clean_private_state(self):
        failed = self.bootstrap_legacy_permission_failure()
        candidate = self.service.store.native_nonwork_permission_candidate(self.room_id)
        self.assertIsNotNone(candidate)
        state = self.service.store.native_workspace(self.room_id, "kimi")
        journal = self.native.session_wire(state["project"], candidate["session_id"])
        raw, calls = journal.read_bytes(), len(self.native.calls)
        for invalid in ("no_host_plan", "wrong_context", "private_D"):
            with self.subTest(invalid=invalid):
                private = Path(state["project"]) / DOCS[0]
                previous = private.read_bytes()
                try:
                    entries = [json.loads(line) for line in raw.splitlines()]
                    if invalid == "no_host_plan":
                        entries = [entry for entry in entries if entry.get("type") != "plan_mode.enter"]
                    elif invalid == "wrong_context":
                        text = entries[1]["input"][0]["text"]
                        prefix, payload = text.split("CONTEXT DATA:\n", 1)
                        payload, separator, schema = payload.partition(KIMI_SCHEMA_SUFFIX)
                        context = json.loads(payload)
                        context["job"]["id"] = str(uuid4())
                        entries[1]["input"][0]["text"] = prefix + "CONTEXT DATA:\n" + json.dumps(context) + separator + schema
                    else:
                        private.write_text("A real unpublished private change.\n")
                    journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
                    code, result = self.fixture.request("POST", f"/api/rooms/{self.room_id}/recover-native", self.recovery_body(failed))
                    self.assertEqual((code, result.get("error")), (409, "native_recovery_unpublished_changes"
                        if invalid == "private_D" else "native_recovery_diagnostic_unconfirmed"))
                    self.assertEqual(len(self.native.calls), calls)
                finally:
                    journal.write_bytes(raw)
                    private.write_bytes(previous)
        with self.service.store._connection(write=True) as db:
            for key, value in (("permission", "full_access"), ("cleanup_confirmed", False)):
                # Negative-only historical metadata variation: an ordinary
                # permission refusal or unconfirmed cleanup is not this bridge.
                db.execute("SAVEPOINT not_host_downgrade")
                try:
                    db.execute("UPDATE native_executions SET metadata_json=json_set(metadata_json,?,json(?)) WHERE job_id=?",
                               ("$." + key, json.dumps(value), candidate["job_id"]))
                    self.assertIsNone(self.service.store._native_nonwork_permission_candidate(db, self.room_id))
                finally:
                    db.execute("ROLLBACK TO not_host_downgrade")
                    db.execute("RELEASE not_host_downgrade")
        self.assertEqual(len(self.native.calls), calls)


def fixture_main():
    case = NativeNonworkHintsHTTPTests()
    case.setUp()
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else "ordinary"
        if mode == "legacy":
            state = case.bootstrap_legacy_stopped()
        elif mode == "permission":
            state = case.bootstrap_legacy_permission_failure()
        elif mode in {"coordination", "inspection"}:
            state = case.bootstrap_recovery_held()
        else:
            state = case.bootstrap_held()
        print(json.dumps({"url": case.server.url, "port": case.port, "instance_id": case.server.instance_id,
            "room_id": case.room_id, "root": str(case.root), "source": str(case.source),
            "fixture_mode": mode, "can_recover_native": state["current_run"]["can_recover_native"],
            "hint_job_status": next(job["status"] for job in state["jobs"] if job["id"] == case.hinted_jobs[0]),
            "real_model_calls": 0}), flush=True)
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
                case.release_claude.set()
                for release in case.inspection_release.values():
                    release.set()
                for release in case.coordinate_release.values():
                    release.set()
                case.release_work.set()
            elif command in {"release-kimi", "release-claude"}:
                provider = command.removeprefix("release-")
                case.inspection_release[provider].set()
                case.coordinate_release[provider].set()
            elif command == "release-work":
                case.release_work.set()
            elif command == "status":
                current = case.read()
                print(json.dumps({"team": current["team"]["phase"], "objective": current["objective"]["status"],
                                  "cancelled_synthetic_calls": len(case.cancelled), "real_model_calls": 0}), flush=True)
    finally:
        case.tearDown()
        print(json.dumps({"closed": True, "temporary_removed": not case.root.exists(), "real_model_calls": 0}), flush=True)


if __name__ == "__main__":
    unittest.main()
