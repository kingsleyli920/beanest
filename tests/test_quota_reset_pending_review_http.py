"""Quota reset resumes published pending bytes through HTTP, without accepting or replaying them."""
from uuid import uuid4
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.agent_registry import default_registry

import test_long_team_result_http as fixture
import test_native_room_service as native
from local_agent_coordinator.room_store import RoomError
from test_team_store import proposal


class QuotaResetPendingReviewHTTPTests(unittest.TestCase):
    request = fixture.LongTeamResultHTTPTests.request
    settled = fixture.LongTeamResultHTTPTests.settled
    tearDown = fixture.LongTeamResultHTTPTests.tearDown

    def setUp(self):
        self.quota_reset = False
        fixture.LongTeamResultHTTPTests.setUp(self)
        self.service._guard_availability = self.guard

    async def guard(self, members):
        for room in self.service.store.list_rooms()["rooms"]:
            tasks = self.service.store.snapshot(room["id"])["team"]["tasks"]
            if not self.quota_reset and any(task["id"] == "T2" and task["result_version"] > 0 for task in tasks):
                raise RoomError("agent_quota_exhausted")

    def handler(self, agent, context, cwd):
        phase, task = context["job"]["job_phase"], context.get("task")
        commands = []
        if phase in {"plan", "coordinate"}:
            if not context["team"]["tasks"]:
                commands = [proposal("T1", paths=["main.py"]),
                            proposal("T2", dependencies=["T1"], paths=["main.py"]),
                            {"type": "claim", "task_id": "T1"}, {"type": "claim", "task_id": "T2"}]
            commands.append({"type": "plan_ready"})
        elif phase == "work":
            (cwd / "main.py").write_text("VALUE = " + ("1" if task["id"] == "T1" else "2") + "\n")
            commands = [{"type": "complete", "task_id": task["id"], "result": "Published exact source bytes."}]
        elif phase == "review":
            commands = [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Checked source bytes."}]
        elif phase == "checkpoint":
            commands = [{"type": "checkpoint", "decision": "complete", "summary": "Both changes reviewed.",
                         "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}]
        elif phase == "checkpoint_review":
            commands = [{"type": "checkpoint_review", "verdict": "accept", "reason": "Verified source.",
                         "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}]
        elif phase != "user":
            self.fail(phase)
        return native.native_reply(context, commands)

    def stopped(self):
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Publish and check two dependent changes of the same file.", "target": "codex", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": ["codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, created)
        self.room_id = created["room"]["id"]
        stopped = self.settled(self.room_id)
        self.assertEqual(stopped["team"]["stop_reason"], "all_partners_quota_exhausted", stopped["team"])
        tasks = {task["id"]: task for task in stopped["team"]["tasks"]}
        self.assertEqual(tasks["T1"]["status"], "self_checked")
        self.assertEqual((tasks["T2"]["status"], tasks["T2"]["result_version"]), ("blocked", 1))
        self.assertEqual(tasks["T2"]["blocked_reason"], "all_partners_quota_exhausted")
        self.quota_reset = True
        return stopped

    def restart(self, snapshot):
        project = snapshot["current_run"]
        return self.request("POST", f"/api/rooms/{self.room_id}/restart-objective", {
            "request_id": str(uuid4()), "run_id": snapshot["current_run"]["id"],
            "revision": project["revision"], "digest": project["digest"], "execution_policy": {"mode": "autonomous"}})

    def test_quota_reset_keeps_pending_review_and_never_replays_published_work(self):
        stopped = self.stopped()
        previous_calls = len(self.native.calls)
        code, resumed = self.restart(stopped)
        self.assertEqual(code, 200, resumed)
        final = self.settled(self.room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        later = self.native.calls[previous_calls:]
        self.assertFalse(any(call["context"]["job"]["job_phase"] == "work" for call in later))
        self.assertTrue(any(call["context"]["job"]["job_phase"] == "review" and
                            call["context"]["task"]["id"] == "T2" for call in later))
        baseline = final["objective"]["baseline"]
        self.assertEqual(baseline["files"], [])
        change, = baseline["changed_since_delivery"]
        self.assertEqual(change["path"], "main.py")
        self.assertEqual(change["published_by"]["task_id"], "T2")
        self.assertEqual(change["published_by"]["review_kind"], "pending_review")

    def test_fresh_positive_observation_wakes_wait_before_periodic_cooldown(self):
        stopped = self.stopped()
        now = [self.service.clock() + 1]
        self.service.clock = lambda: now[0]
        recovered = [False]
        manager = PartnerManager(self.root / "partners", default_registry(), startup_discovery=False)
        real_get = manager.get
        manager.get = lambda provider: {**real_get(provider),
            "availability": {"state": "available" if recovered[0] else "blocked", "observed_at": now[0],
                             "selectable": recovered[0], "reason": None if recovered[0] else "agent_quota_exhausted"},
            "account": {**real_get(provider)["account"], "quota": {"stale": False, "checked_at": now[0],
                "windows": [{"used_percent": 0 if recovered[0] else 100}]}}}
        manager.refresh_availability = AsyncMock()
        self.service.partner_manager = manager

        async def tick():
            await self.service.expire_tasks()
            wait = self.service._quota_waits.get(self.room_id)
            if wait:
                await wait["task"]

        # First finished check still sees a genuine exhausted account.
        self.service._quota_waits.clear()
        self.server.runner.call(tick)
        code, waiting = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(waiting["current_run"]["id"], stopped["current_run"]["id"])
        calls = len(self.native.calls)
        # A second check without a new positive observation is suppressed.
        now[0] += 1
        self.server.runner.call(tick)
        self.assertEqual(len(self.native.calls), calls)
        recovered[0] = True
        now[0] += 1
        self.server.runner.call(tick)
        final = self.settled(self.room_id)
        self.assertNotEqual(final["current_run"]["id"], stopped["current_run"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertTrue(any(event["kind"] == "quota_wait_resumed" for event in final["events"]))
        self.assertFalse(any(call["context"]["job"]["job_phase"] == "work" for call in self.native.calls[calls:]))

    def test_snapshot_hides_published_warnings_but_preserves_changed_bytes_and_history(self):
        self.stopped()
        manifest = self.service.store.native_workspace(self.room_id, "codex")
        manifest["blocked"] = [{"path": "main.py", "reason": "native_path_not_owned"},
                               {"path": "notes.md", "reason": "native_path_not_owned"}]
        private = Path(manifest["project"])
        (private / "notes.md").write_text("Private draft still needs publication.\n")
        self.service.store.save_native_workspace(self.room_id, "codex", manifest)
        code, view = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200, view)
        workspace, = view["native_workspaces"]
        self.assertEqual(workspace["published_count"], 1)
        self.assertEqual([issue["path"] for issue in workspace["blocked"]], ["notes.md"])
        self.assertNotEqual(workspace["blocked"][0]["sha256"], workspace["blocked"][0]["canonical_sha256"])
        self.assertEqual(self.service.store.native_workspace(self.room_id, "codex"), manifest)
        # The next real private edit makes the formerly settled warning relevant again.
        (private / "main.py").write_text("VALUE = 4\n")
        code, changed = self.request("GET", f"/api/rooms/{self.room_id}")
        workspace, = changed["native_workspaces"]
        self.assertEqual(workspace["published_count"], 0)
        self.assertEqual(len(workspace["blocked"]), 2)
        self.assertEqual(self.service.store.native_workspace(self.room_id, "codex"), manifest)

    def test_conflict_only_warning_tracks_current_bytes_without_changing_persisted_manifest(self):
        self.stopped()
        manifest = self.service.store.native_workspace(self.room_id, "codex")
        manifest["blocked"] = []
        manifest["conflicts"] = [{"path": "main.py", "kind": "concurrent_changes",
                                  "candidate_sha256": "0" * 64, "source_sha256": "1" * 64}]
        self.service.store.save_native_workspace(self.room_id, "codex", manifest)
        code, view = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200, view)
        workspace, = view["native_workspaces"]
        self.assertEqual(workspace["conflicts"], [])
        self.assertEqual(workspace["state"], "ready")
        private = Path(manifest["project"])
        (private / "main.py").write_text("VALUE = 400\n")
        code, view = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200, view)
        workspace, = view["native_workspaces"]
        self.assertEqual(workspace["state"], "blocked")
        issue, = workspace["conflicts"]
        self.assertNotEqual(issue["sha256"], issue["canonical_sha256"])
        self.assertEqual(self.service.store.native_workspace(self.room_id, "codex"), manifest)

    def test_warning_audit_failure_keeps_unknown_warning_and_room_readable(self):
        self.stopped()
        manifest = self.service.store.native_workspace(self.room_id, "codex")
        manifest["blocked"] = [{"path": "main.py", "reason": "native_path_not_owned"}]
        self.service.store.save_native_workspace(self.room_id, "codex", manifest)
        from local_agent_coordinator.native_workspace import NativeWorkspaceError
        for error in (NativeWorkspaceError("invalid_native_manifest"), OSError("Private tree unavailable")):
            with self.subTest(error=type(error).__name__), patch(
                    "local_agent_coordinator.room_service.native_workspace.pending_blocked", side_effect=error):
                code, snapshot = self.request("GET", f"/api/rooms/{self.room_id}")
                self.assertEqual(code, 200, snapshot)
                self.assertTrue(snapshot["project"]["available"])
                self.assertEqual(snapshot["native_workspaces"][0]["blocked"], manifest["blocked"])
        with patch.object(self.service.store, "native_workspace", return_value=None):
            code, snapshot = self.request("GET", f"/api/rooms/{self.room_id}")
            self.assertEqual(code, 200, snapshot)
            self.assertTrue(snapshot["project"]["available"])
            self.assertEqual(snapshot["native_workspaces"][0]["blocked"], manifest["blocked"])

    def test_published_binding_is_rechecked_inside_restart_transaction(self):
        stopped = self.stopped()
        action = self.service.store.objective_action

        def changed_before_transaction(*args, **kwargs):
            with self.service.store._connection(write=True) as db:
                db.execute("UPDATE shared_tasks SET result_version=result_version+1 WHERE room_id=? AND run_id=? AND id='T2'",
                           (self.room_id, stopped["current_run"]["id"]))
            return action(*args, **kwargs)

        self.service.store.objective_action = changed_before_transaction
        previous_calls = len(self.native.calls)
        code, rejected = self.restart(stopped)
        self.assertEqual((code, rejected.get("error")), (409, "objective_binding_changed"))
        self.assertEqual(len(self.native.calls), previous_calls)
        code, unchanged = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(unchanged["current_run"]["id"], stopped["current_run"]["id"])

    def test_unpublished_canonical_drift_still_rejects_restart(self):
        stopped = self.stopped()
        project = self.service._project(self.room_id)
        current = project.snapshot()
        project.apply("codex", "unpublished-drift", current["revision"], [{"path": "main.py",
            "previous_sha256": project.files_for_paths(["main.py"])[0]["sha256"], "content": "VALUE = 3\n"}])
        code, fresh = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200, fresh)
        previous_calls = len(self.native.calls)
        code, rejected = self.restart(fresh)
        self.assertEqual((code, rejected.get("error")), (409, "objective_binding_changed"))
        self.assertEqual(len(self.native.calls), previous_calls)
