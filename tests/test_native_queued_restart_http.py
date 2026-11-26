"""A native review queued during a service loss resumes without replaying work."""
import asyncio
import json
from collections import Counter
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_server import create_room_server
from test_team_store import proposal
import test_long_team_result_http as fixture
import test_native_room_service as native


class NativeQueuedRestartHTTPTests(unittest.TestCase):
    request = fixture.LongTeamResultHTTPTests.request
    settled = fixture.LongTeamResultHTTPTests.settled
    tearDown = fixture.LongTeamResultHTTPTests.tearDown

    def setUp(self):
        self.allow_review = False
        self.queued_review = threading.Event()
        self.work = Counter()
        fixture.LongTeamResultHTTPTests.setUp(self)
        self.service._guard_availability = self.guard

    async def guard(self, members):
        for room in self.service.store.list_rooms()["rooms"]:
            tasks = self.service.store.snapshot(room["id"])["team"]["tasks"]
            if not self.allow_review and any(task["id"] == "T1" and task["result_version"] for task in tasks):
                self.queued_review.set()
                await asyncio.Event().wait()

    def handler(self, agent, context, cwd):
        phase, task = context["job"]["job_phase"], context.get("task")
        commands = []
        if phase in {"plan", "coordinate"}:
            if not context["team"]["tasks"]:
                for number in range(3):
                    identifier = "T" + str(number)
                    commands.extend([proposal(identifier, dependencies=["T" + str(number - 1)] if number else [],
                                              paths=[identifier + ".txt"]), {"type": "claim", "task_id": identifier}])
            commands.append({"type": "plan_ready"})
        elif phase == "work":
            self.work[task["id"]] += 1
            (cwd / (task["id"] + ".txt")).write_text("Published " + task["id"] + "\n")
            commands = [{"type": "complete", "task_id": task["id"], "result": "Published the task's exact bytes."}]
        elif phase == "review":
            commands = [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Checked exact bytes."}]
        elif phase == "checkpoint":
            commands = [{"type": "checkpoint", "decision": "complete", "summary": "All tasks checked.", "next_goal": None,
                         "scope_reference": context["objective"]["scope_reference"], "question": None}]
        elif phase == "checkpoint_review":
            commands = [{"type": "checkpoint_review", "verdict": "accept", "reason": "Verified delivery.",
                         "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}]
        elif phase != "user":
            self.fail(phase)
        return native.native_reply(context, commands)

    def restart_service_before_claim(self):
        code, created = self.request("POST", "/api/rooms", {"goal": "Publish three dependent files and check them.",
            "target": "codex", "participants": ["codex"], "request_id": str(uuid4()), "mode": "native_project",
            "project_id": self.project_id, "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        self.room_id = created["room"]["id"]
        self.assertTrue(self.queued_review.wait(5), "Review did not reach the pre-claim guard")
        code, current = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200)
        self.assertEqual(self.work, {"T0": 1, "T1": 1})
        self.assertTrue(any(job["kind"] == "team_review" and job["status"] == "queued" for job in current["jobs"]))
        code, paused = self.request("POST", f"/api/rooms/{self.room_id}/pause", {
            "request_id": str(uuid4()), "objective_id": current["objective"]["id"]})
        self.assertEqual(code, 200, paused)

        async def lose_preclaim_worker():
            # Synthetic process loss: no model/process ran for the queued call.
            workers = [worker for worker in self.service._workers.values() if not worker.done()]
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        self.server.runner.call(lose_preclaim_worker)
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.allow_review = True
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   native_invoke=self.native, registry=default_registry())
        self.service._schedule_auto_continue = lambda *args: None
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        code, stopped = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200, stopped)
        if stopped["objective"]["status"] == "paused":
            code, stopped = self.request("POST", f"/api/rooms/{self.room_id}/resume", {
                "request_id": str(uuid4()), "objective_id": stopped["objective"]["id"]})
            self.assertEqual(code, 200, stopped)
        stopped = self.settled(self.room_id)
        self.assertTrue(stopped["current_run"]["can_restart_objective"], stopped["current_run"])
        self.source_run = stopped["current_run"]["id"]
        return stopped

    def restart(self, stopped):
        run = stopped["current_run"]
        return self.request("POST", f"/api/rooms/{self.room_id}/restart-objective", {
            "request_id": str(uuid4()), "run_id": run["id"], "revision": run["revision"], "digest": run["digest"],
            "execution_policy": {"mode": "autonomous"}})

    def test_queued_review_resumes_on_normal_restart_and_preserves_accepted_work(self):
        stopped = self.restart_service_before_claim()
        old_jobs = [job["id"] for job in stopped["jobs"]]
        before = len(self.native.calls)
        code, resumed = self.restart(stopped)
        self.assertEqual(code, 200, resumed)
        final = self.settled(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(self.work, {"T0": 1, "T1": 1, "T2": 1})
        first = self.native.calls[before]["context"]
        self.assertEqual((first["job"]["job_phase"], first["task"]["id"]), ("review", "T1"))
        self.assertNotIn(first["job"]["id"], old_jobs)
        self.assertEqual(first["task"]["result_version"], 1)

    def legacy_carried(self):
        stopped = self.restart_service_before_claim()
        with patch.object(self.service.store._team, "_resume_unstarted_carried_restart", return_value=None):
            code, current = self.restart(stopped)
        self.assertEqual(code, 200, current)
        self.assertEqual(current["team"]["phase"], "working")
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in current["jobs"]))
        self.assertEqual([task["status"] for task in current["team"]["tasks"]], ["self_checked", "interrupted", "interrupted"])
        return current

    def test_existing_carried_board_resumes_in_same_objective_after_normal_pause_resume(self):
        current = self.legacy_carried()
        code, paused = self.request("POST", f"/api/rooms/{self.room_id}/pause", {
            "request_id": str(uuid4()), "objective_id": current["objective"]["id"]})
        self.assertEqual(code, 200, paused)
        before = len(self.native.calls)
        code, resumed = self.request("POST", f"/api/rooms/{self.room_id}/resume", {
            "request_id": str(uuid4()), "objective_id": current["objective"]["id"]})
        self.assertEqual(code, 200, resumed)
        final = self.settled(self.room_id)
        self.assertEqual(final["objective"]["id"], current["objective"]["id"])
        self.assertEqual(final["current_run"]["id"], current["current_run"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(self.work, {"T0": 1, "T1": 1, "T2": 1})
        self.assertEqual(self.native.calls[before]["context"]["job"]["job_phase"], "review")

    def test_guarded_or_changed_boards_do_not_reset_the_interrupted_review(self):
        current = self.legacy_carried()
        store = self.service.store
        run_id = current["current_run"]["id"]
        objective_id = current["objective"]["id"]
        with store._connection(write=True) as db:
            source_job = db.execute("SELECT j.id FROM jobs j JOIN team_jobs t ON t.job_id=j.id "
                "WHERE t.run_id=? AND t.task_id='T1' AND t.phase='review' AND j.error='service_restarted'",
                (self.source_run,)).fetchone()[0]
            def claimed():
                store._event(db, self.room_id, "job_claimed", sender="codex", job_id=source_job)
                store._event(db, self.room_id, "job_cleanup", sender="system", job_id=source_job, cleanup_confirmed=True)
            cases = {
                "claimed_source_call": claimed,
                "cleanup_unconfirmed": lambda: db.execute("UPDATE rooms SET stop_confirmed=0 WHERE id=?", (self.room_id,)),
                "identity_uncertain": lambda: db.execute("UPDATE members SET identity_uncertain=1 WHERE room_id=?", (self.room_id,)),
                "used_current_run": lambda: db.execute("UPDATE team_runs SET used=1 WHERE id=?", (run_id,)),
                "current_run_job": lambda: store._team._queue(db, self.room_id, "codex", "user", text="Existing queued user request."),
                "changed_task": lambda: db.execute("UPDATE shared_tasks SET review_kind='independent_review' "
                                                    "WHERE run_id=? AND id='T1'", (run_id,)),
                "user_stop": lambda: db.execute("UPDATE rooms SET stop_requested=1 WHERE id=?", (self.room_id,)),
                "user_pause": lambda: db.execute("UPDATE objectives SET status='paused' WHERE id=?", (objective_id,)),
                "quota_retired": lambda: db.execute("UPDATE objectives SET retired_json=? WHERE id=?", (
                    json.dumps({"codex": {"reason": "agent_quota_exhausted", "at": 1, "cleanup_confirmed": True}}), objective_id)),
            }
            for name, alter in cases.items():
                with self.subTest(guard=name):
                    db.execute("SAVEPOINT guarded_board")
                    try:
                        alter()
                        store._team.reconcile(db, self.room_id)
                        task = db.execute("SELECT status,blocked_reason FROM shared_tasks WHERE run_id=? AND id='T1'", (run_id,)).fetchone()
                        self.assertEqual(tuple(task), ("interrupted", "service_restarted"))
                        self.assertFalse(db.execute("SELECT 1 FROM team_jobs WHERE run_id=? AND phase IN ('work','review')", (run_id,)).fetchone())
                    finally:
                        db.execute("ROLLBACK TO guarded_board")
                        db.execute("RELEASE guarded_board")
        self.assertEqual(self.work, {"T0": 1, "T1": 1})
