"""The decoded room timeline cache stays exact while polls stop re-decoding."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_store import RoomStore


class EventTimelineCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RoomStore(Path(directory.name).resolve() / "rooms.sqlite3")
        self.room, _ = self.store.create_room("Fictional cache room", "claude", str(uuid4()))

    def note(self, text, room=None):
        with self.store._connection(write=True) as db:
            self.store._event(db, room or self.room, "warning", sender="system", code="fixture", text=text)

    def texts(self, snapshot):
        return [event.get("text") for event in snapshot["events"] if event["kind"] == "warning"]

    def test_a_poll_decodes_only_events_newer_than_the_cache(self):
        # Catches: re-decoding a long room's whole timeline on every 3 s poll
        # (22.5k events, ~0.2 s CPU each) or missing an event appended since.
        for index in range(50):
            self.note(f"old {index}")
        self.assertEqual(len(self.texts(self.store.snapshot(self.room))), 50)
        self.note("new")
        # The verification mode re-decodes everything on purpose; count without it.
        with patch.dict(os.environ, {"BEANEST_VERIFY_EVENT_CACHE": "0"}), \
                patch("local_agent_coordinator.room_store.json.loads", wraps=json.loads) as decode:
            snapshot = self.store.snapshot(self.room)
        self.assertEqual(self.texts(snapshot)[-1], "new")
        self.assertEqual(len(self.texts(snapshot)), 51)
        self.assertLess(decode.call_count, 25, "older events were decoded again")
        self.assertEqual(snapshot["events"], self.store.snapshot(self.room)["events"])

    def test_a_transaction_never_sees_events_newer_than_its_own_view(self):
        # Catches: a snapshot taking a newer cached tail than the rows its own
        # transaction read, so events and jobs/tasks would disagree. (With the
        # rollback journal a writer cannot commit under a reader, so a newer
        # tail is planted directly.)
        self.note("first")
        self.store.snapshot(self.room)
        seqs, events, rewrites = self.store._timelines[self.room]
        newer = {"seq": seqs[-1] + 1000, "kind": "warning", "text": "not yet in this view"}
        self.store._timelines[self.room] = (seqs + [newer["seq"]], events + [newer], rewrites)
        self.assertEqual(self.texts(self.store.snapshot(self.room)), ["first"])

    def test_a_rewrite_of_stored_events_outside_the_service_is_picked_up(self):
        # Catches: a migration, manual repair or forged-history test editing
        # or deleting stored events while the cache kept serving the old ones.
        self.note("before")
        self.note("kept")
        self.store.snapshot(self.room)
        with self.store._connection(write=True) as db:
            seq = db.execute("SELECT MIN(seq) FROM events WHERE room_id=? AND value_json LIKE '%before%'",
                             (self.room,)).fetchone()[0]
            db.execute("UPDATE events SET value_json=? WHERE seq=?",
                       (json.dumps({"kind": "warning", "sender": "system", "text": "rewritten"}), seq))
        self.assertEqual(self.texts(self.store.snapshot(self.room)), ["rewritten", "kept"])
        with self.store._connection(write=True) as db:
            db.execute("DELETE FROM events WHERE seq=?", (seq,))
        self.assertEqual(self.texts(self.store.snapshot(self.room)), ["kept"])

    def test_changing_a_returned_event_does_not_change_the_next_snapshot(self):
        # Catches: a caller editing an event in place and corrupting every later read.
        self.note("original")
        first = self.store.snapshot(self.room)
        next(event for event in first["events"] if event["kind"] == "warning")["text"] = "edited by a caller"
        first["events"].clear()
        self.assertEqual(self.texts(self.store.snapshot(self.room)), ["original"])

    def test_the_cache_keeps_every_room_the_watchdog_visits_but_bounds_its_size(self):
        # Catches: a per-room cap smaller than the room count, which the
        # 2.5 s watchdog (visiting every room) turned into a full re-decode
        # of every timeline on every tick; and an unbounded cache.
        rooms = [self.store.create_room(f"Fictional room {index}", "claude", str(uuid4()))[0] for index in range(12)]
        for room in rooms:
            self.note("event", room)
            self.store.snapshot(room)
        self.assertEqual(len(self.store._timelines), 12)
        with patch.object(RoomStore, "_TIMELINE_EVENTS", 10):
            self.note("one more", rooms[0])
            self.store.snapshot(rooms[0])
            self.assertLessEqual(sum(len(item[0]) for item in self.store._timelines.values()), 10)
            self.assertIn(rooms[0], self.store._timelines)
        self.assertEqual(self.texts(self.store.snapshot(rooms[1])), ["event"])

if __name__ == "__main__":
    unittest.main()
