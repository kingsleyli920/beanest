"""Restart a real temporary HTTP service; never invoke a model or a real CLI."""
import json
import os
from pathlib import Path
import threading
import unittest
from uuid import uuid4

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
import test_empty_native_plan_recovery_http as fixtures


class EmptyCarryRestartHTTPTests(unittest.TestCase):
    setUp = fixtures.EmptyNativePlanRecoveryHTTPTests.setUp
    tearDown = fixtures.EmptyNativePlanRecoveryHTTPTests.tearDown
    request = fixtures.EmptyNativePlanRecoveryHTTPTests.request
    create = fixtures.EmptyNativePlanRecoveryHTTPTests.create
    settled = fixtures.EmptyNativePlanRecoveryHTTPTests.settled
    continue_body = staticmethod(fixtures.EmptyNativePlanRecoveryHTTPTests.continue_body)
    handler = fixtures.EmptyNativePlanRecoveryHTTPTests.handler
    legacy_failed_and_carried = fixtures.EmptyNativePlanRecoveryHTTPTests.legacy_failed_and_carried
    legacy_empty_plan_and_restart = fixtures.EmptyNativePlanRecoveryHTTPTests.legacy_empty_plan_and_restart
    old_job_rows = fixtures.EmptyNativePlanRecoveryHTTPTests.old_job_rows
    assert_report_unpublished = fixtures.EmptyNativePlanRecoveryHTTPTests.assert_report_unpublished
    report_text = fixtures.EmptyNativePlanRecoveryHTTPTests.report_text
    cache_path = fixtures.EmptyNativePlanRecoveryHTTPTests.cache_path
    cache_bytes = fixtures.EmptyNativePlanRecoveryHTTPTests.cache_bytes

    @staticmethod
    def state_metadata(snapshot):
        return {
            "room_status": snapshot["room"]["status"],
            "team_phase": snapshot["team"]["phase"],
            "team_stop_reason": snapshot["team"]["stop_reason"],
            "objective_id": snapshot["objective"]["id"],
            "objective_status": snapshot["objective"]["status"],
            "objective_stop_reason": snapshot["objective"]["stop_reason"],
            "current_run_id": snapshot["current_run"]["id"],
            "can_recover_native": snapshot["current_run"]["can_recover_native"],
            "native_recovery_reason": snapshot["current_run"]["native_recovery_reason"],
            "native_recovery_kind": snapshot["current_run"]["native_recovery_kind"],
            "task_count": len(snapshot["team"]["tasks"]),
            "job_count": len(snapshot["jobs"]),
            "native_execution_count": len(snapshot["native_executions"]),
            "last_two_events": [{key: event[key] for key in (
                "seq", "kind", "sender", "run_id", "objective_id", "code", "status", "created_at")
                if key in event} for event in snapshot["events"][-2:]],
        }

    def save_observation(self, observation):
        destination = os.environ.get("EMPTY_CARRY_STARTUP_OBSERVATION")
        if destination:
            path = Path(destination)
            data = json.loads(path.read_text()) if path.exists() else {}
            data[self.id()] = {**data.get(self.id(), {}), **observation}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def reopen_empty_carry(self):
        failed, carried, empty, restarted = self.legacy_empty_plan_and_restart()
        room = restarted["room"]["id"]
        body = self.continue_body(restarted)
        old_ids = [job["id"] for job in restarted["jobs"]]
        old_rows = self.old_job_rows(old_ids)
        calls = len(self.native.calls)
        previous_service = self.service
        self.server.shutdown()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.server.server_close()
        self.assertTrue(previous_service._closed)
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   native_invoke=self.native, registry=default_registry())
        self.assertIsNot(self.service, previous_service)
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        code, current = self.request("GET", f"/api/rooms/{room}")
        self.assertEqual(code, 200, {"error": current.get("error")})
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(self.old_job_rows(old_ids), old_rows)
        self.save_observation({"after_reopen_get": self.state_metadata(current),
                               "startup_native_call_count": len(self.native.calls) - calls})
        return failed, carried, empty, restarted, current, body, old_ids, old_rows

    def recover_after_service_restart(self, *, runtime_update=False):
        failed, carried, _, restarted, current, body, old_ids, old_rows = self.reopen_empty_carry()
        room = current["room"]["id"]
        if runtime_update:
            for agent in ("claude", "codex"):
                self.service._runtime_catalogs[agent] = {"state": "ready", "models": [
                    {"id": "new", "efforts": ["high"]}]}
            body["runtime_settings"] = {"permission": "full_access", "members": {
                agent: {"model": "new", "effort": "high"} for agent in ("claude", "codex")}}
        self.inspection_started, self.inspection_release = threading.Event(), threading.Event()
        calls = len(self.native.calls)
        try:
            code, accepted = self.request("POST", f"/api/rooms/{room}/messages", body)
            self.save_observation({
                "after_reopen_get": self.state_metadata(current),
                "continue_http_status": code,
                "continue_error": accepted.get("error"),
                "after_continue": self.state_metadata(accepted) if "current_run" in accepted else None,
                "startup_native_call_count": 0,
                "native_calls_added_by_continue": len(self.native.calls) - calls,
                "runtime_update_requested": runtime_update,
            })
            self.assertTrue(current["current_run"]["can_recover_native"], self.state_metadata(current))
            self.assertEqual(code, 200, {"error": accepted.get("error"),
                                        "native_calls_added": len(self.native.calls) - calls})
            self.assertTrue(self.inspection_started.wait(3), "fresh read-only inspection did not start")
            held = self.service.store.snapshot(room)
            fresh = self.native.calls[calls:]
            self.assertEqual(len(fresh), 1)
            self.assertEqual((fresh[0]["permission"], fresh[0]["session_id"]), ("read_only", None))
            if runtime_update:
                self.assertEqual(fresh[0]["runtime_settings"], {"model": "new", "effort": "high"})
            self.assertEqual(self.service._project(room).snapshot()["revision"], 2)
            self.assert_report_unpublished(room)
            tasks = {task["id"]: task for task in held["team"]["tasks"]}
            old_peer = next(task for task in carried["team"]["tasks"] if task["id"] == "M2")
            self.assertEqual((tasks["M2"]["result"], tasks["M2"]["result_version"], tasks["M2"]["status"]),
                             (old_peer["result"], 1, "review"))
            self.assertEqual(tasks["M3"]["dependencies"], ["M1", "M2"])
            self.assertEqual([tasks[name]["assignment_epoch"] for name in ("M1", "M2", "M3")], [2, 2, 2])
            proof = next(event for event in reversed(held["events"])
                         if event["kind"] == "native_partial_recovery_attestation")
            self.assertFalse(proof["canonical_imported"])
            self.assertEqual(proof["recovery_ancestry"]["source_run_id"], failed["current_run"]["id"])
            self.assertEqual(proof["recovery_ancestry"]["empty_restart"]["run_id"], restarted["current_run"]["id"])
            restart_proof = proof["recovery_ancestry"]["empty_restart"]["service_restart"]
            self.assertEqual(restart_proof, {
                "objective_stopped_seq": current["events"][-2]["seq"],
                "room_status_seq": current["events"][-1]["seq"],
            })
            self.save_observation({"fresh_first_call": {
                "permission": fresh[0]["permission"], "session_id": fresh[0]["session_id"],
                "phase": fresh[0]["context"]["job"]["job_phase"]},
                "service_restart_proof": restart_proof, "canonical_revision_before_inspection_release": 2})
        finally:
            self.inspection_release.set()
        final = self.settled(room)
        self.assertEqual(final["objective"]["status"], "complete", {
            "phase": final["team"]["phase"], "stop_reason": final["team"]["stop_reason"]})
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertEqual(self.old_job_rows(old_ids), old_rows)
        self.assertEqual((self.private_project / "results/report.md").read_text(), self.report_text)
        self.assertEqual((self.private_project / self.cache_path).read_bytes(), self.cache_bytes)
        self.assertEqual(self.writes, 1)
        self.assertEqual(len(self.inspections), 1)
        self.assertEqual(sum(phase[:2] == ("work", "M2") for phase in self.phases), 1)
        self.assertEqual(self.service._project(room).files_for_paths(["results/report.md"])[0]["content"], self.report_text)
        self.save_observation({"final_objective_status": final["objective"]["status"],
                               "old_job_and_native_rows_unchanged": True,
                               "report_and_cache_bytes_unchanged": True,
                               "fresh_inspection_count": len(self.inspections)})

    def test_new_service_recovers_the_same_empty_carry_through_fresh_readonly_before_import(self):
        self.recover_after_service_restart()

    def test_continue_can_change_model_and_effort_without_erasing_restart_authority(self):
        self.recover_after_service_restart(runtime_update=True)

    def test_restart_authority_requires_the_exact_system_stop_and_room_status_events(self):
        _, _, _, _, current, _, _, _ = self.reopen_empty_carry()
        room = current["room"]["id"]
        self.assertTrue(current["current_run"]["can_recover_native"], self.state_metadata(current))
        objective = current["objective"]["id"]
        run = current["current_run"]["id"]
        stopped = next(event["seq"] for event in reversed(current["events"])
                       if event["kind"] == "objective_stopped" and event.get("objective_id") == objective)
        room_status = next(event["seq"] for event in reversed(current["events"])
                           if event["kind"] == "room_status" and event.get("run_id") == run)
        mutations = [
            ("missing objective stop", "DELETE FROM events WHERE room_id=? AND seq=?", (room, stopped)),
            ("non-system stop", "UPDATE events SET value_json=json_set(value_json,'$.sender','user') WHERE room_id=? AND seq=?", (room, stopped)),
            ("wrong objective", "UPDATE events SET value_json=json_set(value_json,'$.objective_id',?) WHERE room_id=? AND seq=?", (str(uuid4()), room, stopped)),
            ("missing room status", "DELETE FROM events WHERE room_id=? AND seq=?", (room, room_status)),
            ("wrong run", "UPDATE events SET value_json=json_set(value_json,'$.run_id',?) WHERE room_id=? AND seq=?", (str(uuid4()), room, room_status)),
            ("user stop code", "UPDATE events SET value_json=json_set(value_json,'$.code','cancelled') WHERE room_id=? AND seq=?", (room, stopped)),
        ]
        calls = len(self.native.calls)
        with self.service.store._connection(write=True) as db:
            for label, sql, parameters in mutations:
                with self.subTest(mutation=label):
                    db.execute("SAVEPOINT missing_restart_authority")
                    try:
                        db.execute(sql, parameters)
                        self.assertEqual(self.service.store._native_revision_hint_ancestor(db, room), [])
                    finally:
                        db.execute("ROLLBACK TO missing_restart_authority")
                        db.execute("RELEASE missing_restart_authority")
            self.assertEqual(len(self.service.store._native_revision_hint_ancestor(db, room)), 1)
        self.assertEqual(len(self.native.calls), calls)
        # An interrupted objective cannot be paused. Exercise its real user
        # cancellation route rather than manufacturing an impossible pause.
        code, cancelled = self.request("POST", f"/api/rooms/{room}/cancel", {})
        self.assertEqual(code, 200, {"error": cancelled.get("error")})
        self.assertTrue(any(event["kind"] == "warning" and event.get("code") == "cancel_requested"
                            for event in cancelled["events"][len(current["events"]):]))
        self.assertFalse(cancelled["current_run"]["can_recover_native"])
        with self.service.store._connection() as db:
            self.assertEqual(self.service.store._native_revision_hint_ancestor(db, room), [])
        self.assertEqual(len(self.native.calls), calls)
        self.save_observation({"rejected_mutations": [label for label, _, _ in mutations],
                               "real_later_control": "HTTP cancel_requested",
                               "native_calls_added_during_negative_checks": len(self.native.calls) - calls})


if __name__ == "__main__":
    unittest.main()
