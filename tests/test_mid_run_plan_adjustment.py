"""The plan author changes unstarted work mid-run without stopping work in progress.

Fictional projects and partner mixes; the store tests use real SQLite and the
service test drives the real job loop with only the native transport synthetic.
"""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError, RoomStore
from tests import test_native_room_service as native
from tests import test_task_rebalance as rebalance
from test_team_store import proposal

native_reply = native.native_reply


def adjust(task_id, dependencies, paths):
    return {"type": "adjust", "task_id": task_id, "dependencies": list(dependencies), "write_paths": list(paths)}


class MidRunStore(unittest.TestCase):
    """A three-partner board: L1 running, L2 waits on L1, D1 waits on L1 without needing it, I1 waits on L2."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RoomStore(Path(self.directory.name) / "rooms.sqlite3", clock=lambda: 1000.0,
                               agents={"claude": "Claude", "codex": "Codex", "kimi": "Kimi"})
        # Planned owners stay put, so each case knows who holds what.
        self.store.task_capabilities = lambda provider: ()
        self.room_id = self.store.create_room("Ship the report tool", "claude", str(uuid4()), collaboration=True)[0]
        plans = {provider: self.store.claim(self.room_id, provider) for provider in ("claude", "codex", "kimi")}
        self.finish(plans["claude"], [proposal("L1", paths=["load.py"]),
                                      proposal("L2", dependencies=["L1"], paths=["core.py", "side.py"]),
                                      proposal("D1", dependencies=["L1"], paths=["docs.md"]),
                                      proposal("I1", dependencies=["L2"], paths=["main.py"]),
                                      {"type": "claim", "task_id": "D1"}, {"type": "claim", "task_id": "I1"}, {"type": "plan_ready"}])
        self.finish(plans["codex"], [{"type": "claim", "task_id": "L2"}, {"type": "plan_ready"}])
        self.finish(plans["kimi"], [{"type": "claim", "task_id": "L1"}, {"type": "plan_ready"}])
        for _ in range(6):
            if self.snapshot()["team"]["phase"] == "working":
                break
            for provider in ("claude", "codex", "kimi"):
                job = self.store.claim(self.room_id, provider)
                if job and job["kind"] in {"team_coordinate", "team_plan"}:
                    self.finish(job, [{"type": "plan_ready"}])
        self.assertEqual(self.snapshot()["team"]["phase"], "working")
        self.l1_work = self.store.claim(self.room_id, "kimi")
        self.assertEqual((self.l1_work["kind"], self.l1_work["team"]["task_id"]), ("team_work", "L1"))
        self.revision = self.snapshot()["team"]["revision"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def snapshot(self):
        return self.store.snapshot(self.room_id)

    def tasks(self):
        return {task["id"]: task for task in self.snapshot()["team"]["tasks"]}

    def finish(self, job, commands):
        self.assertTrue(self.store.prevalidate_team_result(job["id"], commands))
        self.store.finish_job(job["id"], message="Concrete response", team_commands=commands)

    def user_reply_job(self, provider):
        self.assertTrue(self.store.send_message(self.room_id, "Split off the part that does not need L1.", provider, str(uuid4())))
        job = self.store.claim(self.room_id, provider)
        self.assertEqual(job["kind"], "team_user")
        return job

    def refused(self, job, commands, code):
        before = self.tasks()
        with self.assertRaises(RoomError) as error:
            self.store.prevalidate_team_result(job["id"], commands)
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.tasks(), before, "a refused reply changes nothing")

    def split(self):
        return [proposal("L2a", paths=["side.py"]), adjust("L2", ["L1"], ["core.py"]), adjust("D1", [], ["docs.md"]),
                adjust("I1", ["L2", "L2a"], ["main.py"])]

    def test_the_author_splits_and_drops_a_dependency_while_l1_keeps_running(self):
        # Catches a mid-run reply being locked out, the revision bump forcing re-confirmation, or the split part never offered.
        job = self.user_reply_job("claude")
        self.finish(job, self.split())
        tasks = self.tasks()
        self.assertEqual((tasks["L2a"]["status"], tasks["L2a"]["owner"], tasks["L2a"]["write_paths"]), ("open", None, ["side.py"]))
        self.assertEqual((tasks["L2"]["dependencies"], tasks["L2"]["write_paths"]), (["L1"], ["core.py"]))
        self.assertEqual(tasks["D1"]["dependencies"], [])
        self.assertEqual(tasks["I1"]["dependencies"], ["L2", "L2a"])
        self.assertEqual(tasks["L1"]["status"], "working", "the running task is untouched")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["team"]["revision"], self.revision)
        self.assertTrue(all(member["plan_ready"] for member in snapshot["team"]["members"]))
        self.assertEqual(next(job for job in snapshot["jobs"] if job["id"] == self.l1_work["id"])["status"], "working")
        event = next(event for event in snapshot["events"] if event["kind"] == "plan_adjusted")
        self.assertEqual((event["sender"], [item["task_id"] for item in event["added"]]), ("claude", ["L2a"]))
        self.assertEqual({item["task_id"]: (item["previous_dependencies"], item["dependencies"]) for item in event["adjusted"]},
                         {"L2": (["L1"], ["L1"]), "D1": (["L1"], []), "I1": (["L2"], ["L2", "L2a"])})
        self.assertEqual(next(item for item in event["adjusted"] if item["task_id"] == "L2")["previous_write_paths"], ["core.py", "side.py"])
        # The author now has D1 to do; codex only waits on L1, so it is the idle partner offered L2a.
        self.assertEqual(self.store.claim(self.room_id, "claude")["team"]["task_id"], "D1")
        offer = self.store.claim(self.room_id, "codex")
        self.assertEqual(offer["kind"], "team_coordinate")
        self.assertIn('Unclaimed task IDs: ["L2a"]', offer["text"])
        self.finish(offer, [{"type": "claim", "task_id": "L2a"}, {"type": "plan_ready"}])
        work = self.store.claim(self.room_id, "codex")
        self.assertEqual((work["kind"], work["team"]["task_id"]), ("team_work", "L2a"))
        self.assertEqual(self.tasks()["L1"]["status"], "working")

    def test_a_partner_that_is_not_the_plan_author_cannot_change_the_plan(self):
        # Catches the mid-run exception being granted to whoever answers the user.
        job = self.user_reply_job("codex")
        self.refused(job, [proposal("L2a", paths=["side.py"])], "team_plan_locked")
        self.refused(job, [adjust("L2", [], ["core.py", "side.py"])], "team_plan_locked")

    def test_a_started_or_reviewed_task_cannot_be_adjusted(self):
        # Catches a dependency or path being changed under a running call or a delivered result.
        job = self.user_reply_job("claude")
        self.refused(job, [adjust("L1", [], ["load.py"])], "team_plan_locked")
        self.finish(self.l1_work, [{"type": "complete", "task_id": "L1", "result": "Loaded."}])
        self.assertEqual(self.tasks()["L1"]["status"], "review")
        self.refused(job, [adjust("L1", [], ["load.py"])], "team_plan_locked")

    def test_a_task_with_a_queued_work_call_cannot_be_adjusted(self):
        # Catches a queued call being treated as unstarted: it is the owner's next step on the old binding.
        job = self.user_reply_job("claude")
        self.finish(job, [adjust("D1", [], ["docs.md"])])
        self.assertEqual(self.tasks()["D1"]["status"], "assigned")
        queued = [item for item in self.snapshot()["jobs"] if item["kind"] == "team_work" and item["status"] == "queued"]
        self.assertEqual([item["provider"] for item in queued], ["claude"])
        # A second reply from the author while that call is still queued; the busy gate would normally hold it back.
        team = self.store._team
        with self.store._connection(write=True) as db, patch.object(team, "_busy", return_value=False):
            self.assertTrue(team._queue(db, self.room_id, "claude", "user", text="Another note."))
            second = db.execute("SELECT j.id FROM jobs j JOIN team_jobs t ON t.job_id=j.id WHERE t.phase='user' "
                                "AND j.provider='claude' AND j.status='queued'").fetchone()[0]
            db.execute("UPDATE jobs SET status='working' WHERE id=?", (second,))
        self.refused({"id": second}, [adjust("D1", ["L1"], ["docs.md"])], "team_plan_locked")

    def test_a_cycle_refuses_the_whole_reply(self):
        # Catches a valid proposal being kept while the adjustment that closes a loop is dropped.
        job = self.user_reply_job("claude")
        self.refused(job, [proposal("L2a", paths=["extra.py"]), adjust("L2", ["L1", "I1"], ["core.py", "side.py"])],
                     "invalid_team_dependencies")

    def test_a_split_that_leaves_the_path_behind_is_refused_whole(self):
        # Catches two unrelated tasks both owning a file because the split forgot to move it.
        job = self.user_reply_job("claude")
        self.refused(job, [proposal("L2a", paths=["side.py"]), adjust("D1", [], ["docs.md"])], "team_write_path_conflict")

    def test_an_adjustment_cannot_add_a_path(self):
        # Catches adjust being used to grant an unstarted task files it never had.
        job = self.user_reply_job("claude")
        self.refused(job, [adjust("D1", [], ["docs.md", "core.py"])], "invalid_team_task")


class MidRunService(unittest.IsolatedAsyncioTestCase):
    """A fictional project with three partners on three tools, through real storage and the job loop."""
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    propose = rebalance.RebalanceService.propose
    hold_task = rebalance.RebalanceService.hold_task

    async def asyncSetUp(self):
        with patch.object(native, "default_registry", rebalance.registry):
            await native.NativeRoomServiceTests.asyncSetUp(self)
        self.fake.handler = self.handler
        self.plan = [rebalance.plan("L1", "ember"),
                     rebalance.plan("L2", "codex", dependencies=["L1"], paths=["l2_core.py", "l2_side.py"]),
                     rebalance.plan("D1", "claude", dependencies=["L1"]),
                     rebalance.plan("I1", "claude", dependencies=["L2"])]
        self.profiles = {}
        self.service.store.account_workloads = lambda: self.profiles
        self.before_plan = None
        self.blocked_once = set()
        self.hold_coordinate = None
        self.user_replies = []

    async def handler(self, agent, context, cwd):
        if context["job"]["job_phase"] != "user":
            return await rebalance.RebalanceService.handler(self, agent, context, cwd)
        self.user_replies.append(context["self"])
        if context["self"] != context["team"]["plan_author"]:
            return native_reply(context, [])
        # The codex partner takes the split part when it is offered.
        self.plan.append(rebalance.plan("L2a", "codex", paths=["l2_side.py"]))
        return native_reply(context, [
            native.proposal("L2a", paths=["l2_side.py"], capability="python", dependencies=[]),
            adjust("L2", ["L1"], ["l2_core.py"]), adjust("D1", [], ["D1.py"]), adjust("I1", ["L2", "L2a"], ["I1.py"])])

    async def test_a_split_runs_while_the_long_task_keeps_working(self):
        # Catches a mid-run split waiting for in-flight work: the new task and the freed task must run while L1 is held,
        # and L1 must never be cancelled, replanned or restarted.
        room, seen = {}, {}

        async def until():
            state = await self.service.read_room(room["id"])
            status = {task["id"]: task["status"] for task in state["team"]["tasks"]}
            if status.get("L2a") == "accepted" and status.get("D1") == "accepted":
                seen["while_l1_working"] = status["L1"]
                return True
            return False
        self.hold_task("L1", until=until)
        created = await self.create(participants=["claude", "codex", "ember"])
        room["id"] = created["room"]["id"]
        await asyncio.wait_for(self.entered.wait(), 5)
        for _ in range(300):
            state = await self.service.read_room(room["id"])
            if not any(job["provider"] in {"claude", "codex"} and job["status"] in {"queued", "working"} for job in state["jobs"]):
                break
            await asyncio.sleep(0.01)
        revision = state["team"]["revision"]
        self.assertEqual({task["id"]: task["owner"] for task in state["team"]["tasks"]},
                         {"L1": "ember", "L2": "codex", "D1": "claude", "I1": "claude"})
        await self.service.send_message(room["id"], "Half of L2 does not need L1; split it off so it can start now.",
                                        "claude", str(uuid4()), source="user")
        await self.settle()
        state = await self.service.read_room(room["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(seen.get("while_l1_working"), "working", "L2a and D1 were delivered while L1 was still running")
        tasks = {task["id"]: task for task in state["team"]["tasks"]}
        self.assertEqual({key: task["status"] for key, task in tasks.items()}, dict.fromkeys(("L1", "L2", "D1", "I1", "L2a"), "accepted"))
        self.assertEqual((tasks["L2a"]["owner"], tasks["L2a"]["write_paths"]), ("codex", ["l2_side.py"]))
        self.assertEqual((tasks["L2"]["dependencies"], tasks["L2"]["write_paths"]), (["L1"], ["l2_core.py"]))
        self.assertEqual((tasks["D1"]["dependencies"], tasks["I1"]["dependencies"]), ([], ["L2", "L2a"]))
        self.assertEqual(state["team"]["revision"], revision, "nobody had to re-confirm")
        work = [call["context"]["task"]["id"] for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work"]
        self.assertEqual(work.count("L1"), 1, "the held task was never restarted")
        self.assertLess(work.index("L2"), work.index("I1"))
        l1_jobs = [job for job in state["jobs"] if job["kind"] == "team_work" and job["status"] == "cancelled"]
        self.assertEqual(l1_jobs, [])
        kinds = {event["kind"] for event in state["events"]}
        self.assertFalse({"internal_replan_requested", "internal_replan_started"} & kinds)
        self.assertNotIn("native_response_rejected", kinds)
        event = next(event for event in state["events"] if event["kind"] == "plan_adjusted")
        self.assertEqual((event["sender"], [item["task_id"] for item in event["added"]]), ("claude", ["L2a"]))
        # The partner offered the split part sees what changed and why the board looks different.
        offer = next(call for call in self.fake.calls if call["agent"] == "codex" and call["context"]["job"]["job_phase"] == "coordinate"
                     and "L2a" in call["context"]["job"]["text"])
        self.assertEqual(offer["context"]["plan_adjustments"][0]["by"], "claude")
        # The plan author's reply was told it may do this.
        reply = next(call for call in self.fake.calls if call["context"]["job"]["job_phase"] == "user")
        self.assertIn("adjust{type,task_id,dependencies,write_paths}", reply["prompt"])


if __name__ == "__main__":
    unittest.main()
