"""Exact message receipts and permanent registration deletion at real local boundaries."""
import asyncio
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_service import RoomError
from tests import test_room_lifecycle_operations as lifecycle_fixtures
from tests import test_team_runs_http as http_fixtures


class MessageReceiptServiceTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the owned SQLite/tree fixture and its non-network transport.
    asyncSetUp = lifecycle_fixtures.RoomLifecycleOperationTests.asyncSetUp
    asyncTearDown = lifecycle_fixtures.RoomLifecycleOperationTests.asyncTearDown
    new_service = lifecycle_fixtures.RoomLifecycleOperationTests.new_service
    completed = lifecycle_fixtures.RoomLifecycleOperationTests.completed
    settled_operation = lifecycle_fixtures.RoomLifecycleOperationTests.settled_operation

    async def test_missing_receipt_is_read_only_and_concurrent_exact_retry_dispatches_once(self):
        room = await self.completed()
        before, calls = self.service.store.snapshot(room), len(self.calls)
        with self.assertRaisesRegex(RoomError, "message_request_not_found"):
            await self.service.read_message_receipt(room, "One more detail.", "claude", "message")
        self.assertEqual(self.service.store.snapshot(room), before)
        self.assertEqual(len(self.calls), calls)

        await asyncio.gather(*(self.service.send_message(room, "One more detail.", "claude", "message") for _ in range(2)))
        await asyncio.gather(*list(self.service._workers.values()))
        before_receipt = self.service.store.snapshot(room)
        receipt = await self.service.read_message_receipt(room, "One more detail.", "claude", "message")
        self.assertEqual(receipt, {"received": True, "room_id": room, "request_id": "message"})
        self.assertEqual(self.service.store.snapshot(room), before_receipt)
        self.assertEqual(len(self.calls), calls + 1)
        self.assertEqual(len([event for event in before_receipt["events"] if event["kind"] == "message"
                              and event.get("sender") == "user" and event.get("text") == "One more detail."]), 1)
        for text, target in (("Different detail.", "claude"), ("One more detail.", "all")):
            with self.assertRaisesRegex(RoomError, "request_conflict"):
                await self.service.read_message_receipt(room, text, target, "message")

    async def test_receipt_retains_the_deleted_room_tombstone_without_resurrecting_it(self):
        room = await self.completed()
        await self.service.send_message(room, "Keep this receipt.", "claude", "deleted-message")
        await asyncio.gather(*list(self.service._workers.values()))
        await self.service.set_room_lifecycle(room, "remove", "delete")
        self.assertTrue((await self.settled_operation("delete"))["deleted"])
        calls = len(self.calls)
        with self.assertRaisesRegex(RoomError, "room_deleted"):
            await self.service.read_message_receipt(room, "Keep this receipt.", "claude", "deleted-message")
        with self.service.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM rooms WHERE id=?", (room,)).fetchone()[0], 0)
        self.assertEqual(len(self.calls), calls)

    async def test_registration_delete_preserves_source_captured_room_and_neighbor(self):
        source = self.root / "source"
        source.mkdir()
        (source / "README.md").write_text("# Original source\n")
        project = (await self.service.register_project("Captured", str(source), ["README.md"]))["project"]
        neighbor = (await self.service.register_project("Neighbor", str(source), ["README.md"]))["project"]
        self.service._wake = lambda _: None
        captured = await self.service.create_room("Read captured material.", "claude", "capture",
            mode="project_files", project_id=project["id"], work_mode="plan")

        soft = await self.service.set_project_lifecycle(project["id"], "remove")
        self.assertEqual(soft["project"]["lifecycle_state"], "removed")
        await self.service.set_project_lifecycle(project["id"], "restore")
        result = await self.service.set_project_lifecycle(project["id"], "remove", "delete-project")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["project"], {"id": project["id"], "deleted": True})
        self.assertEqual(result["projects"], [neighbor])
        self.assertEqual(await self.service.set_project_lifecycle(project["id"], "remove", "delete-project"), result)
        with self.assertRaisesRegex(RoomError, "request_conflict"):
            await self.service.set_project_lifecycle(neighbor["id"], "remove", "delete-project")
        with self.assertRaisesRegex(RoomError, "request_conflict"):
            await self.service.send_message(captured["room"]["id"], "Another message.", "claude", "delete-project")
        self.service._projects.clear()
        after = await self.service.read_room(captured["room"]["id"])
        self.assertEqual(after["project"]["files"], captured["project"]["files"])
        self.assertEqual((source / "README.md").read_text(), "# Original source\n")
        self.assertEqual(self.calls, [])

    async def test_failed_registration_delete_resumes_same_intent_after_service_restart(self):
        source = self.root / "source"
        source.mkdir()
        (source / "README.md").write_text("Unmodified source.\n")
        project = (await self.service.register_project("Source", str(source), ["README.md"]))["project"]
        with patch.object(self.service.catalog, "remove_registration", side_effect=OSError("Synthetic catalog I/O failure")):
            result = await self.service.set_project_lifecycle(project["id"], "remove", "delete-after-restart")
            self.assertFalse(result["deleted"])
            self.assertEqual(result["project_deletion"]["status"], "pending")
            self.assertEqual(result["project_deletion"]["error"], "project_catalog_unavailable")
            with self.assertRaisesRegex(RoomError, "project_deletion_pending"):
                await self.service.set_project_lifecycle(project["id"], "restore")
            with self.assertRaisesRegex(RoomError, "project_deletion_pending"):
                await self.service.create_room("Do not capture a pending registration.", "claude", "new-capture",
                    mode="project_files", project_id=project["id"])
        await self.service.shutdown()
        self.service = self.new_service()
        result = await self.service.read_project_deletion("delete-after-restart")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["project_deletion"]["request_id"], "delete-after-restart")
        self.assertEqual((source / "README.md").read_text(), "Unmodified source.\n")
        self.assertEqual(self.calls, [])

    async def test_catalog_removal_before_journal_commit_is_idempotent_after_restart(self):
        source = self.root / "source"
        source.mkdir()
        (source / "README.md").write_text("Unmodified source.\n")
        project = (await self.service.register_project("Source", str(source), ["README.md"]))["project"]
        self.service.store.request_project_deletion(project["id"], "lost-result")
        self.service.catalog.remove_registration(project["id"])
        await self.service.shutdown()
        self.service = self.new_service()
        result = await self.service.set_project_lifecycle(project["id"], "remove", "lost-result")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["projects"], [])
        self.assertEqual((source / "README.md").read_text(), "Unmodified source.\n")
        self.assertEqual(self.calls, [])

    async def test_existing_request_keys_cannot_authorize_registration_deletion(self):
        room = await self.completed("existing-key")
        source = self.root / "source"
        source.mkdir()
        (source / "README.md").write_text("Unmodified source.\n")
        project = (await self.service.register_project("Source", str(source), ["README.md"]))["project"]
        for request_id in ("existing-key", "archive-key"):
            if request_id == "archive-key":
                await self.service.set_room_lifecycle(room, "archive", request_id)
            with self.assertRaisesRegex(RoomError, "request_conflict"):
                await self.service.set_project_lifecycle(project["id"], "remove", request_id)
        self.assertEqual(self.service.catalog.get(project["id"]), project)


class MessageReceiptHTTPTests(unittest.TestCase):
    setUp = http_fixtures.TeamRunHTTPTests.setUp
    tearDown = http_fixtures.TeamRunHTTPTests.tearDown
    request = http_fixtures.TeamRunHTTPTests.request

    def completed(self):
        self.provider.handler = lambda *_: {"message": "Finished.", "state": "done", "actions": []}
        code, snapshot = self.request("POST", "/api/rooms", {"goal": "A simple task.", "target": "claude", "request_id": str(uuid4())})
        self.assertEqual(code, 200)
        room = snapshot["room"]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            code, snapshot = self.request("GET", "/api/rooms/" + room)
            self.assertEqual(code, 200)
            if not any(job["status"] in {"queued", "working"} for job in snapshot["jobs"]):
                return room
            time.sleep(0.005)
        self.fail("Synthetic task did not settle")

    def test_receipt_route_checks_exact_original_body_and_user_scope_without_dispatch(self):
        room = self.completed()
        code, status = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(status["capabilities"]["message_receipts"])
        path = f"/api/rooms/{room}/messages"
        body = {"text": "Add one detail.", "target": "claude", "request_id": "http-message"}
        before_calls = len(self.provider.calls)
        code, error = self.request("POST", path + "/receipt", body)
        self.assertEqual((code, error["error"]), (404, "message_request_not_found"))
        self.assertEqual(len(self.provider.calls), before_calls)
        self.assertEqual(self.request("POST", path, body)[0], 200)
        code, receipt = self.request("POST", path + "/receipt", body)
        self.assertEqual(code, 200)
        self.assertEqual(receipt, {"received": True, "room_id": room, "request_id": "http-message"})
        self.assertEqual(self.request("POST", path + "/receipt", {**body, "text": "Different."})[0], 409)
        self.assertEqual(self.request("POST", path + "/receipt", {**body, "extra": True})[0], 400)
        code, error = self.request("POST", path.replace("/api/", "/api/mcp/") + "/receipt", body, mcp=True)
        self.assertEqual((code, error["error"]), (403, "user_message_receipt_required"))

    def test_project_deletion_requires_new_explicit_contract_and_preserves_original_files(self):
        source = self.root / "source"
        source.mkdir()
        (source / "README.md").write_text("Original project stays.\n")
        code, registered = self.request("POST", "/api/projects", {"name": "Source", "path": str(source), "files": ["README.md"]})
        self.assertEqual(code, 200)
        project = registered["project"]["id"]
        path = f"/api/projects/{project}/lifecycle"
        code, soft = self.request("POST", path, {"action": "remove"})
        self.assertEqual((code, soft["project"]["lifecycle_state"]), (200, "removed"))
        self.assertEqual(self.request("POST", path, {"action": "restore"})[0], 200)
        self.assertEqual(self.request("POST", path, {"action": "archive", "request_id": "invalid"})[0], 400)
        body = {"action": "remove", "request_id": "http-project-delete"}
        code, removed = self.request("POST", path, body)
        self.assertEqual(code, 200)
        self.assertTrue(removed["deleted"])
        self.assertEqual(self.request("POST", path, body), (code, removed))
        self.assertEqual(self.request("GET", "/api/project-deletions?request_id=http-project-delete"), (code, removed))
        self.assertEqual(self.request("GET", "/api/project-deletions?request_id=missing")[0], 404)
        self.assertEqual(self.request("GET", "/api/mcp/project-deletions?request_id=http-project-delete", mcp=True)[0], 403)
        self.assertEqual(self.request("GET", "/api/projects")[1]["projects"], [])
        self.assertEqual((source / "README.md").read_text(), "Original project stays.\n")
        self.assertEqual(self.provider.calls, [])

