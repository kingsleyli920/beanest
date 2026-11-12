"""Account shares and the per-round ledger. Fictional projects and partner mixes; only the native transport is synthetic."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_agent_coordinator import workload
from local_agent_coordinator.agent_registry import AgentRegistry, default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from tests import test_native_room_service as native

native_reply = native.native_reply


def load(points=0.0, calls=0, reported=None):
    return {"points": points, "calls": calls, "reported": calls if reported is None else reported}


def profile_from(table):
    return lambda member: table[member]


class UsagePoints(unittest.TestCase):
    def test_separately_counted_cache_is_weighted_on_top_of_input(self):
        # Catches cached reads priced like fresh input, which would make long resumed sessions dominate the ledger.
        usage = {"values": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}}
        self.assertEqual(workload.usage_points("claude", usage), 500.0)

    def test_cache_included_in_input_is_not_counted_twice(self):
        # Catches Codex's cached tokens being charged both as input and as cache.
        usage = {"values": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 10}}
        self.assertEqual(workload.usage_points("codex", usage), 330.0)

    def test_a_running_total_is_charged_only_for_this_call(self):
        # Catches a session's whole history being charged again on every resumed call.
        before = {"values": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 10}}
        after = {"values": {"input_tokens": 1500, "cached_input_tokens": 1200, "output_tokens": 20}}
        self.assertEqual(workload.usage_points("codex", after, previous=before, resumed=True), 190.0)

    def test_a_running_total_without_a_trustworthy_start_says_nothing(self):
        # Catches a missing or reset baseline being read as one enormous call.
        after = {"values": {"input_tokens": 1500, "cached_input_tokens": 1200, "output_tokens": 20}}
        larger = {"values": {"input_tokens": 9000, "cached_input_tokens": 1200, "output_tokens": 20}}
        self.assertIsNone(workload.usage_points("codex", after, previous=None, resumed=True))
        self.assertIsNone(workload.usage_points("codex", after, previous=larger, resumed=True))

    def test_a_new_session_ignores_the_previous_sessions_total(self):
        # Catches the first call of a fresh session being reduced by its predecessor's counts.
        before = {"values": {"input_tokens": 9000, "cached_input_tokens": 0, "output_tokens": 900}}
        after = {"values": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 0}}
        self.assertEqual(workload.usage_points("codex", after, previous=before, resumed=False), 100.0)

    def test_a_provider_nobody_has_described_still_counts(self):
        # Catches a newly connected tool being treated as free because it has no line of its own.
        usage = {"values": {"input_tokens": 10, "output_tokens": 2}}
        self.assertEqual(workload.usage_points("some_future_cli", usage), 20.0)

    def test_nothing_reported_is_none_not_zero(self):
        # Catches a silent provider looking as if the call cost nothing.
        for usage in (None, {}, {"values": {}}, {"values": {"unrelated": 5}}):
            self.assertIsNone(workload.usage_points("kimi", usage))


class Accounts(unittest.TestCase):
    def test_partners_on_one_account_are_one_row(self):
        # Catches two partners on one login each being given a full share of the same quota.
        profile = profile_from({"a": ("shared", "high"), "b": ("shared", "high"), "c": ("other", "low")})
        rows = workload.accounts(["a", "b", "c"], {"a": load(30, 1), "b": load(10, 1), "c": load(5, 1)}, profile)
        self.assertEqual([(row["scope"], row["members"], row["points"], row["share"]) for row in rows],
                         [("shared", ["a", "b"], 40.0, 3), ("other", ["c"], 5.0, 1)])

    def test_a_silent_call_counts_as_an_average_call(self):
        # Catches a provider without usage data always looking least used and being given everything.
        profile = profile_from({"a": ("a", "normal"), "b": ("b", "normal")})
        rows = workload.accounts(["a", "b"], {"a": load(100, 2), "b": load(0, 3, reported=0)}, profile)
        self.assertEqual({row["scope"]: row["points"] for row in rows}, {"a": 100.0, "b": 150.0})

    def test_nothing_reported_by_anyone_falls_back_to_counting_calls(self):
        # Catches a room of silent providers dividing by zero or showing no difference.
        profile = profile_from({"a": ("a", "normal"), "b": ("b", "normal")})
        rows = workload.accounts(["a", "b"], {"a": load(0, 4, reported=0), "b": load(0, 1, reported=0)}, profile)
        self.assertEqual({row["scope"]: row["points"] for row in rows}, {"a": 4.0, "b": 1.0})


class FairOrder(unittest.TestCase):
    def test_equal_shares_keep_the_given_order(self):
        # Catches a room nobody configured changing who reviews.
        profile = profile_from({"a": ("a", "normal"), "b": ("b", "normal")})
        self.assertEqual(workload.fair_order(["a", "b"], ["a", "b"], {"a": load(900, 1)}, profile), ["a", "b"])

    def test_equal_candidates_in_a_configured_room_still_balance(self):
        # Catches two ordinary accounts ignoring usage because a third account has a different share.
        profile = profile_from({"a": ("a", "high"), "b": ("b", "normal"), "c": ("c", "normal")})
        self.assertEqual(workload.fair_order(["b", "c"], ["a", "b", "c"], {"b": load(900, 1), "c": load(10, 1)}, profile)[0], "c")

    def test_ties_go_to_the_larger_share(self):
        # Catches the small account being handed the first job of every round.
        profile = profile_from({"low": ("low", "low"), "high": ("high", "high")})
        self.assertEqual(workload.fair_order(["low", "high"], ["low", "high"], {}, profile), ["high", "low"])

    def test_equal_jobs_land_in_the_set_proportion(self):
        # Catches an inverted ratio or one account starving.
        profile = profile_from({"low": ("low", "low"), "high": ("high", "high")})
        loads, counts = {"low": load(), "high": load()}, {"low": 0, "high": 0}
        for _ in range(8):
            chosen = workload.fair_order(["low", "high"], ["low", "high"], loads, profile)[0]
            loads[chosen] = load(loads[chosen]["points"] + 100, loads[chosen]["calls"] + 1)
            counts[chosen] += 1
        self.assertEqual(counts, {"low": 2, "high": 6})

    def test_one_huge_job_is_paid_back_by_later_ones(self):
        # Catches the scheduler counting jobs instead of what they really cost.
        profile = profile_from({"a": ("a", "low"), "b": ("b", "high")})
        self.assertEqual(workload.fair_order(["a", "b"], ["a", "b"], {"a": load(10, 1), "b": load(6000, 1)}, profile)[0], "a")


class Summary(unittest.TestCase):
    def test_percentages(self):
        # Catches the card showing a share of the wrong total.
        profile = profile_from({"a": ("a", "high"), "b": ("b", "low")})
        value = workload.summary(["a", "b"], {"a": load(60, 1), "b": load(40, 1)}, profile)
        self.assertTrue(value["configured"])
        self.assertEqual([(row["actual_percent"], row["target_percent"]) for row in value["accounts"]], [(60, 75), (40, 25)])

    def test_a_single_account_has_nothing_to_compare(self):
        # Catches a meaningless 100% line on solo rooms and rooms sharing one login.
        profile = profile_from({"a": ("same", "normal"), "b": ("same", "normal")})
        self.assertIsNone(workload.summary(["a", "b"], {"a": load(60, 1)}, profile))

    def test_no_work_yet_has_no_percentage(self):
        # Catches 0% being displayed before anyone has done anything.
        profile = profile_from({"a": ("a", "normal"), "b": ("b", "normal")})
        self.assertIsNone(workload.summary(["a", "b"], {}, profile)["accounts"][0]["actual_percent"])


class PartnerSetting(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "partners"
        async def unused(*args, **kwargs):
            raise AssertionError("Settings must not invoke a model")
        self.base = default_registry(unused)
        self.manager = PartnerManager(self.root, self.base, clock=lambda: 42)

    def level(self, manager, partner):
        return next(item for item in manager.profiles() if item["id"] == partner)["workload_level"]

    def test_default_setting_and_persistence(self):
        # Catches an unset account failing to load, or a choice lost on restart.
        self.assertEqual(self.level(self.manager, "claude"), "normal")
        self.manager.set_workload("claude", "high")
        self.assertEqual(self.level(PartnerManager(self.root, self.base, clock=lambda: 42), "claude"), "high")
        self.assertEqual(self.level(self.manager, "codex"), "normal", "another account is untouched")

    def test_the_setting_belongs_to_the_account_not_the_alias(self):
        # Catches two partners on one login holding contradictory shares.
        self.manager.add(id="alias", name="Second", adapter="claude")
        self.manager.set_workload("alias", "low")
        self.assertEqual(self.level(self.manager, "claude"), "low")
        scopes = self.manager.workload_profiles()
        self.assertEqual(scopes["alias"], scopes["claude"])

    def test_a_low_account_reading_reaches_scheduling_and_the_card(self):
        # Catches the reserve being computed but dropped before dispatch or the partner card.
        reading = {"state": "ready", "email": "person@example.test", "plan": "plus", "auth_method": "chatgpt",
                   "source": "codex_app_server", "scope": "default_cli_login", "reason": None, "checked_at": 40, "stale": False,
                   "quota": {"state": "available", "source": "codex_app_server_account_rate_limits", "scope": "provider_account",
                             "reason": None, "checked_at": 40, "stale": False,
                             "windows": [{"name": "codex:primary", "used_percent": 91, "resets_at": 9000}]}}
        self.manager._account_cache["codex"] = reading
        self.assertTrue(self.manager.workload_profiles()["codex"][2])
        self.assertFalse(self.manager.workload_profiles()["claude"][2])
        self.assertEqual(self.manager.get("codex")["quota_reserve"], 9)
        self.assertIsNone(self.manager.get("claude")["quota_reserve"])

    def test_invalid_input_changes_nothing(self):
        # Catches a bad level being written and then breaking every later load.
        self.manager.set_workload("claude", "high")
        for level in ("huge", "", 3, None):
            with self.assertRaises(PartnerError):
                self.manager.set_workload("claude", level)
        with self.assertRaises(PartnerError):
            self.manager.set_workload("nobody", "low")
        self.assertEqual(self.level(self.manager, "claude"), "high")


def three_partners():
    """Two built-in tools plus a second partner on the first tool's adapter, as a user can add."""
    base = default_registry()
    return AgentRegistry([*(base.get(key) for key in base.ids()), replace(base.get("claude"), id="third", name="Third")])


class WorkloadService(unittest.IsolatedAsyncioTestCase):
    """A three-partner room on a fictional project, through real storage and the job loop."""
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        with patch.object(native, "default_registry", three_partners):
            await native.NativeRoomServiceTests.asyncSetUp(self)
        self.fake.handler = self.handler
        self.task_ids = ["T1", "T2", "T3", "T4"]
        self.profiles = {}
        self.review_tokens = 1000
        self.totals = {}
        self.service.store.account_workloads = lambda: self.profiles
        native.no_rebalance(self.service)
        invoke = self.service.native_invoke
        async def with_usage(agent, prompt, cwd, session_id=None, **options):
            result = await invoke(agent, prompt, cwd, session_id=session_id, **options)
            phase = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])["job"]["job_phase"]
            tokens = self.review_tokens if phase == "review" else 0
            # Codex reports a running total per session; the others report each call.
            self.totals[agent] = self.totals.get(agent, 0) + tokens
            result["usage"] = {"input_tokens": self.totals[agent] if agent == "codex" else tokens, "cached_input_tokens": 0, "output_tokens": 0}
            return result
        self.service.native_invoke = with_usage

    def handler(self, agent, context, cwd):
        phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and agent == board["plan_author"]:
                for index, task_id in enumerate(self.task_ids):
                    commands += [native.proposal(task_id, paths=[f"{task_id}.py"], capability="python",
                                                 dependencies=self.task_ids[:index][-1:]), {"type": "claim", "task_id": task_id}]
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            (cwd / f"{task['id']}.py").write_text("VALUE = 1\n")
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Written."}])
        if phase == "review":
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact tree."}])
        if phase == "checkpoint":
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Done.", "next_goal": None,
                                           "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Compared.",
                                           "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    async def finished(self, target="claude"):
        if target == "claude":
            created = await self.create(participants=["claude", "codex", "third"])
        else:
            created = await self.service.create_room(
                "Update the existing value and preserve notes.", target, str(native.uuid4()), mode="native_project",
                project_id=self.registration["project"]["id"], participants=["claude", "codex", "third"], work_mode="build",
                runtime_settings={"permission": "full_access", "members": {}}, execution_policy={"mode": "autonomous"})
        await self.settle()
        state = await self.service.read_room(created["room"]["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        return state

    @staticmethod
    def reviewers(state):
        return [task["reviewer"] for task in state["team"]["tasks"]]

    async def test_reviews_follow_the_account_shares(self):
        # Catches shares being stored but never reaching the reviewer choice, and Codex's running total being overcharged.
        self.profiles = {"claude": ("claude", "normal"), "codex": ("codex", "high"), "third": ("third", "low")}
        state = await self.finished()
        self.assertEqual(self.reviewers(state), ["codex", "third", "codex", "codex"])
        accounts = {row["scope"]: row for row in state["workload"]["accounts"]}
        self.assertEqual((accounts["codex"]["points"], accounts["third"]["points"]), (3000, 1000))
        self.assertEqual((accounts["codex"]["actual_percent"], accounts["third"]["actual_percent"]), (75, 25))
        self.assertEqual(accounts["codex"]["target_percent"], 50)

    async def test_planners_are_told_related_work_is_not_a_dependency(self):
        # Catches planners being left to chain tasks that partners could do at the same time.
        await self.finished()
        plan = next(call for call in self.fake.calls if call["context"]["job"]["job_phase"] == "plan")
        self.assertIn("merely related or similar has no dependency", plan["prompt"])
        # Catches a task that is only partly blocked keeping its free part waiting (a real plan bundled "queue the lease" with "decide after the test").
        self.assertIn("split off the part that does not into its own task", plan["prompt"])
        # Catches a plan that chains work only because the user numbered it, when nothing in it needs the earlier result.
        self.assertIn("The order or numbering in which the user lists things is not a dependency", plan["prompt"])

    async def test_an_unconfigured_room_chooses_as_it_always_did(self):
        # Catches the feature changing behaviour for rooms that never set a share.
        self.profiles = {}
        state = await self.finished()
        self.assertEqual(set(self.reviewers(state)), {"codex"})
        self.assertFalse(state["workload"]["configured"])

    async def test_two_partners_on_one_account_are_charged_together(self):
        # Catches a shared login looking twice as idle as it is.
        self.profiles = {"claude": ("claude", "normal"), "codex": ("pool", "low"), "third": ("pool", "low")}
        state = await self.finished()
        pool = next(row for row in state["workload"]["accounts"] if row["scope"] == "pool")
        self.assertEqual(sorted(pool["members"]), ["codex", "third"])
        self.assertEqual(pool["points"], 4000)

    async def test_an_account_down_to_its_reserve_reviews_only_when_nobody_else_can(self):
        # Catches the reserve being shown but never reaching the reviewer choice.
        self.profiles = {"codex": ("codex", "normal", True)}
        state = await self.finished()
        self.assertNotIn("codex", self.reviewers(state))
        self.assertEqual(state["team"]["plan_author"], "claude")

    async def test_an_account_down_to_its_reserve_sits_out_planning(self):
        # Catches an account on its last few percent still being asked to discuss and confirm every plan.
        def planning_calls():
            return [call["context"]["self"] for call in self.fake.calls if call["context"]["job"]["job_phase"] in {"plan", "coordinate"}]
        await self.finished()
        self.assertIn("codex", planning_calls(), "without a reserve every member plans")
        self.fake.calls.clear()
        self.profiles = {"codex": ("codex", "normal", True)}
        state = await self.finished()
        self.assertNotIn("codex", planning_calls())
        self.assertIn("third", planning_calls())
        self.assertNotIn("codex", [task["owner"] for task in state["team"]["tasks"]])

    async def test_a_planner_down_to_its_reserve_hands_the_plan_on(self):
        # Catches the first member writing the plan from its last few percent while others have plenty.
        self.profiles = {"claude": ("claude", "normal", True)}
        state = await self.finished("all")
        self.assertEqual(state["team"]["plan_author"], "codex")
        # A partner the user names still writes the plan.
        state = await self.finished("claude")
        self.assertEqual(state["team"]["plan_author"], "claude")

    async def test_everyone_down_to_the_reserve_keeps_working_as_before(self):
        # Catches the protection stopping a room that has nobody better to use.
        self.profiles = {member: (member, "normal", True) for member in ("claude", "codex", "third")}
        state = await self.finished()
        self.assertEqual(set(self.reviewers(state)), {"codex"})
        self.assertEqual(state["team"]["plan_author"], "claude")
