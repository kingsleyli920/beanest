"""Last-resort recovery: a proven-dead unconfirmed call never leaves a dead end."""
import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path
import unittest

import test_native_room_service as native
from test_native_room_service import native_reply


# (phase, failure code, native status) pairs that previously stopped a room
# with every action refused: no specific recovery candidate accepted them.
UNCONFIRMED = [
    ("work", "native_no_progress", "failed"),
    ("work", "unknown_provider_error", "failed"),
    ("work", "native_tool_result_missing", "failed"),
    ("work", "deadline_exceeded", "timeout"),
    ("plan", "cli_nonzero_exit", "failed"),
    ("review", "cli_nonzero_exit", "failed"),
    ("checkpoint", "deadline_exceeded", "timeout"),
]


class UnconfirmedRestartTests(unittest.IsolatedAsyncioTestCase):
    # Borrow the fixture only; inheriting would rerun every native room test.
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create

    def fail_once(self, phase, code, status):
        fired, base = {"done": False}, type(self.fake).__call__
        async def transport(adapter, prompt, cwd, session_id=None, timeout=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            if context["job"]["job_phase"] == phase and not fired["done"]:
                fired["done"] = True
                self.fake.failure = {"status": status, "failure_code": code}
                try:
                    return await base(self.fake, adapter, prompt, cwd, session_id=session_id, timeout=timeout, **options)
                finally:
                    self.fake.failure = None
            return await base(self.fake, adapter, prompt, cwd, session_id=session_id, timeout=timeout, **options)
        self.service.native_invoke = transport
        return fired

    def record_work(self):
        seen, original = [], self.handler
        def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "work":
                seen.append((context["job"]["id"], (cwd / "main.py").read_text()))
                (cwd / "main.py").write_text("VALUE = 2\n")
                return native_reply(context, [{"type": "complete", "task_id": context["task"]["id"], "result": "Updated main."}],
                    unchanged=[{"path": "notes.md", "reason": "Existing notes remain correct."}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        return seen

    async def test_each_unconfirmed_failure_offers_recovery_that_completes_without_replay(self):
        # Catches: 19 of 27 failure/phase combinations left phase unknown with
        # continue, retry, restart, recover and directed messages all refused.
        for phase, code, status in UNCONFIRMED:
            with self.subTest(phase=phase, code=code):
                fired, seen = self.fail_once(phase, code, status), self.record_work()
                created = await self.create()
                await self.settle()
                self.assertTrue(fired["done"])
                room_id = created["room"]["id"]
                stopped = await self.service.read_room(room_id)
                self.assertIn(stopped["team"]["phase"], {"unknown", "stopped", "interrupted"}, stopped["team"])
                run = stopped["current_run"]
                self.assertTrue(run["can_recover_native"], (code, run["native_recovery_reason"]))
                self.assertEqual(run["native_recovery_kind"], "unconfirmed_restart")
                failed_ids = {job["id"] for job in stopped["jobs"] if job["status"] in {"unknown", "failed"}}
                before = len(self.fake.calls)
                await self.service.recover_native(room_id, "abandon-" + code + phase,
                                                  run_id=run["id"], revision=run["revision"], digest=run["digest"])
                await self.settle()
                final = await self.service.read_room(room_id)
                self.assertEqual(final["objective"]["status"], "complete", final["team"])
                later = self.fake.calls[before:]
                self.assertTrue(later)
                self.assertFalse(any(call["context"]["job"]["id"] in failed_ids for call in later), "an old call was replayed")
                self.assertIsNone(later[0]["session_id"], "recovery starts fresh sessions")
                abandoned = [event for event in final["events"] if event["kind"] == "native_unconfirmed_calls_abandoned"]
                self.assertEqual(len(abandoned), 1)
                self.assertFalse(abandoned[0]["old_calls_replayed"])
                self.assertEqual(self.service._project(room_id).files_for_paths(["main.py"])[0]["content"], "VALUE = 2\n")
                if phase == "work":
                    # The interrupted owner's partial bytes stayed for it to finish.
                    self.assertEqual(seen[-1][1], "VALUE = 2\n")

    async def test_a_private_change_no_task_owns_is_reported_to_the_partners_not_held_against_the_room(self):
        # Catches the dead end this fallback used to keep: one stray file in a
        # private project refused every way to continue, with nothing in the
        # app to resolve it. The next run is told about the file instead, and
        # nothing is published by the restart itself.
        self.fail_once("work", "unknown_provider_error", "failed")
        self.record_work()
        created = await self.create()
        await self.settle()
        room_id = created["room"]["id"]
        snapshot = await self.service.read_room(room_id)
        worker = next(job["provider"] for job in snapshot["jobs"] if job["status"] in {"unknown", "failed"})
        state = self.service.store.native_workspace(room_id, worker)
        (Path(state["project"]) / "unowned.txt").write_text("Fictional edit outside every task.\n")
        run = (await self.service.read_room(room_id))["current_run"]
        self.assertTrue(run["can_recover_native"], run["native_recovery_reason"])
        before = len(self.fake.calls)
        await self.service.recover_native(room_id, "abandon-with-stray-file",
                                          run_id=run["id"], revision=run["revision"], digest=run["digest"])
        started = self.service.store.snapshot(room_id)
        abandoned = [event for event in started["events"] if event["kind"] == "native_unconfirmed_calls_abandoned"]
        self.assertEqual([event["unowned_private_changes"] for event in abandoned],
                         [[{"provider": worker, "paths": ["unowned.txt"]}]])
        self.assertEqual(self.service._project(room_id).snapshot()["revision"], 1, "the restart itself published nothing")
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        told = self.fake.calls[before]["context"]["objective"]["continuation"]["previous_stop"]
        self.assertEqual(told["unowned_private_changes"], [{"provider": worker, "paths": ["unowned.txt"]}])
        self.assertEqual([(call["provider"], call["phase"], call["error"]) for call in told["ended_calls"]],
                         [(worker, "work", "unknown_provider_error")])
        self.assertIn("context.objective.continuation.previous_stop", self.fake.calls[before]["prompt"])

    async def test_a_stop_the_user_pressed_mid_call_can_be_continued_by_their_next_message(self):
        # Guards a path that already worked before the fallback was widened:
        # a room the user stopped in the middle of a tool, under Full Access,
        # must still take their next message and finish.
        working, base = asyncio.Event(), type(self.fake).__call__
        async def transport(adapter, prompt, cwd, session_id=None, timeout=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            if context["job"]["job_phase"] == "work" and not working.is_set():
                working.set()
                (Path(cwd) / "main.py").write_text("VALUE = 2\n")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    # Stopped in the middle of a tool, under Full Access.
                    error.native_execution = {"permission": options["permission"], "tool_calls": 2, "tool_results": 1,
                                              "cleanup_confirmed": True, "side_effects_possible": True}
                    raise
            return await base(self.fake, adapter, prompt, cwd, session_id=session_id, timeout=timeout, **options)
        self.service.native_invoke = transport
        created = await self.create()
        room_id = created["room"]["id"]
        await asyncio.wait_for(working.wait(), 5)
        await self.service.cancel_room(room_id)
        await self.settle()
        stopped = await self.service.read_room(room_id)
        self.assertEqual(stopped["team"]["phase"], "cancelled", stopped["team"])
        self.assertTrue(stopped["continuation"]["can_send"], stopped["continuation"])
        self.record_work()
        await self.service.send_message(room_id, "Carry on with the same goal.", "all", "after-stop",
                                        intent="continue", binding=stopped["continuation"]["binding"])
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])

    async def test_unknown_canonical_commit_is_never_abandoned(self):
        # Catches: restarting over a call whose commit outcome is unknown,
        # which could duplicate or hide an already published revision.
        self.fail_once("work", "native_no_progress", "failed")
        self.record_work()
        created = await self.create()
        await self.settle()
        room_id = created["room"]["id"]
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE teams SET stop_reason='project_commit_unconfirmed' WHERE room_id=?", (room_id,))
        run = (await self.service.read_room(room_id))["current_run"]
        self.assertFalse(run["can_recover_native"])
        self.assertIsNone(self.service.store.native_recovery_requirements(room_id)["unconfirmed_restart"])


class KilledHostTests(unittest.IsolatedAsyncioTestCase):
    """The host itself was killed (crash, forced quit, power loss) while a native call ran."""
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create
    record_work = UnconfirmedRestartTests.record_work

    async def kill_host_during_work(self, *, tracked=True):
        """Returns the room and the process the cut-off call had launched, still running."""
        working, base, launched = asyncio.Event(), type(self.fake).__call__, []
        async def transport(adapter, prompt, cwd, session_id=None, timeout=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            if context["job"]["job_phase"] == "work" and not working.is_set():
                launched.append(subprocess.Popen(["sleep", "600"], start_new_session=True))
                self.addCleanup(lambda: launched[0].poll() is None and launched[0].kill())
                if tracked:
                    options["on_process"](launched[0].pid)
                working.set()
                await asyncio.Event().wait()
            return await base(self.fake, adapter, prompt, cwd, session_id=session_id, timeout=timeout, **options)
        self.service.native_invoke = transport
        room_id = (await self.create())["room"]["id"]
        await asyncio.wait_for(working.wait(), 5)
        # The database exactly as a killed process leaves it: a call still working.
        database = self.root / "rooms.sqlite3"
        frozen = sqlite3.connect(":memory:")
        with sqlite3.connect(database) as live:
            live.backup(frozen)
        await self.service.shutdown()
        for suffix in ("-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        with sqlite3.connect(database) as restored:
            frozen.backup(restored)
        frozen.close()
        self.service = native.RoomService(database, self.root / "workspaces", native_invoke=self.fake,
                                          registry=native.default_registry())
        return room_id, launched[0]

    async def host_passes(self, room_id, count):
        for _ in range(count):
            self.service._late_cleanup_checks.clear()
            self.service._confirm_late_cleanup(room_id, self.service.store.snapshot(room_id))
            await asyncio.sleep(0.2)

    async def test_the_new_host_ends_the_cut_off_calls_process_and_the_room_continues(self):
        # Catches: a host killed mid-call leaving the room unable to continue
        # for ever (no cleanup was ever recorded for the cut-off call), while
        # the process that call launched kept running in the private project.
        room_id, orphan = await self.kill_host_during_work()
        before = await self.service.read_room(room_id)
        self.assertEqual(before["team"]["stop_reason"], "service_restarted")
        self.assertFalse(before["continuation"]["can_send"])
        self.assertIsNone(orphan.poll(), "the cut-off call's process is still running")
        await self.host_passes(room_id, 3)
        self.assertEqual(orphan.wait(timeout=5), -9)
        ready = await self.service.read_room(room_id)
        self.assertEqual((ready["current_run"]["native_recovery_kind"], ready["current_run"]["can_recover_native"]),
                         ("unconfirmed_restart", True), ready["current_run"]["native_recovery_reason"])
        self.assertTrue(ready["continuation"]["can_send"], ready["continuation"])
        self.assertEqual(ready["stop_kind"], "auto")
        self.record_work()
        run = ready["current_run"]
        await self.service.recover_native(room_id, "after-kill", run_id=run["id"], revision=run["revision"], digest=run["digest"])
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])

    async def test_a_call_claimed_before_processes_were_recorded_is_not_assumed_clean(self):
        # Catches the confirmation above being given to a call whose process
        # this host has no record of and so cannot have ended.
        room_id, orphan = await self.kill_host_during_work(tracked=False)
        with self.service.store._connection(write=True) as db:
            rows = db.execute("SELECT seq,value_json FROM events WHERE room_id=? AND json_extract(value_json,'$.kind')='job_claimed'",
                              (room_id,)).fetchall()
            for row in rows:
                value = json.loads(row["value_json"])
                value.pop("process_tracked")
                db.execute("UPDATE events SET value_json=? WHERE seq=?", (json.dumps(value), row["seq"]))
        await self.host_passes(room_id, 3)
        self.assertIsNone(orphan.poll())
        waiting = await self.service.read_room(room_id)
        self.assertFalse(waiting["current_run"]["can_recover_native"])
        self.assertFalse(waiting["continuation"]["can_send"])

    async def test_a_reused_process_id_is_never_ended(self):
        # Catches the new host killing an unrelated program that was later given the recorded ID.
        from local_agent_coordinator import native_adapters
        unrelated = subprocess.Popen(["sleep", "600"], start_new_session=True)
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.kill())
        import time
        self.assertFalse(native_adapters.end_orphaned(unrelated.pid, time.time() - 3600))
        await asyncio.sleep(0.2)
        self.assertIsNone(unrelated.poll())
        self.assertTrue(native_adapters.end_orphaned(unrelated.pid, time.time()))
        self.assertEqual(unrelated.wait(timeout=5), -9)


if __name__ == "__main__":
    unittest.main()
