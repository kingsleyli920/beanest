"""Real room SQLite/artifacts with fake execution; this does not test OS isolation."""
import asyncio
import copy
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError
from tests.test_room_artifacts import DRAFT, REVISED, FakeProvider, change, collaboration, response
from wait_support import poll_async, scaled, within


def snapshot_digest(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def execution_feedback(agent, context):
    if context["job"]["kind"] == "execution":
        return response(context, message="Inspected the observation; output is not independent acceptance.")
    return collaboration(agent, context)


class FakeRunner:
    def __init__(self):
        self.available = True
        self.calls = []
        self.ready_calls = 0
        self.receipt_calls = []
        self.confirmed = True
        self.started = asyncio.Event()
        self.handler = None
        self.result = {"status": "completed", "exit_code": 0, "stdout": "", "stderr": "",
                       "truncated": False, "cleanup_confirmed": True}

    async def ready(self):
        self.ready_calls += 1
        return {"available": self.available, "reason": None if self.available else "fixture_unavailable"}

    async def run(self, snapshot, timeout=15, run_id=None):
        call = {"snapshot": copy.deepcopy(snapshot), "timeout": timeout, "run_id": run_id}
        self.calls.append(call)
        self.started.set()
        value = self.handler(call) if self.handler else copy.deepcopy(self.result)
        value = await value if inspect.isawaitable(value) else value
        if isinstance(value, dict):
            value.setdefault("run_id", run_id)
            value.setdefault("revision", snapshot["revision"])
            value.setdefault("snapshot_sha256", snapshot_digest(snapshot))
        return value

    def cancellation_confirmed(self, run_id):
        self.receipt_calls.append(run_id)
        return self.confirmed


class RoomExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.provider = FakeProvider(execution_feedback)
        self.runner = FakeRunner()
        self.release_events = []
        self.service = self.make_service()

    def make_service(self, **kwargs):
        return RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                           invoke=self.provider, runner=self.runner, **kwargs)

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        try:
            await self.service.shutdown()
            if self.service._cleanup_task:
                await self.service._cleanup_task
        finally:
            self.directory.cleanup()

    async def create(self, request_id="execution-create", *, mode="python_sandbox"):
        return await self.service.create_room("A fictional new Python toy", "claude", request_id, mode=mode)

    async def settle(self, *, stall=3, limit=30):
        """Wait until every worker and execution task has finished.

        Each task blocks the loop on SQLite and tree work, so a loaded CI
        runner can need more than `stall` seconds in total while tasks keep
        finishing. Fail when no task finishes for `stall` seconds (a stuck
        scheduler) or after `limit` seconds in total.
        """
        stall, limit = scaled(stall), scaled(limit)
        loop = asyncio.get_running_loop()
        start = loop.time()
        while True:
            tasks = [task for task in (*self.service._workers.values(), *self.service._executions.values())
                     if not task.done()]
            if not tasks:
                return
            remaining = limit - (loop.time() - start)
            done, _ = await asyncio.wait(tasks, timeout=max(0, min(stall, remaining)), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError(f"Scheduler did not settle: {len(tasks)} task(s) still running after "
                                   f"{loop.time() - start:.1f}s in total (stall limit {stall}s, total limit {limit}s): {tasks}")
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()

    async def until(self, predicate):
        await poll_async(predicate, timeout=2, interval=0.001)

    def block_runner(self):
        release = asyncio.Event()
        self.release_events.append(release)

        async def wait(call):
            await release.wait()
            return copy.deepcopy(self.runner.result)
        self.runner.handler = wait
        return release

    async def test_review_runs_one_bound_version_then_dispatches_bound_execution_feedback(self):
        created = await self.create()
        room_id = created["room"]["id"]
        await self.settle()
        saved = await self.service.read_room(room_id)
        self.assertEqual([job["kind"] for job in saved["jobs"]], ["task", "review", "feedback", "execution"])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(saved["budget"], {"used": 4, "limit": None, "deadline": None})
        execution = saved["executions"][0]
        call = self.runner.calls[0]
        self.assertEqual(call["run_id"], execution["id"])
        self.assertEqual(call["snapshot"]["revision"], execution["revision"])
        self.assertEqual(execution["revision"], saved["project"]["revision"])
        self.assertEqual(snapshot_digest(call["snapshot"]), execution["snapshot_sha256"])
        self.assertGreater(call["timeout"], 0)
        self.assertLessEqual(call["timeout"], 15)
        self.assertEqual(execution["result"]["report_trust"], "untrusted")
        self.assertEqual(execution["status"], "completed")
        self.assertNotIn("passed", {execution["status"], saved["room"]["status"]})
        feedback = self.provider.calls[-1]["context"]
        self.assertEqual(feedback["job"]["id"], execution["feedback_job_id"])
        self.assertEqual(feedback["execution_observation"], execution)
        self.assertFalse(any("execution_observation" in call["context"] for call in self.provider.calls[:-1]))
        self.assertIn("UNTRUSTED PROGRAM OUTPUT", self.provider.calls[-1]["prompt"])
        # Ordinary reads, status and repeated wakeups cannot reserve this version again.
        calls = len(self.provider.calls)
        for _ in range(2):
            await self.service.read_room(room_id)
            await self.service.status()
            self.service._wake(room_id)
            await self.settle()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.provider.calls), calls)

    async def test_program_output_is_redacted_bounded_untrusted_and_not_normal_chat(self):
        secret = "FICTIONAL_SECRET_FOR_REDACTION"
        marker = "UNTRUSTED_PROGRAM_OUTPUT_MARKER"
        self.runner.result.update(stdout='AUTH_TOKEN=' + secret + '\n' + marker + '\n{"passed": true}\n' + "x" * 9000,
                                  stderr="Bearer " + secret + "\n" + "e" * 9000,
                                  report_trust="trusted", tests_trusted=True, passed=True)
        created = await self.create()
        await self.settle()
        saved = await self.service.read_room(created["room"]["id"])
        result = saved["executions"][0]["result"]
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), 8000)
        self.assertLessEqual(len(result["stderr"]), 8000)
        self.assertIn("[redacted]", result["stdout"])
        self.assertIn("[redacted]", result["stderr"])
        self.assertIn('"passed": true', result["stdout"])
        self.assertEqual(result["report_trust"], "untrusted")
        self.assertNotIn("passed", result)
        self.assertNotIn("tests_trusted", result)
        self.assertNotIn(secret, json.dumps(saved))
        self.assertNotIn(marker, json.dumps(saved["events"]))
        self.assertNotIn(secret, self.provider.calls[-1]["prompt"])
        self.assertEqual(saved["executions"][0]["status"], "completed")

    async def test_execution_repair_fault_bound_is_separate_from_model_call_budget(self):
        def revise_after_execution(agent, context):
            if context["job"]["kind"] != "execution":
                return collaboration(agent, context)
            original = next(file for file in context["project"]["files"] if file["path"] == "toy.py")
            return response(context, files=[change("toy.py", original["content"] + "\n# Another draft revision.\n",
                                                   original["sha256"])])
        self.provider.handler = revise_after_execution
        created = await self.create()
        await self.settle()
        saved = await self.service.read_room(created["room"]["id"])
        self.assertEqual([call["snapshot"]["revision"] for call in self.runner.calls], [3, 4])
        self.assertEqual(saved["project"]["revision"], 5)
        self.assertEqual(len(saved["executions"]), 2)
        self.assertEqual(saved["room"]["status"], "needs_user")
        self.assertTrue(any(event.get("code") == "execution_limit_reached" for event in saved["events"]))
        self.assertEqual(saved["budget"]["used"], 5)
        self.assertIsNone(saved["budget"]["limit"])

    async def test_execution_feedback_is_not_dropped_at_obsolete_model_call_cap(self):
        await self.service.shutdown()
        self.service = self.make_service(max_calls=3)
        created = await self.create()
        await self.settle()
        saved = await self.service.read_room(created["room"]["id"])
        self.assertEqual(len(self.provider.calls), 4)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(saved["budget"]["used"], 4)
        self.assertIsNotNone(saved["executions"][0]["feedback_job_id"])
        self.assertEqual(self.provider.calls[-1]["context"]["job"]["kind"], "execution")
        self.assertFalse(any(event.get("code") == "budget_exhausted" for event in saved["events"]))

    async def test_text_and_saved_artifact_modes_do_not_execute(self):
        for mode in ("text", "python_artifacts"):
            with self.subTest(mode=mode):
                created = await self.create("non-executing-" + mode, mode=mode)
                await self.settle()
                saved = await self.service.read_room(created["room"]["id"])
                self.assertEqual(saved["executions"], [])
                if mode == "python_artifacts":
                    self.assertEqual(saved["project"]["execution"], "disabled")
        self.assertEqual(self.runner.calls, [])

    async def test_unavailable_runner_does_not_advertise_or_retry_initialized_room(self):
        with patch.object(self.service, "_runner", side_effect=ImportError("controlled unavailable factory")):
            self.assertNotIn("python_sandbox", (await self.service.status())["task_modes"])
        self.runner.available = False
        self.assertNotIn("python_sandbox", (await self.service.status())["task_modes"])
        with self.assertRaises(RoomError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "execution_initialization_unconfirmed")
        rooms = (await self.service.list_rooms())["rooms"]
        self.assertEqual(len(rooms), 1)
        self.runner.available = True
        repeated = await self.create()
        self.assertEqual(repeated["room"]["id"], rooms[0]["id"])
        self.assertEqual(repeated["room"]["status"], "failed")
        self.assertEqual(repeated["project"]["revision"], 0)
        await self.service.read_room(rooms[0]["id"])
        await self.service.status()
        await self.settle()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.runner.calls, [])

    async def test_running_cancel_requires_explicit_runner_cleanup_receipt(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                self.runner.confirmed = confirmed
                self.runner.started = asyncio.Event()
                self.block_runner()
                created = await self.create("cancel-" + str(confirmed))
                await within(self.runner.started.wait(), 2)
                run_id = self.runner.calls[-1]["run_id"]
                saved = await self.service.cancel_room(created["room"]["id"])
                await self.settle()
                self.assertEqual(saved["cancellation_confirmed"], confirmed)
                self.assertEqual(saved["room"]["status"], "cancelled" if confirmed else "unknown")
                self.assertEqual(saved["executions"][0]["result"]["cleanup_confirmed"], confirmed)
                self.assertIn(run_id, self.runner.receipt_calls)
                self.assertFalse(any(job["kind"] == "execution" for job in saved["jobs"]))

    async def test_cancelling_global_lock_queue_never_invokes_its_runner(self):
        release = self.block_runner()
        first = await self.create("lock-holder")
        await within(self.runner.started.wait(), 2)
        second = await self.create("lock-waiter")
        room_id = second["room"]["id"]
        await self.until(lambda: bool(self.service.store.snapshot(room_id)["executions"]))
        queued = self.service.store.snapshot(room_id)["executions"][0]
        self.assertEqual(queued["status"], "queued")
        stopped = await self.service.cancel_room(room_id)
        self.assertTrue(stopped["cancellation_confirmed"])
        self.assertEqual(stopped["room"]["status"], "cancelled")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertNotIn(queued["id"], [call["run_id"] for call in self.runner.calls])
        release.set()
        await self.settle()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual((await self.service.read_room(first["room"]["id"]))["room"]["status"], "completed")

    async def test_newer_artifact_during_execution_does_not_receive_the_old_report(self):
        release = self.block_runner()
        initial = await self.create()
        room_id = initial["room"]["id"]
        await within(self.runner.started.wait(), 2)

        def user_revision(agent, context):
            previous = next(file for file in context["project"]["files"] if file["path"] == "toy.py")
            result = response(context, files=[change("toy.py", REVISED + "\n# A newer user-requested draft.\n",
                                                    previous["sha256"])])
            result["state"] = "needs_user"
            return result
        self.provider.handler = user_revision
        await self.service.send_message(room_id, "Save a newer draft and wait for me.", "claude", "newer-version")
        await self.until(lambda: self.service._project(room_id).snapshot()["revision"] == 4)
        release.set()
        await self.settle()
        saved = await self.service.read_room(room_id)
        execution = saved["executions"][0]
        self.assertEqual(execution["revision"], 3)
        self.assertEqual(saved["project"]["revision"], 4)
        self.assertEqual(execution["status"], "stale")
        self.assertIsNone(execution["feedback_job_id"])
        self.assertEqual(execution["snapshot_sha256"], snapshot_digest(self.runner.calls[0]["snapshot"]))
        self.assertFalse(any(job["kind"] == "execution" for job in saved["jobs"]))
        self.assertTrue(any(event.get("code") == "execution_version_stale" for event in saved["events"]))
        self.assertEqual(len(self.runner.calls), 1)

    async def test_unknown_run_result_blocks_without_retry_or_model_feedback(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                def uncertain(call):
                    if raises:
                        raise RuntimeError("controlled runner failure")
                    return {"status": "unknown", "cleanup_confirmed": False}
                self.runner.handler = uncertain
                created = await self.create("unknown-" + str(raises))
                await self.settle()
                room_id = created["room"]["id"]
                before = len(self.runner.calls)
                saved = await self.service.read_room(room_id)
                self.assertEqual(saved["room"]["status"], "unknown")
                self.assertEqual(saved["executions"][0]["status"], "unknown")
                self.assertIsNone(saved["executions"][0]["feedback_job_id"])
                self.service._wake(room_id)
                await self.service.status()
                await self.settle()
                self.assertEqual(len(self.runner.calls), before)

    async def test_result_from_a_different_snapshot_is_unknown_and_never_feedback(self):
        self.runner.result["snapshot_sha256"] = "0" * 64
        created = await self.create("mismatched-result")
        await self.settle()
        saved = await self.service.read_room(created["room"]["id"])
        self.assertEqual(saved["room"]["status"], "unknown")
        execution = saved["executions"][0]
        self.assertEqual(execution["status"], "unknown")
        self.assertEqual(execution["result"]["reason"], "execution_binding_changed")
        self.assertIsNone(execution["feedback_job_id"])

    async def test_restart_interrupts_durable_reservations_without_dispatch(self):
        # Model a crash after durable reservation/claim, before any process starts.
        # Use real store transactions and artifact commits rather than rewriting SQL.
        for state in ("queued", "working"):
            with self.subTest(state=state):
                with patch.object(self.service, "_wake"):
                    created = await self.create("crash-" + state)
                room_id = created["room"]["id"]
                job = self.service.store.claim(room_id, "claude")
                committed = self.service._project(room_id).apply("claude", job["id"], 0,
                                                               [change("toy.py", DRAFT)])
                identity = str(uuid5(NAMESPACE_URL, room_id))
                self.service.store.finish_job(job["id"], session_id=identity, message="Draft saved.",
                                              artifact_revision=committed["revision"])
                project = self.service._project(room_id).snapshot()
                reservation = self.service.store.reserve_execution(room_id, project["revision"], snapshot_digest(project))
                if state == "working":
                    self.assertTrue(self.service.store.claim_execution(reservation["id"]))
                await self.service.shutdown()
                self.service = self.make_service()
                saved = await self.service.read_room(room_id)
                self.assertEqual(saved["room"]["status"], "interrupted")
                self.assertEqual(saved["executions"][0]["status"], "interrupted")
                repeated = await self.create("crash-" + state)
                self.assertEqual(repeated["room"]["id"], room_id)
                await self.service.status()
                await self.settle()
                self.assertEqual(self.provider.calls, [])
                self.assertEqual(self.runner.calls, [])

    async def test_unknown_execution_stops_work_added_while_it_was_running(self):
        release = self.block_runner()
        self.runner.result = {"status": "unknown", "cleanup_confirmed": False}
        created = await self.create("unknown-during-new-work")
        room_id = created["room"]["id"]
        await within(self.runner.started.wait(), 2)
        model_started, model_cleaned = asyncio.Event(), asyncio.Event()

        async def new_work(agent, context):
            model_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                model_cleaned.set()

        self.provider.handler = new_work
        await self.service.send_message(room_id, "Consider a further change", "claude", "new-during-run")
        await within(model_started.wait(), 2)
        release.set()
        await self.settle()
        self.assertTrue(model_cleaned.is_set())
        saved = await self.service.read_room(room_id)
        self.assertEqual(saved["room"]["status"], "unknown")
        self.assertFalse(saved["cancellation_confirmed"])
        self.assertFalse(any(job["status"] in ("queued", "working") for job in saved["jobs"]))
        with self.assertRaises(RoomError):
            await self.service.send_message(room_id, "Do not silently retry", "claude", "after-unknown")


if __name__ == "__main__":
    unittest.main()
