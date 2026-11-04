"""Durable user intents through actual HTTP/SQLite; synthetic executors only."""
import asyncio
import hashlib
import json
import threading
import time
import unittest
from urllib.parse import quote, urlencode
from uuid import uuid4

from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
import test_room_paging as paging
import test_native_room_http as native_http
from test_folder_service import FolderProvider, response
from test_team_store import proposal


class HostOutboxHTTPTests(unittest.TestCase):
    request = paging.RoomPagingHTTPTests.request
    tearDown = paging.RoomPagingHTTPTests.tearDown

    def setUp(self):
        paging.RoomPagingHTTPTests.setUp(self)
        code, status, _ = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(status["capabilities"]["user_outbox_v1"])
        self.scope = status["user_outbox_scope"]

    def stage(self, kind="create", room=None, body=None, **extra):
        body = body or {"goal": "Keep this exact unsent request. e\u0301", "target": "claude", "request_id": str(uuid4())}
        if room is not None and "source_seq" not in extra:
            events = self.service.store.snapshot(room)["events"]
            extra["source_seq"] = events[-1]["seq"] if events else 0
        code, value, _ = self.request("POST", "/api/outbox", {"scope": self.scope, "request_id": body["request_id"],
            "kind": kind, "room_id": room, "body": body, **extra})
        self.assertEqual(code, 200, value)
        return body, value["outbox"]

    def exact(self, request_id):
        code, value, size = self.request("GET", "/api/outbox/" + quote(request_id, safe="") + "?" + urlencode({"scope": self.scope}))
        self.assertEqual(code, 200, value)
        return value["outbox"], size

    def submit(self, path, body):
        return self.request("POST", path + "?" + urlencode({"outbox_scope": self.scope, "response": "ack"}), body)

    def retire(self, item, disposition):
        return self.request("POST", "/api/outbox/" + quote(item["request_id"], safe="") + "/retire",
            {"scope": self.scope, "fingerprint": item["fingerprint"], "disposition": disposition})

    def restart(self, *, other_workspace=False):
        invoke, runner = self.service.invoke, self.service._runner_instance
        old_port, old_token, old_instance = self.port, self.server.token, self.server.instance_id
        self.server.shutdown(); self.thread.join(3); self.server.server_close()
        self.assertIsNone(self.server.runner.shutdown_error)
        workspace = self.root / ("other-workspaces" if other_workspace else "workspaces")
        self.service = RoomService(self.root / "rooms.sqlite3", workspace, invoke=invoke, runner=runner)
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}); self.thread.start()
        self.assertNotEqual(self.server.token, old_token)
        self.assertNotEqual(self.server.instance_id, old_instance)
        return old_port

    def test_stage_restart_exact_one_mib_body_send_once_and_monotonic_retirement(self):
        text = "e\u0301" + "\x01" * (1024 * 1024 - 3)
        body, item = self.stage(body={"goal": text, "target": "claude", "request_id": str(uuid4())})
        self.assertEqual(item["delivery"]["state"], "staged")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.store.list_rooms()["rooms"], [])
        self.assertEqual(self.retire(item, "received")[0], 409)
        self.assertEqual(self.stage(body=body)[1]["seq"], item["seq"])
        changed = {"scope": self.scope, "request_id": body["request_id"], "kind": "create", "room_id": None,
                   "body": body | {"goal": "Different."}}
        self.assertEqual(self.request("POST", "/api/outbox", changed)[0], 409)
        _, listing, size = self.request("GET", "/api/outbox?" + urlencode({"scope": self.scope}))
        self.assertLess(size, 4096)
        self.assertNotIn("body", listing["entries"][0])
        self.restart()
        self.assertEqual(self.request("GET", "/api/status")[1]["user_outbox_scope"], self.scope)
        original, size = self.exact(body["request_id"])
        self.assertGreater(size, 6 * 1024 * 1024 - 100)
        self.assertLessEqual(size, 8 * 1024 * 1024)
        self.assertEqual(original["body"], body)
        self.assertTrue(original["delivery"]["can_send"])
        code, ack, _ = self.submit("/api/rooms", body)
        self.assertEqual(code, 200, ack)
        self.assertTrue(self.started.wait(3))
        self.assertEqual(len(self.calls), 1)
        received, _ = self.exact(body["request_id"])
        self.assertEqual(received["delivery"]["state"], "received")
        self.assertEqual(received["delivery"]["result_room_id"], ack["room_id"])
        self.assertEqual(self.submit("/api/rooms", body)[1]["room_id"], ack["room_id"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.retire(received, "received")[0], 200)
        retired, _ = self.exact(body["request_id"])
        self.assertIsNone(retired["body"])
        self.assertEqual(retired["delivery"]["state"], "retired")
        self.assertEqual(self.stage(body=body)[1]["status"], "retired")
        self.assertEqual(self.retire(received, "received")[0], 200)
        self.assertEqual(self.submit("/api/rooms", body)[0], 409)

    def test_owned_precommit_handler_blocks_concurrent_dispatch_and_retirement(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._guard_objective_availability
        async def held(*args, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return await original(*args, **kwargs)
        self.service._guard_objective_availability = held
        body, item = self.stage()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.submit("/api/rooms", body)))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            observed, _ = self.exact(body["request_id"])
            self.assertEqual(observed["delivery"]["state"], "in_flight")
            self.assertFalse(observed["delivery"]["can_send"])
            self.assertEqual(self.submit("/api/rooms", body)[0], 409)
            self.assertEqual(self.retire(item, "abandoned")[0], 409)
            self.assertEqual(self.calls, [])
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 200, result)
        self.assertTrue(self.started.wait(3))
        self.assertEqual(len(self.calls), 1)

    def test_legacy_unknown_never_downgrades_and_received_lookup_uses_frozen_original_identity(self):
        missing, item = self.stage(legacy_unknown=True)
        self.assertEqual(item["delivery"]["state"], "unconfirmed")
        self.assertFalse(self.stage(body=missing)[1]["delivery"]["can_send"])
        self.assertEqual(self.retire(item, "abandoned")[0], 409)
        self.assertEqual(self.submit("/api/rooms", missing)[0], 409)
        body = {"goal": "A request already accepted by the old API.", "target": "claude", "request_id": str(uuid4())}
        code, ack, _ = self.request("POST", "/api/rooms?response=ack", body)
        self.assertEqual(code, 200)
        _, linked = self.stage(body=body, legacy_unknown=True)
        self.assertEqual(linked["delivery"]["state"], "received")
        self.assertEqual(linked["delivery"]["result_room_id"], ack["room_id"])
        # Current catalogs are irrelevant to a stored original receipt.
        self.service._runtime_catalogs.clear()
        self.assertEqual(self.exact(body["request_id"])[0]["delivery"]["state"], "received")
        self.assertEqual(self.retire(linked, "received")[0], 200)

    def test_stage_then_stop_or_archive_supersedes_send_without_blocking_controls(self):
        room, _ = paging.RoomPagingHTTPTests.create(self)
        self.assertTrue(self.started.wait(3))
        body, item = self.stage("message", room, {"text": "Do this later.", "target": "claude", "request_id": str(uuid4())})
        code, cancelled, _ = self.request("POST", f"/api/rooms/{room}/cancel?response=ack", {})
        self.assertEqual(code, 200, cancelled)
        blocked, _ = self.exact(body["request_id"])
        self.assertEqual(blocked["delivery"]["state"], "conflict")
        self.assertEqual(blocked["delivery"]["reason"], "outbox_intervening_control")
        before = len(self.calls)
        self.assertEqual(self.submit(f"/api/rooms/{room}/messages", body)[0], 409)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.retire(item, "abandoned")[0], 200)

    def test_delete_maps_unread_create_receipt_and_clears_only_its_pending_bodies(self):
        body, _ = self.stage()
        code, ack, _ = self.submit("/api/rooms", body)
        self.assertEqual(code, 200, ack)
        neighbor, _ = self.stage()
        self.release.set()
        room = ack["room_id"]
        code, operation, _ = self.request("POST", f"/api/rooms/{room}/lifecycle", {"action": "remove", "request_id": str(uuid4())})
        self.assertEqual(code, 200, operation)
        until = time.monotonic() + 4
        while time.monotonic() < until:
            result = self.request("GET", "/api/room-lifecycle-operations/" + operation["lifecycle_operation"]["id"])[1]
            if result["lifecycle_operation"]["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(result["lifecycle_operation"]["status"], "completed", result)
        deleted, _ = self.exact(body["request_id"])
        self.assertEqual(deleted["status"], "deleted")
        self.assertIsNone(deleted["body"])
        self.assertEqual(self.stage(body=body)[1]["status"], "deleted")
        self.assertEqual(self.exact(neighbor["request_id"])[0]["body"], neighbor)
        self.assertEqual(self.request("GET", f"/api/rooms/{room}/state")[0], 404)

    def test_project_removal_clears_only_uncaptured_create_intent(self):
        source = self.root / "source"; source.mkdir(); marker = source / "notes.md"; marker.write_text("Original source.")
        code, registered, _ = self.request("POST", "/api/projects", {"name": "Source", "path": str(source), "selection_kind": "folder"})
        self.assertEqual(code, 200, registered)
        project = registered["project"]["id"]
        body, _ = self.stage(body={"goal": "Capture later.", "target": "claude", "request_id": str(uuid4()),
                                  "mode": "project_folder", "project_id": project})
        neighbor, _ = self.stage()
        code, value, _ = self.request("POST", f"/api/projects/{project}/lifecycle", {"action": "remove", "request_id": str(uuid4())})
        self.assertEqual(code, 200, value)
        until = time.monotonic() + 4
        while time.monotonic() < until and self.exact(body["request_id"])[0]["status"] != "deleted":
            time.sleep(0.01)
        self.assertIsNone(self.exact(body["request_id"])[0]["body"])
        self.assertEqual(self.exact(neighbor["request_id"])[0]["body"], neighbor)
        self.assertEqual(marker.read_text(), "Original source.")
        self.assertEqual(self.calls, [])

    def test_mcp_and_wrong_workspace_cannot_enumerate_or_read_user_unsent_text(self):
        body, _ = self.stage()
        for route in ("/api/mcp/outbox?" + urlencode({"scope": self.scope}),
                      "/api/mcp/outbox/" + body["request_id"] + "?" + urlencode({"scope": self.scope})):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            connection.request("GET", route, headers={"X-LAC-Token": self.server.mcp_token, "X-LAC-Instance": self.server.instance_id})
            response = connection.getresponse(); raw = response.read(); connection.close()
            self.assertEqual(response.status, 403)
            self.assertNotIn(body["goal"].encode(), raw)
        self.restart(other_workspace=True)
        self.assertNotEqual(self.request("GET", "/api/status")[1]["user_outbox_scope"], self.scope)
        self.assertEqual(self.request("GET", "/api/outbox?" + urlencode({"scope": self.scope}))[0], 409)
        self.assertEqual(self.calls, [])

    def test_summary_pages_are_bounded_highwater_and_precommit_restart_is_safe_without_side_effects(self):
        staged = [self.stage()[0] for _ in range(7)]
        listing = self.request("GET", "/api/outbox?" + urlencode({"scope": self.scope, "limit": 3}))[1]
        self.assertTrue(listing["has_more"])
        self.assertEqual(listing["pending_count"], 7)
        anchor = listing["through_seq"]
        self.stage()
        entries = listing["entries"]
        while listing["has_more"]:
            listing = self.request("GET", "/api/outbox?" + urlencode({"scope": self.scope, "limit": 3,
                "after_seq": listing["last_seq"], "through_seq": anchor}))[1]
            entries.extend(listing["entries"])
        self.assertEqual([item["request_id"] for item in entries], [body["request_id"] for body in staged])
        self.assertTrue(all("body" not in item for item in entries))
        self.service.store.begin_outbox_submission(self.scope, staged[0]["request_id"], self.server.instance_id)
        self.restart()
        item, _ = self.exact(staged[0]["request_id"])
        self.assertTrue(item["delivery"]["can_send"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.store.list_rooms()["rooms"], [])

    def test_public_second_service_constructor_cannot_steal_live_precommit_owner(self):
        body, _ = self.stage()
        self.service.store.begin_outbox_submission(self.scope, body["request_id"], self.server.instance_id)
        with self.assertRaisesRegex(RuntimeError, "runtime_in_use"):
            RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=self.service.invoke)
        self.assertEqual(self.exact(body["request_id"])[0]["attempt_count"], 1)
        self.assertEqual(self.calls, [])

    def test_abandoned_unsent_key_cannot_be_revived_by_a_legacy_post_without_outbox_query(self):
        body, item = self.stage()
        self.assertEqual(self.retire(item, "abandoned")[0], 200)
        self.assertEqual(self.request("POST", "/api/rooms?response=ack", body)[0], 409)
        self.assertEqual(self.request("POST", "/api/rooms?response=ack", body | {"goal": "Repurposed ID."})[0], 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.store.list_rooms()["rooms"], [])

    def test_restarted_precommit_without_actual_claim_cleanup_does_not_authorize_retry(self):
        room, _ = self.service.store.create_room("An unrelated previously claimed request.", "claude", str(uuid4()))
        claim = self.service.store.claim(room, "claude")
        body, _ = self.stage("message", room, {"text": "New unsent followup.", "target": "claude", "request_id": str(uuid4())})
        self.service.store.begin_outbox_submission(self.scope, body["request_id"], self.server.instance_id)
        self.service.store.record_job_cleanup(claim["id"], confirmed=False)
        observed = self.service.store.read_outbox(self.scope, body["request_id"], instance_id=str(uuid4()))["outbox"]
        self.assertEqual(observed["delivery"]["state"], "unconfirmed")
        self.assertEqual(observed["delivery"]["reason"], "outbox_cleanup_unconfirmed")
        self.assertFalse(observed["delivery"]["can_send"])
        self.assertEqual(observed["body"], body)
        self.assertEqual(self.calls, [])

    def test_delayed_room_stage_preserves_old_watermark_after_pause_restart_and_restage(self):
        room, _ = self.service.store.create_room("Finish the original task.", "claude", str(uuid4()),
            collaboration=True, execution_policy={"mode": "autonomous"})
        before = self.service.store.snapshot(room)
        source = before["events"][-1]["seq"]
        run = before["current_run"]
        body = {"text": "Continue with the original work.", "target": "all", "request_id": str(uuid4()), "intent": "continue",
                "binding": {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]}}
        code, paused, _ = self.request("POST", f"/api/rooms/{room}/pause?response=ack",
            {"request_id": str(uuid4()), "objective_id": before["objective"]["id"]})
        self.assertEqual(code, 200, paused)
        # The original user gesture occurred before pause, but registration
        # only reaches the host afterward. No browser-memory guard can fix it.
        _, staged = self.stage("message", room, body, source_seq=source)
        self.assertEqual(staged["delivery"]["reason"], "outbox_intervening_control")
        self.assertFalse(staged["delivery"]["can_send"])
        self.restart()
        now = self.service.store.snapshot(room)["events"][-1]["seq"]
        _, repeated = self.stage("message", room, body, source_seq=now)
        self.assertEqual(repeated["source_seq"], source)
        self.assertFalse(repeated["delivery"]["can_send"])
        self.assertEqual(self.submit(f"/api/rooms/{room}/messages", body)[0], 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.exact(body["request_id"])[0]["body"], body)
        # A legacy request cannot be upgraded merely by restaging a new seq.
        legacy = body | {"request_id": str(uuid4())}
        _, unknown = self.stage("message", room, legacy, source_seq=None)
        self.assertEqual(unknown["delivery"]["state"], "unconfirmed")
        self.assertFalse(self.stage("message", room, legacy, source_seq=now)[1]["delivery"]["can_send"])
        foreign, _ = self.service.store.create_room("Another room's history.", "claude", str(uuid4()))
        foreign_seq = self.service.store.snapshot(foreign)["events"][-1]["seq"]
        with self.service.store._connection(write=True) as db:
            self.service.store._event(db, room, "message", sender="user", recipient="all", text="Later same-room record.")
        # A foreign seq can be numerically below this room's latest seq. It is
        # still not a valid watermark of the source conversation.
        wrong_source = body | {"request_id": str(uuid4())}
        self.assertEqual(self.request("POST", "/api/outbox", {"scope": self.scope, "request_id": wrong_source["request_id"],
            "kind": "message", "room_id": room, "body": wrong_source, "source_seq": foreign_seq})[0], 400)

    def test_control_between_begin_and_canonical_commit_rejects_the_old_request(self):
        room, _ = self.service.store.create_room("Finish the original task.", "claude", str(uuid4()),
            collaboration=True, execution_policy={"mode": "autonomous"})
        original = self.service.store.snapshot(room)
        pause = {"request_id": str(uuid4()), "objective_id": original["objective"]["id"]}
        self.assertEqual(self.request("POST", f"/api/rooms/{room}/pause?response=ack", pause)[0], 200)
        body, _ = self.stage("resume", room, {"request_id": str(uuid4()), "objective_id": original["objective"]["id"]})
        entered, release = threading.Event(), threading.Event()
        guard = self.service._guard_objective_availability
        async def held(*args, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return await guard(*args, **kwargs)
        self.service._guard_objective_availability = held
        results = []
        thread = threading.Thread(target=lambda: results.append(self.submit(f"/api/rooms/{room}/resume", body)))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.request("POST", f"/api/rooms/{room}/cancel?response=ack", {})[0], 200)
        finally:
            release.set(); thread.join(5)
        self.assertEqual(results[0][0], 409, results)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.exact(body["request_id"])[0]["delivery"]["can_send"])
        self.assertEqual(self.service.store.snapshot(room)["room"]["status"], "cancelled")


class HostOutboxPreparationTests(unittest.TestCase):
    setUp = native_http.NativeRoomHTTPTests.setUp
    tearDown = native_http.NativeRoomHTTPTests.tearDown
    request = native_http.NativeRoomHTTPTests.request
    configure = native_http.NativeRoomHTTPTests.configure
    wait = native_http.NativeRoomHTTPTests.wait
    handler = native_http.NativeRoomHTTPTests.handler

    def test_actual_partial_native_handoff_never_looks_unsent_or_retires_as_received(self):
        _, project = self.configure()
        old = FolderProvider()
        def handler(agent, context):
            if context["job"]["job_phase"] == "work":
                commands = [{"type": "complete", "task_id": "T1", "result": "Old attempt omitted files."}]
            elif not context["team"]["tasks"] and agent == "claude":
                commands = [proposal("T1", paths=["main.py", "notes.md"]), {"type": "claim", "task_id": "T1"}, {"type": "plan_ready"}]
            else:
                commands = [{"type": "plan_ready"}]
            return response(context, commands=commands)
        old.handler = handler; self.service.invoke = old
        code, created = self.request("POST", "/api/rooms", {"goal": "Update the value and preserve notes.", "target": "claude",
            "request_id": str(uuid4()), "mode": "project_folder", "project_id": project, "work_mode": "build", "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200)
        room = created["room"]["id"]
        before = self.wait(room, lambda item: item["current_run"]["can_native_handoff"])
        current = before["current_run"]
        body = {"run_id": current["id"], "revision": current["revision"], "digest": current["digest"], "request_id": str(uuid4()),
                "execution_policy": {"mode": "autonomous"}, "runtime_settings": {"permission": "full_access", "members": {}}}
        scope = self.service.user_outbox_scope
        code, staged = self.request("POST", "/api/outbox", {"scope": scope, "request_id": body["request_id"], "kind": "native_handoff", "room_id": room, "body": body,
            "source_seq": before["events"][-1]["seq"]})
        self.assertEqual(code, 200, staged)
        def fail(*args, **kwargs):
            raise RuntimeError("Synthetic bootstrap interruption after child registration.")
        self.service._bootstrap_native_handoff = fail
        self.assertGreaterEqual(self.request("POST", f"/api/rooms/{room}/native-handoff?outbox_scope={scope}&response=ack", body)[0], 400)
        code, exact = self.request("GET", "/api/outbox/" + body["request_id"] + "?scope=" + scope)
        self.assertEqual(code, 200)
        self.assertEqual(exact["outbox"]["body"], body)
        self.assertEqual(exact["outbox"]["delivery"]["state"], "unconfirmed")
        self.assertFalse(exact["outbox"]["delivery"]["can_send"])
        self.assertEqual(self.request("POST", "/api/outbox/" + body["request_id"] + "/retire",
            {"scope": scope, "fingerprint": staged["outbox"]["fingerprint"], "disposition": "received"})[0], 409)
        with self.service.store._connection() as db:
            derived = "native-room-" + hashlib.sha256(body["request_id"].encode()).hexdigest()
            self.assertIsNotNone(db.execute("SELECT 1 FROM requests WHERE request_key=?", (hashlib.sha256(derived.encode()).hexdigest(),)).fetchone())
        self.assertEqual(self.native.calls, [])
