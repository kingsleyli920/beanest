"""One-action lifecycle with real SQLite/owned files and synthetic providers."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import quote

from local_agent_coordinator.agent_registry import AgentRegistry
from local_agent_coordinator import room_purge
from local_agent_coordinator.native_session_purge import purge_native_sessions
from local_agent_coordinator.room_purge import purge_room_files, RoomPurgeError
from local_agent_coordinator.room_service import RoomService, RoomError, _project_digest


class RoomLifecycleOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.calls = []
        self.service = self.new_service()

    def new_service(self):
        async def invoke(agent, prompt, cwd, **options):
            self.calls.append(agent)
            return {"session_id": options["session_id"] or str(uuid4()), "status": "completed", "events": [],
                    "text": json.dumps({"message": "Finished.", "state": "done", "actions": []})}
        service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=invoke)
        # This synthetic transport creates no native application's sessions.
        service._native_session_cleanup_targets = lambda room_id: []
        return service

    async def asyncTearDown(self):
        await self.service.shutdown()
        if self.service._cleanup_task:
            await self.service._cleanup_task
        self.temp.cleanup()

    async def completed(self, request_id=None):
        created = await self.service.create_room("Keep this task's local history.", "claude", request_id or str(uuid4()))
        await asyncio.gather(*list(self.service._workers.values()))
        return created["room"]["id"]

    def native_session_fixture(self):
        """Real provider metadata in temporary homes; official command is a stub."""
        codex_home, claude_home = self.root / ".codex", self.root / ".claude"
        codex_home.mkdir(exist_ok=True)
        claude_home.mkdir(exist_ok=True)
        calls, targets_seen, failed = [], [], [False]
        def project_for(cwd):
            return claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        def session(cwd, sid):
            project = project_for(cwd)
            project.mkdir(parents=True, exist_ok=True)
            path = project / (sid + ".jsonl")
            path.write_text(json.dumps({"sessionId": sid, "cwd": str(cwd), "isSidechain": False,
                "timestamp": datetime.now(timezone.utc).isoformat(), "message": "Private synthetic session fixture."}) + "\n")
            return path
        def official(argv, environment):
            calls.append((argv, environment))
            self.assertEqual(argv[:3], ["/stub/claude", "purge", "--yes"])
            self.assertEqual(environment, {"CLAUDE_CONFIG_DIR": str(claude_home)})
            if failed[0]:
                return {"exit_code": 1, "cleanup_confirmed": True}
            shutil.rmtree(project_for(argv[-1]))
            return {"exit_code": 0, "cleanup_confirmed": True}
        def cleanup(runtime, workspace, room, targets, **unused):
            targets_seen.append(json.loads(json.dumps(targets)))
            self.assertTrue(self.service.store.snapshot(room)["room"])
            return purge_native_sessions(runtime, workspace, room, targets, codex_home=codex_home,
                claude_home=claude_home, codex_cli="/stub/codex", claude_cli="/stub/claude", run_command=official)
        return session, cleanup, calls, targets_seen, failed

    async def settled_operation(self, request_id):
        async def wait():
            while True:
                result = await self.service.read_lifecycle_operation(request_id=request_id)
                if result["lifecycle_operation"]["status"] != "pending":
                    return result
                tasks = [task for task in self.service._lifecycle_tasks.values() if not task.done()]
                await asyncio.gather(*tasks)
        return await asyncio.wait_for(wait(), 5)

    async def test_one_archive_request_hides_then_cancels_owned_work_without_user_stop(self):
        started = asyncio.Event()
        async def held(*args, **options):
            started.set()
            await asyncio.Event().wait()
        self.service.invoke = held
        created = await self.service.create_room("A task still running.", "claude", "held-create")
        await started.wait()
        room = created["room"]["id"]
        accepted = await self.service.set_room_lifecycle(room, "archive", "held-archive")
        self.assertEqual(accepted["lifecycle_operation"]["status"], "pending")
        self.assertEqual(self.service.store.snapshot(room)["room"]["lifecycle_state"], "archived")
        result = await self.settled_operation("held-archive")
        self.assertEqual(result["lifecycle_operation"]["status"], "completed")
        self.assertFalse(result["deleted"])
        self.assertTrue(all(task.done() for task in self.service._workers.values()))
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in self.service.store.snapshot(room)["jobs"]))

    async def test_permanent_delete_removes_owned_data_keeps_neighbor_and_blocks_old_create_replay(self):
        room = await self.completed("original-create")
        neighbor = await self.completed("neighbor-create")
        before = self.service.store.snapshot(neighbor)
        for bucket in ("native-receipts", "folder-artifacts", "native-workspaces"):
            directory = self.root / bucket / room
            directory.mkdir(parents=True)
            (directory / "private.txt").write_text("This belongs only to the deleted room.")
        original = self.root / "original-project"
        original.mkdir()
        (original / "source.txt").write_text("Must remain.")
        accepted = await self.service.set_room_lifecycle(room, "remove", "delete-room")
        self.assertFalse(accepted["deleted"])
        result = await self.settled_operation("delete-room")
        self.assertTrue(result["deleted"], result)
        for bucket in ("native-receipts", "folder-artifacts", "native-workspaces", "workspaces"):
            self.assertFalse((self.root / bucket / room).exists())
        self.assertEqual(self.service.store.snapshot(neighbor), before)
        self.assertEqual((original / "source.txt").read_text(), "Must remain.")
        with self.service.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT count(*) FROM rooms WHERE id=?", (room,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE room_id=?", (room,)).fetchone()[0], 0)
        self.assertEqual(await self.service.set_room_lifecycle(room, "remove", "delete-room"), result)
        with self.assertRaisesRegex(RoomError, "room_deleted"):
            await self.service.create_room("Keep this task's local history.", "claude", "original-create")
        with self.assertRaisesRegex(RoomError, "request_conflict"):
            await self.service.create_room("Changed payload.", "claude", "original-create")

    async def test_historical_cancelled_job_without_old_cleanup_receipt_does_not_permanently_lock_actions(self):
        room, _ = self.service.store.create_room("Old cancelled demonstration.", "claude", "legacy-create")
        job = self.service.store.claim(room, "claude")
        self.service.store.finish_stop(room, confirmed=True)
        self.assertEqual(self.service.store.lifecycle_reason(room), "team_cleanup_unconfirmed")
        before = self.service.store.snapshot(room)
        await self.service.set_room_lifecycle(room, "archive", "legacy-archive")
        self.assertEqual((await self.settled_operation("legacy-archive"))["lifecycle_operation"]["status"], "completed")
        after = self.service.store.snapshot(room)
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertFalse(any(event["kind"] == "job_cleanup" for event in after["events"]))
        self.assertEqual(after["jobs"][0]["id"], job["id"])

    async def test_explicit_cleanup_failure_accepts_intent_but_never_deletes_or_claims_success(self):
        room = await self.completed()
        job = self.service.store.snapshot(room)["jobs"][0]
        self.service.store.record_job_cleanup(job["id"], confirmed=False)
        with patch("local_agent_coordinator.room_purge.purge_room_files") as purge:
            accepted = await self.service.set_room_lifecycle(room, "remove", "unclean-delete")
            self.assertEqual(accepted["lifecycle_operation"]["status"], "pending")
            result = await self.settled_operation("unclean-delete")
        self.assertFalse(result["deleted"])
        self.assertEqual(result["lifecycle_operation"]["error"], "team_cleanup_unconfirmed")
        self.assertEqual(result["lifecycle_operation"]["status"], "failed")
        purge.assert_not_called()
        self.assertTrue(self.service.store.snapshot(room)["jobs"])
        self.assertEqual(self.service.store.snapshot(room)["room"]["lifecycle_state"], "active")
        self.assertEqual(next(item["lifecycle_state"] for item in (await self.service.list_rooms())["rooms"] if item["id"] == room), "active")
        await self.service.shutdown()
        self.service = self.new_service()
        self.assertEqual(next(item["lifecycle_state"] for item in (await self.service.list_rooms())["rooms"] if item["id"] == room), "active")
        self.assertEqual((await self.service.read_lifecycle_operation(request_id="unclean-delete"))["lifecycle_operation"]["status"], "failed")

    async def test_incoming_foreign_room_reference_rejects_before_files_are_touched(self):
        room, neighbor = await self.completed(), await self.completed()
        source_job = self.service.store.snapshot(room)["jobs"][0]["id"]
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET parent_id=? WHERE room_id=?", (source_job, neighbor))
        with patch("local_agent_coordinator.room_purge.purge_room_files") as purge:
            await self.service.set_room_lifecycle(room, "remove", "foreign-delete")
            result = await self.settled_operation("foreign-delete")
        self.assertEqual(result["lifecycle_operation"]["error"], "purge_reference_conflict")
        purge.assert_not_called()
        self.assertTrue(self.service.store.snapshot(room)["jobs"])
        self.assertEqual(self.service.store.snapshot(neighbor)["jobs"][0]["parent_id"], source_job)

    async def test_pending_delete_after_files_were_removed_resumes_after_restart_without_model_calls(self):
        room = await self.completed()
        operation = self.service.store.request_room_lifecycle(room, "remove", "crash-delete")
        self.service.store.mark_lifecycle_destructive_started(operation["id"])
        purge_room_files(self.root, self.root / "workspaces", room)
        calls = len(self.calls)
        await self.service.shutdown()
        self.service = self.new_service()
        result = await self.settled_operation("crash-delete")
        self.assertTrue(result["deleted"], result)
        self.assertEqual(len(self.calls), calls)

    async def test_dedicated_sessions_are_officially_deleted_before_runtime_and_same_journal_resumes_failure(self):
        room = await self.completed()
        self.service._native_session_cleanup_targets = RoomService._native_session_cleanup_targets.__get__(self.service)
        session, cleanup, commands, targets_seen, failed = self.native_session_fixture()
        record = self.service.store.native_session_cleanup_records(room)[0]
        native_path = session(self.root / "workspaces" / room / "claude", record["session_id"])
        failed[0] = True
        calls = len(self.calls)
        with patch("local_agent_coordinator.native_session_purge.purge_native_sessions", side_effect=cleanup):
            accepted = await self.service.set_room_lifecycle(room, "remove", "with-session-delete")
            await asyncio.gather(*list(self.service._lifecycle_tasks.values()))
            result = await self.service.read_lifecycle_operation(request_id="with-session-delete")
            self.assertFalse(result["deleted"])
            self.assertFalse(result["lifecycle_operation"]["room_data_deleted"])
            self.assertTrue((self.root / "workspaces" / room).exists())
            self.assertTrue(native_path.exists())
            self.assertEqual(result["lifecycle_operation"]["native_sessions_pending"], 1)
            self.assertEqual(result["lifecycle_operation"]["status"], "pending")
            self.assertEqual(result["lifecycle_operation"]["error"], "native_sessions_pending")
            with self.service.store._connection() as db:
                frozen = db.execute("SELECT native_sessions_json FROM room_lifecycle_operations WHERE id=?",
                                    (accepted["lifecycle_operation"]["id"],)).fetchone()[0]
            await self.service.shutdown()
            self.service = self.new_service()
            failed[0] = False
            done = await self.settled_operation("with-session-delete")
        self.assertTrue(done["deleted"], done)
        self.assertEqual(done["lifecycle_operation"]["id"], accepted["lifecycle_operation"]["id"])
        self.assertEqual(done["lifecycle_operation"]["native_sessions_pending"], 0)
        self.assertFalse(native_path.exists())
        self.assertFalse((self.root / "workspaces" / room).exists())
        self.assertEqual(len(commands), 2)
        self.assertEqual(targets_seen[0], targets_seen[1])
        with self.service.store._connection() as db:
            self.assertEqual(db.execute("SELECT native_sessions_json FROM room_lifecycle_operations WHERE id=?",
                                       (accepted["lifecycle_operation"]["id"],)).fetchone()[0], frozen)
        self.assertEqual(len(self.calls), calls)

    async def native_cancel_fixture(self, *, valid_init):
        self.service._native_session_cleanup_targets = RoomService._native_session_cleanup_targets.__get__(self.service)
        session, cleanup, commands, targets_seen, _ = self.native_session_fixture()
        started, calls, sid = asyncio.Event(), [], str(uuid4())
        async def held(agent, prompt, cwd, **options):
            calls.append(agent)
            directory = options["receipt_dir"]
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            receipt = directory / (str(uuid4()) + ".jsonl")
            data = {"type": "system", "subtype": "init", "session_id": sid} if valid_init else {"type": "turn.started"}
            receipt.write_text(json.dumps({"channel": "stdout", "data": data}) + "\n")
            session(cwd, sid)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                error.native_execution = {"permission": options["permission"], "tool_calls": 0, "tool_results": 0,
                    "side_effects_possible": False, "cleanup_confirmed": True, "exit_code": -15, "receipt_path": str(receipt)}
                raise
        self.service.native_invoke = held
        source = self.root / "source"
        source.mkdir()
        (source / "main.py").write_text("VALUE = 1\n")
        registration = await self.service.register_project("Source", str(source), selection_kind="folder")
        created = await self.service.create_room("Preserve this authorized goal.", "claude", "native-cancel-create",
            mode="native_project", project_id=registration["project"]["id"], participants=["claude"], work_mode="build",
            runtime_settings={"permission": "full_access", "members": {}}, execution_policy={"mode": "autonomous"})
        await started.wait()
        room = created["room"]["id"]
        snapshot = self.service.store.snapshot(room)
        self.assertEqual(self.service.store.native_session_cleanup_records(room), [])
        self.assertIsNone(snapshot["members"][0]["session_id"])
        self.assertEqual(snapshot["native_executions"], [])
        return room, sid, cleanup, commands, targets_seen, calls

    async def test_cancelled_fresh_native_call_retains_exact_receipt_identity_for_official_deletion(self):
        room, sid, cleanup, commands, targets_seen, calls = await self.native_cancel_fixture(valid_init=True)
        self.assertTrue((await self.service.status())["capabilities"]["room_native_session_cleanup"])
        with patch("local_agent_coordinator.native_session_purge.purge_native_sessions", side_effect=cleanup):
            await self.service.set_room_lifecycle(room, "remove", "cancel-native-delete")
            result = await self.settled_operation("cancel-native-delete")
        self.assertTrue(result["deleted"], result)
        self.assertEqual([(item["adapter"], item["session_id"]) for item in targets_seen[0]], [("claude", sid)])
        self.assertEqual(calls, ["claude"])
        self.assertEqual(len(commands), 1)
        self.assertEqual((self.root / "source" / "main.py").read_text(), "VALUE = 1\n")

    async def test_cancelled_native_without_primary_receipt_identity_never_claims_full_deletion(self):
        room, _, _, commands, _, calls = await self.native_cancel_fixture(valid_init=False)
        with patch("local_agent_coordinator.native_session_purge.purge_native_sessions") as official, \
                patch("local_agent_coordinator.room_purge.purge_room_files") as files:
            await self.service.set_room_lifecycle(room, "remove", "missing-init-delete")
            result = await self.settled_operation("missing-init-delete")
        self.assertFalse(result["deleted"])
        self.assertEqual(result["lifecycle_operation"]["error"], "native_session_identity_unconfirmed")
        self.assertFalse(result["lifecycle_operation"]["room_data_deleted"])
        self.assertTrue(self.service.store.snapshot(room)["jobs"])
        self.assertEqual(self.service.store.native_session_cleanup_records(room), [])
        official.assert_not_called()
        files.assert_not_called()
        self.assertEqual(commands, [])
        self.assertEqual(calls, ["claude"])

    async def test_delete_during_actual_auth_preflight_cleans_status_child_without_needing_new_session(self):
        self.service._native_session_cleanup_targets = RoomService._native_session_cleanup_targets.__get__(self.service)
        marker, inference = self.root / "auth.pid", self.root / "inference.started"
        stub = self.root / "claude-stub"
        stub.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
if 'status' in sys.argv:
    Path({str(marker)!r}).write_text(str(os.getpid()))
    time.sleep(60)
else:
    Path({str(inference)!r}).write_text('unexpected inference')
''')
        stub.chmod(0o700)
        source = self.root / "source"
        source.mkdir()
        (source / "main.py").write_text("VALUE = 1\n")
        registration = await self.service.register_project("Source", str(source), selection_kind="folder")
        with patch("local_agent_coordinator.adapters.find_claude", return_value=str(stub)), \
                patch("local_agent_coordinator.native_session_purge.purge_native_sessions") as official:
            created = await self.service.create_room("Preserve this goal.", "claude", "auth-preflight-create",
                mode="native_project", project_id=registration["project"]["id"], participants=["claude"],
                work_mode="build", execution_policy={"mode": "autonomous"})
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(marker.exists())
            room = created["room"]["id"]
            self.assertEqual(self.service.store.native_session_cleanup_records(room), [])
            await self.service.set_room_lifecycle(room, "remove", "auth-preflight-delete")
            result = await self.settled_operation("auth-preflight-delete")
        self.assertTrue(result["deleted"], result)
        self.assertFalse(inference.exists())
        official.assert_not_called()
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
        self.assertEqual((source / "main.py").read_text(), "VALUE = 1\n")

    async def test_native_preparation_failure_has_host_never_started_proof_and_does_not_lock_deletion(self):
        from local_agent_coordinator.native_workspace import NativeWorkspaceError
        self.service._native_session_cleanup_targets = RoomService._native_session_cleanup_targets.__get__(self.service)
        source = self.root / "source"
        source.mkdir()
        (source / "main.py").write_text("VALUE = 1\n")
        registration = await self.service.register_project("Source", str(source), selection_kind="folder")
        with patch.object(self.service, "_prepare_native_workspace", side_effect=NativeWorkspaceError("native_preparation_cancelled")), \
                patch.object(self.service, "native_invoke") as native:
            created = await self.service.create_room("Preserve this goal.", "claude", "preparation-failure-create",
                mode="native_project", project_id=registration["project"]["id"], participants=["claude"],
                work_mode="build", execution_policy={"mode": "autonomous"})
            await asyncio.gather(*list(self.service._workers.values()))
        room = created["room"]["id"]
        observation = self.service.store.snapshot(room)["native_executions"][0]
        self.assertTrue(observation["never_started"])
        self.assertTrue(observation["cleanup_confirmed"])
        self.assertEqual(self.service.store.native_session_cleanup_records(room), [])
        native.assert_not_called()
        await self.service.set_room_lifecycle(room, "remove", "preparation-failure-delete")
        self.assertTrue((await self.settled_operation("preparation-failure-delete"))["deleted"])

    async def test_cancelled_file_phase_waits_for_its_thread_then_the_same_journal_resumes(self):
        room = await self.completed()
        entered, release = threading.Event(), threading.Event()
        def held(*args, **kwargs):
            entered.set()
            release.wait(5)
            return purge_room_files(*args, **kwargs)
        with patch("local_agent_coordinator.room_purge.purge_room_files", side_effect=held):
            accepted = await self.service.set_room_lifecycle(room, "remove", "cancel-file-phase")
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task = self.service._lifecycle_tasks[accepted["lifecycle_operation"]["id"]]
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        result = await self.settled_operation("cancel-file-phase")
        self.assertTrue(result["deleted"], result)

    async def test_partial_file_failure_stays_pending_and_same_journal_finishes_after_restart(self):
        room, neighbor = await self.completed(), await self.completed()
        neighbor_before = self.service.store.snapshot(neighbor)
        first = self.root / "project-artifacts" / room
        second = self.root / "folder-artifacts" / room
        for directory in (first, second):
            directory.mkdir(parents=True)
            (directory / "private.txt").write_text("Only this room's owned bytes.")
        calls = len(self.calls)
        erase = room_purge._erase
        room_deletions = []
        def fail_after_first_bucket(parent, name, device):
            if name == room:
                room_deletions.append(name)
                if len(room_deletions) == 2:
                    raise OSError("Injected IO failure after the first owned bucket.")
            return erase(parent, name, device)
        with patch("local_agent_coordinator.room_purge._erase", side_effect=fail_after_first_bucket):
            accepted = await self.service.set_room_lifecycle(room, "remove", "failed-files")
            await asyncio.gather(*list(self.service._lifecycle_tasks.values()))
            result = await self.service.read_lifecycle_operation(request_id="failed-files")
            self.assertEqual(await self.service.set_room_lifecycle(room, "remove", "failed-files"), result)
            with self.assertRaisesRegex(RoomError, "room_archived"):
                await self.service.send_message(room, "This must not dispatch.", "claude", "during-delete")
        self.assertFalse(result["deleted"])
        self.assertFalse(result["lifecycle_operation"]["room_data_deleted"])
        self.assertEqual(result["lifecycle_operation"]["status"], "pending")
        self.assertEqual(result["lifecycle_operation"]["error"], "purge_files_incomplete")
        self.assertFalse(first.exists())
        self.assertTrue((second / "private.txt").is_file())
        self.assertEqual(self.service.store.snapshot(room)["room"]["lifecycle_state"], "archived")
        with self.service.store._connection() as db:
            self.assertEqual(db.execute("SELECT destructive_started FROM room_lifecycle_operations WHERE id=?",
                                       (accepted["lifecycle_operation"]["id"],)).fetchone()[0], 1)
        self.assertEqual(self.service.store.snapshot(neighbor), neighbor_before)
        self.assertEqual(len(self.service.store.lifecycle_operations(room_id=room)), 1)
        await self.service.shutdown()
        self.service = self.new_service()
        self.assertEqual(self.service.store.snapshot(room)["room"]["lifecycle_state"], "archived")
        self.assertEqual(self.service.store.snapshot(neighbor), neighbor_before)
        completed = await self.settled_operation("failed-files")
        self.assertTrue(completed["deleted"], completed)
        self.assertEqual(completed["lifecycle_operation"]["id"], accepted["lifecycle_operation"]["id"])
        self.assertEqual(len(self.service.store.lifecycle_operations(room_id=room)), 1)
        self.assertFalse(second.exists())
        self.assertEqual(self.service.store.snapshot(neighbor), neighbor_before)
        self.assertEqual(len(self.calls), calls)

    async def test_legacy_lifecycle_actions_cannot_reactivate_room_during_pending_file_deletion(self):
        room = await self.completed()
        jobs = self.service.store.snapshot(room)["jobs"]
        calls = len(self.calls)
        entered, release = threading.Event(), threading.Event()
        def held(*args, **kwargs):
            entered.set()
            release.wait(5)
            return purge_room_files(*args, **kwargs)
        with patch("local_agent_coordinator.room_purge.purge_room_files", side_effect=held):
            await self.service.set_room_lifecycle(room, "remove", "held-delete")
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                for action in ("restore", "archive", "remove"):
                    with self.assertRaisesRegex(RoomError, "room_lifecycle_busy"):
                        # The store transaction is authoritative even if a legacy
                        # request passed the service's earlier fast check.
                        self.service.store.set_lifecycle(room, action)
                    with self.assertRaisesRegex(RoomError, "room_lifecycle_busy"):
                        await asyncio.wait_for(self.service.set_room_lifecycle(room, action), 0.5)
                with self.assertRaisesRegex(RoomError, "room_archived"):
                    self.service.store.send_message(room, "Must remain inactive.", "claude", "held-send")
                self.assertEqual(self.service.store.snapshot(room)["jobs"], jobs)
                self.assertEqual(self.service.store.snapshot(room)["room"]["lifecycle_state"], "archived")
            finally:
                release.set()
                await asyncio.gather(*list(self.service._lifecycle_tasks.values()))
        self.assertTrue((await self.settled_operation("held-delete"))["deleted"])
        self.assertEqual(len(self.calls), calls)

    async def test_explicit_empty_partner_catalog_can_reopen_and_read_history_without_restoring_profiles(self):
        room = await self.completed()
        before = self.service.store.snapshot(room)
        await self.service.shutdown()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   registry=AgentRegistry([], allow_empty=True))
        self.assertEqual(self.service.registry.ids(), ())
        self.assertEqual(self.service.store.agents, {})
        after = await self.service.read_room(room)
        expected_members = [dict(member, configuration_dispatchable=False,
                                 configuration_dispatch_reason="partner_unavailable")
                            for member in before["members"]]
        self.assertEqual(after["members"], expected_members)
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(after["events"], before["events"])

    async def test_child_first_provider_uses_owned_exact_exports_after_source_chat_is_deleted(self):
        source = self.root / "registered-source"
        source.mkdir()
        (source / "main.py").write_text("VALUE = 1\n")
        registered = await self.service.register_project("Fixture", str(source), selection_kind="folder")
        project_id = registered["project"]["id"]
        # Prepare complete private snapshots and queued boards, never call a CLI.
        old = await self.service._create_room("The original goal.", "claude", "source-folder", mode="project_folder",
            project_id=project_id, participants=["claude", "codex"], execution_policy={"mode": "autonomous"}, defer_start=True)
        old_id = old["room"]["id"]
        project = self.service._project(old_id)
        previous_sha = project.metadata_for_paths(["main.py"])[0]["sha256"]
        project.apply("claude", "old-change", 1, [{"path": "main.py", "previous_sha256": previous_sha, "content": "VALUE = 2\n"}])
        snapshot = project.snapshot()
        child = await self.service._create_room("The original goal.", "claude", "native-child", mode="native_project",
            project_id=project_id, participants=["claude", "codex"], execution_policy={"mode": "autonomous"},
            runtime_settings={"permission": "full_access", "members": {}}, defer_start=True)
        child_id = child["room"]["id"]
        with self.service.store._connection(write=True) as db:
            db.execute("INSERT INTO native_handoffs VALUES (?,?,?,?,?)", (child_id, old_id, old["current_run"]["id"], 2, _project_digest(snapshot)))
        await asyncio.to_thread(self.service._retain_handoff_dependencies, old_id)
        retained = self.service.store.handoff_exports(child_id)
        retained_path = Path(retained["previous"]["directory"]) / "main.py"
        exact = retained_path.read_bytes()
        retained_path.write_text("Corrupted dependency export.\n")
        await self.service.set_room_lifecycle(old_id, "remove", "corrupt-source-delete")
        denied = await self.settled_operation("corrupt-source-delete")
        self.assertEqual(denied["lifecycle_operation"]["error"], "purge_dependency_unconfirmed")
        self.assertTrue(self.service.store.snapshot(old_id)["room"])
        retained_path.write_bytes(exact)
        await self.service.set_room_lifecycle(old_id, "remove", "source-delete")
        self.assertTrue((await self.settled_operation("source-delete"))["deleted"])
        self.assertIsNotNone(self.service.store.handoff_exports(child_id))
        job = self.service.store.claim(child_id, "codex")
        self.service._prompt(job)
        original_project = self.service._project
        def no_source(room_id):
            self.assertNotEqual(room_id, old_id)
            return original_project(room_id)
        with patch.object(self.service, "_project", side_effect=no_source):
            prepared = await self.service._native_workspace_for_job(job)
        self.assertEqual((Path(prepared["project"]) / "main.py").read_text(), "VALUE = 2\n")
        self.assertEqual((source / "main.py").read_text(), "VALUE = 1\n")
        self.assertFalse(self.service.store.snapshot(child_id)["native_handoff_source_available"])
        self.service.store.finish_job(job["id"], error="fixture_finished")
        self.service.store.record_job_cleanup(job["id"], confirmed=True)


class RoomLifecycleHTTPTests(unittest.TestCase):
    from test_team_runs_http import TeamRunHTTPTests as _Fixtures
    setUp = _Fixtures.setUp
    tearDown = _Fixtures.tearDown
    request = _Fixtures.request

    def test_explicit_permanent_delete_is_user_only_idempotent_and_queryable_even_after_the_room_is_gone(self):
        async def invoke(agent, prompt, cwd, **options):
            return {"session_id": options["session_id"] or str(uuid4()), "status": "completed", "events": [],
                    "text": json.dumps({"message": "Finished.", "state": "done", "actions": []})}
        self.service.invoke = invoke
        self.service._native_session_cleanup_targets = lambda room_id: []
        payload = {"goal": "A disposable local chat.", "target": "claude", "request_id": "lifecycle-http-create"}
        code, created = self.request("POST", "/api/rooms", payload)
        self.assertEqual(code, 200)
        room = created["room"]["id"]
        request = {"action": "remove", "request_id": "lifecycle-http-delete"}
        path = f"/api/rooms/{room}/lifecycle"
        lookup = "/api/room-lifecycle-operations?request_id=" + quote(request["request_id"])
        self.assertEqual(self.request("GET", lookup)[0], 404)
        self.assertEqual(self.request("POST", f"/api/mcp/rooms/{room}/lifecycle", request, mcp=True)[0], 403)
        self.assertEqual(self.request("POST", path, {**request, "scope": "all"})[0], 400)
        code, accepted = self.request("POST", path, request)
        self.assertEqual(code, 200, accepted)
        identifier = accepted["lifecycle_operation"]["id"]
        until = time.monotonic() + 5
        while time.monotonic() < until:
            code, done = self.request("GET", lookup)
            self.assertEqual(code, 200)
            if done["lifecycle_operation"]["status"] != "pending":
                break
            time.sleep(0.005)
        self.assertTrue(done["deleted"], done)
        self.assertEqual(self.request("GET", "/api/room-lifecycle-operations/" + identifier), (200, done))
        self.assertEqual(self.request("POST", path, request), (200, done))
        self.assertEqual(self.request("POST", path, {**request, "action": "restore"})[0], 409)
        self.assertEqual(self.request("GET", f"/api/rooms/{room}")[0], 404)
        self.assertEqual(self.request("POST", "/api/rooms", payload)[0], 410)
