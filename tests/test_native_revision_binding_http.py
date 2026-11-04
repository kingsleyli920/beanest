"""Native disk changes use host-owned bindings; synthetic tools, real HTTP/trees."""
import asyncio
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_long_team_result_http as http
import test_native_room_service as native
from test_team_store import proposal
from test_folder_service import FolderProvider, response


class NativeRevisionBindingHTTPTests(unittest.TestCase):
    request = http.LongTeamResultHTTPTests.request
    setUp = http.LongTeamResultHTTPTests.setUp
    tearDown = http.LongTeamResultHTTPTests.tearDown
    settled = http.LongTeamResultHTTPTests.settled

    def handler(self, agent, context, cwd):
        async def reply():
            phase, board, task = context["job"]["job_phase"], context["team"], context.get("task")
            call = next(call for call in reversed(self.native.calls) if call["context"]["job"]["id"] == context["job"]["id"])
            if hasattr(self, "phases"):
                self.phases.append((phase, task["id"] if task else None, call["permission"], context["job"]["id"]))
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands = [proposal("M1", paths=["results/report.md"]),
                                proposal("M2", paths=["results/peer.md"] if getattr(self, "disjoint", False) else []),
                                proposal("M3", paths=["results/summary.md"], dependencies=["M1", "M2"]),
                                {"type": "claim", "task_id": "M1"}]
                elif agent == "codex" and board["tasks"]:
                    commands = [{"type": "claim", "task_id": name} for name in ("M2", "M3")
                                if next(item for item in board["tasks"] if item["id"] == name)["owner"] is None]
                return native.native_reply(context, [*commands, {"type": "plan_ready"}])
            if phase == "work":
                if call["permission"] == "read_only":
                    self.assertEqual((cwd / "results/report.md").read_text(), "Observed current behavior; evidence boundaries retained.\n")
                    if hasattr(self, "inspection_started"):
                        self.inspection_started.set()
                        while not self.inspection_release.is_set():
                            await asyncio.sleep(0.005)
                    return native.native_reply(context, [{"type": "complete", "task_id": task["id"],
                        "result": "Retained report inspected; no work or checks replayed."}])
                if task["id"] == "M1":
                    (cwd / "results").mkdir(exist_ok=True)
                    if getattr(self, "writes", 0) == 0:
                        (cwd / "results/report.md").write_text("Observed current behavior; evidence boundaries retained.\n")
                        self.writes = getattr(self, "writes", 0) + 1
                    for _ in range(200):
                        room = self.service.store.list_rooms()["rooms"][0]["id"]
                        tasks = self.service.store.snapshot(room)["team"]["tasks"]
                        peer = next(item for item in tasks if item["id"] == "M2")
                        if peer["result_version"] == 1 or getattr(self, "model_block", False) and peer["status"] == "blocked":
                            break
                        await asyncio.sleep(0.005)
                elif task["id"] == "M2" and getattr(self, "disjoint", False):
                    (cwd / "results").mkdir(exist_ok=True)
                    (cwd / "results/peer.md").write_text("Independent owned peer report.\n")
                if task["id"] == "M2" and getattr(self, "model_block", False):
                    return native.native_reply(context, [{"type": "block", "task_id": "M2",
                        "reason": "project_revision_conflict", "cause": "user_decision"}])
                elif task["id"] == "M3":
                    (cwd / "results").mkdir(exist_ok=True)
                    (cwd / "results/summary.md").write_text("Report and independent read-only verification reviewed.\n")
                value = native.native_reply(context, [{"type": "complete", "task_id": task["id"],
                                                      "result": "The bound owned task is ready for peer review."}])
                if task["id"] == "M1":
                    hint = getattr(self, "hint", 0)
                    value["base_revision"] = value["base_revision"] + 1 if hint == "future" else hint
                return value
            if phase == "review":
                return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept",
                                                       "reason": "Read the exact task result and current files."}])
            if phase == "checkpoint":
                return native.native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Requested report checked.",
                    "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}])
            if phase == "checkpoint_review":
                return native.native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "The goal is supported.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            self.fail(phase)
        return reply()

    def create(self):
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Write and verify the report, then its dependent summary.", "target": "claude", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        return created

    def test_text_peer_finishes_first_and_wrong_native_revision_hint_does_not_discard_owned_disk_changes(self):
        created = self.create()
        final = self.settled(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        tasks = {task["id"]: task for task in final["team"]["tasks"]}
        self.assertLess(tasks["M2"]["result_revision"], tasks["M1"]["result_revision"])
        report = self.service._project(final["room"]["id"]).files_for_paths(["results/report.md"])[0]
        self.assertEqual(report["content"], "Observed current behavior; evidence boundaries retained.\n")
        self.assertEqual(report["owner"], "claude")
        self.assertEqual(final["team"]["tasks"][2]["dependencies"], ["M1", "M2"])
        self.assertFalse(any(job["error"] for job in final["jobs"]))
        self.assertEqual((self.source / "notes.md").read_text(), "Unchanged source.\n")

    def test_future_hint_does_not_replace_the_host_binding(self):
        self.hint = "future"
        self.test_text_peer_finishes_first_and_wrong_native_revision_hint_does_not_discard_owned_disk_changes()

    def test_disjoint_native_commits_accept_the_old_frozen_base_after_peer_head_advances(self):
        self.disjoint = True
        self.test_text_peer_finishes_first_and_wrong_native_revision_hint_does_not_discard_owned_disk_changes()

    def old_hint_failure(self):
        """Only this temporary host reenacts the retired model-echo gate."""
        collect = self.service._collect_native_changes
        def old_collect(job, parsed):
            hint = parsed["base_revision"]
            collect(job, parsed)
            parsed["base_revision"] = hint
        with patch.object(self.service, "_collect_native_changes", side_effect=old_collect):
            created = self.create()
            failed = self.settled(created["room"]["id"])
        self.assertEqual(failed["team"]["stop_reason"], "project_revision_conflict")
        self.assertEqual([(task["id"], task["result_version"], task["status"]) for task in failed["team"]["tasks"]],
                         [("M1", 0, "blocked"), ("M2", 0 if getattr(self, "model_block", False) else 1, "blocked"), ("M3", 0, "blocked")])
        self.assertEqual(failed["current_run"]["revision"], 1)
        return failed

    @staticmethod
    def continue_body(snapshot):
        run = snapshot["current_run"]
        return {"text": "继续", "target": "all", "request_id": str(uuid4()), "intent": "continue",
                "binding": {key: run[key] for key in ("revision", "digest")} | {"run_id": run["id"]}}

    def test_old_hint_failure_recovers_via_fresh_readonly_and_preserves_failed_work_and_dependencies(self):
        self.phases = []
        failed = self.old_hint_failure()
        room = failed["room"]["id"]
        self.assertTrue(failed["current_run"]["can_recover_native"], failed["current_run"])
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        previous_calls = len(self.native.calls)
        body = self.continue_body(failed)
        try:
            code, accepted = self.request("POST", f"/api/rooms/{room}/messages", body)
            self.assertEqual(code, 200, accepted)
            self.assertTrue(self.inspection_started.wait(3))
            held = self.service.store.snapshot(room)
            self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
            self.assertFalse(any(item[0:2] == ("work", "M3") for item in self.phases))
            current = self.native.calls[previous_calls:]
            self.assertEqual(len(current), 1)
            self.assertEqual((current[0]["permission"], current[0]["session_id"]), ("read_only", None))
            proof = next(event for event in held["events"] if event["kind"] == "native_partial_recovery_attestation")
            self.assertEqual(proof["source_rejection"]["kind"], "native_base_revision_hint")
            self.assertTrue(proof["source_rejection"]["inferred_by_exclusion"])
            self.assertFalse(proof["canonical_imported"])
        finally:
            self.inspection_release.set()
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(final["team"]["tasks"][2]["dependencies"], ["M1", "M2"])
        self.assertEqual(self.writes, 1)
        self.assertEqual([(job["id"], job["status"], job["error"]) for job in final["jobs"][:len(failed["jobs"])]],
                         [(job["id"], job["status"], job["error"]) for job in failed["jobs"]])
        self.assertEqual(sum(phase[:2] == ("work", "M2") for phase in self.phases), 1)
        calls = len(self.native.calls)
        self.assertEqual(self.request("POST", f"/api/rooms/{room}/messages", body)[0], 200)
        self.assertEqual(len(self.native.calls), calls)

    def test_old_hint_candidate_needs_exact_identity_cleanup_and_installed_baseline(self):
        failed = self.old_hint_failure()
        room = failed["room"]["id"]
        candidate = self.service.store.native_partial_recovery_candidate(room)
        self.assertIsNotNone(candidate)
        mutations = [
            ("UPDATE members SET identity_uncertain=1 WHERE room_id=? AND provider='claude'", (room,)),
            ("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE run_id=? AND id='M1'", (candidate["run_id"],)),
            ("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.cleanup_confirmed',json('false')) WHERE job_id=?", (candidate["job_id"],)),
            ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_claimed' AND json_extract(value_json,'$.job_id')=?", (room, candidate["job_id"])),
            ("DELETE FROM native_session_history WHERE room_id=? AND provider='claude'", (room,)),
        ]
        with self.service.store._connection(write=True) as db:
            for sql, parameters in mutations:
                with self.subTest(sql=sql):
                    db.execute("SAVEPOINT exact_source")
                    db.execute(sql, parameters)
                    self.assertIsNone(self.service.store._native_partial_recovery_candidate(db, room))
                    db.execute("ROLLBACK TO exact_source")
                    db.execute("RELEASE exact_source")
        state = self.service.store.native_workspace(room, "claude")
        state["index_sha256"] = "0" * 64
        self.service.store.save_native_workspace(room, "claude", state)
        code, rejected = self.request("GET", f"/api/rooms/{room}")
        self.assertEqual(code, 200, rejected)
        self.assertFalse(rejected["current_run"]["can_recover_native"])
        self.assertEqual(rejected["current_run"]["native_recovery_reason"], "native_recovery_binding_changed")

    def test_normal_native_result_rejects_a_mismatched_installed_binding(self):
        collect = self.service._collect_native_changes
        def mismatched(job, parsed):
            if job["team"]["task_id"] == "M1" and job["team"]["job_phase"] == "work":
                job["native_binding"]["index_sha256"] = "0" * 64
            return collect(job, parsed)
        with patch.object(self.service, "_collect_native_changes", side_effect=mismatched):
            failed = self.settled(self.create()["room"]["id"])
        self.assertEqual(failed["team"]["stop_reason"], "native_recovery_binding_changed")
        self.assertEqual(self.service._project(failed["room"]["id"]).snapshot()["revision"], 1)

    def test_actual_same_path_ABA_still_rejects_and_is_not_a_revision_hint_candidate(self):
        (self.source / "results").mkdir()
        (self.source / "results/report.md").write_text("Baseline report.\n")
        collect = self.service._collect_native_changes
        def changed_path(job, parsed):
            if job["team"]["task_id"] == "M1" and job["team"]["job_phase"] == "work":
                project = self.service._project(job["room_id"])
                first = project.files_for_paths(["results/report.md"])[0]
                committed = project.apply("claude", "intermediate-version", 1,
                    [{"path": first["path"], "previous_sha256": first["sha256"], "content": "Intermediate report.\n"}])
                changed = project.files_for_paths([first["path"]])[0]
                project.apply("claude", "restore-original-bytes", committed["revision"],
                    [{"path": first["path"], "previous_sha256": changed["sha256"], "content": first["content"]}])
            return collect(job, parsed)
        with patch.object(self.service, "_collect_native_changes", side_effect=changed_path):
            failed = self.settled(self.create()["room"]["id"])
        self.assertEqual(failed["team"]["stop_reason"], "project_file_conflict")
        room = failed["room"]["id"]
        self.assertFalse(failed["current_run"]["can_recover_native"])
        self.assertIsNone(self.service.store.native_partial_recovery_candidate(room))
        self.assertEqual(self.service._project(room).files_for_paths(["results/report.md"])[0]["content"], "Baseline report.\n")
        state = self.service.store.native_workspace(room, "claude")
        self.assertEqual((Path(state["project"]) / "results/report.md").read_text(),
                         "Observed current behavior; evidence boundaries retained.\n")

    def test_non_native_wrong_base_remains_a_real_rejection(self):
        provider = FolderProvider()
        def wrong_base(agent, context):
            value = response(context, commands=[{"type": "plan_ready"}])
            value["base_revision"] = 0
            return value
        provider.handler = wrong_base
        with patch.object(self.service, "invoke", provider):
            code, created = self.request("POST", "/api/rooms", {
                "goal": "Inspect the captured project.", "target": "claude", "request_id": str(uuid4()),
                "mode": "project_folder", "project_id": self.project_id, "collaboration": True,
                "work_mode": "build", "runtime_settings": {"permission": "workspace_write", "members": {}},
                "execution_policy": {"mode": "autonomous"}})
            self.assertEqual(code, 200, created)
            failed = self.settled(created["room"]["id"])
        self.assertEqual(failed["team"]["stop_reason"], "project_revision_conflict", failed["team"])
        self.assertEqual(len(self.native.calls), 0)

    def test_same_error_model_block_is_preserved_during_precise_host_recovery(self):
        self.model_block = True
        failed = self.old_hint_failure()
        room, run = failed["room"]["id"], failed["current_run"]["id"]
        with self.service.store._connection(write=True) as db:
            # The old flat protocol had no cause. Preserve its real block
            # provenance even when its reason happens to match the host error.
            block = next(event for event in failed["events"] if event["kind"] == "team_command" and event["command"] == "block")
            self.assertEqual((block["sender"], block["task_id"]), ("codex", "M2"))
            db.execute("UPDATE shared_tasks SET block_cause=NULL WHERE room_id=? AND run_id=? AND id='M2'", (room, run))
        code, value = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(failed))
        self.assertEqual(code, 200, value)
        final = self.settled(room)
        task = next(task for task in final["team"]["tasks"] if task["id"] == "M2")
        self.assertEqual((task["status"], task["blocked_reason"]), ("blocked", "project_revision_conflict"))
        self.assertNotEqual(final["objective"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
