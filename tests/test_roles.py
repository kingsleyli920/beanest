"""Per-room roles and workload. Fictional project and partner mix; only the native transport is synthetic."""
import json
import unittest
from collections import Counter
from uuid import uuid4

from local_agent_coordinator import roles
from local_agent_coordinator.runtime_settings import RuntimeSettingsError, model_choice, normalized_settings
from tests import test_native_room_service as native
from tests.test_workload import WorkloadService

MEMBERS = ["codex", "third", "claude"]


def custom(name="文档", duty="只写说明文档", abilities=("work",), identifier="custom:docs"):
    return {"id": identifier, "name": name, "duty": duty, "abilities": list(abilities)}


class RoleSettings(unittest.TestCase):
    def settings(self, members):
        return normalized_settings({"permission": "full_access", "members": members}, MEMBERS, "build")

    def test_builtin_roles_are_stored_by_id_with_their_abilities(self):
        # Catches a client-sent ability list overriding what a built-in role may do.
        stored = self.settings({"codex": {"role": {"id": "reviewer", "abilities": ["plan", "work"]}}})
        self.assertEqual(stored["members"]["codex"], {"model": None, "effort": None,
                                                     "role": {"id": "reviewer", "abilities": ["review"]}})

    def test_all_round_and_unset_keep_the_old_shape(self):
        # Catches every existing room's settings changing shape because the feature exists.
        stored = self.settings({"codex": {"role": {"id": "all"}}, "third": {"model": None, "effort": None}})
        self.assertEqual(stored["members"]["codex"], {"model": None, "effort": None})
        self.assertEqual(stored["members"]["third"], {"model": None, "effort": None})

    def test_custom_role_keeps_the_users_words_and_orders_abilities(self):
        # Catches a custom role losing its duty text or storing abilities in arbitrary order.
        stored = self.settings({"claude": {"role": custom(abilities=["review", "work"]), "workload": "high"}})
        self.assertEqual(stored["members"]["claude"], {"model": None, "effort": None, "workload": "high",
            "role": {"id": "custom:docs", "name": "文档", "duty": "只写说明文档", "abilities": ["work", "review"]}})

    def test_bad_roles_and_levels_are_refused(self):
        # Catches malformed input reaching the scheduler.
        for choice in ({"role": {"id": "boss"}}, {"role": custom(abilities=[])}, {"role": custom(abilities=["fly"])},
                       {"role": custom(name="")}, {"role": custom(name="x" * 21)}, {"role": custom(duty="x" * 301)},
                       {"role": custom(identifier="custom:Bad Id")}, {"role": {**custom(), "extra": 1}},
                       {"role": custom(abilities=["work", "work"])}, {"workload": "max"}, {"role": "builder"}):
            with self.subTest(choice=choice), self.assertRaises(RuntimeSettingsError):
                self.settings({"codex": choice})

    def test_tools_receive_only_the_model_choice(self):
        # Catches Kimi refusing every call once a role is set (it rejects unknown keys).
        stored = self.settings({"codex": {"model": "m1", "effort": "high", "role": {"id": "builder"}, "workload": "low"}})
        self.assertEqual(model_choice(stored["members"]["codex"]), {"model": "m1", "effort": "high"})

    def test_prefer_falls_back_to_everyone(self):
        # Catches a room waiting forever when no member's role fits a step.
        abilities = {"codex": frozenset({"work"}), "third": frozenset({"work"})}
        self.assertEqual(roles.prefer(["codex", "third"], "review", abilities), ["codex", "third"])
        self.assertEqual(roles.prefer(["codex", "claude"], "review", abilities), ["claude"])


class RoleService(unittest.IsolatedAsyncioTestCase):
    """A three-partner room on a fictional project, through real storage and the job loop."""
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    def handler(self, agent, context, cwd):
        # "third" runs on Claude's tool: answer as the member the prompt addresses.
        return WorkloadService.handler(self, context["self"], context, cwd)
    reviewers = staticmethod(WorkloadService.reviewers)

    async def asyncSetUp(self):
        await WorkloadService.asyncSetUp(self)
        # Planned owners may be handed over, so a role that does not build can give its work away.
        self.service.store.task_capabilities = lambda provider: ("text", "python")

    async def create_roles(self, members, *, target="all"):
        return await self.service.create_room("Update the existing value and preserve notes.", target, str(uuid4()),
            mode="native_project", project_id=self.registration["project"]["id"], participants=MEMBERS,
            collaboration=True, work_mode="build", runtime_settings={"permission": "full_access", "members": members},
            execution_policy={"mode": "autonomous"})

    async def finished_with(self, members, *, target="all"):
        created = await self.create_roles(members, target=target)
        await self.settle()
        state = await self.service.read_room(created["room"]["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        return state

    def calls_by(self):
        # The fake records the tool; "third" runs on Claude's tool, so name members by the prompt's own id.
        return Counter((call["context"]["self"], call["context"]["job"]["job_phase"]) for call in self.fake.calls)

    async def test_each_step_goes_to_the_role_that_does_it(self):
        # Catches roles being stored but not steering who plans, builds, reviews and checks.
        # Members are listed codex, third, claude: without roles codex would plan and third would review first.
        state = await self.finished_with({"third": {"role": {"id": "planner"}}, "codex": {"role": {"id": "builder"}},
                                          "claude": {"role": {"id": "reviewer"}}})
        calls = self.calls_by()
        self.assertEqual(state["team"]["plan_author"], "third")
        self.assertEqual({task["owner"] for task in state["team"]["tasks"]}, {"codex"})
        self.assertEqual(set(self.reviewers(state)), {"claude"})
        self.assertEqual({agent for agent, phase in calls if phase == "work"}, {"codex"})
        self.assertEqual({agent for agent, phase in calls if phase == "checkpoint"}, {"third"})
        self.assertEqual({agent for agent, phase in calls if phase == "checkpoint_review"}, {"claude"})
        # The reviewer never spends calls on planning.
        self.assertEqual(calls[("claude", "plan")] + calls[("claude", "coordinate")], 0)

    async def test_work_stays_with_the_builder_on_a_shared_account(self):
        # Catches work being handed to a reviewer because it shares a login with the builder.
        self.profiles = {"codex": ("pool", "normal"), "claude": ("pool", "normal"), "third": ("third", "normal")}
        state = await self.finished_with({"third": {"role": {"id": "planner"}}, "codex": {"role": {"id": "builder"}},
                                          "claude": {"role": {"id": "reviewer"}}})
        self.assertEqual({task["owner"] for task in state["team"]["tasks"]}, {"codex"})

    async def test_each_partner_is_told_its_own_role(self):
        # Catches the role reaching the scheduler but not the partner.
        await self.finished_with({"codex": {"role": {"id": "builder"}}, "third": {"role": custom(name="审稿", duty="逐行核对", abilities=["review"])}})
        work = next(call for call in self.fake.calls if call["context"]["self"] == "codex" and call["context"]["job"]["job_phase"] == "work")
        review = next(call for call in self.fake.calls if call["context"]["self"] == "third")
        self.assertEqual(work["context"]["self_role"]["id"], "builder")
        self.assertEqual(review["context"]["self_role"], {"id": "custom:docs", "abilities": ["review"], "duty": "逐行核对", "name": "审稿"})
        self.assertIn("context.self_role is the user's division of labour", review["prompt"])
        claude = next(call for call in self.fake.calls if call["context"]["self"] == "claude")
        self.assertNotIn("self_role", claude["context"])
        self.assertNotIn("context.self_role", claude["prompt"])

    async def test_a_partners_tool_gets_only_its_model_choice(self):
        # Catches the role and level being handed to the tool; Kimi refuses any unknown key and every call fails.
        self.service._runtime_catalogs = {"codex": {"state": "ready", "models": [{"id": "codex-fixture", "efforts": ["high"]}]}}
        await self.finished_with({"codex": {"model": "codex-fixture", "effort": "high", "role": {"id": "builder"}, "workload": "high"}})
        sent = [call["runtime_settings"] for call in self.fake.calls if call["context"]["self"] == "codex"]
        self.assertTrue(sent)
        self.assertEqual({json.dumps(value, sort_keys=True) for value in sent}, {'{"effort": "high", "model": "codex-fixture"}'})

    async def test_a_room_nobody_can_review_in_still_finishes(self):
        # Catches a missing role leaving delivered work unreviewed and the room stuck.
        state = await self.finished_with({member: {"role": {"id": "builder"}} for member in MEMBERS})
        self.assertTrue(all(self.reviewers(state)))
        self.assertEqual(state["team"]["plan_author"], "codex")

    async def test_naming_a_partner_keeps_the_users_choice_of_planner(self):
        # Catches roles overriding the partner the user explicitly addressed.
        created = await self.create_roles({"claude": {"role": {"id": "planner"}}, "codex": {"role": {"id": "reviewer"}}}, target="codex")
        state = await self.service.read_room(created["room"]["id"])
        self.assertEqual(state["team"]["plan_author"], "codex")

    async def test_a_room_level_overrides_the_account_level(self):
        # Catches the per-room workload being saved but the account setting still deciding reviews.
        self.profiles = {"claude": ("claude", "normal"), "codex": ("codex", "normal"), "third": ("third", "normal")}
        state = await self.finished_with({"codex": {"workload": "high"}, "third": {"workload": "low"}, "claude": {"workload": "normal"}})
        self.assertTrue(state["workload"]["configured"])
        accounts = {row["scope"]: row for row in state["workload"]["accounts"]}
        self.assertEqual((accounts["codex"]["target_percent"], accounts["third"]["target_percent"]), (50, 17))
        counts = Counter(self.reviewers(state))
        self.assertGreater(counts["codex"], counts["third"])

    async def test_cost_is_shown_by_step(self):
        # Catches the ledger only knowing totals, so savings from roles could not be seen.
        state = await self.finished_with({})
        phases = {row["phase"]: row for row in state["workload"]["phases"]}
        self.assertEqual(phases["review"]["points"], 4000)
        self.assertEqual(phases["review"]["calls"], 4)
        self.assertEqual(sum(row["calls"] for row in phases.values()), len(self.fake.calls))
        self.assertEqual(state["workload"]["phases"][0]["phase"], "review")


if __name__ == "__main__":
    unittest.main()
