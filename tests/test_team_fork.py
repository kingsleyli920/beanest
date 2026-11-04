"""Explicit user snapshot forks; real storage, fake providers, no native tools."""
import asyncio
import copy
import unittest
from uuid import uuid4

from local_agent_coordinator.room_service import RoomError
from local_agent_coordinator.message_capacity import MAX_MESSAGE_UTF8_BYTES
from tests import test_team_runs as runs
from tests import test_team_runs_http as http
from tests import test_room_execution as execution


class TeamForkTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runs.TeamRunTests.asyncSetUp
    asyncTearDown = runs.TeamRunTests.asyncTearDown
    settle = runs.TeamRunTests.settle
    scenario = runs.TeamRunTests.scenario
    create = runs.TeamRunTests.create
    next_run = runs.TeamRunTests.next_run

    async def fork(self, snapshot, *, request_id="fork", **overrides):
        current = snapshot["current_run"]
        arguments = {"run_id": current["id"], "revision": current["revision"], "digest": current["digest"]}
        return await self.service.fork_room(snapshot["room"]["id"], "Explicit new task from saved files", "all",
                                            request_id, **(arguments | overrides))

    async def test_eight_run_unknown_source_forks_exact_files_with_fresh_sessions_and_budget(self):
        source = await self.create()
        for number in range(2, 9):
            self.scenario(number)
            await self.next_run(source, number)
            await self.settle()
            source = await self.service.read_room(source["room"]["id"])
        room_id = source["room"]["id"]
        # A trusted operator reconciles this exact already-awaited job,
        # retaining unknown outcome and identity. Aggregate bits are not proof.
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status='unknown',error='deadline_exceeded' WHERE id=?", (source["jobs"][-1]["id"],))
            db.execute("UPDATE members SET identity_uncertain=1 WHERE room_id=?", (room_id,))
        self.service.store.finish_stop(room_id, confirmed=True, final_status="unknown")
        self.service.store.record_job_cleanup(source["jobs"][-1]["id"], confirmed=True)
        source = await self.service.read_room(room_id)
        original = copy.deepcopy(source)
        self.assertTrue(source["current_run"]["can_fork"])
        # Non-native explicit retries start fresh sessions; a historical
        # eight-run count no longer disables that independent user action.
        self.assertTrue(source["current_run"]["can_retry"])
        self.scenario(9)
        start = len(self.provider.calls)
        forked = await self.fork(source)
        self.assertNotEqual(forked["room"]["id"], room_id)
        self.assertEqual(forked["project"]["files"], source["project"]["files"])
        self.assertEqual(forked["project"]["revision"], 1)
        self.assertEqual(forked["executions"], [])
        self.assertEqual(forked["current_run"]["number"], 1)
        self.assertEqual(forked["budget"]["used"], 0)
        self.assertIsNone(forked["budget"]["limit"])
        self.assertTrue(all(member["session_id"] is None for member in forked["members"]))
        self.assertFalse(forked["current_run"]["can_accept"])
        self.assertEqual(await self.service.read_room(room_id), original)
        await self.settle()
        new_room = await self.service.read_room(forked["room"]["id"])
        old_sessions = {member["session_id"] for member in source["members"]}
        self.assertTrue(all(member["session_id"] not in old_sessions for member in new_room["members"]))
        first_calls = [call for call in self.provider.calls[start:] if call["context"]["job"]["job_phase"] == "plan"]
        self.assertTrue(all(call["session_id"] is None for call in first_calls))
        self.assertTrue(all(call["context"]["forked_from"]["checks_inherited"] is False for call in first_calls))

    async def test_idempotent_fork_precedes_stale_source_guards_and_conflicting_body_is_rejected(self):
        source = await self.create()
        self.scenario(2)
        first = await self.fork(source)
        await self.settle()
        count = len(self.provider.calls)
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET error='project_commit_unconfirmed' WHERE id=?", (source["jobs"][-1]["id"],))
        repeated = await self.fork(source)
        self.assertEqual(repeated["room"]["id"], first["room"]["id"])
        self.assertEqual(len(self.provider.calls), count)
        with self.assertRaises(RoomError) as raised:
            await self.fork(source, digest="0" * 64)
        self.assertEqual(raised.exception.code, "request_conflict")
        with self.assertRaises(RoomError) as raised:
            await self.fork(source, request_id="another")
        self.assertEqual(raised.exception.code, "fork_source_unavailable")

    async def test_missing_worker_receipt_cannot_be_laundered_by_cancel_without_live_worker(self):
        source = await self.create()
        room_id = source["room"]["id"]
        with self.service.store._connection(write=True) as db:
            job = source["jobs"][-1]["id"]
            db.execute("UPDATE jobs SET status='interrupted',error='service_restarted' WHERE id=?", (job,))
            db.execute("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_cleanup'", (room_id,))
        self.service.store.request_stop(room_id, "service_restarted")
        before = len(self.provider.calls)
        cancelled = await self.service.cancel_room(room_id)
        self.assertFalse(cancelled["cancellation_confirmed"])
        self.assertFalse(cancelled["current_run"]["can_fork"])
        with self.assertRaises(RoomError) as raised:
            await self.fork(cancelled)
        self.assertEqual(raised.exception.code, "team_cleanup_unconfirmed")
        self.assertEqual(len(self.provider.calls), before)

    async def test_legacy_true_aggregate_receipt_does_not_confirm_interrupted_worker(self):
        source = await self.create()
        room_id, job_id = source["room"]["id"], source["jobs"][-1]["id"]
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status='interrupted',error='service_restarted' WHERE id=?", (job_id,))
            db.execute("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_cleanup'", (room_id,))
        # Reproduce the old service's durable true bit without resetting it via
        # request_stop; no in-memory worker remains to prove process cleanup.
        self.service.store.finish_stop(room_id, confirmed=True, final_status="interrupted")
        legacy = await self.service.read_room(room_id)
        self.assertTrue(legacy["cancellation_confirmed"])
        self.assertFalse(legacy["current_run"]["can_fork"])
        calls = len(self.provider.calls)
        with self.assertRaises(RoomError) as raised:
            await self.fork(legacy)
        self.assertEqual(raised.exception.code, "team_cleanup_unconfirmed")
        cancelled = await self.service.cancel_room(room_id)
        self.assertFalse(cancelled["cancellation_confirmed"])
        self.assertFalse(cancelled["current_run"]["can_fork"])
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(len(cancelled["jobs"]), len(legacy["jobs"]))
        self.service.store.record_job_cleanup(job_id, confirmed=True)
        reconciled = await self.service.read_room(room_id)
        self.assertTrue(reconciled["current_run"]["can_fork"])

    async def test_known_awaited_timeout_records_cleanup_but_never_inherits_success(self):
        source = await self.create()
        async def timed_out(*args, **kwargs):
            raise asyncio.TimeoutError
        self.service.invoke = timed_out
        await self.next_run(source, 2)
        await self.settle()
        failed = await self.service.read_room(source["room"]["id"])
        self.assertTrue(any(event["kind"] == "job_cleanup" and event["cleanup_confirmed"] for event in failed["events"]))
        self.assertTrue(failed["current_run"]["can_fork"])
        self.scenario(3)
        self.service.invoke = self.provider
        forked = await self.fork(failed)
        self.assertEqual(forked["team"]["tasks"], [])
        self.assertFalse(forked["current_run"]["can_accept"])
        await self.settle()

    async def test_cas_unclean_and_orphan_executions_are_rejected(self):
        source = await self.create()
        with self.assertRaises(RoomError) as raised:
            await self.fork(source, digest="0" * 64)
        self.assertEqual(raised.exception.code, "team_run_conflict")
        runner = execution.FakeRunner()
        runner.result.update(status="unknown", exit_code=None, cleanup_confirmed=False)
        self.service._runner_instance = runner
        self.scenario(1)
        second = await self.service.create_room("Second fixture", "all", str(uuid4()), collaboration=True, mode="python_sandbox")
        await self.settle()
        second = await self.service.read_room(second["room"]["id"])
        self.assertFalse(second["current_run"]["can_fork"])
        with self.assertRaises(RoomError) as raised:
            await self.fork(second)
        self.assertEqual(raised.exception.code, "team_cleanup_unconfirmed")
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE executions SET team_run_id=NULL WHERE room_id=?", (second["room"]["id"],))
        orphan = await self.service.read_room(second["room"]["id"])
        self.assertFalse(orphan["current_run"]["can_fork"])
        with self.assertRaises(RoomError) as raised:
            await self.fork(orphan)
        self.assertEqual(raised.exception.code, "team_cleanup_unconfirmed")


class TeamForkHTTPTests(unittest.TestCase):
    setUp = http.TeamRunHTTPTests.setUp
    tearDown = http.TeamRunHTTPTests.tearDown
    request = http.TeamRunHTTPTests.request
    ready = http.TeamRunHTTPTests.ready

    def test_public_fork_creates_real_workspace_and_is_web_only_strict_and_idempotent(self):
        from tests import test_team_service as fixtures
        self.provider.handler = fixtures.TeamScenario(self, [fixtures.task("main", "main.py"), fixtures.task("checks", "checks.py")],
            {"main": "claude", "checks": "codex"}, {"main": "VALUE = 1\n", "checks": "EXPECTED = 1\n"})
        code, source = self.request("POST", "/api/rooms", {"goal": "Python fixture", "target": "all", "request_id": "python",
            "mode": "python_artifacts", "collaboration": True})
        self.assertEqual(code, 200)
        source = self.ready(source["room"]["id"])
        binding = source["current_run"]
        body = {"text": "Continue saved Python files. " + "x" * 3001, "target": "all", "request_id": "web-fork",
                **{key: binding[key] for key in ("revision", "digest")}, "run_id": binding["id"]}
        path = "/api/rooms/" + source["room"]["id"] + "/fork"
        self.assertEqual(self.request("POST", path, body, token=self.server.mcp_token)[0], 403)
        code, rejected = self.request("POST", path.replace("/api/", "/api/mcp/"), body, mcp=True)
        self.assertEqual((code, rejected["error"]), (403, "user_fork_required"))
        self.assertEqual(self.request("POST", path, {**body, 'text': 'x' * (MAX_MESSAGE_UTF8_BYTES + 1)})[0], 413)
        for invalid in ({**body, "revision": True}, {**body, "retry": True}):
            self.assertEqual(self.request("POST", path, invalid)[0], 400)
        code, forked = self.request("POST", path, body)
        self.assertEqual(code, 200)
        self.assertNotEqual(forked["room"]["id"], source["room"]["id"])
        self.assertEqual(forked["project"]["files"], source["project"]["files"])
        self.assertEqual(self.request("POST", path, body)[1]["room"]["id"], forked["room"]["id"])
        self.ready(forked["room"]["id"])
