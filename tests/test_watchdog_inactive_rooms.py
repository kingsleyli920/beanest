"""The 2.5 s watchdog does not keep re-reading rooms that cannot act."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_service import RoomService


class WatchdogInactiveRoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()

        async def never(*args, **kwargs):
            raise AssertionError("the watchdog must not call a model")

        self.service = RoomService(root / "rooms.sqlite3", root / "workspaces", invoke=never, native_invoke=never)
        self.addAsyncCleanup(self.service.shutdown)
        self.active, _ = self.service.store.create_room("Fictional active room", "claude", str(uuid4()))
        self.archived, _ = self.service.store.create_room("Fictional archived room", "claude", str(uuid4()))
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE jobs SET status='completed' WHERE room_id IN (?,?)", (self.active, self.archived))
            db.execute("UPDATE rooms SET status='completed' WHERE id IN (?,?)", (self.active, self.archived))
        self.service.store.set_lifecycle(self.archived, "archive")

    async def test_an_archived_room_is_read_once_per_tick_and_still_gets_late_cleanup(self):
        # Catches: every tick reading each archived/removed room three times
        # (handoff, quota wait, auto continue all return early for it); with a
        # 22.5k-event archived room that alone kept the idle service at ~20% CPU.
        reads = []
        snapshot = self.service.store.snapshot

        def counted(room_id, **options):
            reads.append(room_id)
            return snapshot(room_id, **options)

        with patch.object(self.service.store, "snapshot", side_effect=counted), \
                patch.object(self.service, "_confirm_late_cleanup", wraps=self.service._confirm_late_cleanup) as late, \
                patch.object(self.service, "_schedule_auto_continue", wraps=self.service._schedule_auto_continue) as auto:
            self.service._auto_continues[self.archived] = "stale"
            await self.service.expire_tasks()
        self.assertEqual(reads.count(self.archived), 1)
        self.assertGreater(reads.count(self.active), 1)
        self.assertEqual({call.args[0] for call in late.call_args_list}, {self.active, self.archived})
        self.assertEqual({call.args[0] for call in auto.call_args_list}, {self.active})
        self.assertNotIn(self.archived, self.service._auto_continues)


if __name__ == "__main__":
    unittest.main()
