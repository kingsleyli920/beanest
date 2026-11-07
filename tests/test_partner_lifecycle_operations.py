"""Partner intent regressions through public HTTP; no provider processes.

Only temporary profiles, real SQLite and held synthetic text invocations are
used. The remove-with-request-id case prepares the new lifecycle contract;
legacy action-only removal keeps its existing scope.
"""
import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import NAMESPACE_URL, uuid4, uuid5
from unittest.mock import AsyncMock, patch

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError
import test_native_room_service as native


class PartnerLifecycleOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.calls = []
        self.release = asyncio.Event()
        self.started = {provider: threading.Event() for provider in ("claude-a", "claude-b")}
        self.cancelled = {provider: threading.Event() for provider in self.started}

        async def held(provider, prompt, cwd, **options):
            self.calls.append(provider)
            self.started[provider].set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled[provider].set()
                raise
            return {"session_id": options["session_id"] or str(uuid5(NAMESPACE_URL, str(cwd))),
                    "status": "completed", "events": [], "usage": None,
                    "text": json.dumps({"message": "Synthetic text completed.", "state": "done", "actions": []})}

        self.manager = PartnerManager(self.root / "partners", default_registry(), startup_discovery=False)
        for provider in self.started:
            self.manager.add(id=provider, name=provider, adapter="claude")
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   invoke=held, partners=self.manager)
        self.server = create_room_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.runner.loop.call_soon_threadsafe(self.release.set)
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temporary.cleanup()
        self.assertFalse(self.thread.is_alive())
        self.assertIsNone(self.server.runner.shutdown_error)
        self.assertFalse(self.root.exists())

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        headers = {"X-LAC-Token": self.server.token, "Content-Type": "application/json"}
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_disabling_one_profile_cancels_only_its_owned_worker_not_same_adapter_neighbor(self):
        status, room = self.request("POST", "/api/rooms", {
            "goal": "Two independently owned synthetic text calls.", "target": "all", "request_id": str(uuid4()),
            "participants": ["claude-a", "claude-b"]})
        self.assertEqual(status, 200, room)
        self.assertTrue(all(event.wait(2) for event in self.started.values()), "Both fixture workers must be running.")
        self.assertEqual(self.manager.get("claude-a")["adapter"], self.manager.get("claude-b")["adapter"])
        status, result = self.request("POST", "/api/partners/claude-a/enabled", {"enabled": False})
        self.assertEqual(status, 200, result)
        self.assertTrue(self.cancelled["claude-a"].wait(0.5), "An accepted disable must stop this profile's owned call.")
        self.assertFalse(self.cancelled["claude-b"].is_set(), "Matching adapters do not share a cancellation lease.")
        status, current = self.request("GET", "/api/rooms/" + room["room"]["id"])
        self.assertEqual(status, 200, current)
        self.assertTrue(any(job["provider"] == "claude-b" and job["status"] == "working" for job in current["jobs"]))
        self.assertEqual({member["id"] for member in current["members"]}, {"claude-a", "claude-b"})

    def test_request_id_removal_finishes_while_only_member_room_is_paused_without_invoking(self):
        async def prepare_paused_room():
            room, _ = self.service.store.create_room("Preserve the paused original work.", "claude-a", str(uuid4()),
                participants=["claude-a"], collaboration=True, execution_policy={"mode": "autonomous"})
            snapshot = self.service.store.snapshot(room)
            await self.service.objective_action(room, "pause", str(uuid4()), objective_id=snapshot["objective"]["id"])
            return room

        room = self.server.runner.call(prepare_paused_room)
        status, before = self.request("GET", "/api/rooms/" + room)
        self.assertEqual(status, 200, before)
        self.assertEqual(before["objective"]["status"], "paused")
        self.assertEqual(self.calls, [], "Fixture setup must not start the queued plan.")
        body = {"action": "remove", "request_id": str(uuid4())}
        route = "/api/partners/claude-a/lifecycle"
        status, result = self.request("POST", route, body)
        self.assertEqual(status, 200, result)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, profiles = self.request("GET", "/api/partners")
            self.assertEqual(status, 200, profiles)
            if not any(profile["id"] == "claude-a" for profile in profiles["partners"]):
                break
            time.sleep(0.005)
        self.assertFalse(any(profile["id"] == "claude-a" for profile in profiles["partners"]),
                         "A paused room and absent receiver must not keep configuration removal pending.")
        self.assertEqual(self.request("POST", route, body)[0], 200)
        status, after = self.request("GET", "/api/rooms/" + room)
        self.assertEqual(status, 200, after)
        self.assertEqual(after["objective"]["status"], "paused")
        self.assertEqual(after["objective"]["id"], before["objective"]["id"])
        self.assertEqual(after["current_run"]["id"], before["current_run"]["id"])
        identities = lambda value: [{key: member[key] for key in ("id", "name", "adapter", "session_id")}
                                    for member in value["members"]]
        self.assertEqual(identities(after), identities(before), "Historical copied identities remain readable.")
        self.assertEqual(self.calls, [])

    def test_pending_intent_blocks_claim_and_new_room_before_old_profile_configuration_changes(self):
        room, _ = self.service.store.create_room("A queued call with no native process.", "claude-a", str(uuid4()), participants=["claude-a"])
        operation = self.service.store.request_partner_operation("claude-a", "claude", "disable", "held-intent")
        self.assertTrue(self.manager.get("claude-a")["enabled"])
        self.assertIsNone(self.service.store.claim(room, "claude-a"))
        self.assertFalse(any(job["status"] == "working" for job in self.service.store.snapshot(room)["jobs"]))
        status, result = self.request("POST", "/api/rooms", {"goal": "Must not bypass a pending intent.", "target": "claude-a",
            "request_id": str(uuid4()), "participants": ["claude-a"]})
        self.assertEqual((status, result["error"]), (409, "partner_disabled"))
        self.assertEqual(self.service.store.partner_operation("held-intent")["id"], operation["id"])
        self.assertEqual(self.calls, [])

    def test_operation_request_is_strict_idempotent_and_not_found_is_authoritative(self):
        self.assertEqual(self.request("GET", "/api/partners/lifecycle-operations/not-sent")[0], 404)
        body = {"enabled": False, "request_id": "idempotent-disable"}
        route = "/api/partners/claude-a/enabled"
        status, first = self.request("POST", route, body)
        self.assertEqual(status, 200, first)
        status, again = self.request("POST", route, body)
        self.assertEqual(status, 200, again)
        self.assertEqual(first["partner_lifecycle_operation"]["id"], again["partner_lifecycle_operation"]["id"])
        self.assertEqual(self.request("POST", route, {**body, "enabled": True})[0], 409)
        self.assertEqual(self.request("POST", route, {**body, "request_id": ""})[0], 400)
        self.assertEqual(self.calls, [])

    def test_fresh_quota_never_reenables_manually_disabled_profile(self):
        room, _ = self.service.store.create_room("An original member roster.", "claude-a", str(uuid4()), participants=["claude-a"])
        self.assertEqual(self.request("POST", "/api/partners/claude-a/enabled", {"enabled": False})[0], 200)
        snapshot = self.service.store.snapshot(room)
        snapshot["objective"] = {"retired_partners": {"claude-a": {"reason": "agent_quota_exhausted"}}}
        profile = self.manager.get("claude-a")
        profile["availability"] = {"state": "available", "reason": None}
        with patch.object(self.manager, "get", return_value=profile):
            self.assertEqual(self.service._recovered_quota_members(snapshot), [])

    def test_room_deleted_during_retention_does_not_keep_profile_removal_pending(self):
        room, _ = self.service.store.create_room("A never-started temporary room.", "claude-a", str(uuid4()), participants=["claude-a"])
        entered, release = threading.Event(), threading.Event()
        retain = self.service._retain_partner_target
        def held(operation, target):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Fixture retention was not released.")
            return retain(operation, target)
        with patch.object(self.service, "_retain_partner_target", side_effect=held):
            try:
                status, result = self.request("POST", "/api/partners/claude-a/lifecycle", {"action": "remove", "request_id": "remove-alongside-room"})
                self.assertEqual(status, 200, result)
                self.assertTrue(entered.wait(2))
                status, deleted = self.request("POST", f"/api/rooms/{room}/lifecycle", {"action": "remove", "request_id": "delete-affected-room"})
                self.assertEqual(status, 200, deleted)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and self.request("GET", "/api/rooms/" + room)[0] != 404:
                    time.sleep(.005)
                self.assertEqual(self.request("GET", "/api/rooms/" + room)[0], 404)
            finally:
                release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status, result = self.request("GET", "/api/partners/lifecycle-operations/remove-alongside-room")
                if result["partner_lifecycle_operation"]["status"] == "completed":
                    break
                time.sleep(.005)
        self.assertTrue(result["deleted"], result)
        self.assertEqual(result["partner_lifecycle_operation"]["targets"][0]["state"], "done")
        self.assertEqual(self.calls, [])

    def test_pending_configuration_write_serializes_new_intent_and_same_key_replay(self):
        entered, release = threading.Event(), threading.Event()
        original = self.manager.set_enabled
        responses = []
        def held(provider, enabled):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Fixture configuration write was not released.")
            return original(provider, enabled)
        body = {"enabled": False, "request_id": "held-configuration"}
        with patch.object(self.manager, "set_enabled", side_effect=held):
            thread = threading.Thread(target=lambda: responses.append(self.request("POST", "/api/partners/claude-a/enabled", body)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(RoomError, "partner_lifecycle_busy"):
                    self.service.store.request_partner_operation("claude-a", "claude", "enable", "new-enable-during-write")
                replay = self.service.store.request_partner_operation("claude-a", "claude", "disable", body["request_id"])
                self.assertEqual((replay["status"], replay["action"]), ("pending", "disable"))
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(responses[0][0], 200, responses)
        self.assertFalse(self.manager.get("claude-a")["enabled"])
        status, enabled = self.request("POST", "/api/partners/claude-a/enabled", {"enabled": True, "request_id": "enable-after-completion"})
        self.assertEqual(status, 200, enabled)
        self.assertTrue(self.manager.get("claude-a")["enabled"])
        self.assertEqual(self.calls, [])


class PartnerNativeHandoffTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create

    async def prepare_interrupted_owner(self, *, cleanup=True, runtime_members=None):
        self.working, self.release = asyncio.Event(), asyncio.Event()
        self.canonical_revision_before = 1
        original = self.handler
        self.transport_calls = []

        async def handler(agent, context, cwd):
            if context.get("partner_handoff_inspection"):
                self.assertEqual(agent, "codex")
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                self.assertEqual(self.service._project(self.fixture_room).snapshot()["revision"], self.canonical_revision_before)
                return native.native_reply(context)
            if context["job"]["job_phase"] == "work":
                if agent == "claude":
                    (cwd / "main.py").write_text("VALUE = 2\n")
                    self.working.set()
                    await self.release.wait()
                else:
                    self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                    return native.native_reply(context, [{"type": "complete", "task_id": context["task"]["id"],
                        "result": "Continued from retained bytes; no old command replay."}])
            return original(agent, context, cwd)

        self.fake.handler = handler
        async def transport(agent, prompt, cwd, session_id=None, **options):
            identity = session_id or str(uuid4())
            self.transport_calls.append({"agent": agent, "session_id": session_id, "permission": options["permission"],
                                         "context": json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])})
            directory = options["receipt_dir"]
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            receipt = directory / (str(uuid4()) + ".jsonl")
            data = {"type": "system", "subtype": "init", "session_id": identity} if agent == "claude" else {"type": "thread.started", "thread_id": identity}
            receipt.write_text(json.dumps({"channel": "stdout", "data": data}) + "\n")
            try:
                return await self.fake(agent, prompt, cwd, session_id=identity, **options)
            except asyncio.CancelledError as error:
                error.native_execution = {"cleanup_confirmed": cleanup, "side_effects_possible": True,
                    "permission": options["permission"], "tool_calls": 1, "tool_results": 0,
                    "exit_code": -15, "receipt_path": str(receipt)}
                raise
        self.service.native_invoke = transport
        manager = PartnerManager(self.root / "partners", default_registry(), startup_discovery=False)
        get = manager.get
        manager.get = lambda provider: {**get(provider), "availability": {"state": "available", "reason": None}}
        manager.refresh_availability = AsyncMock()
        self.service.partner_manager = manager
        self.service._sync_partners()
        if runtime_members is None:
            created = await self.create()
        else:
            self.service._runtime_catalogs = {adapter: {"state": "ready", "models": [{"id": adapter + "-fixture", "efforts": ["high", "low"]}]}
                                              for adapter in ("claude", "codex")}
            created = await self.service.create_room("Update the existing value and preserve notes.", "claude", str(uuid4()),
                mode="native_project", project_id=self.registration["project"]["id"], participants=["claude", "codex"],
                work_mode="build", runtime_settings={"permission": "full_access", "members": runtime_members},
                execution_policy={"mode": "autonomous"})
        self.fixture_room = created["room"]["id"]
        await asyncio.wait_for(self.working.wait(), 3)
        return created["room"]["id"]

    async def attempted(self, request_id):
        """Read an operation after its background attempt has ended.

        The request returns after waiting 50 ms for that attempt. Cancelling
        an owned call, recording its cleanup and freezing its tree can take
        longer on a loaded runner, so the stored state may still be the first
        `pending` record. The bound exceeds the attempt's own 14 s wait for a
        cancelled worker. Nothing here restarts the attempt.
        """
        operation = self.service.store.partner_operation(request_id)
        task = self.service._partner_tasks.get(operation["id"])
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=20)
            self.assertTrue(done, "The partner operation attempt did not finish.")
        return self.service.store.partner_operation(request_id)

    def room_idle(self, room):
        return not (room in self.service._captures
                    or any(key == room and not worker.done() for (key, _), worker in self.service._workers.items())
                    or (room in self.service._executions and not self.service._executions[room].done()))

    async def wait_for(self, predicate):
        for _ in range(600):
            self.service._resume_partner_operations()
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.01)
        self.fail("Partner operation did not reach its expected state.")

    async def test_owned_cancel_preserves_d_then_fresh_readonly_transfer_import_self_check_and_natural_followup(self):
        choices = {provider: {"model": provider + "-fixture", "effort": "high"} for provider in ("claude", "codex")}
        room = await self.prepare_interrupted_owner(runtime_members=choices)
        # A constructed canonical ownership record exercises the manual-intent
        # grant; it must not be disguised as quota retirement.
        project = self.service._project(room)
        first = project.metadata_for_paths(["main.py"])[0]
        project.apply("claude", str(uuid4()), 1,
                      [{"path": "main.py", "previous_sha256": first["sha256"], "content": "VALUE = 1\n"}])
        self.canonical_revision_before = 2
        state = self.service.store.native_workspace(room, "claude")
        source = Path(state["project"])
        result = await self.service.set_partner_lifecycle("claude", "remove", request_id="remove-native-owner")
        await self.wait_for(lambda: self.service.store.partner_operation("remove-native-owner")["targets"][0]["state"] == "done")
        # Completion is stored before the last worker returns; until it does
        # the room is still busy and cannot take a follow-up.
        await self.wait_for(lambda: self.service.store.snapshot(room)["objective"]["status"] == "complete" and self.room_idle(room))
        final = await self.service.read_room(room)
        self.assertEqual(self.service.store.partner_operation("remove-native-owner")["status"], "completed")
        self.assertFalse(any(profile["id"] == "claude" for profile in self.service.partner_manager.profiles()))
        inspected = [call for call in self.transport_calls if call["context"].get("partner_handoff_inspection")]
        self.assertEqual(len(inspected), 1)
        self.assertEqual((inspected[0]["permission"], inspected[0]["session_id"]), ("read_only", None))
        self.assertEqual(final["team"]["tasks"][0]["owner"], "codex")
        self.assertEqual(final["team"]["tasks"][0]["status"], "self_checked")
        self.assertEqual(final["objective"]["retired_partners"], {})
        with self.service.store._connection() as db:
            grant = json.loads(db.execute("SELECT ownership_grant_json FROM shared_tasks WHERE room_id=? AND run_id=? AND id='T1'",
                                         (room, final["current_run"]["id"])).fetchone()[0])["main.py"]
        self.assertEqual((grant["old_owner"], grant["new_owner"], grant["sha256"], grant["revision"]),
                         ("claude", "codex", first["sha256"], 2))
        self.assertTrue(any(job["provider"] == "claude" and job["error"] == "partner_removed" and job["status"] == "unknown" for job in final["jobs"]))
        self.assertEqual((source / "main.py").read_text(), "VALUE = 2\n")
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        self.assertEqual(sum(call["agent"] == "claude" and call["context"]["job"]["job_phase"] == "work" for call in self.transport_calls), 1)
        source_member = next(member for member in final["members"] if member["id"] == "claude")
        self.assertTrue(source_member["identity_uncertain"], "The old failed identity is historical, not rewritten.")
        self.assertEqual((source_member["configuration_dispatchable"], source_member["configuration_dispatch_reason"],
                          source_member["configuration_handoff_completed"]), (False, "partner_removed", True))
        self.assertTrue(final["continuation"]["can_send"], final["continuation"])
        started, release = asyncio.Event(), asyncio.Event()
        original = self.fake.handler
        assessment_calls = 0
        async def followup(agent, context, cwd):
            nonlocal assessment_calls
            self.assertEqual(agent, "codex")
            self.assertFalse(next(member for member in context["members"] if member["id"] == "claude")["available"])
            if context["job"]["job_phase"] == "checkpoint":
                assessment_calls += 1
                started.set()
                await release.wait()
                if assessment_calls == 1:
                    return native.native_reply(context, [{"type": "checkpoint", "decision": "continue",
                        "summary": "A followup needs one further permitted work stage.",
                        "next_goal": "Clarify the existing result using the retained current files.",
                        "scope_reference": context["objective"]["original_goal"], "question": None}])
            value = original(agent, context, cwd)
            return await value if asyncio.iscoroutine(value) else value
        self.fake.handler = followup
        calls = len(self.transport_calls)
        try:
            response = await self.service.send_message(room, "Clarify the existing result without replaying old work.", "all",
                "followup-after-partner-removal", intent="continue", binding=final["continuation"]["binding"],
                runtime_settings={"permission": "full_access", "members": {**choices, "codex": {"model": "codex-fixture", "effort": "low"}}})
            await asyncio.wait_for(started.wait(), 3)
            self.assertNotEqual(response["objective"]["id"], final["objective"]["id"])
            self.assertEqual({call["agent"] for call in self.transport_calls[calls:]}, {"codex"})
            self.assertEqual(response["room"]["runtime_settings"]["members"]["claude"], choices["claude"])
            self.assertEqual(self.transport_calls[-1]["context"]["runtime_settings"]["members"]["codex"]["effort"], "low")
        finally:
            release.set()
        await self.wait_for(lambda: self.service.store.snapshot(room)["objective"]["status"] == "complete")
        self.assertTrue(any(call["context"]["job"]["job_phase"] == "work" for call in self.transport_calls[calls:]))
        self.assertTrue(next(member for member in self.service.store.snapshot(room)["members"] if member["id"] == "claude")["identity_uncertain"])

    async def test_fresh_inspection_cannot_reuse_a_receiver_historical_identity_or_import_d(self):
        room = await self.prepare_interrupted_owner()
        old = next(member for member in self.service.store.snapshot(room)["members"] if member["id"] == "codex")["session_id"]
        self.assertIsNotNone(old)
        invoke = self.service.native_invoke
        async def wrong_identity(agent, prompt, cwd, **options):
            result = await invoke(agent, prompt, cwd, **options)
            if json.loads(prompt.split("CONTEXT DATA:\n", 1)[1]).get("partner_handoff_inspection"):
                result["session_id"] = old
            return result
        self.service.native_invoke = wrong_identity
        await self.service.set_partner_lifecycle("claude", "remove", request_id="inspection-old-session")
        await self.wait_for(lambda: self.service.store.partner_operation("inspection-old-session")["targets"][0]["state"] == "blocked")
        operation = self.service.store.partner_operation("inspection-old-session")
        self.assertEqual((operation["status"], operation["targets"][0]["reason"]), ("completed", "session_identity_changed"))
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertEqual(self.service.store.snapshot(room)["team"]["tasks"][0]["owner"], "claude")

    async def test_pause_during_typed_inspection_retains_note_and_d_until_public_resume(self):
        room = await self.prepare_interrupted_owner()
        started, release = asyncio.Event(), asyncio.Event()
        original = self.fake.handler
        async def held(agent, context, cwd):
            if context.get("partner_handoff_inspection"):
                started.set()
                await release.wait()
            value = original(agent, context, cwd)
            return await value if asyncio.iscoroutine(value) else value
        self.fake.handler = held
        await self.service.set_partner_lifecycle("claude", "remove", request_id="pause-typed-inspection")
        await asyncio.wait_for(started.wait(), 3)
        before = self.service.store.snapshot(room)
        await self.service.objective_action(room, "pause", "pause-during-handoff", objective_id=before["objective"]["id"])
        release.set()
        await self.wait_for(lambda: self.service.store.partner_operation("pause-typed-inspection")["targets"][0]["state"] == "inspected")
        paused = await self.service.read_room(room)
        self.assertEqual(paused["objective"]["status"], "paused")
        self.assertEqual(paused["team"]["tasks"][0]["owner"], "claude")
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertTrue(any(job["partner_handoff_inspection"] for job in paused["jobs"]))
        self.assertEqual([entry["permission"] for entry in paused["native_executions"] if entry["job_id"] ==
            self.service.store.partner_operation("pause-typed-inspection")["targets"][0]["inspection_job_id"]], ["read_only"])
        await self.service.send_message(room, "Continue with the retained changes.", "all", "resume-typed-handoff",
                                        intent="continue", binding=paused["continuation"]["binding"])
        await self.wait_for(lambda: self.service.store.snapshot(room)["objective"]["status"] == "complete")
        self.assertEqual(self.service.store.snapshot(room)["team"]["tasks"][0]["owner"], "codex")

    async def test_unconfirmed_owned_cleanup_keeps_configuration_pending_without_transfer(self):
        room = await self.prepare_interrupted_owner(cleanup=False)
        await self.service.set_partner_lifecycle("claude", "remove", request_id="cleanup-unknown")
        operation = await self.attempted("cleanup-unknown")
        self.assertEqual((operation["status"], operation["error"]), ("pending", "partner_cleanup_unconfirmed"))
        self.assertTrue(any(profile["id"] == "claude" for profile in self.service.partner_manager.profiles()))
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertFalse(any(call["context"].get("partner_handoff_inspection") for call in self.transport_calls))

    async def test_cleanup_unknown_in_first_room_still_cancels_same_profile_in_every_owned_room(self):
        first = await self.prepare_interrupted_owner(cleanup=False)
        second = await self.prepare_interrupted_owner(cleanup=True)
        source_trees = [Path(self.service.store.native_workspace(room, "claude")["project"]) for room in (first, second)]
        owned_jobs = {room: {job["id"] for job in self.service.store.snapshot(room)["jobs"]
                             if job["provider"] == "claude" and job["status"] == "working"} for room in (first, second)}
        self.assertTrue(all(owned_jobs.values()))
        await self.service.set_partner_enabled("claude", False, request_id="cancel-owner-in-two-rooms")
        operation = await self.attempted("cancel-owner-in-two-rooms")
        self.assertEqual((operation["status"], operation["error"]), ("pending", "partner_cleanup_unconfirmed"))
        self.assertEqual(len(operation["targets"]), 2)
        for room, source in zip((first, second), source_trees):
            snapshot = self.service.store.snapshot(room)
            jobs = [job for job in snapshot["jobs"] if job["id"] in owned_jobs[room]]
            self.assertFalse(any(job["status"] in {"queued", "working"} for job in jobs), snapshot["jobs"])
            self.assertTrue(any(job["status"] == "unknown" and job["error"] == "partner_disabled" for job in jobs), jobs)
            self.assertEqual((source / "main.py").read_text(), "VALUE = 2\n")
            self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertTrue(self.service.partner_manager.get("claude")["enabled"], "Unknown cleanup must not be falsely completed.")

    async def test_explicit_enable_after_completed_handoff_uses_new_session_and_preserves_old_archive(self):
        room = await self.prepare_interrupted_owner()
        source = self.service.store.native_workspace(room, "claude")
        await self.service.set_partner_enabled("claude", False, request_id="disable-before-fresh-enable")
        await self.wait_for(lambda: self.service.store.snapshot(room)["objective"]["status"] == "complete")
        before = await self.service.read_room(room)
        old_jobs = {job["id"]: (job["status"], job["error"]) for job in before["jobs"]}
        with self.service.store._connection() as db:
            old_sessions = {row[0] for row in db.execute("SELECT session_id FROM native_session_history WHERE room_id=? AND provider='claude'", (room,))}
        await self.service.set_partner_enabled("claude", True, request_id="fresh-enable-after-handoff")
        member = next(member for member in self.service.store.snapshot(room)["members"] if member["id"] == "claude")
        self.assertEqual((member["session_id"], member["identity_uncertain"]), (None, False))
        # A fresh context must still reject any historical UUID before apply.
        with self.service.store._connection() as db:
            self.assertTrue(all(self.service.store._session_conflict(db, room, "claude", sid) for sid in old_sessions))
        calls = len(self.transport_calls)
        self.fake.handler = self.handler
        await self.service.send_message(room, "Clarify the current result together.", "all", "followup-with-reenabled-peer",
                                        intent="continue", binding=before["continuation"]["binding"])
        await self.wait_for(lambda: self.service.store.snapshot(room)["objective"]["status"] == "complete")
        new = [call for call in self.transport_calls[calls:] if call["agent"] == "claude"]
        self.assertTrue(new)
        self.assertIsNone(new[0]["session_id"])
        after = await self.service.read_room(room)
        self.assertEqual({job["id"]: (job["status"], job["error"]) for job in after["jobs"] if job["id"] in old_jobs}, old_jobs)
        self.assertEqual((Path(source["project"]) / "main.py").read_text(), "VALUE = 2\n")
        self.assertNotEqual(self.service.store.native_workspace(room, "claude")["project"], source["project"])

    async def test_pause_allows_configuration_removal_but_drift_blocks_later_handoff_without_import(self):
        room = await self.prepare_interrupted_owner()
        before = self.service.store.snapshot(room)
        await self.service.objective_action(room, "pause", "pause-before-removal", objective_id=before["objective"]["id"])
        await self.service.set_partner_lifecycle("claude", "remove", request_id="paused-native-removal")
        operation = await self.attempted("paused-native-removal")
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(self.service.store.snapshot(room)["objective"]["status"], "paused")
        self.assertFalse(any(call["context"].get("partner_handoff_inspection") for call in self.transport_calls))
        source = self.service.store.native_workspace(room, "claude")
        (Path(source["project"]) / "main.py").write_text("VALUE = 999\n")
        await self.service.objective_action(room, "resume", "resume-after-drift", objective_id=before["objective"]["id"])
        await self.wait_for(lambda: self.service.store.partner_operation("paused-native-removal")["targets"][0]["state"] == "blocked")
        target = self.service.store.partner_operation("paused-native-removal")["targets"][0]
        self.assertEqual(target["reason"], "native_recovery_unpublished_changes")
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertFalse(any(call["context"].get("partner_handoff_inspection") for call in self.transport_calls))

    async def test_source_outside_authorized_paths_is_preserved_but_cannot_be_transferred(self):
        room = await self.prepare_interrupted_owner()
        source = Path(self.service.store.native_workspace(room, "claude")["project"])
        (source / "outside.txt").write_text("Unowned private bytes.\n")
        await self.service.set_partner_lifecycle("claude", "remove", request_id="unowned-private")
        operation = await self.attempted("unowned-private")
        self.assertEqual((operation["status"], operation["error"]), ("completed", None))
        self.assertEqual((operation["targets"][0]["state"], operation["targets"][0]["reason"]), ("blocked", "native_changes_not_imported"))
        self.assertFalse(any(profile["id"] == "claude" for profile in self.service.partner_manager.profiles()))
        self.assertEqual((source / "outside.txt").read_text(), "Unowned private bytes.\n")
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertFalse(any(call["context"].get("partner_handoff_inspection") for call in self.transport_calls))

    async def test_receiver_unpublished_tree_is_not_overwritten_by_retained_source(self):
        room = await self.prepare_interrupted_owner()
        state = self.service.store.native_workspace(room, "codex")
        receiver = Path(state["project"])
        (receiver / "notes.md").write_text("Other private receiver work.\n")
        await self.service.set_partner_lifecycle("claude", "remove", request_id="receiver-dirty")
        target = (await self.attempted("receiver-dirty"))["targets"][0]
        self.assertEqual((target["state"], target["reason"]), ("blocked", "native_recovery_unpublished_changes"))
        self.assertEqual(self.service.store.native_workspace(room, "codex")["project"], str(receiver))
        self.assertEqual((receiver / "notes.md").read_text(), "Other private receiver work.\n")
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        self.assertFalse(any(call["context"].get("partner_handoff_inspection") for call in self.transport_calls))

    async def test_receiver_quota_blocks_handoff_only_and_fresh_availability_continues_automatically(self):
        room = await self.prepare_interrupted_owner()
        with patch.object(self.service, "_guard_objective_availability", side_effect=RoomError("all_partners_quota_exhausted")):
            await self.service.set_partner_enabled("claude", False, request_id="waiting-for-receiver-quota")
            # The refusal must still be in place while the attempt runs.
            operation = await self.attempted("waiting-for-receiver-quota")
        self.assertEqual((operation["status"], operation["targets"][0]["state"], operation["targets"][0]["reason"]),
                         ("completed", "waiting_receiver", "no_available_partner"))
        self.assertEqual(self.service._project(room).snapshot()["revision"], 1)
        await self.wait_for(lambda: self.service.store.snapshot(room)["objective"]["status"] == "complete")
        self.assertEqual(self.service.store.partner_operation("waiting-for-receiver-quota")["targets"][0]["state"], "done")

    async def test_active_fresh_quota_rejoins_without_user_continue_but_disable_and_pause_do_not(self):
        room = await self.prepare_interrupted_owner()
        snapshot = self.service.store.snapshot(room)
        before = snapshot["team"]["tasks"][0]
        self.service.store.retire_quota(room, "codex", "agent_quota_exhausted")
        self.assertIn("codex", self.service.store.snapshot(room)["objective"]["retired_partners"])
        calls = len(self.transport_calls)
        await self.service._refresh_active_quota_members(room)
        current = self.service.store.snapshot(room)
        self.assertNotIn("codex", current["objective"]["retired_partners"])
        self.assertEqual((current["team"]["tasks"][0]["owner"], current["team"]["tasks"][0]["assignment_epoch"]),
                         (before["owner"], before["assignment_epoch"]))
        self.assertEqual(len(self.transport_calls), calls)
        self.service.store.retire_quota(room, "codex", "agent_quota_exhausted")
        await self.service.set_partner_enabled("codex", False, request_id="disable-retired")
        self.assertEqual((await self.attempted("disable-retired"))["status"], "completed")
        await self.service._refresh_active_quota_members(room)
        self.assertIn("codex", self.service.store.snapshot(room)["objective"]["retired_partners"])
        await self.service.objective_action(room, "pause", "pause-quota-rejoin", objective_id=snapshot["objective"]["id"])
        await self.service.set_partner_enabled("codex", True, request_id="enable-without-resume")
        # Only a profile that is enabled again could rejoin; check that first.
        self.assertEqual((await self.attempted("enable-without-resume"))["status"], "completed")
        await self.service._refresh_active_quota_members(room)
        current = self.service.store.snapshot(room)
        self.assertEqual(current["objective"]["status"], "paused")
        self.assertIn("codex", current["objective"]["retired_partners"])


if __name__ == "__main__":
    unittest.main()
