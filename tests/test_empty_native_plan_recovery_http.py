"""Real HTTP recovery of an empty legacy carry; synthetic native transport only."""
import asyncio
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from local_agent_coordinator.project_workspace import ProjectWorkspaceError
from local_agent_coordinator.room_store import RoomError
import test_native_room_service as native
import test_native_revision_ancestor_http as ancestor
import test_native_revision_binding_http as binding


class EmptyNativePlanRecoveryHTTPTests(unittest.TestCase):
    setUp = binding.NativeRevisionBindingHTTPTests.setUp
    tearDown = binding.NativeRevisionBindingHTTPTests.tearDown
    request = binding.NativeRevisionBindingHTTPTests.request
    create = binding.NativeRevisionBindingHTTPTests.create
    settled = ancestor.NativeRevisionAncestorHTTPTests.settled
    continue_body = staticmethod(binding.NativeRevisionBindingHTTPTests.continue_body)

    report_text = "Observed current behavior; evidence boundaries retained.\n"
    cache_path = "scripts/__pycache__/check.cpython-313.pyc"
    cache_bytes = b"\x00\xffsynthetic compiled check\x01"

    def handler(self, agent, context, cwd):
        call = next(call for call in reversed(self.native.calls)
                    if call["context"]["job"]["id"] == context["job"]["id"])
        if context["job"]["job_phase"] == "work":
            if call["permission"] == "read_only":
                self.assertEqual((cwd / "results/report.md").read_text(), self.report_text)
                self.assertEqual((cwd / self.cache_path).read_bytes(), self.cache_bytes)
                self.inspections.append(call)
                self.inspection_started.set()

                async def inspect():
                    while not self.inspection_release.is_set():
                        await asyncio.sleep(0.005)
                    return native.native_reply(context, [{"type": "complete", "task_id": "M1",
                        "result": "Current S/D inspected; the historical checks were not replayed."}])

                return inspect()
            if context["task"]["id"] == "M1" and not getattr(self, "writes", 0):
                cache = cwd / self.cache_path
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(self.cache_bytes)
        return binding.NativeRevisionBindingHTTPTests.handler(self, agent, context, cwd)

    def legacy_failed_and_carried(self):
        """The temporary host alone reenacts the old generated-cache rejection."""
        self.phases, self.inspections, self.disjoint = [], [], True
        collect = self.service._collect_native_changes

        def legacy_cache_gate(job, parsed):
            collect(job, parsed)
            if job["team"]["job_phase"] == "work" and job["team"]["task_id"] == "M1":
                state = self.service.store.native_workspace(job["room_id"], job["provider"])
                self.assertEqual((Path(state["project"]) / self.cache_path).read_bytes(), self.cache_bytes)
                state["blocked"] = [{"path": self.cache_path, "reason": "native_path_not_owned"}]
                state["generated"] = []
                self.service.store.save_native_workspace(job["room_id"], job["provider"], state)
                raise RoomError("native_changes_not_imported")

        with patch.object(self.service, "_collect_native_changes", side_effect=legacy_cache_gate):
            failed = self.settled(self.create()["room"]["id"])
        self.assertEqual(failed["team"]["stop_reason"], "native_changes_not_imported", failed["team"])
        self.assertEqual([(task["id"], task["result_version"], task["status"])
                          for task in failed["team"]["tasks"]],
                         [("M1", 0, "blocked"), ("M2", 1, "blocked"), ("M3", 0, "blocked")])
        room = failed["room"]["id"]
        self.assertEqual(self.service._project(room).snapshot()["revision"], 2)
        self.private_project = Path(self.service.store.native_workspace(room, "claude")["project"])
        calls = len(self.native.calls)
        # This retired host has no provenance bridge; it carries the real board
        # but does not reinterpret its retained cache failure as permission.
        with patch.object(self.service.store, "_native_revision_hint_source", return_value=False):
            code, carried = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(failed))
            self.assertEqual(code, 200, carried)
            carried = self.settled(room)
        self.assertEqual(carried["objective"]["status"], "needs_user")
        self.assertEqual(carried["objective"]["budget"]["used"], 0)
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual([task["assignment_epoch"] for task in carried["team"]["tasks"]], [1, 1, 1])
        return failed, carried

    def legacy_empty_plan_and_restart(self):
        failed, carried = self.legacy_failed_and_carried()
        room, codex_started, codex_job = failed["room"]["id"], threading.Event(), {}

        async def legacy_empty_planners(agent, context, cwd):
            self.assertEqual(context["job"]["job_phase"], "plan")
            self.assertEqual(context["team"]["tasks"], [])
            if agent == "codex":
                codex_job["id"] = context["job"]["id"]
                codex_started.set()
                return native.native_reply(context, [{"type": "plan_ready"}])
            while not codex_started.is_set():
                await asyncio.sleep(0.005)
            for _ in range(200):
                peer = next(job for job in self.service.store.snapshot(room)["jobs"]
                            if job["id"] == codex_job["id"])
                if peer["status"] == "completed":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(peer["status"], "completed")
            self.assertEqual((cwd / "results/report.md").read_text(), self.report_text)
            return native.native_reply(context, [{"type": "plan_ready"}])

        self.native.handler = legacy_empty_planners
        # Without the newly added per-plan freeze, the ordinary collector
        # rejects exactly the previous unpublished report; no new D is written.
        # That legacy host also failed a plan call for any change left in its
        # private project; a room it stopped that way must still recover.
        with (patch.object(self.service.store, "native_preexisting_work_candidate", return_value=None),
              patch.object(native.RoomService, "_tolerated_free_paths", staticmethod(lambda job, free, leftover: set()))):
            code, empty = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(carried))
            self.assertEqual(code, 200, empty)
            empty = self.settled(room)
        self.native.handler = self.handler
        self.assertEqual(empty["team"]["stop_reason"], "native_changes_not_imported", empty["team"])
        self.assertEqual(empty["team"]["tasks"], [])
        self.assertEqual(self.service.store.native_workspace(room, "claude")["blocked"],
                         [{"path": "results/report.md", "reason": "native_path_not_owned"}])
        self.assertEqual((self.private_project / "results/report.md").read_text(), self.report_text)

        def legacy_empty_carry(db, room_id, source_run_id, target_run_id, **kwargs):
            """Exact retired empty branch: working/planned, persisted carry, no jobs."""
            self.assertEqual(room_id, room)
            self.assertEqual(source_run_id, empty["current_run"]["id"])
            self.assertIsNone(db.execute("SELECT 1 FROM shared_tasks WHERE room_id=? AND run_id=?",
                                        (room_id, source_run_id)).fetchone())
            db.execute("UPDATE teams SET phase='working' WHERE room_id=?", (room_id,))
            db.execute("UPDATE team_members SET planned=1,plan_ready_revision=0 WHERE room_id=?", (room_id,))
            objective = self.service.store._team.objectives.current(db, room_id)
            continuation = {"source_run_id": source_run_id, "preserved_task_ids": [],
                            "scope": "unfinished_work_only", "old_calls_replayed": False}
            db.execute("UPDATE objectives SET continuation_json=? WHERE id=?",
                       (json.dumps(continuation), objective["id"]))
            self.service.store._event(db, room_id, "objective_work_carried", sender="user",
                objective_id=objective["id"], source_run_id=source_run_id, run_id=target_run_id, task_ids=[])

        calls = len(self.native.calls)
        with patch.object(self.service.store, "_native_revision_hint_source", return_value=False), \
                patch.object(self.service.store._team, "carry_unfinished", side_effect=legacy_empty_carry):
            code, restarted = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(empty))
            self.assertEqual(code, 200, restarted)
            restarted = self.settled(room)
        self.assertEqual((restarted["team"]["phase"], restarted["objective"]["status"]), ("working", "running"))
        self.assertEqual(restarted["team"]["tasks"], [])
        self.assertEqual(restarted["objective"]["budget"]["used"], 0)
        self.assertTrue(all(member["planned"] for member in restarted["team"]["members"]))
        self.assertEqual(len(self.native.calls), calls)
        with self.service.store._connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM team_jobs WHERE room_id=? AND run_id=?",
                                        (room, restarted["current_run"]["id"])).fetchone())
        return failed, carried, empty, restarted

    def old_job_rows(self, ids):
        placeholders = ",".join("?" for _ in ids)
        with self.service.store._connection() as db:
            return {
                table: [dict(row) for row in db.execute(
                    f"SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}", ids)]
                for table, key in (("jobs", "id"), ("team_jobs", "job_id"), ("native_executions", "job_id"))
            }

    def assert_report_unpublished(self, room):
        with self.assertRaisesRegex(ProjectWorkspaceError, "project_file_unavailable"):
            self.service._project(room).files_for_paths(["results/report.md"])

    def test_exact_empty_restart_recovers_board_through_one_fresh_readonly_inspection(self):
        failed, carried, empty, restarted = self.legacy_empty_plan_and_restart()
        room = restarted["room"]["id"]
        code, current = self.request("GET", f"/api/rooms/{room}")
        self.assertEqual(code, 200, current)
        self.assertTrue(current["current_run"]["can_recover_native"], current["current_run"])
        old_ids = [job["id"] for job in current["jobs"]]
        old_rows = self.old_job_rows(old_ids)
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        calls = len(self.native.calls)
        try:
            code, accepted = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(current))
            self.assertEqual(code, 200, accepted)
            self.assertTrue(self.inspection_started.wait(3))
            held = self.service.store.snapshot(room)
            fresh = self.native.calls[calls:]
            self.assertEqual(len(fresh), 1)
            self.assertEqual((fresh[0]["permission"], fresh[0]["session_id"]), ("read_only", None))
            self.assertEqual(self.service._project(room).snapshot()["revision"], 2)
            self.assert_report_unpublished(room)
            tasks = {task["id"]: task for task in held["team"]["tasks"]}
            peer = next(task for task in carried["team"]["tasks"] if task["id"] == "M2")
            self.assertEqual((tasks["M2"]["result"], tasks["M2"]["result_version"], tasks["M2"]["status"]),
                             (peer["result"], 1, "review"))
            self.assertEqual(tasks["M3"]["dependencies"], ["M1", "M2"])
            self.assertEqual([tasks[name]["assignment_epoch"] for name in ("M1", "M2", "M3")], [2, 2, 2])
            proof = next(event for event in reversed(held["events"])
                         if event["kind"] == "native_partial_recovery_attestation")
            self.assertFalse(proof["canonical_imported"])
            self.assertEqual(proof["recovery_ancestry"]["source_run_id"], failed["current_run"]["id"])
            self.assertEqual(proof["recovery_ancestry"]["board_run_id"], carried["current_run"]["id"])
            self.assertEqual(proof["recovery_ancestry"]["empty_restart"]["run_id"], restarted["current_run"]["id"])
            self.assertEqual(proof["observation"]["files"][0]["sha256"],
                             hashlib.sha256(self.report_text.encode()).hexdigest())
        finally:
            self.inspection_release.set()
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(self.old_job_rows(old_ids), old_rows)
        self.assertEqual(self.writes, 1)
        self.assertEqual(len(self.inspections), 1)
        self.assertEqual(sum(phase[:2] == ("work", "M2") for phase in self.phases), 1)
        self.assertEqual((self.private_project / "results/report.md").read_text(), self.report_text)
        self.assertEqual((self.private_project / self.cache_path).read_bytes(), self.cache_bytes)
        self.assertEqual(self.service._project(room).files_for_paths(["results/report.md"])[0]["content"], self.report_text)
        self.assertEqual((self.source / "notes.md").read_text(), "Unchanged source.\n")

    def test_later_canonical_commit_cannot_be_authorized_by_the_empty_restart_chain(self):
        _, _, _, restarted = self.legacy_empty_plan_and_restart()
        room = restarted["room"]["id"]
        self.service._project(room).apply("codex", "later-independent-result", 2, [
            {"path": "later.md", "previous_sha256": None, "content": "Newer canonical content.\n"}])
        calls = len(self.native.calls)
        code, current = self.request("GET", f"/api/rooms/{room}")
        self.assertEqual(code, 200, current)
        self.assertFalse(current["current_run"]["can_recover_native"])
        self.assertEqual(current["current_run"]["native_recovery_reason"], "native_recovery_binding_changed")
        code, rejected = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(current))
        self.assertEqual(code, 409, {"error": rejected.get("error"),
                                     "continuation": rejected.get("continuation")})
        self.assertEqual(len(self.native.calls), calls)

    def test_normal_empty_plan_preserves_frozen_owner_D_without_importing_or_rejecting_it(self):
        _, carried = self.legacy_failed_and_carried()
        room = carried["room"]["id"]
        calls = len(self.native.calls)

        def inspect_only(agent, context, cwd):
            self.assertEqual(context["job"]["job_phase"], "plan")
            if agent == "claude":
                self.assertEqual((cwd / "results/report.md").read_text(), self.report_text)
                self.assertIn("native_preexisting_work", context)
            return native.native_reply(context, [{"type": "plan_ready"}])

        self.native.handler = inspect_only
        code, response = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(carried))
        self.assertEqual(code, 200, response)
        final = self.settled(room)
        plan_calls = self.native.calls[calls:]
        self.assertEqual(len(plan_calls), 2)
        self.assertTrue(all(call["context"]["job"]["job_phase"] == "plan" for call in plan_calls))
        plan_ids = {call["context"]["job"]["id"] for call in plan_calls}
        self.assertTrue(all(job["status"] == "completed" and job["error"] is None
                            for job in final["jobs"] if job["id"] in plan_ids))
        self.assertNotEqual(final["team"]["stop_reason"], "native_changes_not_imported")
        retained = [event for event in final["events"] if event["kind"] == "native_preexisting_work_retained"]
        self.assertEqual(len(retained), 1)
        self.assertFalse(retained[0]["canonical_imported"])
        self.assertEqual([file["path"] for file in retained[0]["observation"]["files"]], ["results/report.md"])
        self.assertEqual(retained[0]["observation"]["files"][0]["sha256"],
                         hashlib.sha256(self.report_text.encode()).hexdigest())
        self.assertEqual(self.service._project(room).snapshot()["revision"], 2)
        self.assert_report_unpublished(room)
        self.assertEqual((self.private_project / "results/report.md").read_text(), self.report_text)
        self.assertEqual((self.private_project / self.cache_path).read_bytes(), self.cache_bytes)

    def test_normal_plan_cannot_delete_the_frozen_untracked_owner_report(self):
        _, carried = self.legacy_failed_and_carried()
        room = carried["room"]["id"]

        def delete_retained_report(agent, context, cwd):
            self.assertEqual(context["job"]["job_phase"], "plan")
            if agent == "claude":
                self.assertIn("native_preexisting_work", context)
                self.assertEqual((cwd / "results/report.md").read_text(), self.report_text)
                (cwd / "results/report.md").unlink()
            return native.native_reply(context, [{"type": "plan_ready"}])

        self.native.handler = delete_retained_report
        code, response = self.request("POST", f"/api/rooms/{room}/messages", self.continue_body(carried))
        self.assertEqual(code, 200, response)
        final = self.settled(room)
        self.assertEqual(final["team"]["stop_reason"], "native_recovery_unpublished_changes", final["team"])
        self.assertNotEqual(final["objective"]["status"], "complete")
        self.assertFalse(any(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(self.service._project(room).snapshot()["revision"], 2)
        self.assert_report_unpublished(room)
        self.assertEqual((self.private_project / self.cache_path).read_bytes(), self.cache_bytes)


if __name__ == "__main__":
    unittest.main()
