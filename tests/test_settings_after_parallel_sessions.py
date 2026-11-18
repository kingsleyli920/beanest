"""A room that has used a parallel session still starts its next round with the settings it had.

Fictional project, real SQLite and the real job loop; only the native transport
is synthetic (see test_task_pool).
"""
import json
import unittest
from uuid import uuid4

from local_agent_coordinator.runtime_settings import normalized_settings
from tests import test_parallel_sessions as parallel
from tests import test_task_pool as pool


class NextRoundAfterParallelSessions(parallel.ParallelSessions):
    # The parallel-session cases run in test_parallel_sessions; here only the case below.
    locals().update({name: None for name in dir(parallel.ParallelSessions) if name.startswith("test_")})

    async def next_round(self, text, source):
        run = (await self.state())["current_run"]
        await self.service.start_run(self.room_id, text, "all", str(uuid4()), run_id=run["id"], revision=run["revision"],
                                     digest=run["digest"], source=source)
        await self.settle()
        return await self.state()

    def stored_members(self):
        with self.service.store._connection() as db:
            row = db.execute("SELECT runtime_settings_json FROM rooms WHERE id=?", (self.room_id,)).fetchone()
        return sorted(json.loads(row[0])["members"])

    async def test_a_round_started_from_outside_inherits_the_settings_after_a_parallel_session_was_used(self):
        # Catches the real failure: once a partner had worked in a parallel session, the next round
        # stored that session among the per-member choices, and every later round that inherited the
        # settings (a follow-up sent through the local MCP entry) was refused as invalid_runtime_settings.
        self.plan = [pool.plan("T1"), pool.plan("T2"), pool.plan("T3")]
        self.hold_until_started(3)
        await self.create(["claude", "codex"])
        await self.finished()
        with self.service.store._connection() as db:
            members = [row[0] for row in db.execute("SELECT provider FROM members WHERE room_id=? ORDER BY rowid", (self.room_id,))]
        # At least the session that took the third task; a review may have used one too.
        self.assertEqual(members[:2], ["claude", "codex"])
        self.assertTrue(len(members) >= 3 and all("~x" in member for member in members[2:]), members)
        self.holds.clear()
        self.plan = [pool.plan("U1")]
        second = await self.next_round("Add the next small part.", "user")
        self.assertEqual(second["team"]["phase"], "ready_for_user", second["team"])
        self.assertEqual(self.stored_members(), ["claude", "codex"])
        self.plan = [pool.plan("V1")]
        third = await self.next_round("And one more, sent from outside.", "mcp")
        self.assertEqual(third["team"]["phase"], "ready_for_user", third["team"])
        self.assertEqual([task["status"] for task in third["team"]["tasks"]], ["accepted"])
        self.assertEqual(third["room"]["runtime_settings"]["permission"], "full_access")


class StoredSettings(unittest.TestCase):
    def test_settings_stored_with_a_parallel_session_among_the_members_are_read_without_it(self):
        # Catches a room already in that state staying unusable: its stored settings are accepted and
        # the parallel session's entry is dropped, with the partners' own choices kept.
        stored = {"permission": "full_access", "effective_permission": "full_access",
                  "members": {"claude": {"model": "example", "effort": "high"}, "codex": {"model": None, "effort": None},
                              "claude~x2": {"model": None, "effort": None}}}
        self.assertEqual(normalized_settings(stored, ["claude", "codex"], "build"),
                         {"permission": "full_access", "effective_permission": "full_access",
                          "members": {"claude": {"model": "example", "effort": "high"}, "codex": {"model": None, "effort": None}}})
        self.assertEqual(sorted(normalized_settings(stored, ["claude", "codex", "claude~x2"], "build")["members"]), ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
