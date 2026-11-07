"""Queries run on every 3-second UI poll must use an index, never scan all events."""
from pathlib import Path
import tempfile
import unittest

from local_agent_coordinator.room_store import RoomStore


class PollIndexTests(unittest.TestCase):
    def test_partner_monitor_and_room_event_filters_search_an_index(self):
        # Catches: the partners endpoint scanned every event of every room per
        # partner per poll (about 15-40 ms each on a 20k-event database).
        with tempfile.TemporaryDirectory() as directory:
            store = RoomStore(Path(directory) / "rooms.sqlite3")
            queries = [
                ("SELECT value_json FROM events WHERE json_extract(value_json,'$.sender')=? ORDER BY seq DESC LIMIT 1", ("codex",)),
                ("SELECT value_json FROM events WHERE json_extract(value_json,'$.sender')=? "
                 "AND json_extract(value_json,'$.kind')='usage' ORDER BY seq DESC LIMIT 1", ("codex",)),
                ("SELECT value_json FROM events WHERE room_id=? AND json_extract(value_json,'$.kind') "
                 "IN ('job_claimed','job_cleanup') ORDER BY seq", ("room",)),
                ("SELECT value_json FROM events WHERE json_extract(value_json,'$.job_id')=?", ("job",)),
                # The room list's timeline-free snapshot reads only inspection events.
                ("SELECT json_extract(value_json,'$.job_id') FROM events WHERE room_id=? "
                 "AND json_extract(value_json,'$.kind')='partner_handoff_inspection'", ("room",)),
            ]
            with store._connection() as db:
                for sql, parameters in queries:
                    with self.subTest(sql=sql):
                        plan = " ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters))
                        self.assertIn("USING INDEX", plan)
                        self.assertNotIn("SCAN events", plan)


if __name__ == "__main__":
    unittest.main()
