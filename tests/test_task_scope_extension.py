"""A partner working a task takes files no unfinished task holds into it and keeps working.

Fictional projects and partner mixes. The store tests use real SQLite; the
service tests drive the real job loop and private native trees with only the
native transport synthetic.
"""
import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError, RoomStore
from tests import test_native_room_service as native
from test_team_store import proposal

native_reply = native.native_reply


def extend(task_id, paths):
    return {"type": "extend", "task_id": task_id, "write_paths": list(paths)}


def complete(task_id):
    return {"type": "complete", "task_id": task_id, "result": "Delivered with the extra file."}


class ScopeExtensionStore(unittest.TestCase):
    """T0 (codex, base.py) and T1 (claude, a.py) run now; T2 (kimi, b.py) waits on T0."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RoomStore(Path(self.directory.name) / "rooms.sqlite3", clock=lambda: 1000.0,
                               agents={"claude": "Claude", "codex": "Codex", "kimi": "Kimi"})
        self.store.task_capabilities = lambda provider: ()  # planned owners stay put
        self.room_id = self.store.create_room("Ship the ledger tool", "claude", str(uuid4()), collaboration=True)[0]
        plans = {provider: self.store.claim(self.room_id, provider) for provider in ("claude", "codex", "kimi")}
        self.finish(plans["claude"], [proposal("T0", paths=["base.py"]), proposal("T1", paths=["a.py"]),
                                      proposal("T2", dependencies=["T0"], paths=["b.py"]),
                                      {"type": "claim", "task_id": "T1"}, {"type": "plan_ready"}])
        self.finish(plans["codex"], [{"type": "claim", "task_id": "T0"}, {"type": "plan_ready"}])
        self.finish(plans["kimi"], [{"type": "claim", "task_id": "T2"}, {"type": "plan_ready"}])
        for _ in range(6):
            if self.store.snapshot(self.room_id)["team"]["phase"] == "working":
                break
            for provider in ("claude", "codex", "kimi"):
                job = self.store.claim(self.room_id, provider)
                if job and job["kind"] in {"team_coordinate", "team_plan"}:
                    self.finish(job, [{"type": "plan_ready"}])
        self.t1_work = self.store.claim(self.room_id, "claude")
        self.t0_work = self.store.claim(self.room_id, "codex")
        self.assertEqual((self.t1_work["team"]["task_id"], self.t0_work["team"]["task_id"]), ("T1", "T0"))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def tasks(self):
        return {task["id"]: task for task in self.store.snapshot(self.room_id)["team"]["tasks"]}

    def events(self, kind):
        return [event for event in self.store.snapshot(self.room_id)["events"] if event["kind"] == kind]

    def finish(self, job, commands):
        self.assertTrue(self.store.prevalidate_team_result(job["id"], commands))
        self.store.finish_job(job["id"], message="Concrete response", team_commands=commands)

    def refused(self, job, commands, code):
        before = self.tasks()
        with self.assertRaises(RoomError) as error:
            self.store.prevalidate_team_result(job["id"], commands)
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.tasks(), before, "a refused reply changes nothing")

    def accept_t0(self):
        self.finish(self.t0_work, [complete("T0")])
        review = self.store.claim(self.room_id, "kimi")
        self.assertEqual((review["team"]["job_phase"], review["team"]["task_id"]), ("review", "T0"))
        self.finish(review, [{"type": "review", "task_id": "T0", "verdict": "accept", "reason": "Read the result."}])
        self.assertEqual(self.tasks()["T0"]["status"], "accepted")

    def test_the_owner_takes_a_file_no_task_holds_and_keeps_working(self):
        # Catches the old dead end: a needed file outside the task forced a stop and a replan.
        self.finish(self.t1_work, [extend("T1", ["shared/util.py"]), complete("T1")])
        task = self.tasks()["T1"]
        self.assertEqual((task["status"], task["write_paths"], task["dependencies"]), ("review", ["a.py", "shared/util.py"], []))
        [event] = self.events("task_scope_extended")
        self.assertEqual({key: event[key] for key in ("sender", "task_id", "paths", "taken_from", "dependencies_added")},
                         {"sender": "claude", "task_id": "T1", "paths": ["shared/util.py"], "taken_from": [], "dependencies_added": []})
        self.assertEqual(self.store.snapshot(self.room_id)["team"]["phase"], "working")
        self.assertEqual([event for event in self.store.snapshot(self.room_id)["events"]
                          if event["kind"].startswith("internal_replan")], [])

    def test_a_file_an_unfinished_task_holds_is_refused_whole(self):
        # Catches two tasks that are both still open writing the same file, ignoring case too.
        self.refused(self.t1_work, [extend("T1", ["b.py"]), complete("T1")], "team_extend_path_held")
        self.refused(self.t1_work, [extend("T1", ["shared/util.py", "B.PY"]), complete("T1")], "team_extend_path_held")
        # A task under review has not finished either.
        self.finish(self.t0_work, [complete("T0")])
        self.assertEqual(self.tasks()["T0"]["status"], "review")
        self.refused(self.t1_work, [extend("T1", ["base.py"]), complete("T1")], "team_extend_path_held")
        self.assertEqual(self.events("task_scope_extended"), [])

    def test_a_finished_tasks_file_may_be_taken_and_the_two_stay_ordered(self):
        # Catches a finished task's file staying locked, or the taken path leaving two unordered writers
        # that would make every later reply in the room fail the path-conflict check.
        self.accept_t0()
        self.finish(self.t1_work, [extend("T1", ["base.py"]), complete("T1")])
        task = self.tasks()["T1"]
        self.assertEqual((task["write_paths"], task["dependencies"]), (["a.py", "base.py"], ["T0"]))
        [event] = self.events("task_scope_extended")
        self.assertEqual((event["taken_from"], event["dependencies_added"]), ([{"path": "base.py", "task_ids": ["T0"]}], ["T0"]))
        later = self.store.claim(self.room_id, "kimi")
        self.assertEqual(later["team"]["task_id"], "T2")
        self.assertTrue(self.store.prevalidate_team_result(later["id"], [complete("T2")]))

    def test_only_the_owners_own_work_call_may_extend(self):
        # Catches a peer, a reviewer or a user reply widening a task it does not work on.
        self.refused(self.t0_work, [extend("T1", ["x.py"]), complete("T0")], "team_task_permission")
        self.finish(self.t0_work, [complete("T0")])
        review = self.store.claim(self.room_id, "kimi")
        self.assertEqual(review["team"]["job_phase"], "review")
        self.refused(review, [extend("T0", ["x.py"]),
                              {"type": "review", "task_id": "T0", "verdict": "accept", "reason": "Read it."}], "team_task_permission")
        self.assertTrue(self.store.send_message(self.room_id, "Please also touch x.py.", "codex", str(uuid4())))
        reply = self.store.claim(self.room_id, "codex")
        self.assertEqual(reply["kind"], "team_user")
        self.refused(reply, [extend("T0", ["x.py"])], "team_task_permission")


class ScopeExtensionService(unittest.IsolatedAsyncioTestCase):
    """A fictional two-file project; claude works T1 (main.py), codex works T2 (notes.md) after it."""
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        await native.NativeRoomServiceTests.asyncSetUp(self)
        native.no_rebalance(self.service)
        self.fake.handler = self.handler
        self.plan = [("T1", "claude", ["main.py"], []), ("T2", "codex", ["notes.md"], ["T1"])]
        self.work = {}

    def handler(self, agent, context, cwd):
        phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and context["self"] == board["plan_author"]:
                for task_id, owner, paths, dependencies in self.plan:
                    commands.append(native.proposal(task_id, paths=paths, capability="python", dependencies=dependencies))
                    if owner == context["self"]:
                        commands.append({"type": "claim", "task_id": task_id})
            for item in board["tasks"]:
                planned = next((value for value in self.plan if value[0] == item["id"]), None)
                if planned and planned[1] == context["self"] and item["owner"] is None:
                    commands.append({"type": "claim", "task_id": item["id"]})
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            self.work.setdefault(task["id"], []).append(context)
            return self.do_work(task, cwd, len(self.work[task["id"]]), context)
        if phase == "review":
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact tree."}])
        if phase == "checkpoint":
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Done.", "next_goal": None,
                                           "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Compared.",
                                           "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    def do_work(self, task, cwd, attempt, context):
        for path in task["write_paths"]:
            (cwd / path).write_text(f"# {task['id']} attempt {attempt}\n")
        return native_reply(context, [complete(task["id"])])

    async def run_room(self):
        created = await self.create()
        await self.settle()
        state = await self.service.read_room(created["room"]["id"])
        return created["room"]["id"], state

    def canonical(self, room_id, path):
        [entry] = self.service._project(room_id).metadata_for_paths([path])
        return entry["owner"], entry["sha256"]

    async def test_a_work_call_edits_an_unowned_file_extends_and_is_accepted_without_replanning(self):
        # Catches the extension landing after the import (the new file is then refused as unowned and the
        # room stops or replans), or the file being left out of the reviewed result.
        def work(task, cwd, attempt, context):
            if task["id"] == "T1":
                (cwd / "main.py").write_text("VALUE = 2\n")
                (cwd / "util.py").write_text("def helper():\n    return 2\n")
                return native_reply(context, [extend("T1", ["util.py"]), complete("T1")])
            return ScopeExtensionService.do_work(self, task, cwd, attempt, context)
        self.do_work = work
        room_id, state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        tasks = {task["id"]: task for task in state["team"]["tasks"]}
        self.assertEqual((tasks["T1"]["status"], tasks["T1"]["write_paths"]), ("accepted", ["main.py", "util.py"]))
        self.assertEqual(sorted(file["path"] for file in tasks["T1"]["result_files"]), ["main.py", "util.py"])
        self.assertEqual(self.canonical(room_id, "util.py"),
                         ("claude", hashlib.sha256(b"def helper():\n    return 2\n").hexdigest()))
        self.assertEqual(len(self.work["T1"]), 1, "accepted on the first reply, no correction call")
        kinds = [event["kind"] for event in state["events"]]
        self.assertEqual([kind for kind in kinds if kind.startswith("internal_replan")], [])
        self.assertNotIn("native_response_rejected", kinds)
        [event] = [event for event in state["events"] if event["kind"] == "task_scope_extended"]
        self.assertEqual((event["sender"], event["task_id"], event["paths"]), ("claude", "T1", ["util.py"]))
        self.assertIn("extend{type,task_id,write_paths:[the new paths]}", self.fake.calls[0]["prompt"])

    async def test_a_file_another_unfinished_task_holds_is_sent_back_for_correction(self):
        # Catches the refusal stopping the room (the edit to the held file read as an unknown side effect)
        # instead of the partner restoring it and finishing within its scope.
        def work(task, cwd, attempt, context):
            if task["id"] == "T1" and attempt == 1:
                (cwd / "main.py").write_text("VALUE = 2\n")
                (cwd / "notes.md").write_text("Rewritten by T1.\n")
                return native_reply(context, [extend("T1", ["notes.md"]), complete("T1")])
            if task["id"] == "T1":
                (cwd / "notes.md").write_text("Original notes.\n")
                return native_reply(context, [complete("T1")])
            return ScopeExtensionService.do_work(self, task, cwd, attempt, context)
        self.do_work = work
        room_id, state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        tasks = {task["id"]: task for task in state["team"]["tasks"]}
        self.assertEqual((tasks["T1"]["write_paths"], tasks["T2"]["status"]), (["main.py"], "accepted"))
        self.assertEqual(len(self.work["T1"]), 2)
        self.assertIn("team_extend_path_held", self.work["T1"][1]["job"]["text"])
        self.assertIn("Restore that file to its shared version", self.work["T1"][1]["job"]["text"])
        rejected = [event for event in state["events"] if event["kind"] == "native_response_rejected"]
        self.assertEqual([event["evidence"]["code"] for event in rejected], ["team_extend_path_held"])
        self.assertEqual([event for event in state["events"] if event["kind"] == "task_scope_extended"], [])
        self.assertEqual(self.canonical(room_id, "notes.md")[0], "codex", "only T2's owner wrote notes.md")

    async def test_a_finished_tasks_file_another_active_partner_owns_goes_back_to_its_owner(self):
        # Catches an extension overriding a file's canonical owner (the import would fail on ownership and stop the room).
        self.plan = [("T1", "codex", ["notes.md"], []), ("T2", "claude", ["main.py"], ["T1"])]

        def work(task, cwd, attempt, context):
            if task["id"] == "T2" and attempt == 1:
                (cwd / "main.py").write_text("VALUE = 2\n")
                (cwd / "notes.md").write_text("Claude's take.\n")
                return native_reply(context, [extend("T2", ["notes.md"]), complete("T2")])
            if task["id"] == "T2":
                (cwd / "notes.md").write_text("# T1 attempt 1\n")
                (cwd / "main.py").write_text("VALUE = 2\n")
                return native_reply(context, [complete("T2")])
            return ScopeExtensionService.do_work(self, task, cwd, attempt, context)
        self.do_work = work
        room_id, state = await self.run_room()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        tasks = {task["id"]: task for task in state["team"]["tasks"]}
        self.assertEqual((tasks["T2"]["status"], tasks["T2"]["write_paths"]), ("accepted", ["main.py"]))
        self.assertIn("team_extend_path_owned", self.work["T2"][1]["job"]["text"])
        self.assertEqual(self.canonical(room_id, "notes.md"), ("codex", hashlib.sha256(b"# T1 attempt 1\n").hexdigest()))


if __name__ == "__main__":
    unittest.main()
