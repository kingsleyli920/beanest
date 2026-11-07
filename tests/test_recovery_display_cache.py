"""Idle polls reuse the private-tree recovery audit; actions never do."""
from pathlib import Path
import unittest
from uuid import uuid4

import test_native_room_service as native
import test_native_unconfirmed_restart as unconfirmed
from local_agent_coordinator.room_store import RoomError, RoomStore


class RecoveryDisplayCacheTests(unittest.IsolatedAsyncioTestCase):
    # Borrow the fixture only; inheriting would rerun every native room test.
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create
    fail_once = unconfirmed.UnconfirmedRestartTests.fail_once
    record_work = unconfirmed.UnconfirmedRestartTests.record_work

    async def asyncSetUp(self):
        await native.NativeRoomServiceTests.asyncSetUp(self)
        # A tracked file in a subfolder that no task names: only a per-file
        # stat sees an in-place edit to it, no folder or task path changes.
        (self.source / "lib").mkdir()
        (self.source / "lib" / "extra.py").write_text("EXTRA = 1\n")
        self.registration = await self.service.register_project("Example with lib", str(self.source), selection_kind="folder")

    async def stopped_room(self):
        """A room stopped by an unconfirmed work call, offering recover-native."""
        self.fail_once("work", "native_no_progress", "failed")
        self.record_work()
        created = await self.create()
        await self.settle()
        room_id = created["room"]["id"]
        self.audits = []
        audit = self.service._audit_native_recovery
        def counted(*args, **kwargs):
            self.audits.append(kwargs)
            return audit(*args, **kwargs)
        self.service._audit_native_recovery = counted
        return room_id

    def bystander_file(self, room_id, name="main.py"):
        """A tracked file in the private tree of the member that was not working:
        no unfinished task of that member authorizes changing it."""
        snapshot = self.service.store.snapshot(room_id)
        worker = next(job["provider"] for job in snapshot["jobs"] if job["status"] in {"unknown", "failed"})
        other = next(member["id"] for member in snapshot["members"] if member["id"] != worker)
        state = self.service.store.native_workspace(room_id, other)
        self.assertIsNotNone(state, "both members planned, so both have private trees")
        return Path(state["project"]) / name

    async def test_idle_polls_reuse_one_audit(self):
        # Catches: every 3-second poll re-walking each private tree to show
        # can_recover_native, which kept an idle desktop service at 25-45% CPU.
        room_id = await self.stopped_room()
        first = await self.service.read_room(room_id)
        state = await self.service.read_room_state(room_id)
        again = await self.service.read_room(room_id)
        self.assertEqual(len(self.audits), 1)
        for snapshot in (first, state, again):
            self.assertIs(snapshot["current_run"]["can_recover_native"], True)
            self.assertEqual(snapshot["current_run"]["native_recovery_kind"], "unconfirmed_restart")

    async def test_new_room_event_reaudits(self):
        # Catches: a key blind to the room's records, so a poll keeps showing a
        # verdict reached before newer events were written.
        room_id = await self.stopped_room()
        await self.service.read_room(room_id)
        with self.service.store._connection(write=True) as db:
            self.service.store._event(db, room_id, "warning", sender="system", code="fictional_test_warning")
        await self.service.read_room(room_id)
        self.assertEqual(len(self.audits), 2)

    async def test_in_place_edit_of_a_private_file_reaudits_at_once(self):
        # Catches: a key that only watches folders or stored state, so a
        # same-size in-place edit (no folder change) under Full Access keeps
        # the old "recoverable" verdict on screen.
        room_id = await self.stopped_room()
        self.assertIs((await self.service.read_room(room_id))["current_run"]["can_recover_native"], True)
        path = self.bystander_file(room_id, "lib/extra.py")
        self.assertEqual(path.read_text(), "EXTRA = 1\n")
        folder = path.parent.stat().st_mtime_ns
        path.write_text("EXTRA = 3\n")
        self.assertEqual(path.parent.stat().st_mtime_ns, folder, "a same-size in-place edit leaves the folder alone")
        run = (await self.service.read_room(room_id))["current_run"]
        self.assertEqual(len(self.audits), 2)
        self.assertIs(run["can_recover_native"], False)
        self.assertEqual(run["native_recovery_reason"], "native_recovery_unpublished_changes")

    async def test_cached_audit_expires(self):
        # Catches: a cache that never expires, so changes the key cannot see
        # (a new file inside an unlisted folder, an outside receipt) never show.
        room_id = await self.stopped_room()
        now = [self.service.clock()]
        self.service.clock = lambda: now[0]
        await self.service.read_room(room_id)
        now[0] += 29
        await self.service.read_room(room_id)
        self.assertEqual(len(self.audits), 1)
        now[0] += 2
        await self.service.read_room(room_id)
        self.assertEqual(len(self.audits), 2)

    async def stale_cache(self):
        """A poll cached "recoverable"; then the tree changed in a way the key
        misses (simulated by freezing the key), so polls still say recoverable."""
        room_id = await self.stopped_room()
        self.service._recovery_display_key = lambda *args: "frozen"
        run = (await self.service.read_room(room_id))["current_run"]
        self.assertIs(run["can_recover_native"], True)
        self.bystander_file(room_id).write_text("VALUE = 3\n")
        stale = (await self.service.read_room(room_id))["current_run"]
        self.assertIs(stale["can_recover_native"], True, "the frozen key makes the poll reuse the old verdict")
        self.assertEqual(len(self.audits), 1)
        return room_id, run

    async def test_recover_native_audits_afresh_despite_a_stale_cache(self):
        # Catches: recover-native acting on the cached poll verdict, which
        # would start a new run over an unpublished private edit.
        room_id, run = await self.stale_cache()
        calls = len(self.fake.calls)
        with self.assertRaises(RoomError) as raised:
            await self.service.recover_native(room_id, str(uuid4()),
                                              run_id=run["id"], revision=run["revision"], digest=run["digest"])
        self.assertEqual(raised.exception.code, "native_recovery_unpublished_changes")
        await self.settle()
        self.assertGreater(len(self.audits), 1)
        self.assertEqual(len(self.fake.calls), calls, "no partner was called")
        after = self.service.store.snapshot(room_id)
        self.assertEqual(after["current_run"]["id"], run["id"], "no new run was started")

    async def test_recover_native_is_not_refused_by_a_stale_cached_refusal(self):
        # Catches: recover-native gating on the cached poll verdict, so a user
        # who already undid a stray private edit is still refused until expiry.
        room_id = await self.stopped_room()
        self.service._recovery_display_key = lambda *args: "frozen"
        path = self.bystander_file(room_id)
        path.write_text("VALUE = 3\n")
        self.assertIs((await self.service.read_room(room_id))["current_run"]["can_recover_native"], False)
        path.write_text("VALUE = 1\n")
        run = (await self.service.read_room(room_id))["current_run"]
        self.assertIs(run["can_recover_native"], False, "the frozen key makes the poll reuse the old verdict")
        self.assertEqual(len(self.audits), 1)
        calls = len(self.fake.calls)
        await self.service.recover_native(room_id, str(uuid4()),
                                          run_id=run["id"], revision=run["revision"], digest=run["digest"])
        await self.settle()
        self.assertGreater(len(self.fake.calls), calls, "the fresh audit allowed the recovery")
        self.assertNotEqual(self.service.store.snapshot(room_id)["current_run"]["id"], run["id"])

    async def test_automatic_continue_audits_afresh_despite_a_stale_cache(self):
        # Catches: the host pressing 继续 for the user on the cached poll
        # verdict instead of a fresh audit of the private trees.
        room_id, run = await self.stale_cache()
        calls = len(self.fake.calls)
        stop_reason = self.service.store.snapshot(room_id)["team"]["stop_reason"]
        await self.service._auto_continue(room_id, run["id"], stop_reason)
        await self.settle()
        self.assertGreater(len(self.audits), 1)
        self.assertEqual(len(self.fake.calls), calls, "no partner was called")
        self.assertEqual(self.service.store.snapshot(room_id)["current_run"]["id"], run["id"], "no new run was started")


class ListSnapshotTests(unittest.TestCase):
    def test_snapshot_without_events_keeps_job_inspection_flags(self):
        # Catches: the room list's timeline-free snapshot dropping a job's
        # partner_handoff_inspection flag, or silently returning an empty
        # timeline that a reader could mistake for a room without events.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            store = RoomStore(Path(directory) / "rooms.sqlite3")
            room_id, _ = store.create_room("A fictional request", "claude", str(uuid4()))
            job = store.claim(room_id, "claude")
            with store._connection(write=True) as db:
                store._event(db, room_id, "partner_handoff_inspection", sender="system", job_id=job["id"])
            full, light = store.snapshot(room_id), store.snapshot(room_id, events=False)
            self.assertNotIn("events", light)
            self.assertEqual(light["jobs"], full["jobs"])
            self.assertTrue(any(item["partner_handoff_inspection"] for item in light["jobs"]))
            self.assertEqual({key: value for key, value in full.items() if key != "events"}, light)


if __name__ == "__main__":
    unittest.main()
