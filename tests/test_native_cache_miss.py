"""A resumed Claude call that rewrites its whole cache is reported, and nothing else is.

Every expected value below is a hand-written literal. No real model is invoked.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_projection import room_state
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomStore


SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"


def usage(read=0, written=790000):
    return {"source": "claude_cli", "scope": "provider_reported_latest_result",
            "values": {"input_tokens": 12, "output_tokens": 340,
                       "cache_read_input_tokens": read, "cache_creation_input_tokens": written},
            "observed_at": 1.0}


class NativeCacheMissStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = RoomStore(Path(self.directory.name) / "rooms.sqlite3", clock=lambda: self.now,
                               agent_adapters={"claude": "claude", "codex": "codex"})
        self.room = self.store.create_room("Goal", "claude", str(uuid4()))[0]
        self.calls = 0

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def call(self, *, session=SESSION, **values):
        """Run one Claude call through the real claim/finish path."""
        if self.calls:
            self.store.send_message(self.room, "继续", "claude", str(uuid4()))
        self.calls += 1
        job = self.store.claim(self.room, "claude")
        self.store.finish_job(job["id"], session_id=session, message="done", usage=usage(**values))
        return job

    def misses(self):
        return [event for event in self.store.snapshot(self.room)["events"] if event["kind"] == "native_cache_miss"]

    def previous_call(self):
        first = self.call(read=0, written=790000)
        self.now += 35
        return first

    def test_hit_records_metadata_only(self):
        # Catches: the incident shape (35s apart, rewrite, zero read) going unreported.
        self.previous_call()
        second = self.call(read=0, written=800000)
        misses = self.misses()
        self.assertEqual(len(misses), 1)
        event = misses[0]
        self.assertEqual(event["provider"], "claude")
        self.assertEqual(event["sender"], "claude")
        self.assertEqual(event["job_id"], second["id"])
        self.assertEqual(event["cache_creation_input_tokens"], 800000)
        self.assertEqual(event["seconds_since_previous_call"], 35.0)
        self.assertEqual(event["phase"], "task")
        self.assertIsNone(event["run_id"])
        self.assertEqual(set(event) - {"seq"}, {"kind", "created_at", "sender", "provider", "job_id", "run_id", "phase",
                                                "cache_creation_input_tokens", "seconds_since_previous_call"})

    def test_team_job_reports_its_own_phase_and_run(self):
        # Catches: the event carrying the room's phase or no run, and a failed call with usage being skipped.
        self.room = self.store.create_room("Update app", "claude", str(uuid4()), mode="project_folder",
                                           project_id=str(uuid4()), collaboration=True)[0]
        author = self.store.claim(self.room, "claude")
        peer = self.store.claim(self.room, "codex")
        proposal = {"type": "propose", "id": "main", "title": "Update app", "description": "Update the app.",
                    "acceptance": "Peer reviews changes", "dependencies": [], "write_paths": ["src/app.py"],
                    "capability": "text"}
        self.store.finish_job(author["id"], session_id=SESSION, usage=usage(read=0, written=790000),
                              team_commands=[proposal, {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
        self.store.finish_job(peer["id"], session_id=OTHER_SESSION, team_commands=[{"type": "plan_ready"}])
        confirmation = self.store.claim(self.room, "codex")
        self.store.finish_job(confirmation["id"], session_id=OTHER_SESSION, team_commands=[{"type": "plan_ready"}])
        self.now += 35
        work = self.store.claim(self.room, "claude")
        self.assertEqual(work["team"]["job_phase"], "work")
        self.store.finish_job(work["id"], session_id=SESSION, usage=usage(read=0, written=800000), error="provider_failed")
        misses = self.misses()
        self.assertEqual(len(misses), 1)
        self.assertEqual(misses[0]["phase"], "work")
        self.assertEqual(misses[0]["run_id"], work["team"]["run_id"])
        self.assertEqual(misses[0]["job_id"], work["id"])

    def test_new_session_is_not_reported(self):
        # Catches: a first call, which legitimately writes everything, being reported.
        self.call(read=0, written=790000)
        self.assertEqual(self.misses(), [])

    def test_fresh_claim_is_not_reported_even_soon_after_a_prior_call(self):
        # Catches: judging "resumed" from the returned id alone instead of what the call was claimed with.
        self.previous_call()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=NULL WHERE room_id=? AND provider='claude'", (self.room,))
        self.call(read=0, written=800000)
        self.assertEqual(self.misses(), [])

    def test_resumed_call_without_an_earlier_usage_is_not_reported(self):
        # Catches: the call's own usage event being mistaken for the previous call (gap 0).
        with self.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=? WHERE room_id=? AND provider='claude'", (SESSION, self.room))
        self.call(read=0, written=800000)
        self.assertEqual(self.misses(), [])

    def test_call_on_a_different_session_is_not_reported(self):
        # Catches: comparing against the previous call of any session.
        self.call(read=0, written=790000, session=OTHER_SESSION)
        self.now += 35
        with self.store._connection(write=True) as db:
            db.execute("UPDATE members SET session_id=? WHERE room_id=? AND provider='claude'", (SESSION, self.room))
        self.call(read=0, written=800000, session=SESSION)
        self.assertEqual(self.misses(), [])

    def test_any_cache_read_is_not_reported(self):
        # Catches: treating a partial cache hit as a miss.
        self.previous_call()
        self.call(read=1, written=800000)
        self.assertEqual(self.misses(), [])

    def test_write_below_threshold_is_not_reported(self):
        # Catches: reporting ordinary small writes; 199999 is one under the threshold.
        self.previous_call()
        self.call(read=0, written=199999)
        self.assertEqual(self.misses(), [])

    def test_write_at_threshold_is_reported(self):
        # Catches: an off-by-one that treats the threshold as exclusive.
        self.previous_call()
        self.call(read=0, written=200000)
        self.assertEqual([event["cache_creation_input_tokens"] for event in self.misses()], [200000])

    def test_gap_at_or_over_five_minutes_is_not_reported(self):
        # Catches: reporting a natural cache expiry. Exactly 300s is no longer "under 5 minutes".
        self.call(read=0, written=790000)
        self.now += 300
        self.call(read=0, written=800000)
        self.assertEqual(self.misses(), [])

    def test_gap_just_under_five_minutes_is_reported(self):
        # Catches: a window narrower than the specified 5 minutes.
        self.call(read=0, written=790000)
        self.now += 299
        self.call(read=0, written=800000)
        self.assertEqual([event["seconds_since_previous_call"] for event in self.misses()], [299.0])

    def test_non_claude_adapter_is_not_reported(self):
        # Catches: the monitor spreading to Codex or Kimi.
        self.previous_call()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE members SET adapter='codex' WHERE room_id=? AND provider='claude'", (self.room,))
        self.call(read=0, written=800000)
        self.assertEqual(self.misses(), [])

    def test_detector_failure_leaves_usage_and_job_untouched(self):
        # Catches: a diagnostic bug failing the job or losing its usage record.
        self.previous_call()
        with patch.object(RoomStore, "_record_native_cache_miss", side_effect=RuntimeError("broken")) as detector:
            second = self.call(read=0, written=800000)
        self.assertEqual(detector.call_count, 1)
        snapshot = self.store.snapshot(self.room)
        job = next(item for item in snapshot["jobs"] if item["id"] == second["id"])
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["error"])
        recorded = [event for event in snapshot["events"] if event["kind"] == "usage" and event["job_id"] == second["id"]]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["usage"]["values"]["cache_creation_input_tokens"], 800000)
        claude = next(member for member in snapshot["members"] if member["id"] == "claude")
        self.assertEqual(claude["usage"]["values"]["cache_creation_input_tokens"], 800000)
        self.assertEqual(self.misses(), [])

    def test_state_projection_returns_only_the_latest_miss(self):
        # Catches: the paged state dropping the signal, or returning an older one.
        self.call(read=0, written=790000)
        for written in (800000, 810000):
            self.now += 35
            self.call(read=0, written=written)
        self.assertEqual(len(self.misses()), 2)
        state = room_state(self.store.snapshot(self.room), {}, lambda job: "read_only")
        self.assertEqual(state["events"], [])
        self.assertEqual(state["latest_native_cache_miss"]["cache_creation_input_tokens"], 810000)

    def test_state_projection_is_null_without_a_miss(self):
        # Catches: a stale or fabricated warning in a room with no miss.
        self.call(read=0, written=790000)
        state = room_state(self.store.snapshot(self.room), {}, lambda job: "read_only")
        self.assertIsNone(state["latest_native_cache_miss"])


class FakeInvoke:
    """Replays canned provider results; no model or CLI is involved."""
    def __init__(self, results):
        self.results = results

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        await asyncio.sleep(0)
        return {"session_id": SESSION, "status": "completed", "events": [],
                "text": json.dumps({"message": "Done", "actions": [], "state": "done"}),
                "usage": self.results.pop(0)}


class NativeCacheMissServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.now = 1000.0
        self.fake = FakeInvoke([
            {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 790000},
            {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 800000}])
        self.service = RoomService(root / "rooms.sqlite3", root / "workspaces", invoke=self.fake, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.directory.cleanup()

    async def settle(self):
        while tasks := [task for task in self.service._workers.values() if not task.done()]:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_miss_flows_from_provider_usage_to_room_state(self):
        # Catches: the real provider usage shape never reaching the detector.
        created = await self.service.create_room("Goal", "claude", str(uuid4()))
        room_id = created["room"]["id"]
        await self.settle()
        self.now += 35
        await self.service.send_message(room_id, "继续", "claude", str(uuid4()))
        await self.settle()
        state = await self.service.read_room_state(room_id)
        miss = state["latest_native_cache_miss"]
        self.assertEqual(miss["provider"], "claude")
        self.assertEqual(miss["cache_creation_input_tokens"], 800000)
        self.assertEqual(miss["seconds_since_previous_call"], 35.0)
        self.assertNotIn("text", miss)
        self.assertEqual([job["status"] for job in self.service.store.snapshot(room_id)["jobs"]], ["completed", "completed"])


if __name__ == "__main__":
    unittest.main()
