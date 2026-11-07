"""Room invitation over real HTTP; temporary profiles and synthetic native work."""
from __future__ import annotations

import asyncio
import hashlib
import json
import select
import signal
import sys
import threading
import time
import unittest
from urllib.parse import quote, urlencode
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator.room_store import RoomError
from test_kimi_native_setup import configuration
from test_kimi_setup_http import SetupFixture
from test_native_room_service import NativeTransport, native_reply
from test_team_store import proposal


SHARED = "Published current canonical delivery before the invitation.\n"
CONTENTS = {"T0": SHARED, "T1": "Left owner completed the original work.\n",
            "T2": "Right owner completed the original work.\n"}
PATHS = {"T0": "deliveries/shared.md", "T1": "deliveries/left.md", "T2": "deliveries/right.md"}


class RoomInvitationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SetupFixture(discover=False)
        self.service, self.server = self.fixture.service, self.fixture.server
        self.port, self.root, self.source = self.fixture.port, self.fixture.root, self.fixture.source
        self.fixture.config.write_text(configuration(), encoding="utf-8")
        self.native = NativeTransport()
        self.native.handler = self.handler
        self.service.native_invoke = self.native
        self.old_started = {provider: threading.Event() for provider in ("claude", "codex")}
        self.plan_started = {provider: threading.Event() for provider in ("claude", "codex")}
        self.new_started = threading.Event()
        self.release_old = threading.Event()
        self.release_new = threading.Event()
        self.release_plan = threading.Event()
        self.cancelled_calls = []
        self.held_goal = "Complete the held invitation fixture without restarting existing work."
        self.planning_goal = "Plan the held invitation fixture without restarting existing calls."
        self.completed_goal = "Complete the disposable invitation fixture."
        self.legacy_patch = None
        self.new_call = None
        self.room_id = None
        self.completed_room_id = None
        self.addCleanup(self.fixture.close)

        async def no_python_runner():
            return {"available": False, "reason": "fictional_fixture"}
        self.service._execution_ready = no_python_runner
        self.request("POST", "/api/partners/discover", {})
        registration = self.request("POST", "/api/projects", {
            "name": "Invitation fixture project", "path": str(self.source), "selection_kind": "folder",
        })
        self.project_id = registration["project"]["id"]

    def tearDown(self):
        self.release_workers()
        try:
            self.doCleanups()
        finally:
            if self.legacy_patch is not None:
                self.legacy_patch.stop()

    def request(self, method, path, body=None, *, expected=200, mcp=False):
        code, value = self.fixture.request(method, path, body, mcp=mcp)
        self.assertEqual(code, expected, {"status": code, "error": value.get("error"), "path": path.split("?")[0]})
        return value

    def read(self, room_id=None):
        return self.request("GET", f"/api/rooms/{room_id or self.room_id}")

    def wait_room(self, room_id, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = self.read(room_id)
            if condition(snapshot):
                return snapshot
            time.sleep(.005)
        self.fail("Invitation fixture did not settle: " + json.dumps({
            "room": snapshot["room"]["status"], "team": snapshot["team"]["phase"],
            "objective": snapshot["objective"]["status"],
            "jobs": [{key: job[key] for key in ("provider", "status", "error")} for job in snapshot["jobs"]],
        }))

    def create(self, goal):
        return self.request("POST", "/api/rooms", {"goal": goal, "target": "claude", "request_id": str(uuid4()),
            "mode": "native_project", "project_id": self.project_id, "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})

    def bootstrap_held(self):
        created = self.create(self.held_goal)
        self.room_id = created["room"]["id"]
        return self.wait_room(self.room_id, lambda snapshot: all(event.is_set() for event in self.old_started.values())
                              and next(task for task in snapshot["team"]["tasks"] if task["id"] == "T0")["status"] == "accepted")

    def bootstrap_complete(self):
        created = self.create(self.completed_goal)
        self.completed_room_id = created["room"]["id"]
        return self.wait_room(self.completed_room_id, lambda snapshot: snapshot["objective"]["status"] == "complete"
                              and not any(job["status"] in {"queued", "working"} for job in snapshot["jobs"]))

    def bootstrap_planning(self):
        created = self.create(self.planning_goal)
        self.room_id = created["room"]["id"]
        return self.wait_room(self.room_id, lambda snapshot: all(event.is_set() for event in self.plan_started.values()))

    def release_workers(self):
        self.release_old.set()
        self.release_new.set()
        self.release_plan.set()

    async def hold(self, event, context):
        try:
            while not event.is_set():
                await asyncio.sleep(.005)
        except asyncio.CancelledError:
            self.cancelled_calls.append(context["job"]["id"])
            raise

    async def handler(self, agent, context, cwd):
        phase, task = context["job"]["job_phase"], context.get("task")
        goal = context["objective"]["original_goal"]
        if agent == "kimi" and phase in {"plan", "coordinate", "user"}:
            self.new_call = self.native.calls[-1]
            self.new_started.set()
            await self.hold(self.release_new, context)
            return native_reply(context, [{"type": "plan_ready"}] if phase in {"plan", "coordinate"} else [])
        if phase in {"plan", "coordinate"}:
            if goal == self.planning_goal and phase == "plan":
                self.plan_started[agent].set()
                await self.hold(self.release_plan, context)
            tasks = context["team"]["tasks"]
            commands = []
            if not tasks and agent == "claude":
                commands = [proposal("T0", paths=[PATHS["T0"]]),
                            proposal("T1", dependencies=["T0"], paths=[PATHS["T1"]]),
                            proposal("T2", dependencies=["T0"], paths=[PATHS["T2"]]),
                            {"type": "claim", "task_id": "T0"}, {"type": "claim", "task_id": "T1"}]
            elif agent == "codex" and any(item["id"] == "T2" and item["owner"] is None for item in tasks):
                commands = [{"type": "claim", "task_id": "T2"}]
            return native_reply(context, [*commands, {"type": "plan_ready"}])
        if phase == "work":
            path = cwd / PATHS[task["id"]]
            path.parent.mkdir(exist_ok=True)
            path.write_text(CONTENTS[task["id"]], encoding="utf-8")
            if goal == self.held_goal and task["id"] in {"T1", "T2"}:
                self.old_started[agent].set()
                await self.hold(self.release_old, context)
            return native_reply(context, [{"type": "complete", "task_id": task["id"],
                "result": "Published the exact " + task["id"] + " file for independent review."}])
        if phase == "review":
            self.assertEqual((cwd / PATHS[task["id"]]).read_text(), CONTENTS[task["id"]])
            return native_reply(context, [{"type": "review", "task_id": task["id"],
                "verdict": "accept", "reason": "Read this task's exact published file."}])
        if phase == "checkpoint":
            return native_reply(context, [{"type": "checkpoint", "decision": "complete",
                "summary": "All original files have completed ordinary work and review.", "next_goal": None,
                "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept",
                "reason": "The original scope is complete.",
                "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        if phase == "user":
            return native_reply(context)
        self.fail("Unexpected synthetic phase: " + phase)

    def with_legacy_handoff_metadata(self, room_id):
        """Only the old handoff field is synthetic; this does not test creating a handoff.

        The referenced old project has ordinary immutable revisions. Every
        current accepted result and held claim still comes from real HTTP work.
        """
        old_room, _ = self.service.store.create_room("Legacy handoff metadata fixture", "claude", str(uuid4()),
            mode="project_folder", project_id=self.project_id, participants=["claude", "codex"], collaboration=True)
        old = self.service._project(old_room, create=True)
        old.seed_folder(self.service.catalog.get(self.project_id))
        baseline = old.metadata_for_paths(["README.md"])[0]
        previous = old.apply("claude", "legacy-handoff-candidate", old.snapshot()["revision"], [{
            "path": "README.md", "previous_sha256": baseline["sha256"], "content": "Old handoff candidate must not enter a new invitee tree.\n",
        }])
        binding = {"source_room_id": old_room, "source_run_id": self.service.store.snapshot(old_room)["current_run"]["id"],
                   "revision": previous["revision"], "digest": previous.get("snapshot_sha256")}
        snapshot = self.service.store.snapshot
        def legacy_snapshot(identifier, **options):
            value = snapshot(identifier, **options)
            if identifier == room_id:
                value["native_handoff"] = binding
            return value
        self.legacy_patch = patch.object(self.service.store, "snapshot", side_effect=legacy_snapshot)
        self.legacy_patch.start()
        return binding

    def invite(self, room_id, participants, *, request_id=None, query="", expected=200, mcp=False):
        body = {"request_id": request_id or str(uuid4()), "participants": participants}
        prefix = "/api/mcp" if mcp else "/api"
        return self.request("POST", f"{prefix}/rooms/{room_id}/members" + query, body, expected=expected, mcp=mcp)

    def test_invite_during_two_owned_calls_keeps_claims_and_uses_current_canonical_tree(self):
        before = self.bootstrap_held()
        room_id = before["room"]["id"]
        self.with_legacy_handoff_metadata(room_id)
        old_workers = {provider: self.service._workers[(room_id, provider)] for provider in ("claude", "codex")}
        old_jobs = {job["id"]: job for job in before["jobs"] if job["status"] == "working"}
        self.assertEqual(len(old_jobs), 2)
        old_tasks = {task["id"]: task for task in before["team"]["tasks"]}
        self.invite(room_id, ["kimi"])
        joined = self.wait_room(room_id, lambda snapshot: self.new_started.is_set())
        self.assertEqual(joined["current_run"]["id"], before["current_run"]["id"])
        self.assertEqual(joined["objective"]["id"], before["objective"]["id"])
        self.assertEqual({member["id"] for member in joined["members"]}, {"claude", "codex", "kimi"})
        self.assertEqual(self.cancelled_calls, [])
        for provider, worker in old_workers.items():
            self.assertIs(self.service._workers[(room_id, provider)], worker)
            self.assertFalse(worker.done())
        for job in joined["jobs"]:
            if job["id"] in old_jobs:
                self.assertEqual(job, old_jobs[job["id"]])
        self.assertEqual({task["id"]: task for task in joined["team"]["tasks"]}, old_tasks)
        call = self.new_call
        self.assertIsNone(call["session_id"])
        self.assertEqual(call["context"]["job"]["job_phase"], "coordinate")
        self.assertEqual((call["cwd"] / PATHS["T0"]).read_text(), SHARED)
        self.assertEqual((call["cwd"] / "README.md").read_text(), (self.source / "README.md").read_text())
        self.assertFalse((call["cwd"] / PATHS["T1"]).exists())
        self.assertFalse((call["cwd"] / PATHS["T2"]).exists())
        shared = next(task for task in call["context"]["team"]["tasks"] if task["id"] == "T0")
        self.assertEqual(shared["status"], "accepted")
        self.assertEqual(shared["result_files"][0]["sha256"], hashlib.sha256(SHARED.encode()).hexdigest())
        self.release_old.set()
        accepted = self.wait_room(room_id, lambda snapshot: all(task["status"] == "accepted" for task in snapshot["team"]["tasks"]))
        self.assertFalse(self.release_new.is_set())
        self.assertTrue(all(next(job for job in accepted["jobs"] if job["id"] == identifier)["status"] == "completed" for identifier in old_jobs))
        self.assertEqual(next(job for job in accepted["jobs"] if job["provider"] == "kimi")["status"], "working")
        new_job_id = call["context"]["job"]["id"]
        with self.assertRaises(RoomError) as late_proposal:
            self.service.store.prevalidate_team_result(new_job_id, [proposal("LATE", paths=["deliveries/late.md"])])
        self.assertEqual(late_proposal.exception.code, "team_plan_locked")
        self.assertTrue(self.service.store.prevalidate_team_result(new_job_id, [{"type": "plan_ready"}]))
        self.assertEqual(self.read(room_id)["team"]["tasks"], accepted["team"]["tasks"])
        self.release_new.set()
        final = self.wait_room(room_id, lambda snapshot: snapshot["objective"]["status"] == "complete")
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
        self.assertFalse(any(job["error"] for job in final["jobs"]))
        for task in final["team"]["tasks"]:
            self.assertEqual((task["owner"], task["assignment_epoch"]), (old_tasks[task["id"]]["owner"], old_tasks[task["id"]]["assignment_epoch"]))
        self.assertTrue(all(next(job for job in final["jobs"] if job["id"] == identifier)["status"] == "completed" for identifier in old_jobs))

    def test_completed_room_invitation_only_adds_an_idle_member(self):
        before = self.bootstrap_complete()
        room_id = before["room"]["id"]
        calls = len(self.native.calls)
        joined = self.invite(room_id, ["kimi"])
        self.assertEqual(joined["objective"]["status"], "complete")
        self.assertEqual(joined["current_run"]["id"], before["current_run"]["id"])
        self.assertEqual(joined["jobs"], before["jobs"])
        self.assertEqual(joined["team"]["tasks"], before["team"]["tasks"])
        member = next(member for member in joined["members"] if member["id"] == "kimi")
        self.assertEqual(member["status"], "idle")
        self.assertIsNone(member["session_id"])
        self.assertIsInstance(member["joined_pending"], bool)
        self.assertEqual(len(self.native.calls), calls)
        self.assertTrue(joined["member_invitation"]["supported"])

    def test_invite_ack_outbox_and_both_idempotency_keys_create_one_initial_job(self):
        before = self.bootstrap_held()
        room_id = before["room"]["id"]
        scope = self.request("GET", "/api/status")["user_outbox_scope"]
        key = str(uuid4())
        body = {"request_id": key, "participants": ["kimi"]}
        staged = self.request("POST", "/api/outbox", {"scope": scope, "request_id": key, "kind": "invite",
            "room_id": room_id, "body": body, "source_seq": before["events"][-1]["seq"]})
        self.assertEqual(staged["outbox"]["kind"], "invite")
        query = "?" + urlencode({"outbox_scope": scope, "response": "ack"})
        ack = self.invite(room_id, ["kimi"], request_id=key, query=query)
        self.assertEqual(ack, {"acknowledged": True, "room_id": room_id, "request_id": key,
            "state_url": f"/api/rooms/{room_id}/state", "status": "running", "cancellation_confirmed": None,
            "current_run_id": before["current_run"]["id"], "objective_id": before["objective"]["id"]})
        self.wait_room(room_id, lambda snapshot: self.new_started.is_set())
        joined = self.read(room_id)
        initial_jobs = [job["id"] for job in joined["jobs"] if job["provider"] == "kimi"]
        self.assertEqual(len(initial_jobs), 1)
        self.assertEqual(self.invite(room_id, ["kimi"], request_id=key, query=query), ack)
        self.invite(room_id, ["kimi"], request_id=str(uuid4()))
        repeated = self.read(room_id)
        self.assertEqual([job["id"] for job in repeated["jobs"] if job["provider"] == "kimi"], initial_jobs)
        self.assertEqual(sum(member["id"] == "kimi" for member in repeated["members"]), 1)
        self.assertEqual(sum(member["id"] == "kimi" for member in repeated["team"]["members"]), 1)
        self.assertEqual(sum(call["agent"] == "kimi" for call in self.native.calls), 1)
        conflict = self.invite(room_id, ["codex"], request_id=key, expected=409)
        self.assertEqual(conflict["error"], "request_conflict")
        exact = self.request("GET", "/api/outbox/" + quote(key, safe="") + "?" + urlencode({"scope": scope}))
        self.assertEqual(exact["outbox"]["delivery"]["state"], "received")

    def test_invalid_batch_and_mcp_never_partially_add_or_dispatch_a_member(self):
        before = self.bootstrap_held()
        room_id = before["room"]["id"]
        self.request("POST", "/api/partners", {"id": "old_json", "name": "Old Kimi JSON",
                     "adapter": "kimi", "connection": None})
        calls = len(self.native.calls)
        for members, status in ((["kimi", "old_json"], 409), (["kimi", "missing_partner"], 409), (["kimi", "kimi"], 400)):
            with self.subTest(participants=members):
                denied = self.invite(room_id, members, expected=status)
                if "missing_partner" in members:
                    self.assertEqual(denied["error"], "agent_unavailable")
                snapshot = self.read(room_id)
                self.assertEqual(snapshot["members"], before["members"])
                self.assertEqual(snapshot["jobs"], before["jobs"])
        self.invite(room_id, ["kimi"], expected=403, mcp=True)
        after = self.read(room_id)
        self.assertEqual(after["members"], before["members"])
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(len(self.native.calls), calls)

    def test_planning_invitee_gets_one_plan_call_while_old_plans_stay_in_flight(self):
        before = self.bootstrap_planning()
        room_id = before["room"]["id"]
        workers = {provider: self.service._workers[(room_id, provider)] for provider in ("claude", "codex")}
        self.invite(room_id, ["kimi"])
        joined = self.wait_room(room_id, lambda snapshot: self.new_started.is_set())
        self.assertEqual(self.new_call["context"]["job"]["job_phase"], "plan")
        self.assertIsNone(self.new_call["session_id"])
        self.assertEqual(joined["current_run"]["id"], before["current_run"]["id"])
        self.assertEqual(self.cancelled_calls, [])
        for provider, worker in workers.items():
            self.assertIs(self.service._workers[(room_id, provider)], worker)
            self.assertFalse(worker.done())

    def test_paused_planning_invitee_stays_idle_until_resume_queues_its_first_plan(self):
        planning = self.bootstrap_planning()
        room_id, objective_id = planning["room"]["id"], planning["objective"]["id"]
        self.request("POST", f"/api/rooms/{room_id}/pause", {"request_id": str(uuid4()), "objective_id": objective_id})
        self.release_plan.set()
        paused = self.wait_room(room_id, lambda snapshot: snapshot["objective"]["status"] == "paused"
                                and not any(job["status"] in {"queued", "working"} for job in snapshot["jobs"]))
        self.assertEqual(paused["team"]["phase"], "planning")
        joined = self.invite(room_id, ["kimi"])
        self.assertEqual(joined["objective"]["status"], "paused")
        self.assertEqual(joined["jobs"], paused["jobs"])
        self.assertEqual(next(member for member in joined["members"] if member["id"] == "kimi")["status"], "idle")
        self.assertFalse(self.new_started.is_set())
        self.request("POST", f"/api/rooms/{room_id}/resume", {"request_id": str(uuid4()), "objective_id": objective_id})
        resumed = self.wait_room(room_id, lambda snapshot: self.new_started.is_set())
        self.assertEqual(resumed["current_run"]["id"], paused["current_run"]["id"])
        self.assertEqual(self.new_call["context"]["job"]["job_phase"], "plan")
        self.assertIsNone(self.new_call["session_id"])


def fixture_main():
    case = RoomInvitationHTTPTests()
    case.setUp()
    try:
        complete = case.bootstrap_complete()
        held = case.bootstrap_held()
        info = {"url": case.server.url, "port": case.port, "instance_id": case.server.instance_id,
                "root": str(case.root), "source": str(case.source), "held_room": held["room"]["id"],
                "completed_room": complete["room"]["id"], "real_model_calls": 0}
        print(json.dumps(info, ensure_ascii=False), flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.is_set():
            available, _, _ = select.select([sys.stdin], [], [], .25)
            if not available:
                continue
            command = sys.stdin.readline().strip()
            if command in {"", "stop"}:
                break
            if command == "release":
                case.release_workers()
            elif command == "release-old":
                case.release_old.set()
            elif command == "release-new":
                case.release_new.set()
            elif command == "status":
                current = case.read(held["room"]["id"])
                print(json.dumps({"room": current["room"]["status"], "objective": current["objective"]["status"],
                    "members": [member["id"] for member in current["members"]],
                    "synthetic_calls": len(case.native.calls), "real_model_calls": 0}), flush=True)
    finally:
        case.tearDown()
        print(json.dumps({"closed": True, "temporary_removed": not case.root.exists(), "real_model_calls": 0}), flush=True)


if __name__ == "__main__":
    unittest.main()
