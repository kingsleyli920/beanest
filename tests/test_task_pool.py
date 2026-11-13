"""Tasks wait in a shared pool and a free partner takes the next ready one.

Fictional projects and partner mixes. The store tests use real SQLite; the
service tests drive the real job loop with only the native transport synthetic.
"""
import asyncio
import concurrent.futures
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.agent_registry import AgentRegistry, default_registry
from local_agent_coordinator.room_store import RoomError, RoomStore
from tests import test_native_room_service as native
from test_team_store import proposal

native_reply = native.native_reply


def registry():
    """The built-in tools, "third" (a second partner on Claude's tool) and "ember" (on Kimi's)."""
    base = default_registry()
    return AgentRegistry([*(base.get(key) for key in base.ids()), replace(base.get("claude"), id="third", name="Third"),
                          replace(base.get("codex"), id="ember", name="Ember", adapter="kimi"),
                          replace(base.get("codex"), id="scribe", name="Scribe", task_capabilities=("text",))])


def plan(task_id, *, dependencies=(), capability="python", paths=None):
    return {"id": task_id, "dependencies": list(dependencies), "capability": capability,
            "paths": [f"{task_id}.py"] if paths is None else paths}


class PoolService(unittest.IsolatedAsyncioTestCase):
    """Rooms on a fictional project, through real storage and the job loop."""
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        with patch.object(native, "default_registry", registry):
            await native.NativeRoomServiceTests.asyncSetUp(self)
        self.fake.handler = self.handler
        self.plan = []
        self.profiles = {}
        self.service.store.account_workloads = lambda: self.profiles
        self.holds = {}       # task id -> async condition that releases its work call
        self.works = []       # (partner, task id) in the order work calls started
        self.user_claims = {}
        self.room_id = None

    async def create(self, participants, *, members=None, objective=True):
        created = await self.service.create_room(
            "Update the existing value and preserve notes.", participants[0], str(uuid4()), mode="native_project",
            project_id=self.registration["project"]["id"], participants=participants, work_mode="build",
            runtime_settings={"permission": "full_access", "members": members or {}},
            **({"execution_policy": {"mode": "autonomous"}} if objective else {}))
        self.room_id = created["room"]["id"]
        return created

    async def state(self):
        return await self.service.read_room(self.room_id)

    async def handler(self, agent, context, cwd):
        # "third" runs on Claude's tool, so answer as the partner the prompt addresses.
        member, phase, board, task = context["self"], context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and member == board["plan_author"]:
                commands += [native.proposal(item["id"], paths=item["paths"], capability=item["capability"],
                                             dependencies=item["dependencies"]) for item in self.plan]
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            self.works.append((member, task["id"]))
            hold = self.holds.get(task["id"])
            if hold is not None:
                for _ in range(500):
                    if await hold():
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail(f"{task['id']} was held past its condition")
            for path in task["write_paths"]:
                (cwd / path).write_text(f"# {task['id']} by {member}\n")
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Written."}])
        if phase == "review":
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact tree."}])
        if phase == "user":
            return native_reply(context, [{"type": "claim", "task_id": value} for value in self.user_claims.get(member, [])])
        if phase == "checkpoint":
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Done.", "next_goal": None,
                                           "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Compared.",
                                           "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    def workers(self):
        return {task_id: member for member, task_id in self.works}

    async def statuses(self):
        return {task["id"]: task["status"] for task in (await self.state())["team"]["tasks"]}

    def until_statuses(self, wanted, status):
        async def condition():
            current = await self.statuses()
            return all(current.get(task_id) == status for task_id in wanted)
        return condition

    async def finished(self):
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(sorted(task_id for _, task_id in self.works), sorted(item["id"] for item in self.plan),
                         "every task was worked exactly once")
        return state

    async def test_free_partners_take_chain_steps_and_independent_work_as_it_becomes_ready(self):
        # The user's case: chain T1 -> T2 -> T3 and independent T4, T5, T6 among three partners.
        # Catches tasks split up front, so that whoever finished T4-T6 waits while a ready task sits
        # in someone else's queue, or a chain step handed to a stranger instead of the partner who did the step before.
        self.plan = [plan("T1"), plan("T2", dependencies=["T1"]), plan("T3", dependencies=["T2"]),
                     plan("T4"), plan("T5"), plan("T6")]
        seen_t6 = {}
        async def until_independent_done():
            current = await self.statuses()
            if "T6" in self.workers() and "T6" not in seen_t6:
                seen_t6["while_t1_held"] = current.get("T1") == "working"
            return all(current.get(task_id) == "accepted" for task_id in ("T4", "T5", "T6"))
        self.holds["T1"] = until_independent_done
        await self.create(["claude", "codex", "third"])
        state = await self.finished()
        workers = self.workers()
        # First dispatch: free partners in room order take the ready tasks in plan order.
        self.assertEqual((workers["T1"], workers["T4"], workers["T5"]), ("claude", "codex", "third"))
        self.assertIn(workers["T6"], {"codex", "third"})
        self.assertTrue(seen_t6["while_t1_held"], "T6 was taken by a free partner while T1 was still running")
        self.assertEqual((workers["T2"], workers["T3"]), ("claude", "claude"), "each chain step went to whoever did the step before")
        self.assertLess(self.works.index(("claude", "T1")), self.works.index(("claude", "T2")))
        self.assertLess(self.works.index(("claude", "T2")), self.works.index(("claude", "T3")))
        taken = {event["task_id"]: (event["owner"], event["reason"]) for event in state["events"] if event["kind"] == "task_taken"}
        self.assertEqual(taken["T2"], ("claude", "chain"))
        self.assertEqual(taken["T1"], ("claude", "next_ready"))
        self.assertFalse(any(event["kind"] == "task_rebalanced" for event in state["events"]))

    async def test_a_free_partner_takes_the_next_task_instead_of_waiting_behind_a_long_one(self):
        # Catches the "I take 1, 2; you take 3, 4" split: codex would idle while T2 waited behind claude's long T1.
        self.plan = [plan("T1"), plan("T2"), plan("T3"), plan("T4")]
        self.holds["T1"] = self.until_statuses(("T2", "T3", "T4"), "review")
        await self.create(["claude", "codex"])
        await self.finished()
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "codex", "T3": "codex", "T4": "codex"})
        self.assertEqual([task_id for member, task_id in self.works if member == "codex"], ["T2", "T3", "T4"])

    async def test_the_partner_that_did_a_step_gets_the_next_one_when_several_are_free(self):
        # Catches plain plan/room order winning over the chain: claude is first in the room and free too,
        # but codex did T1, so T2 must go to codex.
        self.plan = [plan("T0"), plan("T1"), plan("T2", dependencies=["T1"])]
        self.holds["T1"] = self.until_statuses(("T0",), "accepted")
        await self.create(["claude", "codex", "third"])
        state = await self.finished()
        self.assertEqual(self.workers(), {"T0": "claude", "T1": "codex", "T2": "codex"})
        event = next(event for event in state["events"] if event["kind"] == "task_taken" and event["task_id"] == "T2")
        self.assertEqual((event["owner"], event["reason"]), ("codex", "chain"))

    async def test_a_partner_out_of_quota_stops_taking_and_its_task_goes_back_to_the_pool(self):
        # Catches a retired partner's queued task being parked with one fixed successor instead of the pool,
        # or a restored partner staying idle until some rebalance step that no longer exists.
        self.plan = [plan(f"T{number}") for number in range(1, 6)]
        out = {"third"}
        async def guard(members):
            # third plans normally, is handed T3, and finds its quota gone when that work call is about to start.
            if (out & set(members) and self.room_id is not None
                    and (await self.state())["team"]["phase"] == "working"):
                raise RoomError("agent_quota_exhausted")
        self.service._guard_availability = guard
        restored = asyncio.Event()
        async def until_restored():
            return restored.is_set()
        async def until_returned():
            return any(event["kind"] == "task_returned_to_pool" for event in (await self.state())["events"])
        self.holds.update(T1=until_restored, T2=until_returned, T3=until_restored)
        await self.create(["claude", "codex", "third"])
        for _ in range(500):
            if ("codex", "T3") in self.works:
                break
            await asyncio.sleep(0.01)
        state = await self.state()
        returned = next(event for event in state["events"] if event["kind"] == "task_returned_to_pool")
        self.assertEqual((returned["task_id"], returned["previous_owner"]), ("T3", "third"))
        self.assertIn(("codex", "T3"), self.works, "a free partner took the returned task")
        self.assertNotIn("third", {member for member, _ in self.works})
        out.clear()
        self.service.store.restore_active_quota_members(self.room_id, ["third"])
        self.service.store.reconcile_team(self.room_id)
        self.service._wake(self.room_id)
        for _ in range(500):
            if any(member == "third" for member, _ in self.works):
                break
            await asyncio.sleep(0.01)
        restored.set()
        await self.finished()
        self.assertEqual(self.workers()["T4"], "third", "the restored partner took the next ready task")

    async def test_an_account_down_to_its_reserve_waits_while_another_partner_can_build(self):
        # Catches the pool handing build work to an account on its last few percent just because it is free.
        self.profiles = {"codex": ("codex", "normal", True)}
        self.plan = [plan("T1"), plan("T2")]
        await self.create(["claude", "codex"])
        await self.finished()
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "claude"})

    async def test_a_reviewer_role_does_not_take_build_work(self):
        # Catches a free reviewer taking build work from the pool while a builder can do it.
        self.plan = [plan("T1"), plan("T2")]
        await self.create(["claude", "codex"], members={"claude": {"role": {"id": "reviewer"}}})
        await self.finished()
        self.assertEqual(self.workers(), {"T1": "codex", "T2": "codex"})

    async def test_a_task_the_user_gave_a_partner_waits_for_that_partner(self):
        # Catches a user's pin being ignored: without it, claude did T1 and takes T3 next by the chain rule.
        self.plan = [plan("T1"), plan("T2"), plan("T3", dependencies=["T1"])]
        pinned = asyncio.Event()
        async def until_pinned():
            return pinned.is_set()
        self.holds["T1"] = until_pinned
        self.user_claims = {"codex": ["T3"]}
        await self.create(["claude", "codex"])
        for _ in range(500):
            if (await self.statuses()).get("T2") == "review":
                break
            await asyncio.sleep(0.01)
        await self.service.send_message(self.room_id, "Codex, please do T3 when it is ready.", "codex", str(uuid4()), source="user")
        for _ in range(500):
            task = next(task for task in (await self.state())["team"]["tasks"] if task["id"] == "T3")
            if task["owner"] == "codex":
                break
            await asyncio.sleep(0.01)
        self.assertEqual((task["owner"], task["status"]), ("codex", "assigned"))
        pinned.set()
        await self.finished()
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "codex", "T3": "codex"})

    async def test_a_free_partner_takes_a_task_on_a_file_another_partner_wrote_and_finished(self):
        # Catches the author pin: a later task on a file another present partner wrote in finished work must go to
        # whoever is free, import its change without team_scope_conflict, and move the file to its new author.
        self.plan = [plan("T1", paths=["f.md"])]
        await self.create(["codex", "claude"], objective=False)
        await self.settle()
        first = await self.state()
        self.assertEqual(first["team"]["phase"], "ready_for_user", first["team"])
        self.assertEqual(self.workers(), {"T1": "codex"})
        self.assertEqual({file["path"]: file["owner"] for file in first["project"]["files"] if file["path"] == "f.md"}, {"f.md": "codex"})
        self.plan = [plan("T0", paths=["other.md"]), plan("T2", paths=["f.md"])]
        self.works.clear()
        current = first["current_run"]
        await self.service.start_run(self.room_id, "Update the notes again.", "all", str(uuid4()), run_id=current["id"],
                                     revision=current["revision"], digest=current["digest"], retry=False)
        await self.settle()
        second = await self.state()
        self.assertEqual(second["team"]["phase"], "ready_for_user", second["team"])
        self.assertIsNone(second["team"]["stop_reason"])
        # Free partners in room order take ready tasks in plan order; claude gets the task on codex's file.
        self.assertEqual(self.workers(), {"T0": "codex", "T2": "claude"})
        granted = [(event["task_id"], event["provider"], event["paths"]) for event in second["events"] if event["kind"] == "task_files_granted"]
        self.assertEqual(granted, [("T2", "claude", ["f.md"])])
        self.assertFalse(any(job["error"] for job in second["jobs"] if job["kind"] == "team_work"))
        shared = next(file for file in second["project"]["files"] if file["path"] == "f.md")
        self.assertEqual(shared["owner"], "claude")

    async def test_a_file_whose_author_ran_out_of_quota_moves_to_whoever_takes_the_next_step(self):
        # Catches a pool task on a retired partner's file being taken without a transfer grant:
        # the import would refuse the new owner's change and the room would stop.
        self.plan = [plan("T1"), plan("T0", paths=["shared.py"]), plan("T2", dependencies=["T0"], paths=["shared.py"])]
        async def guard(members):
            # codex wrote shared.py in T0 and runs out of quota once T0 is accepted.
            if ("codex" in members and self.room_id is not None
                    and (await self.statuses()).get("T0") == "accepted"):
                raise RoomError("agent_quota_exhausted")
        self.service._guard_availability = guard
        self.holds["T1"] = self.until_statuses(("T0",), "review")
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertEqual(self.workers(), {"T1": "claude", "T0": "codex", "T2": "claude"})
        self.assertTrue(any(event["kind"] == "partner_retired" and event["provider"] == "codex" for event in state["events"]))
        shared = next(file for file in state["project"]["files"] if file["path"] == "shared.py")
        self.assertEqual(shared["owner"], "claude")

    async def test_a_partner_is_only_given_tasks_it_can_do(self):
        # Catches the pool handing a Python task to a text-only partner just because it is free.
        self.plan = [plan("T1"), plan("T2"), plan("T3", capability="text")]
        await self.create(["claude", "scribe"])
        await self.finished()
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "claude", "T3": "scribe"})

    async def test_a_review_waits_for_a_busy_partner_rather_than_use_a_reserve(self):
        # Catches the reviewer choice falling through to an account on its last few percent just because the others were busy.
        self.profiles = {"codex": ("codex", "normal", True)}
        self.plan = [plan("T1"), plan("T2")]
        seen = {}
        async def until():
            # Keep third busy well past the moment T1 is ready for review.
            if (await self.statuses()).get("T1") == "review":
                seen.setdefault("at", asyncio.get_running_loop().time())
            return "at" in seen and asyncio.get_running_loop().time() - seen["at"] > 0.5
        self.holds["T2"] = until
        await self.create(["claude", "codex", "third"])
        state = await self.finished()
        self.assertIn("at", seen, "T1 never waited for review while third was busy")
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "third"}, "the reserve took no build work either")
        self.assertEqual({task["id"]: task["reviewer"] for task in state["team"]["tasks"]}, {"T1": "third", "T2": "claude"})

    async def test_a_task_that_owns_many_files_is_planned_and_delivered(self):
        # Catches a fixed small cap (it was 16 paths and 8 dependencies) rejecting a real plan on a large project,
        # or the delivered files being refused at import because a task changed more than 16 of them.
        paths = [f"part_{index:02d}.py" for index in range(25)]
        self.plan = [plan(f"P{index}", paths=[f"pre_{index}.py"]) for index in range(10)]
        self.plan.append(plan("BIG", paths=paths, dependencies=[f"P{index}" for index in range(10)]))
        await self.create(["claude", "codex"])
        state = await self.finished()
        big = next(task for task in state["team"]["tasks"] if task["id"] == "BIG")
        self.assertEqual((big["status"], len(big["write_paths"]), len(big["dependencies"])), ("accepted", 25, 10))
        self.assertEqual(sorted(entry["path"] for entry in big["result_files"]), paths)
        self.assertFalse(any(event["kind"] == "native_response_rejected" for event in state["events"]))

    async def test_a_rejected_plan_is_told_which_rule_it_broke(self):
        # Catches a rejection that names only an error code, so the partner rewrites the plan by guessing.
        self.plan = [plan("T1")]
        attempts = []
        handler = self.handler
        async def bad_first_plan(agent, context, cwd):
            if (context["job"]["job_phase"] == "plan" and not context["team"]["tasks"]
                    and context["self"] == context["team"]["plan_author"] and not attempts):
                attempts.append(1)
                return native_reply(context, [native.proposal("T1", paths=["src/"], capability="python", dependencies=[])])
            return await handler(agent, context, cwd)
        self.fake.handler = bad_first_plan
        await self.create(["claude", "codex"])
        await self.finished()
        # Only the plan author's own correction call; a peer's prompt shows the author's current task cut to a preview.
        correction = next(call for call in self.fake.calls if call["context"]["self"] == "claude"
                          and call["context"]["job"]["job_phase"] == "plan" and "its response was rejected" in call["prompt"])
        self.assertIn("Error code: invalid_team_task", correction["prompt"])
        self.assertIn("no leading or trailing '/'", correction["prompt"])
        self.assertIn("at most 1000", correction["prompt"])

    async def test_a_partner_joining_mid_run_takes_waiting_work(self):
        # Catches a newcomer sitting idle while tasks wait in the pool, or a started task being taken from its owner.
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        joined = asyncio.Event()
        async def until_joined_and_working():
            return joined.is_set() and any(member == "codex" for member, _ in self.works)
        self.holds["T1"] = until_joined_and_working
        await self.create(["claude"])
        for _ in range(500):
            if ("claude", "T1") in self.works:
                break
            await asyncio.sleep(0.01)
        await self.service.invite_members(self.room_id, ["codex"], str(uuid4()))
        joined.set()
        state = await self.finished()
        self.assertEqual(self.workers()["T1"], "claude")
        self.assertEqual(self.workers()["T2"], "codex")
        invited = next(call for call in self.fake.calls if call["context"]["self"] == "codex"
                       and call["context"]["job"]["job_phase"] == "coordinate")
        self.assertIn("Preserve the owners of started tasks and reviewed results", invited["prompt"])
        self.assertIn("the host hands you the next ready task from the shared pool", invited["prompt"])
        self.assertEqual(next(task for task in state["team"]["tasks"] if task["id"] == "T1")["owner"], "claude")


class PoolStore(unittest.TestCase):
    """The take itself, on real SQLite without a job loop."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "rooms.sqlite3"
        self.store = RoomStore(self.path, clock=lambda: 1000.0, agents={"claude": "Claude", "codex": "Codex", "kimi": "Kimi"})
        self.room_id = self.store.create_room("Ship the report tool", "claude", str(uuid4()), collaboration=True)[0]
        plans = {provider: self.store.claim(self.room_id, provider) for provider in ("claude", "codex", "kimi")}
        self.finish(plans["claude"], [proposal("A"), proposal("B"), proposal("C", dependencies=["A"]), proposal("D"),
                                      {"type": "claim", "task_id": "D"}, {"type": "plan_ready"}])
        self.finish(plans["codex"], [{"type": "claim", "task_id": "A"}, {"type": "plan_ready"}])
        self.finish(plans["kimi"], [])
        # codex confirmed against the board before the proposal, so it confirms again; kimi's confirmation is left to each test.
        codex = self.store.claim(self.room_id, "codex")
        self.assertEqual(codex["kind"], "team_coordinate")
        self.finish(codex, [{"type": "plan_ready"}])
        self.confirmation = self.store.claim(self.room_id, "kimi")
        self.assertEqual(self.confirmation["kind"], "team_coordinate")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def finish(self, job, commands):
        self.assertTrue(self.store.prevalidate_team_result(job["id"], commands))
        self.store.finish_job(job["id"], message="Concrete response", team_commands=commands)

    def tasks(self):
        return {task["id"]: task for task in self.store.snapshot(self.room_id)["team"]["tasks"]}

    def queued_work(self):
        return sorted((job["provider"], job["text"].rsplit("Task ", 1)[1].split(":")[0])
                      for job in self.store.snapshot(self.room_id)["jobs"] if job["kind"] == "team_work" and job["status"] == "queued")

    def test_planning_claims_reserve_nothing_and_confirmation_starts_work_from_the_pool(self):
        # Catches the old rule that every task needs an owner before work, or a planning claim still pinning a task.
        self.assertEqual({task["owner"] for task in self.tasks().values()}, {None})
        self.assertNotIn("claim", {event.get("command") for event in self.store.snapshot(self.room_id)["events"]
                                   if event["kind"] == "team_command"})
        self.finish(self.confirmation, [{"type": "plan_ready"}])
        self.assertEqual(self.store.snapshot(self.room_id)["team"]["phase"], "working")
        # Free partners in room order take ready tasks in plan order; C waits for A.
        self.assertEqual(self.queued_work(), [("claude", "A"), ("codex", "B"), ("kimi", "D")])
        tasks = self.tasks()
        self.assertEqual({key: (task["owner"], task["status"]) for key, task in tasks.items()},
                         {"A": ("claude", "assigned"), "B": ("codex", "assigned"), "C": (None, "open"), "D": ("kimi", "assigned")})

    def test_two_takes_of_one_task_bind_it_once(self):
        # Catches a take that is not guarded on the task still being open: two partners would hold one task.
        team = self.store._team
        with self.store._connection(write=True) as db:
            db.execute("UPDATE teams SET phase='working' WHERE room_id=?", (self.room_id,))
            task = team.task(db, self.room_id, "B")
            self.assertTrue(team._take(db, self.room_id, task, "claude", chained=False))
            self.assertFalse(team._take(db, self.room_id, task, "codex", chained=False))
            rows = db.execute("SELECT j.provider FROM jobs j JOIN team_jobs t ON t.job_id=j.id WHERE t.task_id='B' AND t.phase='work'").fetchall()
        self.assertEqual([row[0] for row in rows], ["claude"])
        self.assertEqual(self.tasks()["B"]["owner"], "claude")

    def test_concurrent_reconciles_never_give_one_task_to_two_partners(self):
        # Catches a race between two dispatch passes: each ready task gets exactly one work call.
        self.finish(self.confirmation, [{"type": "plan_ready"}])
        for job_id in [job["id"] for job in self.store.snapshot(self.room_id)["jobs"] if job["status"] == "queued"]:
            with self.store._connection(write=True) as db:
                db.execute("UPDATE jobs SET status='cancelled',error='test_reset' WHERE id=?", (job_id,))
                db.execute("UPDATE shared_tasks SET owner=NULL,status='open' WHERE room_id=? AND status='assigned'", (self.room_id,))
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: self.store.reconcile_team(self.room_id), range(8)))
        work = self.queued_work()
        self.assertEqual(sorted(task for _, task in work), ["A", "B", "D"])
        self.assertEqual(len({provider for provider, _ in work}), 3)

    def test_a_taken_task_gets_its_untouched_source_file_but_never_a_changed_one(self):
        # Catches a pool task being unable to change a selected source file first assigned elsewhere
        # (selected-files mode), or that grant reaching a file someone has already changed.
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET write_paths_json=? WHERE room_id=? AND id='A'", ('["a.py"]', self.room_id))
        self.finish(self.confirmation, [{"type": "plan_ready"}])
        work = self.store.claim(self.room_id, "claude")
        self.assertEqual(work["team"]["task_id"], "A")
        # The baseline owner is not a partner, so only the untouched-source rule can move it.
        source = {"path": "a.py", "owner": "seeder", "sha256": "1" * 64}
        self.assertEqual(self.store.grant_taken_files(work["id"], [{**source, "sha256": "2" * 64}], 3,
                                                      baseline={"a.py": ("seeder", "1" * 64)}), {})
        self.assertEqual(self.store.ownership_grant(work["id"]), {})
        granted = self.store.grant_taken_files(work["id"], [source], 3, baseline={"a.py": ("seeder", "1" * 64)})
        expected = {"a.py": {"old_owner": "seeder", "new_owner": "claude", "sha256": "1" * 64, "revision": 3, "assignment_epoch": 0}}
        self.assertEqual(granted, expected)
        self.assertEqual(self.store.ownership_grant(work["id"]), expected)

    def test_a_partners_file_moves_once_none_of_its_unfinished_tasks_holds_it(self):
        # Catches the two ways the file rule can go wrong: a present partner keeping a file forever after its work
        # on it is finished (the pool could then hand most tasks to one partner or to nobody), or a file moving away
        # while that partner's unfinished task still lists it (two editors at once).
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET write_paths_json=? WHERE room_id=? AND id='B'", ('["Shared.md"]', self.room_id))
        self.finish(self.confirmation, [{"type": "plan_ready"}])
        self.assertEqual(self.tasks()["B"]["owner"], "codex")
        team = self.store._team
        files = [{"path": "shared.md", "owner": "codex", "sha256": "3" * 64}, {"path": "done.md", "owner": "codex", "sha256": "4" * 64}]
        with self.store._connection(write=True) as db:
            self.assertEqual(team.make_grant(db, self.room_id, "claude", ["done.md"], files, 5, 2),
                             {"done.md": {"old_owner": "codex", "new_owner": "claude", "sha256": "4" * 64, "revision": 5, "assignment_epoch": 2}})
            with self.assertRaises(RoomError) as held:
                team.make_grant(db, self.room_id, "claude", ["shared.md"], files, 5, 2)
            self.assertEqual(held.exception.code, "team_scope_conflict")
            db.execute("UPDATE shared_tasks SET status='accepted' WHERE room_id=? AND id='B'", (self.room_id,))
            self.assertEqual(team.make_grant(db, self.room_id, "claude", ["shared.md"], files, 5, 2)["shared.md"]["old_owner"], "codex")

    def test_a_task_nobody_in_the_room_can_do_stops_instead_of_waiting_forever(self):
        # Catches ready work that no partner can take leaving the room silently idle.
        self.store.task_capabilities = lambda provider: ()
        self.finish(self.confirmation, [{"type": "plan_ready"}])
        snapshot = self.store.snapshot(self.room_id)
        self.assertEqual((snapshot["team"]["phase"], snapshot["team"]["stop_reason"]), ("stopped", "team_plan_stalled"))
        warning = next(event for event in snapshot["events"] if event["kind"] == "warning" and event["code"] == "team_plan_stalled")
        self.assertEqual(warning["unowned_task_ids"], ["A", "B", "D"])


if __name__ == "__main__":
    unittest.main()
