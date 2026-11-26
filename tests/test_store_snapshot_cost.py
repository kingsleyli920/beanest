"""Long-running rooms keep complete results without transferring unused blobs."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_store import RoomStore


class StoreSnapshotCostTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = RoomStore(Path(directory.name).resolve() / "rooms.sqlite3")
        self.addCleanup(self.store.close)
        self.room = self.store.create_room("Fictional performance fixture", "claude", str(uuid4()),
                                           collaboration=True)[0]

    def test_run_listing_does_not_read_saved_project_snapshot(self):
        with self.store._connection(write=True) as db:
            db.execute("UPDATE team_runs SET snapshot_json=? WHERE room_id=?",
                       (json.dumps({"fixture": "x" * 1_000_000}), self.room))
        with self.store._connection() as db:
            reads = []
            def authorize(action, table, column, database, source):
                if action == sqlite3.SQLITE_READ:
                    reads.append((table, column))
                return sqlite3.SQLITE_OK
            expected = self.store._team.runs(db, self.room)
            db.set_authorizer(authorize)
            actual = self.store._team.runs(db, self.room)
            db.set_authorizer(None)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 1)
        self.assertNotIn(("team_runs", "snapshot_json"), reads)

    def test_workspace_projection_avoids_decoding_baseline_and_stays_fresh(self):
        blocked = [{"path": "fixture.py", "reason": "native_path_not_owned"}]
        state = {"baseline": {"fixture": "x" * 1_000_000}, "blocked": blocked,
                 "conflicts": [{"path": "conflict.py", "private": "retained"}]}
        with self.store._connection(write=True) as db:
            db.execute("INSERT INTO native_workspaces VALUES (?,?,?)", (self.room, "claude", json.dumps(state)))
        with patch("local_agent_coordinator.room_store.json.loads", wraps=json.loads) as decode:
            actual = self.store.snapshot(self.room, events=False)["native_workspaces"]
        self.assertEqual(actual, [{"provider": "claude", "state": "blocked", "blocked": blocked,
                                   "conflicts": [{"path": "conflict.py", "reason": "native_merge_conflict"}]}])
        self.assertLess(max(len(call.args[0]) for call in decode.call_args_list), 50_000)
        for state, expected_blocked in (({}, []), ({"blocked": None}, None), ({"blocked": []}, [])):
            with self.store._connection(write=True) as db:
                db.execute("UPDATE native_workspaces SET state_json=? WHERE room_id=?", (json.dumps(state), self.room))
            self.assertEqual(self.store.snapshot(self.room, events=False)["native_workspaces"],
                             [{"provider": "claude", "state": "ready", "blocked": expected_blocked, "conflicts": []}])
        # Explicit null conflicts remain invalid, rather than silently becoming
        # a clean diagnostic list while missing conflicts still default to [].
        with self.store._connection(write=True) as db:
            db.execute("UPDATE native_workspaces SET state_json=? WHERE room_id=?",
                       (json.dumps({"conflicts": None}), self.room))
        with self.assertRaises(TypeError):
            self.store.snapshot(self.room, events=False)

    def test_large_and_small_room_polls_keep_bounded_decoded_histories(self):
        other = self.store.create_room("Small fixture", "claude", str(uuid4()))[0]
        def append(room, count):
            with self.store._connection(write=True) as db:
                for index in range(count):
                    self.store._event(db, room, "warning", sender="system", text=str(index))
        append(self.room, 120)
        append(other, 17)
        def timeline(room):
            with self.store._connection() as db:
                return self.store._timeline(db, room)
        with patch.object(RoomStore, "_TIMELINE_EVENTS", 60), \
                patch.dict(os.environ, {"BEANEST_VERIFY_EVENT_CACHE": "0"}):
            expected = timeline(self.room)
            timeline(other)
            with patch("local_agent_coordinator.room_store.json.loads", wraps=json.loads) as decode:
                self.assertEqual(timeline(self.room), expected)
                timeline(other)
            self.assertEqual(decode.call_count, 0, "alternating rooms re-decoded retained history")
            with self.store._connection(write=True) as db:
                seq = db.execute("SELECT MAX(seq) FROM events WHERE room_id=?", (self.room,)).fetchone()[0]
                db.execute("DELETE FROM events WHERE seq=?", (seq,))
            self.assertEqual(len(timeline(self.room)), len(expected) - 1)
            timeline(self.room)[0]["kind"] = "caller mutation"
            self.assertNotEqual(timeline(self.room)[0]["kind"], "caller mutation")
            third = self.store.create_room("Another large fixture", "claude", str(uuid4()))[0]
            append(third, 130)
            timeline(third)
            self.assertNotIn(self.room, self.store._timelines)
            self.assertIn(third, self.store._timelines)
            sizes = [len(item[0]) for item in self.store._timelines.values()]
            self.assertLessEqual(sum(size > 60 for size in sizes), 1)
            self.assertLessEqual(sum(size for size in sizes if size <= 60), 60)

    def test_event_page_uses_one_indexed_range_without_per_event_queries(self):
        with self.store._connection(write=True) as db:
            for index in range(1000):
                self.store._event(db, self.room, "warning" if index % 2 else "note",
                                  sender="system", text=str(index))
            latest = db.execute("SELECT MAX(seq) FROM events WHERE room_id=?", (self.room,)).fetchone()[0]
        queries = []
        connection = self.store._connection
        @contextmanager
        def traced_connection(**kwargs):
            with connection(**kwargs) as db:
                db.set_trace_callback(queries.append)
                yield db
        with patch.object(self.store, "_connection", traced_connection):
            result = self.store.read_events(self.room, through_seq=latest, limit=100, max_bytes=262144)
        self.assertEqual(len(result["events"]), 100)
        self.assertEqual(result["last_seq"], latest)
        self.assertTrue(result["has_before"])
        self.assertFalse(result["has_after"])
        selects = [query for query in queries if query.lstrip().upper().startswith("SELECT")]
        self.assertLessEqual(len(selects), 6, "event candidates each issued boundary queries")
        with connection() as db:
            plan = db.execute("EXPLAIN QUERY PLAN SELECT seq,value_json FROM events "
                              "WHERE room_id=? AND seq<=? ORDER BY seq DESC LIMIT ?",
                              (self.room, latest, 100)).fetchall()
        details = " ".join(row[3] for row in plan)
        self.assertIn("events_room_seq", details)
        self.assertNotIn("TEMP B-TREE", details)

    def test_page_boundaries_use_room_rows_inside_highwater_even_across_gaps(self):
        other = self.store.create_room("Unrelated room", "claude", str(uuid4()))[0]
        with self.store._connection(write=True) as db:
            first = db.execute("SELECT MIN(seq) FROM events WHERE room_id=?", (self.room,)).fetchone()[0]
            for room in (self.room, other, self.room):
                self.store._event(db, room, "warning", sender="system", text="gap fixture")
            room_seqs = [row[0] for row in db.execute("SELECT seq FROM events WHERE room_id=? ORDER BY seq", (self.room,))]
            through = room_seqs[-2]
            db.execute("DELETE FROM events WHERE room_id=? AND seq>? AND seq<?", (self.room, first, through))
        for cursors in ({"before_seq": through}, {"after_seq": first}, {"before_seq": through + 1},
                        {"after_seq": through}, {"before_seq": first}, {}):
            with self.subTest(cursors=cursors):
                result = self.store.read_events(self.room, through_seq=through, limit=100, **cursors)
                with self.store._connection() as db:
                    actual_rows = [row[0] for row in db.execute("SELECT seq FROM events WHERE room_id=? AND seq<=? ORDER BY seq",
                                                               (self.room, through))]
                first_result, last_result = result["first_seq"], result["last_seq"]
                before = first_result if first_result is not None else cursors.get("before_seq", cursors.get("after_seq", 0) + 1)
                after = last_result if last_result is not None else cursors.get("after_seq", cursors["before_seq"] - 1 if "before_seq" in cursors else through)
                self.assertEqual(result["has_before"], any(seq < before for seq in actual_rows))
                self.assertEqual(result["has_after"], any(seq > after for seq in actual_rows))


if __name__ == "__main__":
    unittest.main()
