"""The user withdraws unfinished tasks from a round. Real SQLite, job loop and private trees; only the native transport is synthetic."""
import asyncio
import base64
import io
import os
import unittest
import zipfile
from pathlib import Path
from uuid import uuid4

from local_agent_coordinator.room_service import RoomError
from tests import test_native_room_service as native
from test_team_store import proposal
from wait_support import poll_async, within

native_reply = native.native_reply


class TaskWithdrawal(unittest.IsolatedAsyncioTestCase):
    """Two partners on a fictional project; the plan author owns every task so the order of work is known."""
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    create = native.NativeRoomServiceTests.create

    # Each test sets the plan: (id, paths, dependencies).
    plan = [("T1", ["main.py"], [])]
    contents = {"main.py": "VALUE = 2\n", "notes.md": "Updated notes.\n", "extra.py": "EXTRA = 1\n", "more.py": "MORE = 1\n"}

    def handler(self, agent, context, cwd):
        return self.respond(agent, context, cwd)

    async def respond(self, agent, context, cwd):
        phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and agent == board["plan_author"]:
                for task_id, paths, dependencies in self.plan:
                    commands += [proposal(task_id, paths=paths, capability="python", dependencies=dependencies),
                                 {"type": "claim", "task_id": task_id}]
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            self.worked.append(task["id"])
            hold = self.holds.get(("work", task["id"]))
            if hold:
                hold[0].set()
                await hold[1].wait()
            for path in task["write_paths"]:
                (cwd / path).write_text(self.contents[path])
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Wrote " + ", ".join(task["write_paths"]) + "."}])
        if phase == "review":
            hold = self.holds.get(("review", task["id"]))
            if hold:
                hold[0].set()
                await hold[1].wait()
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact tree."}])
        if phase == "checkpoint":
            self.checkpoints.append(context)
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Done.", "next_goal": None,
                                           "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Compared.",
                                           "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    async def asyncSetUp(self):
        await native.NativeRoomServiceTests.asyncSetUp(self)
        self.fake.handler = self.handler
        native.no_rebalance(self.service)
        self.worked, self.checkpoints, self.holds = [], [], {}
        # Holding a partner before it claims leaves its next call queued, as when it is still busy elsewhere.
        self.gated = {}
        guard = self.service._guard_availability
        async def gated(members):
            for member in members:
                if member not in self.gated:
                    continue
                release, kinds = self.gated[member]
                with self.service.store._connection() as db:
                    queued = {row[0] for row in db.execute("SELECT kind FROM jobs WHERE provider=? AND status='queued'", (member,))}
                if queued & kinds:
                    await release.wait()
            return await guard(members)
        self.service._guard_availability = gated

    def gate(self, member, *kinds):
        release = asyncio.Event()
        self.gated[member] = (release, set(kinds))
        return release

    def hold(self, phase, task_id):
        started, release = asyncio.Event(), asyncio.Event()
        self.holds[(phase, task_id)] = (started, release)
        return started, release

    async def start(self):
        created = await self.create()
        return created["room"]["id"]

    def state(self, room_id):
        return self.service._snapshot(room_id)

    def task(self, state, task_id):
        return next((task for task in state["team"]["tasks"] if task["id"] == task_id), {})

    async def withdraw(self, room_id, task_id, **options):
        run_id = self.state(room_id)["current_run"]["id"]
        return await self.service.withdraw_task(room_id, str(uuid4()), run_id=run_id, task_id=task_id, **options)

    async def refused(self, code, room_id, task_id, **options):
        with self.assertRaises(RoomError) as caught:
            await self.withdraw(room_id, task_id, **options)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    async def finished(self, room_id):
        await self.settle()
        state = await self.service.read_room(room_id)
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        return state

    async def zip_names(self, room_id):
        revision = self.service._project(room_id).snapshot()["revision"]
        result = await self.service.export_room(room_id, revision)
        return set(zipfile.ZipFile(io.BytesIO(base64.b64decode(result["content_base64"]))).namelist())

    async def test_an_unstarted_task_is_withdrawn_and_the_round_still_delivers_without_it(self):
        # Catches a withdrawn task still being worked, counted as delivered, or keeping the round from finishing.
        self.plan = [("T1", ["main.py"], []), ("T2", ["notes.md"], []), ("T3", ["extra.py"], [])]
        started, release = self.hold("work", "T1")
        room_id = await self.start()
        await within(started.wait(), 5)
        busy = await self.refused("task_withdraw_busy", room_id, "T1")
        self.assertEqual(self.task(self.state(room_id), "T1")["status"], "working", "a refused withdrawal changes nothing")
        self.assertIsNone(getattr(busy, "details", None))
        state = await self.withdraw(room_id, "T3")
        self.assertEqual(self.task(state, "T3")["status"], "withdrawn")
        event = next(event for event in state["events"] if event["kind"] == "task_withdrawn")
        self.assertEqual((event["sender"], event["task_id"], event["title"]), ("user", "T3", "Deliver T3"))
        await self.refused("task_already_withdrawn", room_id, "T3")
        release.set()
        final = await self.finished(room_id)
        self.assertEqual(self.worked, ["T1", "T2"])
        self.assertEqual([task["status"] for task in final["team"]["tasks"]], ["accepted", "accepted", "withdrawn"])
        self.assertEqual(sorted(file["path"] for file in final["objective"]["delivery"]["files"]), ["main.py", "notes.md"])
        self.assertTrue(final["objective"]["delivery"]["verified"])
        self.assertNotIn("extra.py", {file["path"] for file in self.service._project(room_id).snapshot()["files"]})
        collaboration = final["collaboration"]
        self.assertEqual(collaboration["summary"], "共享任务 2/2 已交付并检查，1 项已撤掉")
        self.assertEqual(collaboration["outcome"]["remaining"], [])
        self.assertEqual(next(task for task in collaboration["tasks"] if task["id"] == "T3")["status"], "withdrawn")
        # The stage check was told the user withdrew it, so it does not plan it again.
        withdrawn = self.checkpoints[-1]["withdrawn_by_user"]
        self.assertEqual([task["id"] for task in withdrawn["tasks"]], ["T3"])
        self.assertIn("Do not do, redo, recreate or propose them again", withdrawn["instruction"])
        board = {task["id"]: task for task in self.checkpoints[-1]["team"]["tasks"]}
        self.assertTrue(board["T3"]["user_withdrawn"])
        self.assertNotIn("user_withdrawn", board["T1"])
        # A delivered task is not withdrawable.
        await self.refused("task_withdraw_finished", room_id, "T1")

    async def test_a_task_others_need_is_withdrawn_only_together_with_them(self):
        # Catches dependents being left waiting forever on a withdrawn task, or withdrawn without the user seeing them.
        self.plan = [("T4", ["more.py"], []), ("T1", ["main.py"], []), ("T2", ["notes.md"], ["T1"]), ("T3", ["extra.py"], ["T2"])]
        started, release = self.hold("work", "T4")
        room_id = await self.start()
        await within(started.wait(), 5)
        conflict = await self.refused("task_withdraw_dependents", room_id, "T1")
        self.assertEqual(conflict.details, {"task_id": "T1", "dependents": [{"id": "T2", "title": "Deliver T2"}, {"id": "T3", "title": "Deliver T3"}]})
        self.assertEqual({task["id"]: task["status"] for task in self.state(room_id)["team"]["tasks"]}["T1"], "assigned")
        state = await self.withdraw(room_id, "T1", include_dependents=True)
        self.assertEqual({task["id"]: task["status"] for task in state["team"]["tasks"]},
                         {"T4": "working", "T1": "withdrawn", "T2": "withdrawn", "T3": "withdrawn"})
        event = next(event for event in state["events"] if event["kind"] == "task_withdrawn")
        self.assertEqual([task["id"] for task in event["tasks"]], ["T1", "T2", "T3"])
        release.set()
        final = await self.finished(room_id)
        self.assertEqual(self.worked, ["T4"])
        self.assertEqual([file["path"] for file in final["objective"]["delivery"]["files"]], ["more.py"])

    async def test_a_queued_call_is_cancelled_and_withdrawing_everything_still_finishes_the_round(self):
        # Catches a cancelled call still running, and a round with nothing left hanging instead of being checked.
        self.plan = [("T1", ["main.py"], []), ("T2", ["notes.md"], [])]
        release = self.gate("claude", "team_work")
        room_id = await self.start()
        queued = await poll_async(lambda: [job for job in self.state(room_id)["jobs"] if job["kind"] == "team_work" and job["status"] == "queued"],
                                  timeout=5)
        self.assertEqual(len(queued), 1)
        await self.withdraw(room_id, "T1")
        await self.withdraw(room_id, "T2")
        state = self.state(room_id)
        job = next(job for job in state["jobs"] if job["id"] == queued[0]["id"])
        self.assertEqual((job["status"], job["error"]), ("cancelled", "task_withdrawn"))
        release.set()
        final = await self.finished(room_id)
        self.assertEqual(self.worked, [], "no work call ran for a withdrawn task")
        self.assertEqual({task["status"] for task in final["team"]["tasks"]}, {"withdrawn"})
        self.assertEqual(final["objective"]["delivery"]["files"], [])
        self.assertNotIn(final["room"]["status"], {"running", "needs_user"})
        self.assertEqual(len(self.checkpoints), 1, "the stage check still ran once")
        self.assertEqual([task["id"] for task in self.checkpoints[0]["withdrawn_by_user"]["tasks"]], ["T1", "T2"])
        self.assertEqual(final["collaboration"]["summary"], "共享任务 0/0 已交付并检查，2 项已撤掉")

    async def test_a_published_but_unreviewed_result_leaves_the_shared_project_and_the_zip(self):
        # Catches bytes nobody accepted staying in the shared project, the ZIP or a partner's tree after withdrawal.
        self.plan = [("T1", ["main.py", "extra.py"], []), ("T2", ["notes.md"], ["T1"]), ("T3", ["more.py"], [])]
        # The reviewer is held before it claims, so T1's bytes sit in the shared project waiting for review.
        release = self.gate("codex", "team_review")
        room_id = await self.start()
        await poll_async(lambda: self.task(self.state(room_id), "T1").get("status") == "review"
                         and any(job["kind"] == "team_review" and job["status"] == "queued" for job in self.state(room_id)["jobs"]), timeout=5)
        project = self.service._project(room_id)
        self.assertEqual({file["path"] for file in project.snapshot()["files"]}, {"main.py", "extra.py"})
        await self.withdraw(room_id, "T1", include_dependents=True)
        head = project.snapshot()
        self.assertEqual(head["files"], [], "the unreviewed change and the new file are gone from the shared project")
        self.assertEqual(project.files_for_paths(["main.py"])[0]["content"], "VALUE = 1\n")
        release.set()
        final = await self.finished(room_id)
        self.assertEqual([file["path"] for file in final["objective"]["delivery"]["files"]], ["more.py"])
        self.assertEqual(await self.zip_names(room_id), {"more.py", ".beanest-export.json"})
        restored = [event for event in final["events"] if event["kind"] == "task_withdrawal_restored"]
        self.assertEqual(sorted(restored[0]["paths"]), ["extra.py", "main.py"])
        # The author's private tree followed the shared project on its next call.
        tree = self.service.store.native_workspace(room_id, "claude")["project"]
        self.assertEqual(Path(tree + "/main.py").read_text(), "VALUE = 1\n")
        self.assertFalse(os.path.exists(tree + "/extra.py"))

    async def test_unsubmitted_private_bytes_of_a_withdrawn_task_are_dropped_before_anyone_can_submit_them(self):
        # Catches leftover partial work of a withdrawn task surviving in the owner's tree, where a later call would collect it.
        self.plan = [("T1", ["main.py"], []), ("T2", ["notes.md"], ["T1"])]
        started, release = self.hold("review", "T1")
        room_id = await self.start()
        await within(started.wait(), 5)
        tree = self.service.store.native_workspace(room_id, "claude")["project"]
        Path(tree, "notes.md").write_text("Half-written notes from an interrupted attempt.\n")
        Path(tree, "scratch-notes.md").write_text("unrelated\n")
        await self.withdraw(room_id, "T2")
        self.assertEqual(Path(tree + "/notes.md").read_text(), "Original notes.\n", "the owner was idle, so its tree was cleaned at once")
        self.assertEqual(Path(tree + "/scratch-notes.md").read_text(), "unrelated\n", "only the withdrawn task's paths are touched")
        os.remove(tree + "/scratch-notes.md")
        release.set()
        final = await self.finished(room_id)
        self.assertEqual([file["path"] for file in final["objective"]["delivery"]["files"]], ["main.py"])
        self.assertNotIn("notes.md", {file["path"] for file in self.service._project(room_id).snapshot()["files"]})
        discarded = [event for event in final["events"] if event["kind"] == "task_withdrawal_discarded"]
        self.assertEqual([(event["provider"], event["paths"]) for event in discarded], [("claude", ["notes.md"])])

    async def test_a_partner_busy_elsewhere_drops_the_bytes_before_its_next_call(self):
        # Catches cleanup running under a live call, or being forgotten once that call ends.
        self.plan = [("T1", ["main.py"], []), ("T2", ["notes.md"], [])]
        started, release = self.hold("work", "T1")
        room_id = await self.start()
        await within(started.wait(), 5)
        tree = self.service.store.native_workspace(room_id, "claude")["project"]
        await self.withdraw(room_id, "T2")
        self.assertFalse(any(event["kind"] == "task_withdrawal_discarded" for event in self.state(room_id)["events"]),
                         "nothing is touched while the owner's call runs")
        release.set()
        final = await self.finished(room_id)
        discarded = [event for event in final["events"] if event["kind"] == "task_withdrawal_discarded"]
        self.assertEqual([event["provider"] for event in discarded], ["claude"])
        self.assertEqual(Path(tree + "/notes.md").read_text(), "Original notes.\n")

    async def test_withdrawing_the_task_that_asked_the_user_lets_the_round_go_on(self):
        # Catches the room waiting forever on a question that only the withdrawn task raised.
        self.plan = [("T1", ["main.py"], []), ("T2", ["notes.md"], [])]
        respond = self.respond
        async def asking(agent, context, cwd):
            if context["job"]["job_phase"] == "work" and context["task"]["id"] == "T2":
                return native_reply(context, [{"type": "block", "task_id": "T2", "reason": "Which tone should the notes use?",
                                               "cause": "user_decision"}])
            return await respond(agent, context, cwd)
        self.fake.handler = asking
        room_id = await self.start()
        await self.settle()
        waiting = await self.service.read_room(room_id)
        self.assertEqual(waiting["objective"]["status"], "needs_user", waiting["team"])
        self.assertEqual(self.task(waiting, "T2")["status"], "blocked")
        await self.withdraw(room_id, "T2")
        final = await self.finished(room_id)
        self.assertEqual([task["status"] for task in final["team"]["tasks"]], ["accepted", "withdrawn"])
        self.assertEqual([file["path"] for file in final["objective"]["delivery"]["files"]], ["main.py"])

    async def test_a_call_with_an_unconfirmed_outcome_must_be_recovered_before_its_task_is_withdrawn(self):
        # Catches a withdrawal removing the task a pending recovery needs, which left the room unable to continue.
        self.plan = [("T1", ["main.py"], [])]
        async def failing(agent, context, cwd):
            if context["job"]["job_phase"] == "work":
                (cwd / "main.py").write_text("VALUE = 2\n")
                self.fake.failure = {"status": "failed", "failure_code": "cli_nonzero_exit", "native_execution": {
                    "permission": "full_access", "tool_calls": 1, "tool_results": 1, "cleanup_confirmed": True,
                    "side_effects_possible": True, "exit_code": 1, "duration_seconds": 0.01}}
                return native_reply(context)
            return await self.respond(agent, context, cwd)
        self.fake.handler = failing
        room_id = await self.start()
        await self.settle()
        self.fake.failure = None
        stopped = await self.service.read_room(room_id)
        self.assertEqual(stopped["team"]["phase"], "unknown")
        await self.refused("task_withdraw_unconfirmed", room_id, "T1")
        after = await self.service.read_room(room_id)
        self.assertEqual(self.task(after, "T1")["status"], self.task(stopped, "T1")["status"])
        self.assertTrue(after["continuation"]["can_send"], "the room can still be continued and recovered")

    async def test_a_repeated_request_is_answered_once(self):
        # Catches a retried click being read as a second withdrawal (and rejected) or recorded twice.
        self.plan = [("T1", ["main.py"], []), ("T2", ["notes.md"], [])]
        started, release = self.hold("work", "T1")
        room_id = await self.start()
        await within(started.wait(), 5)
        run_id = self.state(room_id)["current_run"]["id"]
        request = str(uuid4())
        for _ in range(2):
            await self.service.withdraw_task(room_id, request, run_id=run_id, task_id="T2")
        self.assertEqual(sum(event["kind"] == "task_withdrawn" for event in self.state(room_id)["events"]), 1)
        with self.assertRaises(RoomError) as caught:
            await self.service.withdraw_task(room_id, str(uuid4()), run_id=str(uuid4()), task_id="T1")
        self.assertEqual(caught.exception.code, "team_run_conflict", "only the current round's tasks can be withdrawn")
        await self.refused("team_task_not_found", room_id, "T9")
        release.set()
        await self.finished(room_id)


if __name__ == "__main__":
    unittest.main()
