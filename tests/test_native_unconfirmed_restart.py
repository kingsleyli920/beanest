"""Last-resort recovery: a proven-dead unconfirmed call never leaves a dead end."""
import asyncio
import json
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


if __name__ == "__main__":
    unittest.main()
