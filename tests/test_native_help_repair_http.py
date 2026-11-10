"""Canonical-owner engineering repairs: synthetic native calls, real HTTP/trees."""
import asyncio
from pathlib import Path
import threading
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator.room_store import RoomError

import test_long_team_result_http as http
import test_native_room_service as native
from test_team_store import proposal
from wait_support import poll, scaled


class NativeHelpRepairHTTPTests(unittest.TestCase):
    request = http.LongTeamResultHTTPTests.request
    setUp = http.LongTeamResultHTTPTests.setUp
    tearDown = http.LongTeamResultHTTPTests.tearDown
    settled = http.LongTeamResultHTTPTests.settled

    def handler(self, agent, context, cwd):
        async def reply():
            phase, task, board = context["job"]["job_phase"], context.get("task"), context["team"]
            call = self.native.calls[-1]
            self.phases.append((phase, task["id"] if task else None, agent, call["permission"]))
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands = [proposal("M1", paths=[getattr(self, "long_path", "src/a.txt")]), proposal("M2", paths=["src/b.txt"]),
                                proposal("M3", paths=["docs/result.md"], dependencies=["M1", "M2"]),
                                *[proposal("M" + str(number)) for number in range(4, 9) if getattr(self, "large_board", False)],
                                {"type": "claim", "task_id": "M1"}]
                elif agent == "codex" and board["tasks"]:
                    names = ["M2", "M3", *["M" + str(number) for number in range(4, 9) if getattr(self, "large_board", False)]]
                    commands = [{"type": "claim", "task_id": name} for name in names
                                if next(item for item in board["tasks"] if item["id"] == name)["owner"] is None]
                return native.native_reply(context, [*commands, {"type": "plan_ready"}])
            if phase == "work":
                if call["permission"] == "read_only":
                    path = task["write_paths"][0]
                    self.assertEqual((cwd / path).read_text(), "repaired " + path + "\n")
                    other = "src/b.txt" if path == "src/a.txt" else "src/a.txt"
                    self.assertEqual((cwd / other).read_text(), "original " + other + "\n")
                    self.inspections.append((agent, path))
                    if hasattr(self, "inspection_release"):
                        self.inspection_started.set()
                        while not self.inspection_release.is_set():
                            await asyncio.sleep(0.005)
                    return native.native_reply(context, [{"type": "complete", "task_id": task["id"],
                        "result": "Inspected the proposed bytes against S and the engineering request; not published."}])
                if task["id"] in {"M1", "M2"}:
                    path = task["write_paths"][0]
                    (cwd / path).parent.mkdir(parents=True, exist_ok=True)
                    (cwd / path).write_text("original " + path + "\n")
                elif task["id"] == "M3" and not self.help_requested:
                    self.help_requested = True
                    (cwd / "docs").mkdir(exist_ok=True)
                    (cwd / "docs/result.md").write_text("Initial owned report.\n")
                    if getattr(self, "solo", False):
                        # The only other peer hits its quota before this request.
                        with self.service.store._connection() as db:
                            room_id = db.execute("SELECT room_id FROM jobs WHERE id=?", (context["job"]["id"],)).fetchone()[0]
                        self.service._retire_quota(room_id, "claude", "agent_quota_exhausted")
                    command = {"type": "request_help", "task_id": "M3", "to": None if getattr(self, "solo", False) else "claude",
                               "capability": "text",
                               "text": "Repair the two existing source files within this goal, preserving unrelated evidence."}
                    if not self.legacy_help:
                        command["repair_paths"] = getattr(self, "repair_paths", ["src/a.txt", "src/b.txt"])
                    return native.native_reply(context, [command])
                elif task["id"] == "M3":
                    reviewed = "self_checked" if getattr(self, "solo", False) else "accepted"
                    self.assertTrue(all(item["status"] == reviewed for item in board["tasks"] if item["id"].startswith("R")))
                    if getattr(self, "repeat_requests", False):
                        self.repeated_requests += 1
                        return native.native_reply(context, [{"type": "request_help", "task_id": "M3", "to": "claude", "capability": "text",
                            "text": "Changed explanation " + str(self.repeated_requests), "repair_paths": ["src/a.txt", "src/b.txt"]}])
                    self.resumed += 1
                    (cwd / "docs/result.md").write_text("Repairs independently reviewed; original evidence boundaries retained.\n")
                elif task["id"].startswith("R"):
                    path = task["write_paths"][0]
                    self.assertEqual(agent, "codex" if getattr(self, "solo", False) else
                                     "claude" if path == getattr(self, "long_path", "src/a.txt") else "codex")
                    for path in task["write_paths"]:
                        (cwd / path).write_text(("owner final " if getattr(self, "helper_foreign_only", False) else "repaired ") + path + "\n")
                return native.native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Current owned files ready for independent review."}])
            if phase == "help":
                self.old_helper_cwd = cwd
                if getattr(self, "text_help", False):
                    return native.native_reply(context)
                self.assertTrue(self.legacy_help)
                for path in ("src/a.txt", "src/b.txt"):
                    if getattr(self, "helper_foreign_only", False) and path == "src/a.txt":
                        continue
                    (cwd / path).write_text("repaired " + path + "\n")
                return native.native_reply(context)
            if phase == "review":
                if getattr(self, "helper_foreign_only", False) and task["id"].startswith("R"):
                    self.assertNotEqual(cwd, self.old_helper_cwd)
                    self.assertEqual((cwd / "src/b.txt").read_text(), "owner final src/b.txt\n")
                return native.native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept",
                    "reason": "Read the exact current task result and owned file version; no old check claimed for changed bytes."}])
            if phase == "checkpoint":
                return native.native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Requested repairs reviewed.",
                    "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}])
            if phase == "checkpoint_review":
                return native.native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "All dependencies reviewed.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            self.fail(phase)
        return reply()

    def create(self, *, legacy=False):
        self.phases, self.inspections, self.resumed = [], [], 0
        self.help_requested, self.legacy_help = False, legacy
        code, value = self.request("POST", "/api/rooms", {"goal": "Implement the two source files, repair their integration and review the report.",
            "target": "claude", "request_id": str(uuid4()), "mode": "native_project", "project_id": self.project_id,
            "participants": ["claude", "codex"], "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, value)
        return self.settled(value["room"]["id"])

    def assert_repaired(self, final):
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        tasks = {item["id"]: item for item in final["team"]["tasks"]}
        repairs = [item for item in tasks.values() if item["id"].startswith("R")]
        self.assertEqual(len(repairs), 2)
        self.assertEqual({(item["owner"], tuple(item["write_paths"])) for item in repairs},
                         {("claude", ("src/a.txt",)), ("codex", ("src/b.txt",))})
        self.assertTrue(all(item["status"] == "accepted" and item["result_version"] == 1 for item in tasks.values()))
        self.assertTrue(set(item["id"] for item in repairs) <= set(tasks["M3"]["dependencies"]))
        self.assertEqual(self.resumed, 1)
        self.assertEqual(sum(phase == "work" and name in {"M1", "M2"} for phase, name, *_ in self.phases), 2)
        files = self.service._project(final["room"]["id"]).files_for_paths(["src/a.txt", "src/b.txt"])
        self.assertEqual([(file["owner"], file["content"]) for file in files],
                         [("claude", "repaired src/a.txt\n"), ("codex", "repaired src/b.txt\n")])
        self.assertEqual((self.source / "notes.md").read_text(), "Unchanged source.\n")

    def test_structured_repairs_use_actual_canonical_owners_work_review_and_parent_dependencies(self):
        final = self.create()
        self.assert_repaired(final)
        self.assertFalse(any(phase == "help" for phase, *_ in self.phases))
        self.assertFalse(any(job["error"] for job in final["jobs"]))

    def test_lone_peer_repair_without_target_takes_over_retired_owner_files_and_self_checks(self):
        # Catches: after quota retirement left one peer, request_help with
        # repair_paths and to=null failed as invalid_target and stopped the
        # objective, discarding the lone peer's finished work call.
        self.solo = True
        final = self.create()
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(set(final["objective"]["retired_partners"]), {"claude"})
        tasks = {item["id"]: item for item in final["team"]["tasks"]}
        repairs = [item for item in tasks.values() if item["id"].startswith("R")]
        # The retired owner's file is taken over by the lone peer, never dispatched to claude.
        self.assertEqual(sorted((item["owner"], tuple(item["write_paths"]), item["status"], item["review_kind"]) for item in repairs),
                         [("codex", ("src/a.txt",), "self_checked", "self_check"),
                          ("codex", ("src/b.txt",), "self_checked", "self_check")])
        self.assertFalse(any(agent == "claude" and name.startswith("R") for _, name, agent, _ in self.phases if name))
        self.assertEqual(tasks["M3"]["status"], "self_checked")
        self.assertEqual(self.resumed, 1)
        self.assertFalse(any(job["error"] == "invalid_target" for job in final["jobs"]))
        self.assertFalse(any(event["kind"] == "message" and event.get("recipient") is None for event in final["events"]))
        files = self.service._project(final["room"]["id"]).files_for_paths(["src/a.txt", "src/b.txt"])
        self.assertEqual([(file["owner"], file["content"]) for file in files],
                         [("codex", "repaired src/a.txt\n"), ("codex", "repaired src/b.txt\n")])

    def test_repairs_do_not_turn_a_full_initial_board_into_a_total_task_limit(self):
        self.large_board = True
        final = self.create()
        self.assertEqual(len(final["team"]["tasks"]), 10)
        self.assert_repaired(final)

    def test_existing_native_long_path_uses_the_same_path_contract_for_repairs(self):
        self.long_path = "/".join(["src", *["subdirectory" + str(number) + "_" * 30 for number in range(7)], "a.txt"])
        self.assertGreater(len(self.long_path), 240)
        self.repair_paths = [self.long_path, "src/b.txt"]
        final = self.create()
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        file = self.service._project(final["room"]["id"]).files_for_paths([self.long_path])[0]
        self.assertEqual((file["owner"], file["content"]), ("claude", "repaired " + self.long_path + "\n"))

    def test_new_repair_ids_and_reason_wording_do_not_reset_same_file_evidence_protection(self):
        self.repeat_requests, self.repeated_requests = True, 0
        final = self.create()
        self.assertEqual(final["objective"]["stop_reason"], "objective_no_progress", final["objective"])
        self.assertGreaterEqual(len(final["team"]["tasks"]), 9)
        self.assertGreater(self.repeated_requests, 2)
        self.assertEqual(self.resumed, 0)

    def test_failed_help_keeps_old_whole_D_and_fresh_inspects_split_owner_proposals_before_work(self):
        failed = self.create(legacy=True)
        self.assertEqual(failed["team"]["stop_reason"], "native_changes_not_imported")
        self.assertTrue(failed["current_run"]["can_recover_native"], failed["current_run"])
        room = failed["room"]["id"]
        old_jobs = [(job["id"], job["status"], job["error"]) for job in failed["jobs"]]
        old_results = {item["id"]: item for item in failed["team"]["tasks"] if item["id"] in {"M1", "M2"}}
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        run = failed["current_run"]
        try:
            code, value = self.request("POST", f"/api/rooms/{room}/messages", {"text": "继续", "target": "all", "request_id": str(uuid4()),
                "intent": "continue", "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}})
            self.assertEqual(code, 200, value)
            self.assertTrue(self.inspection_started.wait(scaled(3)))
            current = self.service.store.snapshot(room)
            self.assertFalse(any(job["status"] == "working" and job["kind"] == "team_work" for job in current["jobs"]
                                 if not any(event["kind"] == "native_partial_inspection_queued" and event.get("job_id") == job["id"] for event in current["events"])))
            self.assertEqual(self.service._project(room).snapshot()["revision"], run["revision"])
            for path in ("src/a.txt", "src/b.txt"):
                self.assertEqual((self.old_helper_cwd / path).read_text(), "repaired " + path + "\n")
        finally:
            self.inspection_release.set()
        final = self.settled(room)
        self.assert_repaired(final)
        self.assertEqual(set(self.inspections), {("claude", "src/a.txt"), ("codex", "src/b.txt")})
        inspections = [call for call in self.native.calls if call["permission"] == "read_only"]
        self.assertTrue(all(call["session_id"] is None for call in inspections))
        self.assertEqual([(job["id"], job["status"], job["error"]) for job in final["jobs"][:len(old_jobs)]], old_jobs)
        for item in final["team"]["tasks"]:
            if item["id"] in old_results:
                for key in ("result", "result_version", "result_files", "result_digest", "review_kind"):
                    self.assertEqual(item[key], old_results[item["id"]][key])
        self.assertTrue(any(event["kind"] == "native_help_repair_retained" and not event["canonical_imported"] for event in final["events"]))

    def test_plain_help_text_does_not_schedule_or_authorize_path_repairs(self):
        self.repair_paths, self.text_help = None, True
        final = self.create()
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(final["team"]["tasks"]), 3)
        files = self.service._project(final["room"]["id"]).files_for_paths(["src/a.txt", "src/b.txt"])
        self.assertEqual([file["content"] for file in files], ["original src/a.txt\n", "original src/b.txt\n"])
        self.assertFalse(any(event["kind"] == "native_repairs_scheduled" for event in final["events"]))

    def test_unowned_or_unknown_structured_repair_path_cannot_create_a_write_task(self):
        self.repair_paths = ["notes.md"]  # Captured source has no canonical member owner.
        final = self.create()
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        failed = [job for job in final["jobs"] if job["error"] == "invalid_team_commands"]
        self.assertEqual(len(failed), 1)
        corrections = [call for call in self.native.calls if call["context"]["job"].get("correction_of") == failed[0]["id"]]
        self.assertEqual(len(corrections), 1)
        self.assertIn("commands[0].repair_paths", corrections[0]["prompt"])
        self.assertIn("canonical member owner", corrections[0]["prompt"])
        self.assertEqual(corrections[0]["permission"], "full_access")
        self.assertEqual(len(final["team"]["tasks"]), 3)
        self.assertFalse(any(event["kind"] == "native_repairs_scheduled" for event in final["events"]))
        self.assertEqual(self.service._project(final["room"]["id"]).files_for_paths(["notes.md"])[0]["content"], "Unchanged source.\n")

    def test_old_failed_help_requires_real_request_identity_cleanup_and_no_explicit_block(self):
        failed = self.create(legacy=True)
        room = failed["room"]["id"]
        candidate = self.service.store.native_help_repair_candidate(room)
        self.assertIsNotNone(candidate)
        # These are temporary fixture mutations, each rolled back. They are
        # negative provenance controls, never a migration of historical jobs.
        with self.service.store._connection(write=True) as db:
            for label, mutate in (
                ("no history", lambda: db.execute("DELETE FROM native_session_history WHERE room_id=? AND provider=?", (room, "claude"))),
                ("unknown identity", lambda: db.execute("UPDATE members SET identity_uncertain=1 WHERE room_id=? AND provider=?", (room, "claude"))),
                ("no cleanup", lambda: db.execute("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_cleanup' AND json_extract(value_json,'$.job_id')=?", (room, candidate["job_id"]))),
                ("wrong version", lambda: db.execute("UPDATE shared_tasks SET result_version=result_version+1 WHERE room_id=? AND run_id=? AND id='M3'", (room, candidate["run_id"]))),
                ("changed request", lambda: db.execute("UPDATE shared_tasks SET help_text='Different engineering request' WHERE room_id=? AND run_id=? AND id='M3'", (room, candidate["run_id"]))),
                ("no request", lambda: db.execute("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='team_command' AND json_extract(value_json,'$.command')='request_help'", (room,))),
                ("explicit legacy block", lambda: self.service.store._event(db, room, "team_command", sender="codex", job_id=candidate["request_job_id"], command="block", task_id="M3")),
                ("readonly execution", lambda: db.execute("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.permission','read_only') WHERE job_id=?", (candidate["job_id"],))),
                ("unpaired tools", lambda: db.execute("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.tool_results',0) WHERE job_id=?", (candidate["job_id"],))),
            ):
                with self.subTest(label=label):
                    db.execute("SAVEPOINT negative")
                    mutate()
                    self.assertIsNone(self.service.store._native_help_repair_candidate(db, room))
                    db.execute("ROLLBACK TO negative")
                    db.execute("RELEASE negative")
        self.assertTrue(self.request("GET", f"/api/rooms/{room}")[1]["current_run"]["can_recover_native"])

    def test_retained_helper_new_path_is_not_promoted_to_a_repair_and_D_is_not_discarded(self):
        failed = self.create(legacy=True)
        room = failed["room"]["id"]
        unknown = self.old_helper_cwd / "unrequested.txt"
        unknown.write_text("Unowned helper proposal, not authorized by a prose request.\n")
        code, current = self.request("GET", f"/api/rooms/{room}")
        self.assertEqual(code, 200, current)
        self.assertFalse(current["current_run"]["can_recover_native"])
        self.assertEqual(current["current_run"]["native_recovery_reason"], "native_recovery_unpublished_changes")
        self.assertTrue(unknown.exists())
        self.assertEqual(len(current["team"]["tasks"]), 3)

    def test_split_proposal_drift_during_readonly_inspection_is_rejected_before_import(self):
        failed = self.create(legacy=True)
        room, run = failed["room"]["id"], failed["current_run"]
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        before_calls = len(self.native.calls)
        try:
            code, value = self.request("POST", f"/api/rooms/{room}/messages", {"text": "继续", "target": "all", "request_id": str(uuid4()),
                "intent": "continue", "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}})
            self.assertEqual(code, 200, value)
            self.assertTrue(self.inspection_started.wait(scaled(3)))
            call = self.native.calls[before_calls]
            path = call["context"]["task"]["write_paths"][0]
            (call["cwd"] / path).write_text("Changed after host froze the proposed bytes.\n")
        finally:
            self.inspection_release.set()
        final = self.settled(room)
        self.assertEqual(self.service._project(room).snapshot()["revision"], run["revision"])
        self.assertTrue(any(job["error"] == "native_recovery_unpublished_changes" for job in final["jobs"]))
        self.assertEqual(self.resumed, 0)
        self.assertEqual(self.service._project(room).files_for_paths(["src/a.txt"])[0]["content"], "original src/a.txt\n")

    def test_pause_during_split_inspections_preserves_D_and_never_releases_work_until_resume(self):
        failed = self.create(legacy=True)
        room, run = failed["room"]["id"], failed["current_run"]
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        before_calls = len(self.native.calls)
        try:
            code, recovering = self.request("POST", f"/api/rooms/{room}/messages", {"text": "继续", "target": "all", "request_id": str(uuid4()),
                "intent": "continue", "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}})
            self.assertEqual(code, 200, recovering)
            self.assertTrue(self.inspection_started.wait(scaled(3)))
            code, paused = self.request("POST", f"/api/rooms/{room}/pause", {"request_id": str(uuid4()), "objective_id": recovering["objective"]["id"]})
            self.assertEqual(code, 200, paused)
        finally:
            self.inspection_release.set()
        paused = self.settled(room)
        self.assertEqual(paused["objective"]["status"], "paused", paused["team"])
        self.assertEqual(self.service._project(room).snapshot()["revision"], run["revision"])
        self.assertTrue(all(call["permission"] == "read_only" for call in self.native.calls[before_calls:]))
        self.assertEqual(self.resumed, 0)
        code, resumed = self.request("POST", f"/api/rooms/{room}/resume", {"request_id": str(uuid4()), "objective_id": paused["objective"]["id"]})
        self.assertEqual(code, 200, resumed)
        self.assert_repaired(self.settled(room))

    def test_database_rejection_removes_uninstalled_split_trees_and_preserves_original_D(self):
        failed = self.create(legacy=True)
        room, run = failed["room"]["id"], failed["current_run"]
        prepared = {}
        original = self.service._prepare_native_help_repairs
        def capture(*args):
            prepared.update(original(*args))
            return prepared
        before_calls = len(self.native.calls)
        with patch.object(self.service, "_prepare_native_help_repairs", side_effect=capture), patch.object(
                self.service.store, "recover_native", side_effect=RoomError("native_recovery_binding_changed")):
            code, value = self.request("POST", f"/api/rooms/{room}/messages", {"text": "继续", "target": "all", "request_id": str(uuid4()),
                "intent": "continue", "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}})
        self.assertEqual((code, value.get("error")), (409, "native_recovery_binding_changed"))
        self.assertTrue(prepared)
        for workspace in prepared.values():
            self.assertFalse(Path(workspace["project"]).parent.parent.exists())
        self.assertEqual(len(self.native.calls), before_calls)
        self.assertEqual(self.service.store.snapshot(room)["current_run"]["id"], run["id"])
        for path in ("src/a.txt", "src/b.txt"):
            self.assertEqual((self.old_helper_cwd / path).read_text(), "repaired " + path + "\n")

    def test_helper_with_only_foreign_D_can_review_owner_changed_final_bytes_from_a_fresh_tree(self):
        self.helper_foreign_only = True
        failed = self.create(legacy=True)
        room, run = failed["room"]["id"], failed["current_run"]
        code, value = self.request("POST", f"/api/rooms/{room}/messages", {"text": "继续", "target": "all", "request_id": str(uuid4()),
            "intent": "continue", "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}})
        self.assertEqual(code, 200, value)
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        files = self.service._project(room).files_for_paths(["src/a.txt", "src/b.txt"])
        self.assertEqual([file["content"] for file in files], ["original src/a.txt\n", "owner final src/b.txt\n"])
        self.assertEqual((self.old_helper_cwd / "src/b.txt").read_text(), "repaired src/b.txt\n")
        self.assertFalse(any(job["error"] for job in final["jobs"][len(failed["jobs"]):]))

    def test_cancelled_preparation_waits_for_thread_and_discards_only_new_trees(self):
        failed = self.create(legacy=True)
        room, run = failed["room"]["id"], failed["current_run"]
        prepared, started, release = {}, threading.Event(), threading.Event()
        original = self.service._prepare_native_help_repairs
        def hold(*args):
            prepared.update(original(*args))
            started.set()
            release.wait(scaled(3))
            return prepared
        with patch.object(self.service, "_prepare_native_help_repairs", side_effect=hold):
            future = asyncio.run_coroutine_threadsafe(self.service.recover_native(room, str(uuid4()), run_id=run["id"],
                revision=run["revision"], digest=run["digest"]), self.server.runner.loop)
            try:
                self.assertTrue(started.wait(scaled(3)))
                future.cancel()
            finally:
                release.set()
            poll(lambda: not any(Path(item["project"]).parent.parent.exists() for item in prepared.values()), timeout=3)
        self.assertTrue(prepared)
        self.assertTrue(all(not Path(item["project"]).parent.parent.exists() for item in prepared.values()))
        self.assertEqual(self.service.store.snapshot(room)["current_run"]["id"], run["id"])
        self.assertEqual((self.old_helper_cwd / "src/b.txt").read_text(), "repaired src/b.txt\n")
