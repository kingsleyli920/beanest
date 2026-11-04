import asyncio
from functools import partial
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.agent_registry import AgentRegistry, AgentSpec
from local_agent_coordinator.room_service import RoomService


class HeldInvoke:
    def __init__(self):
        self.calls = []
        self.cleaned = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, provider, prompt, cwd, **kwargs):
        self.calls.append(provider)
        if len(self.calls) == 2:
            self.started.set()
        try:
            await self.release.wait()
        finally:
            self.cleaned.append(provider)


class LegacyRoomSettleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.invoke = HeldInvoke()
        registry = AgentRegistry([AgentSpec(provider, provider.title(), partial(self.invoke, provider))
                                  for provider in ("claude", "codex")])
        self.service = RoomService(root / "rooms.sqlite3", root / "workspaces",
                                   registry=registry,
                                   call_timeout=0.2)
        self.store = self.service.store

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.directory.cleanup()

    async def test_timed_out_owners_leave_retained_queue_unknown_not_running(self):
        created = await self.service.create_room("A legacy text request", "both", str(uuid4()))
        room_id = created["room"]["id"]
        await asyncio.wait_for(self.invoke.started.wait(), 2)
        await self.service.send_message(room_id, "Keep this followup", "all", str(uuid4()))
        queued = [job for job in self.store.snapshot(room_id)["jobs"] if job["status"] == "queued"]
        self.assertEqual(len(queued), 2)
        await asyncio.wait_for(asyncio.gather(*tuple(self.service._workers.values())), 2)

        snapshot = await self.service.read_room(room_id)
        self.assertEqual(snapshot["room"]["status"], "unknown")
        self.assertEqual(snapshot["room"]["status_reason"], "session_identity_unknown")
        self.assertEqual([job for job in snapshot["jobs"] if job["status"] == "queued"], queued)
        failures = [job for job in snapshot["jobs"] if job["status"] == "unknown"]
        self.assertEqual(len(failures), 2)
        self.assertEqual({job["error"] for job in failures}, {"deadline_exceeded"})
        self.assertTrue(all(member["identity_uncertain"] for member in snapshot["members"]))
        self.assertIsNone(self.store.claim(room_id, "claude"))
        self.assertIsNone(self.store.claim(room_id, "codex"))
        self.assertCountEqual(self.invoke.cleaned, ["claude", "codex"])
        self.assertEqual(len(self.invoke.calls), 2)
        self.assertEqual(snapshot["budget"]["used"], 2)
        self.assertIsNone(snapshot["budget"]["deadline"])
        self.assertEqual(next(event for event in reversed(snapshot["events"])
                              if event["kind"] == "room_status")["code"], "session_identity_unknown")
        state = await self.service.read_room_state(room_id)
        self.assertEqual(state["room"]["status_reason"], "session_identity_unknown")

    async def test_one_known_queued_owner_still_runs_and_can_be_claimed(self):
        room_id, _ = self.store.create_room("Queued control", "both", str(uuid4()))
        failed = self.store.claim(room_id, "claude")
        self.store.send_message(room_id, "Retain unknown owner's followup", "claude", str(uuid4()))
        self.store.finish_job(failed["id"], error="deadline_exceeded", uncertain=True)

        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "running")
        self.assertNotIn("status_reason", snapshot["room"])
        self.assertIsNone(self.store.claim(room_id, "claude"))
        self.assertIsNotNone(self.store.claim(room_id, "codex"))

    async def test_real_working_job_keeps_running_then_settles_unknown_without_losing_result(self):
        room_id, _ = self.store.create_room("Working control", "both", str(uuid4()))
        failed = self.store.claim(room_id, "claude")
        working = self.store.claim(room_id, "codex")
        self.store.send_message(room_id, "Retain unknown owner's followup", "claude", str(uuid4()))
        self.store.finish_job(failed["id"], error="deadline_exceeded", uncertain=True)
        self.assertEqual(self.store.snapshot(room_id)["room"]["status"], "running")

        self.store.finish_job(working["id"], message="The other result is preserved")
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "unknown")
        self.assertEqual(snapshot["room"]["status_reason"], "session_identity_unknown")
        self.assertEqual(next(job for job in snapshot["jobs"] if job["id"] == working["id"])["status"], "completed")
        self.assertTrue(any(event.get("text") == "The other result is preserved" for event in snapshot["events"]))
        self.assertEqual(sum(job["status"] == "queued" for job in snapshot["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
