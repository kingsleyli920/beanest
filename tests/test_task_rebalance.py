"""Moving claimed work nobody has begun, just before work starts. Fictional projects and partner mixes; only the native transport is synthetic."""
import asyncio
from collections import Counter
import json
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import workload
from local_agent_coordinator.agent_registry import AgentRegistry, default_registry
from local_agent_coordinator.room_store import RoomError
from tests import test_native_room_service as native

native_reply = native.native_reply


def task(task_id, owner, dependencies=(), movable=True):
    return {"id": task_id, "owner": owner, "dependencies": list(dependencies), "movable": movable}


def moves(tasks, profiles=None, *, members=("a", "b"), loads=None, can_take=lambda task, member: True):
    profiles = profiles or {member: (member, "normal") for member in members}
    return [(item["id"], receiver) for item, receiver in
            workload.rebalance(tasks, list(members), loads or {}, lambda member: profiles[member], can_take)]


class Quotas(unittest.TestCase):
    def test_equal_shares_balance_a_lone_owner(self):
        # Catches the old "equal shares do nothing" rule leaking in and leaving a partner idle.
        self.assertEqual(moves([task("T1", "a"), task("T2", "a")]), [("T2", "b")])

    def test_a_rounding_extra_stays_with_its_holder(self):
        # Catches 2-to-1 being read as unfair and tasks being bounced for no gain.
        self.assertEqual(moves([task("T1", "a"), task("T2", "a"), task("T3", "b")]), [])

    def test_the_low_account_keeps_only_its_share(self):
        # Catches an inverted ratio: a low account holding everything gives most of it away, high keeps three of four.
        profiles = {"a": ("a", "low"), "b": ("b", "high")}
        self.assertEqual(sorted(moves([task(f"T{n}", "a") for n in range(1, 5)], profiles)), [("T2", "b"), ("T3", "b"), ("T4", "b")])
        self.assertEqual(moves([task(f"T{n}", "b") for n in range(1, 5)], profiles), [("T4", "a")])

    def test_rounding_goes_to_the_larger_share(self):
        # Catches five tasks at one to three giving the small account two.
        profiles = {"a": ("a", "low"), "b": ("b", "high")}
        self.assertEqual(len(moves([task(f"T{n}", "a") for n in range(1, 6)], profiles)), 4)

    def test_one_task_or_one_account_never_moves(self):
        # Catches a room with nothing to compare still shuffling its work.
        self.assertEqual(moves([task("T1", "a")], {"a": ("a", "low"), "b": ("b", "high")}), [])
        self.assertEqual(moves([task("T1", "a"), task("T2", "a")], {"a": ("pool", "normal"), "b": ("pool", "normal")}), [])

    def test_a_lone_task_leaves_an_account_that_should_get_none(self):
        # Catches the one-task shortcut keeping work on an account down to its reserve while another can take it.
        self.assertEqual(moves([task("T1", "a")], {"a": ("a", workload.IDLE), "b": ("b", "normal")}), [("T1", "b")])
        self.assertEqual(moves([task("T1", "a", movable=False)], {"a": ("a", workload.IDLE), "b": ("b", "normal")}), [])

    def test_partners_on_one_account_share_one_quota(self):
        # Catches a task moving between two partners on the same login, which changes no real load.
        members = ("a", "a2", "b")
        profiles = {"a": ("pool", "normal"), "a2": ("pool", "normal"), "b": ("b", "normal")}
        self.assertEqual(moves([task("T1", "a"), task("T2", "a2"), task("T3", "a"), task("T4", "a2")], profiles, members=members),
                         [("T4", "b"), ("T3", "b")])

    def test_ties_go_to_the_account_that_has_used_least(self):
        # Catches a partner who already spent the most this round being picked first.
        members = ("a", "b", "c")
        loads = {"b": {"points": 500.0, "calls": 1, "reported": 1}, "c": {"points": 10.0, "calls": 1, "reported": 1}}
        self.assertEqual(moves([task("T1", "a"), task("T2", "a"), task("T3", "a")], members=members, loads=loads)[0], ("T3", "c"))

    def test_only_what_is_held_above_quota_moves(self):
        # Catches a fixed task being counted away: it still occupies its owner.
        tasks = [task("T1", "a", movable=False), task("T2", "a", movable=False), task("T3", "a"), task("T4", "b")]
        self.assertEqual(moves(tasks), [("T3", "b")])

    def test_a_receiver_that_cannot_do_a_task_is_passed_over(self):
        # Catches a task landing on a partner that cannot do it just because that partner is emptiest.
        tasks = [task("T1", "a"), task("T2", "a"), task("T3", "a")]
        self.assertEqual(moves(tasks, members=("a", "b", "c"), can_take=lambda item, member: member == "c"), [("T3", "c")])


class Chains(unittest.TestCase):
    def test_a_chain_stays_contiguous(self):
        # Catches the moved tasks being picked from the middle, which makes partners alternate along one chain.
        tasks = [task("T1", "a"), task("T2", "a", ["T1"]), task("T3", "a", ["T2"]), task("T4", "a", ["T3"])]
        self.assertEqual(moves(tasks), [("T4", "b"), ("T3", "b")])

    def test_independent_tasks_are_moved_before_a_chain_is_split(self):
        # Catches a chain being broken when free-standing tasks could have been moved instead.
        tasks = [task("T1", "a"), task("T2", "a"), task("T3", "a"), task("T4", "a", ["T3"]), task("T5", "a", ["T4"])]
        self.assertEqual(moves(tasks), [("T2", "b"), ("T1", "b")])

    def test_a_whole_chain_moves_rather_than_two_halves(self):
        # Catches both chains being cut in half when one could change hands intact.
        tasks = [task("T1", "a"), task("T2", "a", ["T1"]), task("T3", "a"), task("T4", "a", ["T3"])]
        self.assertEqual(moves(tasks), [("T4", "b"), ("T3", "b")])

    def test_a_receiver_continues_the_chain_it_already_holds(self):
        # Catches a task next to the receiver's own chain being passed over for an unrelated later task.
        tasks = [task("T1", "b"), task("T2", "a", ["T1"]), task("T3", "a"), task("T4", "a", ["T3"])]
        self.assertEqual(moves(tasks), [("T2", "b")])

    def test_a_longer_chain_is_cut_once_per_receiver(self):
        # Catches two receivers alternating along the same chain.
        tasks = [task("T1", "a")] + [task(f"T{n}", "a", [f"T{n - 1}"]) for n in range(2, 7)]
        result = dict(moves(tasks, members=("a", "b", "c")))
        self.assertEqual(result, {"T6": "b", "T5": "b", "T4": "c", "T3": "c"})

    def test_a_fixed_later_step_keeps_the_earlier_steps_home(self):
        # Catches a chain being pulled apart around a task that has to stay.
        tasks = [task("T1", "a"), task("T2", "a", ["T1"]), task("T3", "a", ["T2"], movable=False)]
        self.assertEqual(moves(tasks), [])


def registry():
    """The built-in tools, a second partner on the first tool's adapter, a text-only partner, and one on a third adapter."""
    base = default_registry()
    return AgentRegistry([*(base.get(key) for key in base.ids()),
                          replace(base.get("claude"), id="third", name="Third"),
                          replace(base.get("codex"), id="scribe", name="Scribe", task_capabilities=("text",)),
                          replace(base.get("codex"), id="ember", name="Ember", adapter="kimi")])


def plan(task_id, owner, *, dependencies=(), paths=None, capability="python"):
    return {"id": task_id, "owner": owner, "dependencies": list(dependencies),
            "paths": [f"{task_id}.py"] if paths is None else paths, "capability": capability}


class RebalanceService(unittest.IsolatedAsyncioTestCase):
    """Rooms on a fictional project, through real storage and the job loop."""
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        with patch.object(native, "default_registry", registry):
            await native.NativeRoomServiceTests.asyncSetUp(self)
        self.fake.handler = self.handler
        self.plan = []
        self.profiles = {}
        self.service.store.account_workloads = lambda: self.profiles
        self.before_plan = None
        self.blocked_once = set()
        self.hold_coordinate = None
        self.user_claims = []

    def propose(self, context, board):
        commands = []
        for item in self.plan:
            commands.append(native.proposal(item["id"], paths=item["paths"], capability=item["capability"], dependencies=item["dependencies"]))
            if item["owner"] == board["plan_author"]:
                commands.append({"type": "claim", "task_id": item["id"]})
        return commands

    async def handler(self, agent, context, cwd):
        phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            if phase == "coordinate" and self.hold_coordinate is not None and agent == "codex":
                await self.hold_coordinate.wait()
            commands = []
            if not board["tasks"] and agent == board["plan_author"]:
                if self.before_plan:
                    outcome = self.before_plan()
                    if asyncio.iscoroutine(outcome):
                        await outcome
                commands += self.propose(context, board)
            for item in board["tasks"]:
                planned = next((value for value in self.plan if value["id"] == item["id"]), None)
                # The transport is addressed by adapter; the partner's own identity is in its context.
                if planned and planned["owner"] == context["self"] and item["owner"] is None:
                    commands.append({"type": "claim", "task_id": item["id"]})
            if getattr(self, "on_coordinate", None) and phase == "coordinate":
                commands += self.on_coordinate(agent, board)
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            if task["id"] in getattr(self, "block_first", ()) and task["id"] not in self.blocked_once:
                self.blocked_once.add(task["id"])
                return native_reply(context, [{"type": "block", "task_id": task["id"], "cause": "coordination_required",
                                               "reason": "Needs a prerequisite planned first."}])
            for path in task["write_paths"]:
                (cwd / path).write_text(f"# {task['id']} by {agent}\n")
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Written."}])
        if phase == "review":
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact tree."}])
        if phase == "checkpoint":
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Done.", "next_goal": None,
                                           "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Compared.",
                                           "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        if phase == "user":
            return native_reply(context, [{"type": "claim", "task_id": value} for value in self.user_claims])
        self.fail(phase)

    async def finished(self, participants):
        created = await self.create(participants=participants)
        await self.settle()
        state = await self.service.read_room(created["room"]["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        return state

    @staticmethod
    def owners(state):
        return {task["id"]: task["owner"] for task in state["team"]["tasks"]}

    @staticmethod
    def moved(state):
        return [(event["task_id"], event["previous_owner"], event["owner"]) for event in state["events"] if event["kind"] == "task_rebalanced"]

    def workers(self):
        return {call["context"]["task"]["id"]: call["agent"] for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work"}

    async def test_two_partners_with_equal_shares(self):
        # Catches work staying with the plan author, the moved task not reaching its new owner, or the timeline missing the reason.
        self.plan = [plan("T1", "claude"), plan("T2", "claude"), plan("T3", "claude")]
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.owners(state), {"T1": "claude", "T2": "claude", "T3": "codex"})
        self.assertEqual(self.moved(state), [("T3", "claude", "codex")])
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "claude", "T3": "codex"})
        event = next(event for event in state["events"] if event["kind"] == "task_rebalanced")
        self.assertEqual((event["reason"], event["assignment_epoch"], event["sender"]), ("over_share", 1, "system"))
        self.assertEqual({task["id"]: task["status"] for task in state["team"]["tasks"]}, dict.fromkeys(("T1", "T2", "T3"), "accepted"))
        self.assertEqual({task["id"]: task["assignment_epoch"] for task in state["team"]["tasks"]}, {"T1": 0, "T2": 0, "T3": 1})

    async def test_three_partners_on_different_adapters(self):
        # Catches the third partner being skipped, or a mixed-adapter room being treated as a single-tool room.
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 5)]
        state = await self.finished(["claude", "codex", "ember"])
        self.assertEqual(self.owners(state), {"T1": "claude", "T2": "claude", "T3": "ember", "T4": "codex"})
        # The native transport is addressed by adapter, so the Kimi-adapter partner shows up as its adapter.
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "claude", "T3": "kimi", "T4": "codex"})

    async def test_a_low_account_is_left_with_its_share(self):
        # Catches shares being ignored: the low account keeps one of four, the high account gets the rest.
        self.profiles = {"claude": ("a", "low"), "codex": ("b", "high")}
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 5)]
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.owners(state), {"T1": "claude", "T2": "codex", "T3": "codex", "T4": "codex"})

    async def test_a_high_account_holding_its_share_is_left_alone(self):
        # Catches a configured room being evened out to equal counts.
        self.profiles = {"claude": ("a", "high"), "codex": ("b", "low")}
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 4)] + [plan("T4", "codex")]
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.moved(state), [])

    async def test_a_partner_that_cannot_do_it_is_not_given_it(self):
        # Catches a task being handed to a partner without that capability.
        self.plan = [plan("T1", "claude"), plan("T2", "claude"), plan("T3", "claude", capability="text")]
        state = await self.finished(["claude", "scribe"])
        self.assertEqual(self.moved(state), [("T3", "claude", "scribe")])
        self.assertEqual(self.owners(state)["T2"], "claude")

    async def test_nothing_moves_when_no_task_matches_the_partner(self):
        # Catches a capability mismatch being ignored once it is the only obstacle.
        self.plan = [plan("T1", "claude"), plan("T2", "claude")]
        self.assertEqual(self.moved(await self.finished(["claude", "scribe"])), [])

    async def test_tasks_sharing_a_path_stay_with_one_owner(self):
        # Catches two tasks on the same file ending up with different owners, which the claim rule forbids.
        self.plan = [plan("T1", "claude", paths=["shared.py"]), plan("T2", "claude", paths=["shared.py"], dependencies=["T1"]),
                     plan("T3", "claude")]
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.moved(state), [("T3", "claude", "codex")])

    async def test_a_path_written_in_an_earlier_round_stays_with_its_author(self):
        # Catches a task being moved onto a file whose authorship cannot move with it.
        self.plan = [plan("T1", "claude"), plan("T2", "claude", paths=["kept.py"])]
        room, earlier = {}, str(uuid4())
        def earlier_round():
            with self.service.store._connection(write=True) as db:
                room["id"] = db.execute("SELECT id FROM rooms ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                # A far-off number, so the room's own "previous round" lookup never picks this row.
                db.execute("INSERT INTO team_runs(id,room_id,number,goal,created_at,deadline,call_limit,status) VALUES (?,?,-5,'old',0,0,0,'accepted')",
                           (earlier, room["id"]))
                db.execute("INSERT INTO shared_tasks(room_id,run_id,id,title,description,acceptance,dependencies_json,write_paths_json,capability,status,owner) "
                           "VALUES (?,?,'OLD','Old','Old','Old','[]',?,'python','accepted','claude')", (room["id"], earlier, json.dumps(["KEPT.py"])))
        self.before_plan = earlier_round
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.moved(state), [("T1", "claude", "codex")])

    async def test_a_dependency_chain_is_handed_over_once(self):
        # Catches partners alternating along one chain; order of execution must still follow the dependencies.
        self.plan = [plan("T1", "claude"), plan("T2", "claude", dependencies=["T1"]),
                     plan("T3", "claude", dependencies=["T2"]), plan("T4", "claude", dependencies=["T3"])]
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.owners(state), {"T1": "claude", "T2": "claude", "T3": "codex", "T4": "codex"})
        self.assertEqual([call["context"]["task"]["id"] for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work"],
                         ["T1", "T2", "T3", "T4"])

    async def test_work_already_started_is_not_moved(self):
        # Catches a replan taking away a task its owner has already begun.
        self.plan = [plan("T1", "claude"), plan("T2", "codex")]
        self.block_first = {"T1"}
        added = []
        def on_coordinate(agent, board):
            if agent != board["plan_author"] or added or not any(task["block_cause"] == "coordination_required" for task in board["tasks"]):
                return []
            added.append(True)
            commands = []
            for task_id in ("P1", "P2"):
                commands += [native.proposal(task_id, paths=[f"{task_id}.py"], capability="python", dependencies=[]),
                             {"type": "claim", "task_id": task_id}]
            return commands
        self.on_coordinate = on_coordinate
        state = await self.finished(["claude", "codex"])
        self.assertTrue(added, "the replan ran")
        self.assertEqual(self.owners(state)["T1"], "claude")
        self.assertEqual(self.moved(state), [])

    async def test_a_partner_out_of_quota_receives_nothing(self):
        # Catches work being sent to a partner already known to have no quota left.
        async def guard(members):
            if "third" in members:
                raise RoomError("agent_quota_exhausted")
        self.service._guard_availability = guard
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 5)]
        state = await self.finished(["claude", "codex", "third"])
        self.assertEqual(sorted(self.owners(state).values()), ["claude", "claude", "codex", "codex"])
        self.assertFalse(any(call["agent"] == "third" for call in self.fake.calls))

    async def test_a_disabled_partner_receives_nothing(self):
        # Catches a disabled partner being counted as a receiver; only its lifecycle flag is synthetic.
        actual = self.service.store._partner_dispatch_reason
        disabled = set()
        self.service.store._partner_dispatch_reason = lambda db, provider: "partner_disabled" if provider in disabled else actual(db, provider)
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 5)]
        created = await self.create(participants=["claude", "codex", "third"])
        disabled.add("third")
        await self.settle()
        state = await self.service.read_room(created["room"]["id"])
        # A bare flag has no lifecycle record, so the objective's closing step is out of scope here.
        self.assertEqual(state["team"]["phase"], "ready_for_user", state["team"])
        self.assertEqual(sorted(self.owners(state).values()), ["claude", "claude", "codex", "codex"])

    async def test_a_single_task_room_is_left_alone(self):
        # Catches a one-task room having its only task taken from the partner who planned it.
        self.profiles = {"claude": ("a", "low"), "codex": ("b", "high")}
        self.plan = [plan("T1", "claude")]
        self.assertEqual(self.moved(await self.finished(["claude", "codex"])), [])

    async def test_a_lone_task_on_an_account_down_to_its_reserve_moves(self):
        # Catches a lone task staying with an account that dropped to its last few percent after claiming it, while another partner is free.
        self.plan = [plan("T1", "codex")]
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.owners(state), {"T1": "codex"}, "without a reserve a lone task stays with its owner")
        def drops_after_claiming(agent, board):
            if agent == "codex":
                self.profiles = {"codex": ("codex", "normal", True)}
            return []
        self.on_coordinate = drops_after_claiming
        self.fake.calls.clear()
        state = await self.finished(["claude", "codex"])
        self.assertEqual(self.moved(state), [("T1", "codex", "claude")])
        self.assertEqual(self.workers(), {"T1": "claude"})

    async def test_a_review_waits_for_a_busy_partner_rather_than_use_a_reserve(self):
        # Catches the reviewer choice falling through to an account on its last few percent just because the others were busy.
        self.profiles = {"codex": ("codex", "normal", True)}
        self.plan = [plan("T1", "claude"), plan("T2", "third")]
        room, seen = {}, {}
        async def until():
            # Keep third busy well past the moment T1 is ready for review.
            state = await self.service.read_room(room["id"])
            if any(task["id"] == "T1" and task["status"] == "review" for task in state["team"]["tasks"]):
                seen.setdefault("at", asyncio.get_running_loop().time())
            return "at" in seen and asyncio.get_running_loop().time() - seen["at"] > 0.5
        self.hold_task("T2", until=until)
        created = await self.create(participants=["claude", "codex", "third"])
        room["id"] = created["room"]["id"]
        await self.settle()
        state = await self.service.read_room(room["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertIn("at", seen, "T1 never waited for review while third was busy")
        self.assertEqual({task["id"]: task["reviewer"] for task in state["team"]["tasks"]}, {"T1": "third", "T2": "claude"})

    async def test_a_task_that_owns_many_files_is_planned_and_delivered(self):
        # Catches a fixed small cap (it was 16 paths and 8 dependencies) rejecting a real plan on a large project,
        # or the delivered files being refused at import because a task changed more than 16 of them.
        paths = [f"part_{index:02d}.py" for index in range(25)]
        self.plan = [plan(f"P{index}", "claude", paths=[f"pre_{index}.py"]) for index in range(10)]
        self.plan.append(plan("BIG", "codex", paths=paths, dependencies=[f"P{index}" for index in range(10)]))
        state = await self.finished(["claude", "codex"])
        big = next(task for task in state["team"]["tasks"] if task["id"] == "BIG")
        self.assertEqual((big["status"], len(big["write_paths"]), len(big["dependencies"])), ("accepted", 25, 10))
        self.assertEqual(sorted(entry["path"] for entry in big["result_files"]), paths)
        self.assertFalse(any(event["kind"] == "native_response_rejected" for event in state["events"]))

    async def test_a_rejected_plan_is_told_which_rule_it_broke(self):
        # Catches a rejection that names only an error code, so the partner rewrites the plan by guessing.
        attempts = []
        original = self.propose
        def propose(context, board):
            attempts.append(1)
            if len(attempts) == 1:
                return [native.proposal("T1", paths=["src/"], capability="python", dependencies=[]), {"type": "claim", "task_id": "T1"}]
            return original(context, board)
        self.propose = propose
        self.plan = [plan("T1", "claude")]
        state = await self.finished(["claude", "codex"])
        self.assertGreaterEqual(len(attempts), 2)
        correction = next(call for call in self.fake.calls if "its response was rejected" in call["prompt"])
        self.assertIn("Error code: invalid_team_task", correction["prompt"])
        self.assertIn("no leading or trailing '/'", correction["prompt"])
        self.assertIn("at most 1000", correction["prompt"])

    async def test_a_room_with_one_partner_does_not_rebalance(self):
        # Catches the balancer running where there is nobody to balance with.
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 4)]
        state = await self.finished(["claude"])
        self.assertEqual(self.moved(state), [])
        self.assertEqual(set(self.owners(state).values()), {"claude"})

    async def test_two_partners_of_the_same_tool_share_the_work(self):
        # Catches two accounts of one tool being treated as one worker: limits differ per account, not per tool.
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 4)]
        state = await self.finished(["claude", "third"])
        self.assertEqual(self.moved(state), [("T3", "claude", "third")])

    def hold_task(self, task_id, *, until):
        """Keep one task's work call in progress until the condition holds, so a later trigger meets a started task."""
        self.held_task = task_id
        self.entered = asyncio.Event()
        original = self.fake.handler
        async def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "work" and context["task"]["id"] == task_id:
                self.entered.set()
                for _ in range(400):
                    if await until():
                        break
                    await asyncio.sleep(0.01)
            return await original(agent, context, cwd)
        self.fake.handler = handler

    async def wait_for_rebalance(self, room_id):
        state = await self.service.read_room(room_id)
        return bool(self.moved(state))

    async def test_an_accepted_task_lets_unstarted_work_move(self):
        # Catches the balance being done only once: a partner who finishes early must pick up unstarted work.
        self.plan = [plan("T1", "claude"), plan("T2", "claude"), plan("T3", "codex"), plan("T4", "third")]
        room = {}
        self.hold_task("T1", until=lambda: self.wait_for_rebalance(room["id"]))
        created = await self.create(participants=["claude", "codex", "third"])
        room["id"] = created["room"]["id"]
        await self.settle()
        state = await self.service.read_room(room["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(len(self.moved(state)), 1)
        self.assertEqual(self.moved(state)[0][:2], ("T2", "claude"))
        self.assertIn(self.moved(state)[0][2], {"codex", "third"})
        self.assertEqual(self.owners(state)["T1"], "claude", "the task in progress stays")
        self.assertEqual(self.workers()["T2"], self.moved(state)[0][2])

    async def test_a_partner_joining_mid_run_gets_unstarted_work(self):
        # Catches a newcomer sitting idle while others hold unstarted tasks, or a started task being taken from its owner.
        self.plan = [plan("T1", "claude"), plan("T2", "claude"), plan("T3", "claude")]
        room = {}
        joined = asyncio.Event()
        async def until():
            return joined.is_set() and await self.wait_for_rebalance(room["id"])
        self.hold_task("T1", until=until)
        created = await self.create(participants=["claude"])
        room["id"] = created["room"]["id"]
        await asyncio.wait_for(self.entered.wait(), 5)
        await self.service.invite_members(room["id"], ["codex"], str(uuid4()))
        joined.set()
        await self.settle()
        state = await self.service.read_room(room["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        self.assertEqual(self.moved(state), [("T3", "claude", "codex")])
        self.assertEqual(self.owners(state)["T1"], "claude")
        self.assertEqual(self.workers()["T3"], "codex")
        invited = next(call for call in self.fake.calls if call["agent"] == "codex" and call["context"]["job"]["job_phase"] == "coordinate")
        self.assertIn("Preserve the owners of started tasks and reviewed results", invited["prompt"])
        self.assertNotIn("Preserve existing owners and reviewed results", invited["prompt"])

    async def test_a_task_with_a_queued_work_call_stays_with_its_owner(self):
        # Catches a queued work call being overtaken: it is the owner's next step, and its task is skipped, not moved.
        self.plan = [plan("T1", "claude"), plan("T2", "claude"), plan("T3", "claude")]
        room = {}
        joined = asyncio.Event()
        async def until():
            return joined.is_set()
        self.hold_task("T1", until=until)
        created = await self.create(participants=["claude"])
        room["id"] = created["room"]["id"]
        await asyncio.wait_for(self.entered.wait(), 5)
        team = self.service.store._team
        with self.service.store._connection(write=True) as db:
            # The owner is busy on T1, so the normal gate would hold this back; this is the window being tested.
            with patch.object(team, "_busy", return_value=False):
                self.assertTrue(team._queue(db, room["id"], "claude", "work", task=team.task(db, room["id"], "T3")))
        await self.service.invite_members(room["id"], ["codex"], str(uuid4()))
        with self.service.store._connection() as db:
            queued = db.execute("SELECT j.status FROM jobs j JOIN team_jobs t ON t.job_id=j.id WHERE t.task_id='T3' AND t.phase='work'").fetchall()
        self.assertEqual([row[0] for row in queued], ["queued"], "the queued call was left in place")
        joined.set()
        await self.settle()
        state = await self.service.read_room(room["id"])
        self.assertEqual(self.moved(state), [("T2", "claude", "codex")])
        self.assertEqual((self.owners(state)["T3"], self.workers()["T3"]), ("claude", "claude"))

    async def test_a_moved_task_never_moves_again(self):
        # Catches a task bouncing between partners when a later trigger finds its new owner over quota.
        self.profiles = {"claude": ("claude", "normal"), "codex": ("codex", "low"), "ember": ("ember", "high")}
        self.plan = [plan(f"T{n}", "claude") for n in range(1, 6)]
        room = {}
        joined = asyncio.Event()
        async def until():
            return joined.is_set()
        self.hold_task("T1", until=until)
        created = await self.create(participants=["claude", "codex"])
        room["id"] = created["room"]["id"]
        await asyncio.wait_for(self.entered.wait(), 5)
        self.assertEqual(self.moved(await self.service.read_room(room["id"])), [("T5", "claude", "codex"), ("T4", "claude", "codex")])
        await self.service.invite_members(room["id"], ["ember"], str(uuid4()))
        joined.set()
        await self.settle()
        state = await self.service.read_room(room["id"])
        moves = self.moved(state)
        self.assertEqual(moves[:3], [("T5", "claude", "codex"), ("T4", "claude", "codex"), ("T3", "claude", "ember")])
        self.assertEqual(max(Counter(item[0] for item in moves).values()), 1, "no task moved twice")
        self.assertEqual((self.owners(state)["T4"], self.owners(state)["T5"]), ("codex", "codex"))

    async def test_a_claim_made_for_the_user_is_not_moved(self):
        # Catches a task the user directed being taken away; the ordinary tail choice would have moved it.
        self.plan = [plan("T1", "claude"), plan("T2", "claude"), plan("T3", None)]
        self.hold_coordinate = asyncio.Event()
        self.user_claims = ["T3"]
        created = await self.create(participants=["claude", "codex"])
        room_id = created["room"]["id"]
        for _ in range(200):
            state = await self.service.read_room(room_id)
            if state["team"]["tasks"] and any(call["agent"] == "codex" and call["context"]["job"]["job_phase"] == "coordinate" for call in self.fake.calls):
                break
            await asyncio.sleep(0.01)
        await self.service.send_message(room_id, "Please take the last part yourself.", "claude", str(uuid4()), source="user")
        for _ in range(200):
            state = await self.service.read_room(room_id)
            if next(task for task in state["team"]["tasks"] if task["id"] == "T3")["owner"] == "claude":
                break
            await asyncio.sleep(0.01)
        self.hold_coordinate.set()
        await self.settle()
        state = await self.service.read_room(room_id)
        self.assertEqual(self.moved(state), [("T2", "claude", "codex")])
        self.assertEqual(self.owners(state)["T3"], "claude")


if __name__ == "__main__":
    unittest.main()
