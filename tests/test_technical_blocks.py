"""Typed technical blockers continue through real task/SQLite/private-tree lifecycles."""
import asyncio
import json
import unittest
from uuid import uuid4

from local_agent_coordinator.room_service import RoomError, _response
from local_agent_coordinator.team_store import _COMMAND_FIELDS
from tests import test_native_room_service as native_fixtures
from tests import test_team_store as store_fixtures


class TechnicalBlockNativeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native_fixtures.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native_fixtures.NativeRoomServiceTests.asyncTearDown
    handler = native_fixtures.NativeRoomServiceTests.handler
    create = native_fixtures.NativeRoomServiceTests.create
    settle = native_fixtures.NativeRoomServiceTests.settle
    partial_work_failure = native_fixtures.NativeRoomServiceTests.partial_work_failure

    async def test_peer_diagnoses_technical_failure_then_same_owner_continues_exact_partial_files(self):
        original, works = self.handler, []
        def handler(agent, context, cwd):
            phase, task = context["job"]["job_phase"], context["task"]
            if phase == "work":
                works.append(context)
                if len(works) == 1:
                    (cwd / "main.py").write_text("VALUE = 2\n")
                    return native_fixtures.native_reply(context, [{"type": "block", "task_id": "T1",
                        "cause": "technical", "reason": "The assertion exposes an implementation mismatch."}])
                self.assertEqual(task["owner"], works[0]["task"]["owner"])
                self.assertEqual(task["assignment_epoch"], works[0]["task"]["assignment_epoch"])
                self.assertEqual(task["result_version"], 0)
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                self.assertEqual(task["help_result"], "Use the checked existing implementation to correct the value.")
                (cwd / "main.py").write_text("VALUE = 3\n")
                return native_fixtures.native_reply(context, [{"type": "complete", "task_id": "T1", "result": "Corrected the actual mismatch."}],
                    unchanged=[{"path": "notes.md", "reason": "Notes still apply."}])
            if phase == "help":
                self.assertEqual(context["native_permission"], "full_access")
                self.assertNotEqual(agent, task["owner"])
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                reply = native_fixtures.native_reply(context)
                reply["message"] = "Use the checked existing implementation to correct the value."
                return reply
            if phase == "review":
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 3\n")
                return native_fixtures.native_reply(context, [{"type": "review", "task_id": "T1", "verdict": "accept", "reason": "Checked the corrected exact version."}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(works), 2)
        self.assertEqual((final["team"]["tasks"][0]["status"], final["team"]["tasks"][0]["result_version"]), ("accepted", 1))
        self.assertFalse(any(event["kind"] == "objective_decision_required" for event in final["events"]))
        self.assertTrue(any(event["kind"] == "technical_block_continued" and event["helper"] == "codex" for event in final["events"]))
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")

    async def test_technical_review_failure_returns_to_owner_then_requires_a_new_exact_review(self):
        original, reviews, works = self.handler, [], []
        def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "review":
                reviews.append(context["task"]["result_version"])
                if len(reviews) == 1:
                    return native_fixtures.native_reply(context, [{"type": "block", "task_id": "T1", "cause": "technical",
                        "reason": "A bound verification check found a concrete correction to investigate."}])
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 3\n")
                return native_fixtures.native_reply(context, [{"type": "review", "task_id": "T1", "verdict": "accept", "reason": "Checked corrected version two."}])
            if phase == "help":
                reply = native_fixtures.native_reply(context)
                reply["message"] = "Correct the existing implementation before publishing a new result version."
                return reply
            if phase == "work":
                works.append(agent)
                if len(works) > 1:
                    self.assertEqual(context["task"]["result_version"], 1)
                    self.assertIn("new result version", context["task"]["help_result"])
                    (cwd / "main.py").write_text("VALUE = 3\n")
                    return native_fixtures.native_reply(context, [{"type": "complete", "task_id": "T1", "result": "Corrected the review finding."}],
                        unchanged=[{"path": "notes.md", "reason": "Notes still apply."}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(reviews, [1, 2])
        self.assertEqual(works, ["claude", "claude"])
        self.assertEqual(final["team"]["tasks"][0]["status"], "accepted")
        self.assertEqual(final["team"]["tasks"][0]["review_kind"], "independent_review")
        self.assertFalse(any(event["kind"] == "objective_decision_required" for event in final["events"]))

    async def test_solo_keeps_advancing_four_distinct_file_versions_without_fake_peer_help(self):
        original, works = self.handler, []
        def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "work":
                works.append(context)
                if len(works) > 1:
                    self.assertIn("not independent help", context["task"]["help_result"])
                (cwd / "main.py").write_text(f"VALUE = {len(works) + 1}\n")
                if len(works) <= 4:
                    return native_fixtures.native_reply(context, [{"type": "block", "task_id": "T1", "cause": "technical",
                        "reason": "The next concrete implementation issue requires further diagnosis."}])
                return native_fixtures.native_reply(context, [{"type": "complete", "task_id": "T1", "result": "Finished after concrete file progress."}],
                    unchanged=[{"path": "notes.md", "reason": "Notes still apply."}])
            if context["job"]["job_phase"] == "review":
                return native_fixtures.native_reply(context, [{"type": "review", "task_id": "T1", "verdict": "accept", "reason": "Self-checked the exact final version."}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create(participants=["claude"])
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(works), 5)
        self.assertEqual(final["team"]["tasks"][0]["status"], "self_checked")
        events = [event for event in final["events"] if event["kind"] == "technical_block_continued"]
        self.assertEqual(len(events), 4)
        self.assertTrue(all(event["helper"] is None for event in events))
        self.assertFalse(any(call["context"]["job"]["job_phase"] == "help" for call in self.fake.calls))

    async def test_same_file_evidence_hands_repeated_blocks_to_the_team_despite_new_wording(self):
        # Catches: three technical blocks with unchanged files stopped the objective as
        # objective_no_progress and left it for the user. Now the third one is handed to the team
        # to replan; here nobody proposes anything, so the plan stalls and the host continues it.
        original, count = self.handler, []
        def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "work":
                count.append(context["job"]["id"])
                return native_fixtures.native_reply(context, [{"type": "block", "task_id": "T1", "cause": "technical",
                    "reason": f"Different diagnosis wording {len(count)} without any new file evidence."}])
            if phase == "help":
                reply = native_fixtures.native_reply(context)
                reply["message"] = f"A differently worded suggestion {len(count)}."
                return reply
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(len(count), 3)
        self.assertEqual(len(set(count)), 3)
        self.assertEqual(final["objective"]["stop_reason"], "team_plan_stalled")
        self.assertEqual(final["stop_kind"], "auto")
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "technical_block_continued"]), 2)
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "internal_replan_started"]), 1)
        self.assertEqual((final["team"]["tasks"][0]["status"], final["team"]["tasks"][0]["block_cause"]), ("blocked", "technical"))
        self.assertEqual(final["team"]["tasks"][0]["result_version"], 0)
        self.assertFalse(any(event.get("code") == "objective_no_progress" for event in final["events"]))
        self.assertFalse(any(event["kind"] == "objective_decision_required" for event in final["events"]))

    async def test_only_explicit_human_causes_wait_for_the_user_and_none_is_guessed_from_reason_text(self):
        # Catches: guessing the cause from the reason's wording; a user decision or permission block
        # not waiting for the user; and an external or uncategorised block still becoming a question
        # for the user instead of going back to the team (here the team proposes nothing, so the
        # plan stalls, which the host continues by itself).
        original = self.handler
        for cause in ("user_decision", "permission_required", "external_dependency", None):
            with self.subTest(cause=cause):
                def handler(agent, context, cwd):
                    if context["job"]["job_phase"] == "work":
                        return native_fixtures.native_reply(context, [{"type": "block", "task_id": "T1", "cause": cause,
                            "reason": "The same text describes a technical failure and a requirement needing clarification."}])
                    return original(agent, context, cwd)
                self.fake.handler = handler
                start = len(self.fake.calls)
                created = await self.create()
                await self.settle()
                final = await self.service.read_room(created["room"]["id"])
                if cause in ("user_decision", "permission_required"):
                    self.assertEqual((final["objective"]["status"], final["stop_kind"]), ("needs_user", None))
                else:
                    self.assertEqual((final["objective"]["status"], final["objective"]["stop_reason"], final["stop_kind"]),
                                     ("stopped", "team_plan_stalled", "auto"))
                    self.assertFalse(any(event["kind"] == "objective_decision_required" for event in final["events"]))
                self.assertEqual(final["team"]["tasks"][0]["block_cause"], cause or "unclassified")
                self.assertEqual(final["team"]["tasks"][0]["status"], "blocked")
                self.assertEqual(final["room"]["runtime_settings"]["permission"], "full_access")
                self.assertFalse(any(call["context"]["job"]["job_phase"] == "help" for call in self.fake.calls[start:]))

    async def test_pause_during_peer_diagnosis_preserves_answer_without_dispatching_work(self):
        ready, release = asyncio.Event(), asyncio.Event()
        original, works = self.handler, []
        async def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "work":
                works.append(context)
                if len(works) == 1:
                    return native_fixtures.native_reply(context, [{"type": "block", "task_id": "T1", "cause": "technical", "reason": "Investigate the bound failing assertion."}])
                self.assertEqual(context["task"]["help_result"], "The paused diagnosis answer remains available.")
            if phase == "help":
                ready.set()
                await release.wait()
                reply = native_fixtures.native_reply(context)
                reply["message"] = "The paused diagnosis answer remains available."
                return reply
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await asyncio.wait_for(ready.wait(), 3)
        room = created["room"]["id"]
        objective = (await self.service.read_room(room))["objective"]["id"]
        await self.service.objective_action(room, "pause", "pause-technical-help", objective_id=objective)
        release.set()
        await self.settle()
        paused = await self.service.read_room(room)
        self.assertEqual(paused["objective"]["status"], "paused")
        self.assertEqual(len(works), 1)
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in paused["jobs"]))
        await self.service.objective_action(room, "resume", "resume-technical-help", objective_id=objective)
        await self.settle()
        self.assertEqual((await self.service.read_room(room))["objective"]["status"], "complete")

    async def test_technical_label_cannot_bypass_retained_private_inspection(self):
        before, binding = await self.partial_work_failure()
        room, start = before["room"]["id"], len(self.fake.calls)
        def handler(agent, context, cwd):
            self.assertTrue(context["native_partial_inspection"])
            return native_fixtures.native_reply(context, [{"type": "block", "task_id": "T1", "cause": "technical",
                "reason": "Retained evidence cannot yet establish a safe continuation."}])
        self.fake.handler = handler
        await self.service.recover_native(room, "partial-technical-inspection", **binding)
        await self.settle()
        final = await self.service.read_room(room)
        self.assertEqual(len(self.fake.calls[start:]), 1)
        self.assertEqual(final["team"]["tasks"][0]["status"], "blocked", {
            "team": final["team"], "calls": [(call["context"]["job"]["job_phase"],
                bool(call["context"].get("native_partial_inspection"))) for call in self.fake.calls[start:]]})
        self.assertEqual(final["team"]["tasks"][0]["result_version"], 0)
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertFalse(any(event["kind"] == "technical_block_continued" for event in final["events"]))

    async def test_fresh_inspection_rejects_an_old_session_before_importing_retained_bytes(self):
        before, binding = await self.partial_work_failure()
        room, start = before["room"]["id"], len(self.fake.calls)
        old_identity = next(member["session_id"] for member in before["members"] if member["id"] == "claude")
        old_jobs = {job["id"]: (job["status"], job["error"]) for job in before["jobs"]}
        def handler(agent, context, cwd):
            self.assertTrue(context["native_partial_inspection"])
            return native_fixtures.native_reply(context, [{"type": "complete", "task_id": "T1", "result": "Read the retained bytes."}])
        self.fake.handler = handler
        self.fake.failure = {"session_id": old_identity}
        await self.service.recover_native(room, "old-session-refused", **binding)
        await self.settle()
        final = await self.service.read_room(room)
        self.assertEqual(len(self.fake.calls[start:]), 1)
        self.assertEqual(final["team"]["stop_reason"], "session_identity_changed")
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertEqual({job["id"]: (job["status"], job["error"]) for job in final["jobs"] if job["id"] in old_jobs}, old_jobs)
        self.assertFalse(any(event["kind"] == "native_partial_work_queued" for event in final["events"]))


class TechnicalBlockStoreTests(unittest.TestCase):
    setUp = store_fixtures.TeamStoreTests.setUp
    tearDown = store_fixtures.TeamStoreTests.tearDown
    finish = store_fixtures.TeamStoreTests.finish
    plan = store_fixtures.TeamStoreTests.plan
    assertCode = store_fixtures.TeamStoreTests.assertCode

    def room(self, **options):
        return store_fixtures.TeamStoreTests.room(self, execution_policy={"mode": "autonomous"}, **options)

    def test_cause_never_overrides_assignment_epoch_or_closed_command_validation(self):
        room = self.plan(dependency=True)
        job = self.store.claim(room, "claude")
        command = {"type": "block", "task_id": "main", "cause": "technical", "reason": "A technical failure."}
        self.assertCode("invalid_team_commands", lambda: self.store.prevalidate_team_result(job["id"], [{**command, "cause": "anything"}]))
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND id='main'", (room,))
        self.assertCode("team_assignment_changed", lambda: self.store.prevalidate_team_result(job["id"], [command]))
        self.assertFalse(any(event["kind"] == "technical_block_continued" for event in self.store.snapshot(room)["events"]))

    def test_previous_static_flat_block_shape_remains_unknown_not_implicitly_technical(self):
        fields = set().union(*_COMMAND_FIELDS.values()) - {"cause"}
        command = {field: None for field in fields}
        command.update(type="block", task_id="main", reason="A technical failure in old data.")
        parsed = _response(json.dumps({"message": "A recorded obstacle.", "state": "needs_user", "commands": [command]}),
                           "claude", ("claude", "codex"), team=True)
        self.assertEqual(parsed["commands"], [{"type": "block", "task_id": "main", "reason": command["reason"], "cause": None}])
        room = self.plan(dependency=True)
        self.finish(self.store.claim(room, "claude"), parsed["commands"])
        task = next(task for task in self.store.snapshot(room)["team"]["tasks"] if task["id"] == "main")
        self.assertEqual((task["status"], task["block_cause"]), ("blocked", "unclassified"))

    def test_first_cleanup_identity_registration_is_bound_to_this_job_room_and_owner(self):
        room, _ = self.store.create_room("A native fixture.", "claude", "identity-room", mode="native_project", project_id=str(uuid4()))
        first, peer = self.store.claim(room, "claude"), self.store.claim(room, "codex")
        identity = str(uuid4())
        with self.store._connection(write=True) as db:
            db.execute("UPDATE team_runs SET fresh_sessions=1 WHERE room_id=?", (room,))
            # Model aliases using the same adapter still may not share sessions.
            db.execute("UPDATE members SET adapter='claude' WHERE room_id=?", (room,))
        self.store.retain_native_session_identity(first["id"], identity, source="native_result")
        with self.store._connection() as db:
            self.assertFalse(self.store._session_conflict(db, room, "claude", identity, job_id=first["id"]))
            self.assertTrue(self.store._session_conflict(db, room, "claude", identity, job_id=peer["id"]))
            self.assertTrue(self.store._session_conflict(db, room, "codex", identity, job_id=peer["id"]))
            self.assertTrue(self.store._session_conflict(db, room, "claude", identity))
        other, _ = self.store.create_room("Another native fixture.", "claude", "other-identity-room", mode="native_project", project_id=str(uuid4()))
        second = self.store.claim(other, "claude")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE team_runs SET fresh_sessions=1 WHERE room_id=?", (other,))
            db.execute("UPDATE members SET adapter='claude' WHERE room_id=?", (other,))
        self.store.retain_native_session_identity(second["id"], identity, source="native_result")
        with self.store._connection() as db:
            self.assertTrue(self.store._session_conflict(db, other, "claude", identity, job_id=second["id"]))

