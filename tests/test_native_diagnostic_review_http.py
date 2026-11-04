"""Recover an old sanitized Codex diagnostic rejection through actual HTTP."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from uuid import uuid4

import test_long_team_result_http as http
import test_native_revision_ancestor_http as ancestor
import test_native_room_service as native
from test_team_store import proposal


class NativeDiagnosticReviewHTTPTests(unittest.TestCase):
    setUp = http.LongTeamResultHTTPTests.setUp
    tearDown = http.LongTeamResultHTTPTests.tearDown
    request = http.LongTeamResultHTTPTests.request
    settled = ancestor.NativeRevisionAncestorHTTPTests.settled

    def handler(self, agent, context, cwd):
        async def reply():
            phase, task = context["job"]["job_phase"], context.get("task")
            task_id = task["id"] if task else None
            call = next(call for call in reversed(self.native.calls)
                        if call["context"]["job"]["id"] == context["job"]["id"])
            self.phases.append((phase, task_id, call["permission"], context["job"]["id"]))
            if phase in {"plan", "coordinate"}:
                board, commands = context["team"], []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands = [proposal("T1", paths=["results/first.md"]),
                        proposal("T2", paths=["results/second.md"]),
                        proposal("T3", paths=["results/summary.md"], dependencies=["T1", "T2"]),
                        {"type": "claim", "task_id": "T1"}, {"type": "claim", "task_id": "T3"}]
                elif board["tasks"] and agent == "claude":
                    if next(item for item in board["tasks"] if item["id"] == "T2")["owner"] is None:
                        commands = [{"type": "claim", "task_id": "T2"}]
                return native.native_reply(context, [*commands, {"type": "plan_ready"}])
            if phase == "work":
                self.work_counts[task_id] += 1
                (cwd / "results").mkdir(exist_ok=True)
                path = {"T1": "first.md", "T2": "second.md", "T3": "summary.md"}[task_id]
                (cwd / "results" / path).write_text(f"Synthetic independently owned {task_id} delivery.\n")
                return native.native_reply(context, [{"type": "complete", "task_id": task_id,
                    "result": f"The {task_id} file is ready for independent review."}])
            if phase == "review":
                if task_id == "T2" and not self.failure_injected:
                    # The successful T1 delivery and review are real HTTP-run
                    # results, not rows manufactured for recovery eligibility.
                    for _ in range(400):
                        snapshot = self.service.store.snapshot(self.room_id)
                        first = next(item for item in snapshot["team"]["tasks"] if item["id"] == "T1")
                        if first["status"] == "accepted":
                            break
                        await asyncio.sleep(0.005)
                    self.assertEqual(first["status"], "accepted")
                    # Make the frozen review revision genuinely older than the
                    # canonical head without changing either task's file SHA.
                    project = self.service._project(self.room_id)
                    project.apply("codex", "independent-later-fixture", project.snapshot()["revision"], [{
                        "path": "later.md", "previous_sha256": None, "content": "Unrelated later canonical evidence.\n"}])
                elif task_id == "T2" and self.failure_injected:
                    self.review_started.set()
                    while not self.review_release.is_set():
                        await asyncio.sleep(0.005)
                return native.native_reply(context, [{"type": "review", "task_id": task_id,
                    "verdict": "accept", "reason": "Inspected the exact task result and current files."}])
            if phase == "checkpoint":
                return native.native_reply(context, [{"type": "checkpoint", "decision": "complete",
                    "summary": "The independent deliveries and summary were reviewed.", "next_goal": None,
                    "scope_reference": context["objective"]["scope_reference"], "question": None}])
            if phase == "checkpoint_review":
                return native.native_reply(context, [{"type": "checkpoint_review", "verdict": "accept",
                    "reason": "The bound goal is complete.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            self.fail(phase)
        return reply()

    def create(self):
        self.phases, self.failure_injected = [], False
        self.work_counts = {task: 0 for task in ("T1", "T2", "T3")}
        self.review_started, self.review_release = threading.Event(), threading.Event()
        transport = self.native

        async def old_diagnostic_host(agent, prompt, cwd, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            # This fixture owns exactly one room. Its first call may race the
            # create response, so resolve the already-persisted room locally.
            self.room_id = self.service.store.list_rooms()["rooms"][0]["id"]
            result = await transport(agent, prompt, cwd, **options)
            if (agent == "codex" and context["job"]["job_phase"] == "review"
                    and context["task"]["id"] == "T2" and not self.failure_injected):
                self.failure_injected = True
                directory = Path(options["receipt_dir"])
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                receipt = directory / (str(uuid4()) + ".jsonl")
                events = [
                    {"type": "thread.started", "thread_id": result["session_id"]},
                    {"type": "item.completed", "item": {"type": "error", "code": "code_mode_disabled_notice"}},
                    {"type": "item.completed", "item": {"type": "error", "code": "ignored_legacy_config_notice"}},
                    {"type": "turn.started"},
                    *({"type": "error", "usage": None} for _ in range(4)),
                    {"type": "item.completed", "item": {"type": "error", "code": "unknown_provider_error"}},
                ]
                for index in range(4):
                    events.extend([
                        {"type": "item.started", "item": {"id": f"closed-{index}",
                            "type": "command_execution", "status": "in_progress"}},
                        {"type": "item.completed", "item": {"id": f"closed-{index}",
                            "type": "command_execution", "status": "completed", "exit_code": 0}},
                    ])
                events.append({"type": "turn.completed", "usage": None})
                receipt.write_text("".join(json.dumps({"channel": "stdout", "data": event}) + "\n"
                                           for event in events))
                receipt.chmod(0o600)
                self.failed_receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
                result.update(status="failed", failure_code="unknown_provider_error",
                    native_execution={**result["native_execution"], "tool_calls": 4, "tool_results": 4,
                        "permission": "full_access", "cleanup_confirmed": True, "exit_code": 0,
                        "side_effects_possible": True, "receipt_path": str(receipt)})
            return result

        self.service.native_invoke = old_diagnostic_host
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Implement and review two independent reports, then their dependent summary.",
            "target": "codex", "request_id": str(uuid4()), "mode": "native_project",
            "project_id": self.project_id, "participants": ["codex", "claude"], "work_mode": "build",
            "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, {"error": created.get("error")})
        return created

    def job_rows(self, ids):
        placeholders = ",".join("?" for _ in ids)
        with self.service.store._connection() as db:
            return {table: [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}", ids)]
                for table, key in (("jobs", "id"), ("team_jobs", "job_id"), ("native_executions", "job_id"))}

    def save_metadata(self, value):
        destination = os.environ.get("NATIVE_DIAGNOSTIC_REVIEW_OBSERVATION")
        if destination:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def test_continue_refreshes_only_the_failed_review_and_preserves_accepted_and_delivered_work(self):
        failed = self.settled(self.create()["room"]["id"])
        room = failed["room"]["id"]
        tasks = {task["id"]: task for task in failed["team"]["tasks"]}
        state = {name: {key: task[key] for key in ("status", "result_version", "assignment_epoch", "owner")}
                 for name, task in tasks.items()}
        self.assertEqual(state["T1"]["status"], "accepted", state)
        self.assertEqual((state["T2"]["status"], state["T2"]["result_version"]), ("unknown", 1), state)
        self.assertEqual((state["T3"]["status"], state["T3"]["result_version"]), ("unknown", 0), state)
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 0})
        self.assertTrue(self.failure_injected)
        code, current = self.request("GET", f"/api/rooms/{room}")
        self.assertEqual(code, 200, {"error": current.get("error")})
        failed_job = next(job for job in current["jobs"] if job["error"] == "unknown_provider_error")
        failed_native = next(item for item in current["native_executions"] if item["job_id"] == failed_job["id"])
        metadata = {"team_phase": current["team"]["phase"], "team_stop_reason": current["team"]["stop_reason"],
            "can_recover_native": current["current_run"]["can_recover_native"],
            "native_recovery_reason": current["current_run"]["native_recovery_reason"], "tasks": state,
            "sanitized_receipt_sha256": self.failed_receipt_sha256,
            "failed_review_binding_revision": failed_native["binding"]["revision"],
            "current_canonical_revision": current["current_run"]["revision"]}
        self.save_metadata(metadata)
        self.assertLess(metadata["failed_review_binding_revision"], metadata["current_canonical_revision"], metadata)
        self.assertTrue(current["current_run"]["can_recover_native"], metadata)
        old_ids = [job["id"] for job in current["jobs"]]
        old_rows = self.job_rows(old_ids)
        project = self.service._project(room)
        before_files = {file["path"]: file["sha256"] for file in project.files_for_paths([
            "results/first.md", "results/second.md"])}
        prior_calls, prior_phases = len(self.native.calls), len(self.phases)
        run = current["current_run"]
        body = {"text": "继续", "target": "all", "request_id": str(uuid4()), "intent": "continue",
            "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}}
        try:
            code, accepted = self.request("POST", f"/api/rooms/{room}/messages", body)
            self.assertEqual(code, 200, {"error": accepted.get("error")})
            self.assertTrue(self.review_started.wait(3), "fresh review did not start")
            calls = self.native.calls[prior_calls:]
            self.assertEqual(len(calls), 1)
            call = calls[0]
            self.assertEqual((call["agent"], call["permission"], call["session_id"]), ("codex", "full_access", None))
            self.assertEqual((call["context"]["job"]["job_phase"], call["context"]["task"]["id"]), ("review", "T2"))
            self.assertEqual(call["context"]["task"]["result_version"], 1)
            self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 0})
            held = self.service.store.snapshot(room)
            held_tasks = {task["id"]: task for task in held["team"]["tasks"]}
            self.assertEqual((held_tasks["T1"]["status"], held_tasks["T1"]["result_version"]), ("accepted", 1))
            self.assertEqual(held_tasks["T3"]["dependencies"], ["T1", "T2"])
        finally:
            self.review_release.set()
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete", {
            "phase": final["team"]["phase"], "stop_reason": final["team"]["stop_reason"]})
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(self.work_counts, {"T1": 1, "T2": 1, "T3": 1})
        self.assertFalse(any(phase[0] == "work" and phase[1] in {"T1", "T2"}
                             for phase in self.phases[prior_phases:]))
        self.assertEqual(self.job_rows(old_ids), old_rows)
        self.assertEqual({file["path"]: file["sha256"] for file in project.files_for_paths(list(before_files))}, before_files)
        self.assertEqual((self.source / "notes.md").read_text(), "Unchanged source.\n")
        self.save_metadata({**metadata, "continue_http_status": 200, "fresh_first_phase": "review",
            "fresh_first_task": "T2", "fresh_first_permission": "full_access", "fresh_session": True,
            "final_objective_status": final["objective"]["status"], "old_rows_unchanged": True})


if __name__ == "__main__":
    unittest.main()
