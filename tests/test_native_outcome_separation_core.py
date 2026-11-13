"""Native execution facts stay independent of rejected business submissions."""
import json
import unittest

from tests import test_native_room_service as native

native_reply = native.native_reply


class NativeOutcomeCoreTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    async def create(self):
        created = await native.NativeRoomServiceTests.create(self)
        await self.settle()
        return await self.service.read_room(created["room"]["id"])
    handler = native.NativeRoomServiceTests.handler

    async def test_raw_bad_json_corrects_after_native_completion(self):
        invoke = self.service.native_invoke
        rejected = []
        async def malformed(*args, **kwargs):
            result = await invoke(*args, **kwargs)
            call = self.fake.calls[-1]
            if call["agent"] == "claude" and call["context"]["job"]["job_phase"] == "plan" and not rejected:
                rejected.append(call["context"]["job"]["id"])
                result["text"] = "{ broken"
            return result
        self.service.native_invoke = malformed
        final = await self.create()
        self.assertEqual(final["objective"]["status"], "complete")
        failed = next(job for job in final["jobs"] if job["id"] == rejected[0])
        self.assertEqual((failed["status"], failed["error"]), ("failed", "invalid_agent_response"))
        execution = next(item for item in final["native_executions"] if item["job_id"] == rejected[0])
        self.assertEqual(execution["status"], "completed")
        self.assertTrue(execution["cleanup_confirmed"])
        correction = next(call for call in self.fake.calls if call["context"]["job"]["correction_of"] == rejected[0])
        self.assertEqual(correction["permission"], "full_access")
        self.assertFalse(any(member["identity_uncertain"] for member in final["members"]))

    async def test_changed_rejection_can_correct_without_fixed_attempt_cap_and_retains_private_work(self):
        invoke = self.service.native_invoke
        work_calls = []
        original = self.handler
        def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "work":
                work_calls.append(context["job"]["id"])
                if len(work_calls) > 1:
                    self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                    if len(work_calls) == 3:
                        (cwd / "main.py").write_text("VALUE = 3\n")
                    return native_reply(context, [{"type":"complete", "task_id": context["task"]["id"], "result":"Current exact files."}],
                        unchanged=[{"path":"notes.md", "reason":"Existing notes apply."}])
            if context["job"]["job_phase"] == "review":
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 3\n")
                return native_reply(context, [{"type":"review", "task_id":context["task"]["id"], "verdict":"accept", "reason":"Read improved authorized bytes."}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        async def malformed(*args, **kwargs):
            result = await invoke(*args, **kwargs)
            phase = self.fake.calls[-1]["context"]["job"]["job_phase"]
            if phase == "work" and len(work_calls) == 1:
                result["text"] = "{ broken"
            elif phase == "work" and len(work_calls) == 2:
                value = json.loads(result["text"])
                value["commands"][0]["unexpected"] = True
                result["text"] = json.dumps(value)
            return result
        self.service.native_invoke = malformed
        final = await self.create()
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(len(work_calls), 3)
        self.assertEqual(self.service._project(final["room"]["id"]).files_for_paths(["main.py"])[0]["content"], "VALUE = 3\n")
        self.assertEqual(sum(event["kind"] == "format_correction_queued" for event in final["events"]), 2)
        self.assertTrue(all(call["permission"] == "full_access" for call in self.fake.calls))
        self.assertFalse(any(member["identity_uncertain"] for member in final["members"]))

    async def test_native_failed_execution_preserves_confirmed_identity(self):
        self.fake.failure = {"status":"failed", "failure_code":"native_protocol_invalid"}
        final = await self.create()
        self.assertEqual(final["team"]["phase"], "unknown")
        failed = [job for job in final["jobs"] if job["error"] == "native_protocol_invalid"]
        self.assertTrue(failed)
        providers = {job["provider"] for job in failed}
        self.assertTrue(all(not member["identity_uncertain"] for member in final["members"] if member["id"] in providers))
        self.assertFalse(any(event["kind"] == "format_correction_queued" for event in final["events"]))

    async def test_repeated_work_rejection_blocks_only_bound_task_and_keeps_other_dispatch(self):
        native.planned_owners(self.service)
        invoke = self.service.native_invoke
        original = self.handler
        def handler(agent, context, cwd):
            phase, task = context["job"]["job_phase"], context["task"]
            if phase in {"plan", "coordinate"} and agent == context["team"]["plan_author"] and not context["team"]["tasks"]:
                return native_reply(context, [
                    native.proposal("T1", paths=["main.py", "notes.md"], capability="python"),
                    {"type":"claim", "task_id":"T1"},
                    native.proposal("T2", paths=[], capability="text"),
                    {"type":"claim", "task_id":"T2"}, {"type":"plan_ready"}])
            if task and task["id"] == "T2":
                if phase == "work":
                    self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                    return native_reply(context, [{"type":"complete", "task_id":"T2", "result":"Independent text result."}])
                if phase == "review":
                    return native_reply(context, [{"type":"review", "task_id":"T2", "verdict":"accept", "reason":"Read text result."}])
            if phase == "work" and context["job"]["format_attempt"]:
                return native_reply(context)
            return original(agent, context, cwd)
        self.fake.handler = handler
        async def malformed(*args, **kwargs):
            result = await invoke(*args, **kwargs)
            context = self.fake.calls[-1]["context"]
            if context["job"]["job_phase"] == "work" and context["task"]["id"] == "T1":
                result["text"] = "{ broken"
            return result
        self.service.native_invoke = malformed
        final = await self.create()
        self.assertEqual(final["room"]["status"], "needs_user")
        self.assertEqual(final["team"]["phase"], "working")
        tasks = {task["id"]: task for task in final["team"]["tasks"]}
        self.assertEqual(tasks["T1"]["status"], "blocked")
        self.assertEqual(tasks["T2"]["status"], "accepted")
        self.assertTrue(any(call["agent"] == "claude" and call["context"]["job"]["job_phase"] == "work"
                            and call["context"]["task"]["id"] == "T2" for call in self.fake.calls))
        self.assertFalse(any(member["identity_uncertain"] for member in final["members"]))
        room_id = final["room"]["id"]
        with self.service.store._connection(write=True) as db:
            team = self.service.store._team
            blocked, independent = team.task(db, room_id, "T1"), team.task(db, room_id, "T2")
            self.assertTrue(team.response_blocked(db, room_id, "claude", "work", blocked))
            self.assertFalse(team.response_blocked(db, room_id, "claude", "work", independent))
            self.service.store._event(db, room_id, "message", sender="user", recipient="codex", text="Review your separate result.")
            self.assertTrue(team.response_blocked(db, room_id, "claude", "work", blocked))
            self.service.store._event(db, room_id, "message", sender="user", recipient="claude", text="Correct the blocked response.")
            self.assertFalse(team.response_blocked(db, room_id, "claude", "work", blocked))

    async def test_archived_private_bytes_are_preserved_and_drift_rejected_at_same_audit_boundary(self):
        import hashlib
        from local_agent_coordinator.room_store import RoomError
        final = await self.create()
        room_id = final["room"]["id"]
        state = self.service.store.native_workspace(room_id, "claude")
        from pathlib import Path
        path = Path(state["project"]) / "retained.txt"
        path.write_text("Retained original private bytes.\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        state["partial_archive"] = {"paths": ["retained.txt"],
            "observation": {"files": [{"path":"retained.txt", "previous_sha256":None, "sha256":digest}]}}
        job = {"room_id":room_id, "provider":"claude", "team":{"task_id":None}}
        changes = self.service._audited_native_changes(job, state, [], [])
        self.assertFalse(changes["blocked"])
        self.assertEqual(changes["files"], [])
        self.assertEqual(changes["retained_files"], [{"path":"retained.txt", "sha256":digest}])
        path.write_text("Different bytes.\n")
        with self.assertRaises(RoomError) as failure:
            self.service._audited_native_changes(job, state, [], [])
        self.assertEqual(failure.exception.code, "native_recovery_unpublished_changes")

    async def test_owned_fresh_cancellation_identity_does_not_authorize_reuse_or_foreign_collision(self):
        from uuid import uuid4
        final = await self.create()
        room_id = final["room"]["id"]
        job_id = next(call["context"]["job"]["id"] for call in self.fake.calls
                      if call["agent"] == "claude" and call["context"]["job"]["job_phase"] == "plan")
        sid = str(uuid4())
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=NULL WHERE room_id=? AND provider='claude'", (room_id,))
            db.execute("UPDATE team_runs SET fresh_sessions=1 WHERE id=?", (final["current_run"]["id"],))
            db.execute("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='native_session_cleanup_identity' "
                       "AND json_extract(value_json,'$.job_id')=?", (room_id, job_id))
        self.service.store.retain_native_session_identity(job_id, sid, source="owned_receipt")
        with self.service.store._connection(write=True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.assertTrue(self.service.store.native_job_identity_confirmed(db, job))
            # Identity is known, but ordinary session reuse remains forbidden.
            self.assertTrue(self.service.store._session_conflict(db, room_id, "claude", sid, job_id=job_id))
            db.execute("INSERT INTO native_session_history VALUES (?,?,?,?)", (str(uuid4()), "claude", "claude", sid))
            self.assertFalse(self.service.store.native_job_identity_confirmed(db, job))
