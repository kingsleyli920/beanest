"""Lifecycle changes hide local registrations without destroying task evidence."""
import asyncio
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from uuid import NAMESPACE_URL, uuid4, uuid5

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.project_catalog import ProjectCatalogError
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError, RoomStore
from wait_support import scaled


class LifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "rooms.sqlite3"
        self.store = RoomStore(self.path, clock=lambda: 1000)
        self.addCleanup(lambda: self.store.close())

    def room(self):
        return self.store.create_room("Keep this history", "claude", str(uuid4()))[0]

    def complete(self, room_id, *, cleanup=True):
        job = self.store.claim(room_id, "claude")
        self.store.finish_job(job["id"], session_id=str(uuid4()), message="Delivered")
        if cleanup is not None:
            self.store.record_job_cleanup(job["id"], confirmed=cleanup)
        return job

    def test_archive_remove_restore_preserve_rows_and_are_idempotent(self):
        room_id = self.room()
        self.complete(room_id)
        before = self.store.snapshot(room_id)
        for action, state in (("archive", "archived"), ("remove", "removed"), ("restore", "active")):
            self.store.set_lifecycle(room_id, action)
            self.store.set_lifecycle(room_id, action)
            after = self.store.snapshot(room_id)
            self.assertEqual(after["room"]["lifecycle_state"], state)
            self.assertEqual(after["jobs"], before["jobs"])
            self.assertEqual(after["members"], before["members"])
            self.assertEqual(after["executions"], before["executions"])
            self.assertEqual(after["events"][:len(before["events"])], before["events"])
            self.assertEqual(self.store.list_rooms()["rooms"][0]["lifecycle_state"], state)
        self.assertEqual(sum(event["kind"] == "room_lifecycle" for event in after["events"]), 3)

    def test_archive_and_restore_keep_the_last_activity_time(self):
        # Catches archiving or restoring moving a task to the top of the list: the list is
        # ordered by updated_at, and a lifecycle change is not activity.
        now = [1000]
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: now[0])
        room_id = self.room()
        self.complete(room_id)
        self.assertEqual(self.store.snapshot(room_id)["room"]["updated_at"], 1000)
        for action, moment in (("archive", 2000), ("restore", 3000)):
            now[0] = moment
            self.store.set_lifecycle(room_id, action)
            self.assertEqual(self.store.snapshot(room_id)["room"]["updated_at"], 1000)
            self.assertEqual(self.store.list_rooms()["rooms"][0]["updated_at"], 1000)
        now[0] = 4000
        with self.store._connection(write=True) as db:
            self.store._event(db, room_id, "warning", code="team_plan_changed")
        self.assertEqual(self.store.snapshot(room_id)["room"]["updated_at"], 4000, "real activity still moves the time")

    def test_old_database_adds_active_column_without_rewriting_history(self):
        room_id = self.room()
        self.complete(room_id)
        before = self.store.snapshot(room_id)
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE rooms DROP COLUMN lifecycle_state")
            db.commit()
        self.store = RoomStore(self.path, clock=lambda: 1000)
        after = self.store.snapshot(room_id)
        self.assertEqual(after["room"]["lifecycle_state"], "active")
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(after["members"], before["members"])

    def test_pending_and_latest_cleanup_receipt_gate_hiding(self):
        room_id = self.room()
        for action in ("archive", "remove"):
            with self.assertRaisesRegex(RoomError, "room_lifecycle_busy"):
                self.store.set_lifecycle(room_id, action)
        job = self.complete(room_id, cleanup=None)
        self.assertIsNone(self.store.lifecycle_reason(room_id))
        self.store.record_job_cleanup(job["id"], confirmed=True)
        self.store.record_job_cleanup(job["id"], confirmed=False)
        with self.assertRaisesRegex(RoomError, "team_cleanup_unconfirmed"):
            self.store.set_lifecycle(room_id, "archive")
        self.store.record_job_cleanup(job["id"], confirmed=True)
        self.store.set_lifecycle(room_id, "archive")
        self.assertTrue(self.store.snapshot(room_id)["room"]["lifecycle_can_change"])

    def test_legacy_single_completion_can_be_hidden_without_fabricating_receipts(self):
        room_id = self.room()
        self.complete(room_id, cleanup=None)
        before = self.store.snapshot(room_id)
        self.store.set_lifecycle(room_id, "archive")
        self.store.set_lifecycle(room_id, "restore")
        after = self.store.snapshot(room_id)
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertFalse(any(event["kind"] == "job_cleanup" for event in after["events"]))
        self.assertEqual(after["room"]["lifecycle_state"], "active")

    def test_legacy_exception_never_weakens_team_or_uncertain_cleanup(self):
        team_id, _ = self.store.create_room("Team task", "claude", "team", collaboration=True)
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status='completed' WHERE room_id=?", (team_id,))
        self.assertEqual(self.store.lifecycle_reason(team_id), "team_cleanup_unconfirmed")
        room_id = self.room()
        job = self.complete(room_id, cleanup=None)
        for status in ("unknown", "interrupted", "failed"):
            with self.store._connection(write=True) as db:
                db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job["id"]))
            self.assertEqual(self.store.lifecycle_reason(room_id), "team_cleanup_unconfirmed")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status='completed' WHERE id=?", (job["id"],))
            db.execute("UPDATE rooms SET stop_confirmed=0 WHERE id=?", (room_id,))
        self.assertEqual(self.store.lifecycle_reason(room_id), "team_cleanup_unconfirmed")

    def test_cancelled_never_claimed_job_does_not_require_a_process_receipt(self):
        room_id = self.room()
        self.store.request_stop(room_id, "cancel_requested")
        self.store.finish_stop(room_id, confirmed=True)
        self.store.set_lifecycle(room_id, "remove")
        self.assertEqual(self.store.snapshot(room_id)["room"]["lifecycle_state"], "removed")

    def test_unknown_execution_cleanup_is_not_treated_as_a_finished_process(self):
        room_id = self.room()
        self.complete(room_id)
        execution_id = str(uuid4())
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO executions(id,room_id,revision,snapshot_sha256,status,created_at,result_json) "
                       "VALUES(?,?,1,?,'failed',1000,?)", (execution_id, room_id, "a" * 64,
                                                        json.dumps({"cleanup_confirmed": False})))
        with self.assertRaisesRegex(RoomError, "team_cleanup_unconfirmed"):
            self.store.set_lifecycle(room_id, "remove")
        with self.store._connection(write=True) as db:
            db.execute("UPDATE executions SET result_json=? WHERE id=?",
                       (json.dumps({"cleanup_confirmed": True}), execution_id))
        self.store.set_lifecycle(room_id, "remove")

    def test_cancelled_reservation_is_safe_only_when_never_started(self):
        room_id = self.room()
        self.complete(room_id)
        execution_id = str(uuid4())
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO executions(id,room_id,revision,snapshot_sha256,status,created_at) "
                       "VALUES(?,?,1,?,'cancelled',1000)", (execution_id, room_id, "a" * 64))
            self.store._event(db, room_id, "execution_queued", run_id=execution_id)
        self.assertIsNone(self.store.lifecycle_reason(room_id))
        with self.store._connection(write=True) as db:
            self.store._event(db, room_id, "execution_started", run_id=execution_id)
        self.assertEqual(self.store.lifecycle_reason(room_id), "team_cleanup_unconfirmed")

    def test_inactive_room_blocks_even_duplicate_writes_and_can_restore(self):
        room_id = self.room()
        self.complete(room_id)
        self.store.set_lifecycle(room_id, "archive")
        calls = [lambda: self.store.send_message(room_id, "Extra", "claude", "extra"),
                 lambda: self.store.start_run(room_id, "More", "all", "run", run_id=str(uuid4()),
                                             revision=None, digest="a" * 64),
                 lambda: self.store.accept_run(room_id, "accept", run_id=str(uuid4()),
                                              revision=None, digest="a" * 64)]
        for operation in calls:
            with self.assertRaisesRegex(RoomError, "room_archived"):
                operation()
        self.store.set_lifecycle(room_id, "restore")
        self.assertTrue(self.store.send_message(room_id, "Extra", "claude", "extra"))

    def test_invalid_action_never_changes_any_room(self):
        room_id = self.room()
        for action in (None, [], {}, True, "delete", "", "Archive"):
            with self.assertRaisesRegex(RoomError, "invalid_lifecycle_action"):
                self.store.set_lifecycle(room_id, action)
        self.assertEqual(self.store.snapshot(room_id)["room"]["lifecycle_state"], "active")


class LifecycleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.calls = []

        async def fake(agent, prompt, cwd, **kwargs):
            self.calls.append(agent)
            return {"status": "completed", "session_id": str(uuid5(NAMESPACE_URL, str(cwd))),
                    "text": json.dumps({"message": "Done", "actions": [], "state": "done"}),
                    "events": [], "usage": None}

        manager = PartnerManager(self.root / "partners", default_registry(), startup_discovery=False)
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=fake,
                                   clock=lambda: 1000, partners=manager)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def settle(self):
        while any(not task.done() for task in self.service._workers.values()):
            await asyncio.gather(*list(self.service._workers.values()))

    async def completed_room(self):
        room = await self.service.create_room("Do a small task", "claude", str(uuid4()))
        await self.settle()
        return room["room"]["id"]

    async def test_partner_retirement_requires_explicit_restore_and_preserves_history(self):
        room_id = await self.completed_room()
        before = (await self.service.read_room(room_id))["members"]
        result = await self.service.set_partner_lifecycle("claude", "remove")
        self.assertEqual(result["partners"][0]["lifecycle_state"], "removed")
        with self.assertRaisesRegex(RoomError, "invalid_participants"):
            await self.service.create_room("New task", "claude", str(uuid4()), participants=["claude"])
        jobs_before = (await self.service.read_room(room_id))["jobs"]
        with self.assertRaisesRegex(RoomError, "partner_removed"):
            await self.service.send_message(room_id, "Existing continuation", "claude", "continue")
        blocked = await self.service.read_room(room_id)
        self.assertEqual(blocked["jobs"], jobs_before)
        self.assertEqual(blocked["members"][0]["session_id"], before[0]["session_id"])
        self.assertEqual(self.calls, ["claude"])
        restored = await self.service.set_partner_lifecycle("claude", "restore")
        self.assertEqual(restored["partners"][0]["lifecycle_state"], "active")
        fresh = await self.service.read_room(room_id)
        self.assertIsNone(fresh["members"][0]["session_id"])
        self.assertEqual(fresh["jobs"], jobs_before)
        self.assertTrue(any(event["kind"] == "partner_session_freshened"
                            and event["old_calls_replayed"] is False for event in fresh["events"]))
        await self.service.send_message(room_id, "Existing continuation", "claude", "after-restore")
        await self.settle()
        after = await self.service.read_room(room_id)
        old_jobs = {job["id"]: job for job in jobs_before}
        self.assertEqual({job["id"]: job for job in after["jobs"] if job["id"] in old_jobs}, old_jobs)
        self.assertEqual(self.calls, ["claude", "claude"])

    async def test_inactive_project_rejected_before_creating_room_and_source_is_preserved(self):
        source = self.root / "source"
        source.mkdir()
        (source / "README.md").write_text("# Original\n")
        project = (await self.service.register_project("Sample", str(source), ["README.md"]))["project"]
        result = await self.service.set_project_lifecycle(project["id"], "archive")
        self.assertEqual(result["project"]["lifecycle_state"], "archived")
        with self.assertRaisesRegex(ProjectCatalogError, "project_inactive"):
            await self.service.create_room("Edit", "claude", str(uuid4()), mode="project_files", project_id=project["id"])
        self.assertEqual((await self.service.list_rooms())["rooms"], [])
        self.assertEqual((source / "README.md").read_text(), "# Original\n")
        self.assertEqual(self.calls, [])

    async def test_runtime_worker_execution_and_capture_take_precedence_over_finished_rows(self):
        room_id = await self.completed_room()
        for kind in ("worker", "execution", "capture"):
            with self.subTest(kind=kind):
                gate = asyncio.Event()
                task = asyncio.create_task(gate.wait())
                if kind == "worker":
                    self.service._workers[(room_id, "synthetic_wait")] = task
                elif kind == "execution":
                    self.service._executions[room_id] = task
                else:
                    self.service._captures[room_id] = (task, threading.Event())
                try:
                    snapshot = await self.service.read_room(room_id)
                    self.assertFalse(snapshot["room"]["lifecycle_can_change"])
                    with self.assertRaisesRegex(RoomError, "room_lifecycle_busy"):
                        await self.service.set_room_lifecycle(room_id, "archive")
                finally:
                    gate.set()
                    await task
                    self.service._captures.pop(room_id, None)
        await self.service.set_room_lifecycle(room_id, "archive")
        self.assertEqual(len(self.calls), 1)

    async def test_archive_and_message_share_the_control_lock(self):
        room_id = await self.completed_room()
        lock = self.service._run_controls.setdefault(room_id, asyncio.Lock())
        await lock.acquire()
        archive = asyncio.create_task(self.service.set_room_lifecycle(room_id, "archive"))
        await asyncio.sleep(0)
        message = asyncio.create_task(self.service.send_message(room_id, "Do more", "claude", "racing-message"))
        await asyncio.sleep(0)
        lock.release()
        await archive
        with self.assertRaisesRegex(RoomError, "room_archived"):
            await message
        self.assertEqual(len((await self.service.read_room(room_id))["jobs"]), 1)
        self.assertEqual(self.calls, ["claude"])

    async def test_project_retirement_keeps_existing_captured_files_readable(self):
        source = self.root / "captured-source"
        source.mkdir()
        (source / "README.md").write_text("# Keep the isolated copy\n")
        project = (await self.service.register_project("Captured", str(source), ["README.md"]))["project"]
        # Capture occurs before dispatch; this test never needs a native process.
        self.service._wake = lambda _: None
        snapshot = await self.service.create_room("Read captured material", "claude", str(uuid4()),
                                                  mode="project_files", project_id=project["id"], work_mode="plan")
        await self.service.set_project_lifecycle(project["id"], "remove")
        after = await self.service.read_room(snapshot["room"]["id"])
        self.assertTrue(after["project"]["available"])
        self.assertEqual(after["project"]["files"], snapshot["project"]["files"])
        self.assertEqual((source / "README.md").read_text(), "# Keep the isolated copy\n")
        self.assertEqual(self.calls, [])

    async def test_archived_history_reads_no_dispatch_and_all_advancement_requires_restore(self):
        room_id = await self.completed_room()
        before = await self.service.read_room(room_id)
        await self.service.set_room_lifecycle(room_id, "remove")
        for operation in (
            lambda: self.service.send_message(room_id, "More", "claude", "new"),
            lambda: self.service.start_run(room_id, "More", "all", "run", run_id=str(uuid4()), revision=None, digest="a" * 64),
            lambda: self.service.accept_run(room_id, "accept", run_id=str(uuid4()), revision=None, digest="a" * 64),
            lambda: self.service.fork_room(room_id, "Fork", "all", "fork", run_id=str(uuid4()), revision=1, digest="a" * 64),
        ):
            with self.assertRaisesRegex(RoomError, "room_removed"):
                await operation()
        after = await self.service.read_room(room_id)
        self.assertEqual(before["jobs"], after["jobs"])
        self.assertEqual(before["members"], after["members"])
        self.assertEqual(len(self.calls), 1)
        await self.service.set_room_lifecycle(room_id, "restore")
        await self.service.send_message(room_id, "Continue", "claude", "after-restore")
        await self.settle()
        self.assertEqual(len(self.calls), 2)


class LifecycleHTTPFake:
    def __init__(self):
        self.calls = []
        self.error = None

    async def _set(self, kind, identifier, action):
        self.calls.append((kind, identifier, action))
        if self.error:
            raise RoomError(self.error)
        return {"lifecycle_state": {"archive": "archived", "restore": "active", "remove": "removed"}[action]}

    async def set_project_lifecycle(self, identifier, action):
        return await self._set("projects", identifier, action)

    async def set_partner_lifecycle(self, identifier, action):
        return await self._set("partners", identifier, action)

    async def set_room_lifecycle(self, identifier, action):
        return await self._set("rooms", identifier, action)

    async def expire_tasks(self):
        pass

    async def shutdown(self):
        pass


class LifecycleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.assets = Path(self.temp.name)
        (self.assets / "index.html").write_text("<title>Test</title>")
        self.service = LifecycleHTTPFake()
        self.server = create_room_server(self.service, port=0, asset_dir=self.assets)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, body, *, token=None):
        port = self.server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=scaled(3))
        data = json.dumps(body).encode()
        try:
            connection.request("POST", path, data, {"Content-Type": "application/json",
                               "X-LAC-Token": token or self.server.token,
                               "X-LAC-Instance": self.server.instance_id})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_management_routes_are_web_only_and_strictly_parse_action(self):
        identifier = str(uuid4())
        for kind, item_id in (("rooms", identifier), ("projects", identifier), ("partners", "claude")):
            path = f"/api/{kind}/{item_id}/lifecycle"
            for action in ("archive", "remove", "restore"):
                self.assertEqual(self.request(path, {"action": action})[0], 200)
                self.assertEqual(self.service.calls[-1], (kind, item_id, action))
            calls = len(self.service.calls)
            for body in ({}, {"action": []}, {"action": {}}, {"action": None}, {"action": True},
                         {"action": "delete"}, {"action": "archive", "force": True}):
                self.assertEqual(self.request(path, body)[0], 400)
            self.assertEqual(len(self.service.calls), calls)
            status, result = self.request(path.replace("/api/", "/api/mcp/"), {"action": "archive"}, token=self.server.mcp_token)
            self.assertEqual((status, result["error"]), (403, "user_lifecycle_required"))
            self.assertEqual(self.request(path, {"action": "archive"}, token=self.server.mcp_token)[0], 403)
            self.assertEqual(len(self.service.calls), calls)

    def test_lifecycle_errors_are_stable_and_unknown_codes_do_not_leak(self):
        path = f"/api/rooms/{uuid4()}/lifecycle"
        for code, expected_status in (("room_lifecycle_busy", 409), ("team_cleanup_unconfirmed", 409),
                                      ("room_not_found", 404), ("invalid_lifecycle_action", 400),
                                      ("/private/path should not escape", 409)):
            self.service.error = code
            status, body = self.request(path, {"action": "archive"})
            self.assertEqual(status, expected_status)
            self.assertEqual(body["error"], code if not code.startswith("/") else "operation_failed")
