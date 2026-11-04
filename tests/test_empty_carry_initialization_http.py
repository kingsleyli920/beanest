"""An empty failed plan restarts real planning instead of an inert working board."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from local_agent_coordinator.room_store import RoomError
import test_native_room_service as native
import test_native_revision_binding_http as binding
import test_empty_native_plan_recovery_http as retained
from local_agent_coordinator.room_service import _project_digest


class EmptyCarryInitializationHTTPTests(unittest.TestCase):
    setUp = binding.NativeRevisionBindingHTTPTests.setUp
    tearDown = binding.NativeRevisionBindingHTTPTests.tearDown
    request = binding.NativeRevisionBindingHTTPTests.request
    create = binding.NativeRevisionBindingHTTPTests.create
    settled = binding.NativeRevisionBindingHTTPTests.settled

    def handler(self, agent, context, cwd):
        if not getattr(self, "legacy_empty_plan", False):
            return binding.NativeRevisionBindingHTTPTests.handler(self, agent, context, cwd)

        async def empty_reply():
            self.assertEqual(context["job"]["job_phase"], "plan")
            self.assertEqual(context["team"]["tasks"], [])
            if agent == "codex":
                self.peer_job = context["job"]["id"]
            else:
                for _ in range(300):
                    if getattr(self, "peer_job", None):
                        room = self.service.store.list_rooms()["rooms"][0]["id"]
                        peer = next(job for job in self.service.store.snapshot(room)["jobs"]
                                    if job["id"] == self.peer_job)
                        if peer["status"] == "completed":
                            break
                    await asyncio.sleep(0.005)
                else:
                    self.fail("Synthetic peer plan did not finish")
            return native.native_reply(context, [{"type": "plan_ready"}])
        return empty_reply()

    def test_continue_from_an_empty_failed_plan_initializes_and_completes_actual_work(self):
        self.legacy_empty_plan = True
        collect = self.service._collect_native_changes

        def old_plan_failure(job, parsed):
            collect(job, parsed)
            if job["provider"] == "claude" and job["team"]["job_phase"] == "plan":
                raise RoomError("native_changes_not_imported")

        # A temporary legacy host stopped before accepting any task proposal.
        # Neither planner wrote files; no retained-D candidate grants anything.
        with patch.object(self.service, "_collect_native_changes", side_effect=old_plan_failure):
            failed = self.settled(self.create()["room"]["id"])
        self.assertEqual(failed["team"]["tasks"], [])
        self.assertEqual(failed["team"]["stop_reason"], "native_changes_not_imported")
        self.assertTrue(failed["current_run"]["can_restart_objective"])
        self.assertFalse(failed["current_run"]["can_recover_native"])
        old_jobs = {job["id"]: (job["status"], job["error"]) for job in failed["jobs"]}
        prior = len(self.native.calls)
        self.legacy_empty_plan = False
        room = failed["room"]["id"]
        code, accepted = self.request("POST", f"/api/rooms/{room}/messages",
                                      binding.NativeRevisionBindingHTTPTests.continue_body(failed))
        self.assertEqual(code, 200, accepted)
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertNotEqual(final["current_run"]["id"], failed["current_run"]["id"])
        phases = [call["context"]["job"]["job_phase"] for call in self.native.calls[prior:]]
        self.assertIn("plan", phases)
        self.assertIn("work", phases)
        self.assertEqual(len(final["team"]["tasks"]), 3)
        self.assertEqual({job["id"]: (job["status"], job["error"]) for job in final["jobs"] if job["id"] in old_jobs}, old_jobs)
        self.assertEqual((self.source / "notes.md").read_text(), "Unchanged source.\n")


class EmptyCarryProvenanceTests(unittest.TestCase):
    def fixture(self):
        case = retained.EmptyNativePlanRecoveryHTTPTests(
            "test_exact_empty_restart_recovers_board_through_one_fresh_readonly_inspection")
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def test_exact_empty_restart_cannot_borrow_missing_changed_or_unclean_provenance(self):
        case = self.fixture()
        failed, carried, empty, current = case.legacy_empty_plan_and_restart()
        room = current["room"]["id"]
        source = next(job["id"] for job in failed["jobs"] if job["error"] == "native_changes_not_imported")
        with case.service.store._connection() as db:
            plan = db.execute("SELECT j.id FROM jobs j JOIN team_jobs t ON t.job_id=j.id "
                              "WHERE t.run_id=? AND t.phase='plan' AND j.error='native_changes_not_imported'",
                              (empty["current_run"]["id"],)).fetchone()["id"]
        mutations = [
            ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='objective_work_carried' "
             "AND json_extract(value_json,'$.run_id')=?", (room, current["current_run"]["id"])),
            ("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='objective_decision' "
             "AND json_extract(value_json,'$.run_id')=?", (room, empty["current_run"]["id"])),
            ("UPDATE shared_tasks SET owner='codex' WHERE run_id=? AND id='M1'", (carried["current_run"]["id"],)),
            ("UPDATE shared_tasks SET result_version=result_version+1 WHERE run_id=? AND id='M2'", (carried["current_run"]["id"],)),
            ("UPDATE shared_tasks SET dependencies_json='[]' WHERE run_id=? AND id='M3'", (carried["current_run"]["id"],)),
            ("UPDATE shared_tasks SET block_cause='permission_required' WHERE run_id=? AND id='M1'", (failed["current_run"]["id"],)),
            ("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.cleanup_confirmed',json('false')) WHERE job_id=?", (source,)),
            ("UPDATE native_executions SET metadata_json=json_set(metadata_json,'$.permission','read_only') WHERE job_id=?", (source,)),
            ("UPDATE team_jobs SET run_id=?,phase='user' WHERE job_id=?", (current["current_run"]["id"], plan)),
        ]
        calls = len(case.native.calls)
        with case.service.store._connection(write=True) as db:
            self.assertEqual(len(case.service.store._native_revision_hint_ancestor(db, room)), 1)
            for sql, parameters in mutations:
                with self.subTest(sql=sql):
                    db.execute("SAVEPOINT invalid_proof")
                    db.execute(sql, parameters)
                    self.assertEqual(case.service.store._native_revision_hint_ancestor(db, room), [])
                    db.execute("ROLLBACK TO invalid_proof")
                    db.execute("RELEASE invalid_proof")
        self.assertEqual(len(case.native.calls), calls)

    def test_task_path_changes_cannot_be_rebased_even_with_a_newer_planning_head(self):
        case = self.fixture()
        _, _, _, current = case.legacy_empty_plan_and_restart()
        room = current["room"]["id"]
        candidate = case.service.store.native_partial_recovery_candidate(room)
        project = case.service._project(room)
        project.apply("claude", "intervening-task-path", 2, [{
            "path": "results/report.md", "previous_sha256": None, "content": "A different canonical task result.\n"}])
        actual = project.snapshot()
        # Give the audit an otherwise matching current S. An updated overall
        # binding cannot erase the source task path's intervening version.
        candidate = copy.deepcopy(candidate)
        candidate["recovery_ancestry"]["planning_binding"] = {
            "revision": actual["revision"], "digest": _project_digest(actual),
            "index_sha256": project.index_sha256(actual["revision"])}
        state = case.service.store.native_workspace(room, "claude")
        state.update(revision=actual["revision"], index_sha256=project.index_sha256(actual["revision"]))
        state["files"] = project.metadata_for_paths(["results/report.md"])
        with self.assertRaisesRegex(RoomError, "native_recovery_binding_changed"):
            case.service._audit_retained_native_scope(project, candidate, state, actual)


if __name__ == "__main__":
    unittest.main()
