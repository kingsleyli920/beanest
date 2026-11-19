"""The partners split a round's work themselves, inside what the user fixed.

Fictional projects and partner mixes. The service tests use real storage and
the real job loop; only the native transport is synthetic.
"""
import asyncio
import json
import unittest
from uuid import uuid4

from local_agent_coordinator import workload
from local_agent_coordinator.room_prompts import ALLOCATE_INSTRUCTION
from local_agent_coordinator.room_response_schema import native_room_response_schema, response_schema
from local_agent_coordinator.room_service import _response
from local_agent_coordinator.runtime_settings import RuntimeSettingsError, normalized_settings
from tests import test_native_room_service as native
import test_task_pool as pool

native_reply = native.native_reply
plan = pool.plan


def load(points=0.0, calls=0):
    return {"points": points, "calls": calls, "reported": calls}


def profile_of(members, limits=None, allocation=None):
    shares = workload.targets(members, limits, allocation)
    return lambda member: (member, "normal", *shares[member])


class Targets(unittest.TestCase):
    def test_an_untouched_room_keeps_plain_level_parts_and_the_given_order(self):
        # Catches the new shares changing who goes first in a room nobody configured.
        self.assertEqual(workload.targets(["a", "b", "c"]), {"a": (2, "default"), "b": (2, "default"), "c": (2, "default")})
        profile = profile_of(["a", "b"])
        self.assertEqual(workload.fair_order(["a", "b"], ["a", "b"], {"a": load(900, 1), "b": load(10, 1)}, profile), ["a", "b"])

    def test_levels_alone_mean_what_they_always_did(self):
        # Catches a fixed level being turned into some other proportion than 1, 2 or 3 parts.
        limits = {"a": ("level", "high"), "b": ("level", "low")}
        self.assertEqual(workload.targets(["a", "b", "c"], limits), {"a": (3, "user"), "b": (1, "user"), "c": (2, "default")})

    def test_the_partners_allocation_becomes_the_shares(self):
        # Catches an allocation being stored but never reaching the order work is handed out in.
        self.assertEqual(workload.targets(["a", "b"], None, {"a": 80, "b": 20}), {"a": (80.0, "partners"), "b": (20.0, "partners")})
        profile = profile_of(["a", "b"], None, {"a": 80, "b": 20})
        self.assertEqual(workload.fair_order(["b", "a"], ["a", "b"], {"a": load(100, 1), "b": load(100, 1)}, profile), ["a", "b"])

    def test_shares_that_do_not_add_up_are_scaled_to_the_whole(self):
        # Catches shares being read as absolute percents, so a split of 30 and 30 would leave 40 for nobody.
        self.assertEqual(workload.targets(["a", "b"], None, {"a": 30, "b": 30}), {"a": (50.0, "partners"), "b": (50.0, "partners")})

    def test_a_member_the_partners_never_mentioned_gets_an_even_part(self):
        # Catches a member who joined after the allocation being left with nothing.
        self.assertEqual(workload.targets(["a", "b"], None, {"a": 50}), {"a": (50.0, "partners"), "b": (50.0, "default")})

    def test_the_users_ceiling_holds_whatever_the_partners_gave(self):
        # Catches a ceiling applied to the raw number only: 30 of (30 + 20) is 60 percent, twice what the user allowed.
        self.assertEqual(workload.targets(["a", "b"], {"a": ("percent", 30)}, {"a": 30, "b": 20}),
                         {"a": (30.0, "user"), "b": (70.0, "partners")})

    def test_a_ceiling_with_no_allocation_still_caps_the_even_split(self):
        # Catches the ceiling only working once the partners have allocated.
        self.assertEqual(workload.targets(["a", "b"], {"a": ("percent", 10)}), {"a": (10.0, "user"), "b": (90.0, "default")})

    def test_a_ceiling_above_the_members_part_changes_nothing(self):
        # Catches a ceiling being treated as the member's share instead of its upper bound.
        self.assertEqual(workload.targets(["a", "b"], {"a": ("percent", 80)}, {"a": 40, "b": 60}),
                         {"a": (40.0, "partners"), "b": (60.0, "partners")})

    def test_a_level_the_user_fixed_is_not_the_partners_to_move(self):
        # Catches the partners' numbers overriding a level the user chose for a member.
        limits = {"a": ("level", "high")}
        self.assertEqual(workload.targets(["a", "b"], limits, {"a": 5, "b": 95}), {"a": (60.0, "user"), "b": (40.0, "partners")})

    def test_everyone_at_zero_falls_back_to_an_even_split(self):
        # Catches a room where nobody may take work once every stored share is 0.
        self.assertEqual(workload.targets(["a", "b"], None, {"a": 0, "b": 0}), {"a": (2, "default"), "b": (2, "default")})

    def test_a_split_with_nobody_available_is_empty_instead_of_an_error(self):
        # Catches a division by zero once every member is unavailable while a split is recorded: the
        # room could then not be read at all, so the user could neither see it nor continue it.
        self.assertEqual(workload.targets([], None, {"a": 60, "b": 40}), {})
        self.assertEqual(workload.targets([], {"a": ("percent", 30)}, None), {})

    def test_clamping_reports_what_the_user_setting_changed(self):
        # Catches a share above the user's ceiling being stored as asked, or cut without a record.
        limits = {"a": ("percent", 30), "c": ("level", "low")}
        shares, limited = workload.clamp_allocation(["a", "b", "c"], limits, {"a": 70, "b": 20, "c": 10})
        self.assertEqual(shares, {"a": 30, "b": 20, "c": 20})
        self.assertEqual(limited, [{"member": "a", "asked": 70, "applied": 30, "by": "user_percent"},
                                   {"member": "c", "asked": 10, "applied": 20, "by": "user_level"}])
        self.assertEqual(workload.clamp_allocation(["a", "b"], {"a": ("percent", 30)}, {"a": 30, "b": 70}),
                         ({"a": 30, "b": 70}, []))

    def test_the_pool_orders_equal_shares_by_tasks_already_taken(self):
        # Catches new work always going to the first member in room order while a free one behind it has had none,
        # or the first hand-out of a round (nobody has taken anything) leaving room order.
        profile = profile_of(["a", "b", "c"])
        self.assertEqual(workload.fair_order(["a", "b", "c"], ["a", "b", "c"], {}, profile, when_equal={"a": 2, "b": 0, "c": 1}.get),
                         ["b", "c", "a"])
        self.assertEqual(workload.fair_order(["a", "b", "c"], ["a", "b", "c"], {"a": load(900, 3)}, profile,
                                             when_equal={"a": 0, "b": 0, "c": 0}.get), ["a", "b", "c"])
        # Unequal shares are still ordered by use for the share, whatever the task counts say.
        uneven = profile_of(["a", "b"], None, {"a": 80, "b": 20})
        self.assertEqual(workload.fair_order(["b", "a"], ["a", "b"], {"a": load(100, 1), "b": load(100, 1)}, uneven,
                                             when_equal={"a": 5, "b": 0}.get), ["a", "b"])

    def test_the_summary_says_who_set_each_share(self):
        # Catches the page being unable to tell the user's setting from the partners' split.
        profile = profile_of(["a", "b"], {"a": ("percent", 30)}, {"a": 60, "b": 40})
        value = workload.summary(["a", "b"], {"a": load(50, 1), "b": load(50, 1)}, profile)
        self.assertEqual([(row["target_percent"], row["source"], row["actual_percent"]) for row in value["accounts"]],
                         [(30, "user", 50), (70, "partners", 50)])


class Setting(unittest.TestCase):
    def stored(self, value):
        return normalized_settings({"permission": "full_access", "members": {"codex": {"workload": value}}},
                                   ["codex", "claude"], "build")["members"]["codex"]

    def test_the_three_kinds_of_setting_are_kept_as_given(self):
        # Catches a ceiling being dropped or turned into a level when the room saves it.
        self.assertEqual(self.stored(30), {"model": None, "effort": None, "workload": 30})
        self.assertEqual(self.stored(1), {"model": None, "effort": None, "workload": 1})
        self.assertEqual(self.stored(100), {"model": None, "effort": None, "workload": 100})
        self.assertEqual(self.stored("low"), {"model": None, "effort": None, "workload": "low"})
        self.assertEqual(self.stored(None), {"model": None, "effort": None})

    def test_anything_else_is_refused_by_name(self):
        # Catches 0, 101, a fraction, a number in a string or true being stored and breaking every later hand-out.
        for value in (0, 101, -5, 30.5, "30", True, [30], "huge"):
            with self.subTest(value=value), self.assertRaises(RuntimeSettingsError) as raised:
                self.stored(value)
            self.assertEqual(str(raised.exception), "invalid_workload")


def command_item(schema):
    return schema["properties"]["commands"]["items"]["properties"]


class Schemas(unittest.TestCase):
    members = ("claude", "codex", "ember")

    def test_every_partner_tool_may_send_allocate_where_the_split_can_be_judged(self):
        # Catches one tool's schema leaving allocate out, so that partner could never split or re-split the work.
        for adapter in ("codex", "kimi", "dsh"):
            for phase, allowed in (("plan", True), ("coordinate", True), ("user", True), ("work", True), ("checkpoint", True),
                                   ("review", False), ("checkpoint_review", False)):
                with self.subTest(adapter=adapter, phase=phase):
                    item = command_item(native_room_response_schema(adapter, member_ids=self.members, provider="codex",
                                                                    team_phase=phase, can_propose=False))
                    self.assertEqual("allocate" in item["type"]["enum"], allowed)
                    self.assertEqual("shares" in item, allowed)
        # Claude's one fixed shape lists it for every phase.
        item = command_item(native_room_response_schema("claude", member_ids=self.members, provider="claude",
                                                        team_phase="review", can_propose=False))
        self.assertIn("allocate", item["type"]["enum"])
        self.assertIn("shares", item)

    def test_shares_name_exactly_the_room_members_as_whole_numbers(self):
        # Catches a shape a strict generator refuses (free-form keys) or one that lets a share of 150 through.
        item = command_item(native_room_response_schema("codex", member_ids=self.members, provider="codex",
                                                        team_phase="user", can_propose=False))
        self.assertEqual(item["shares"]["type"], ["object", "null"])
        self.assertEqual(item["shares"]["required"], ["claude", "codex", "ember"])
        self.assertIs(item["shares"]["additionalProperties"], False)
        self.assertEqual(item["shares"]["properties"]["ember"], {"type": ["integer", "null"], "minimum": 0, "maximum": 100})

    def test_a_receipt_proves_itself_whatever_order_the_roster_was_recorded_in(self):
        # Catches the quota hand-off regression: the call's schema listed members in room order (kimi first), the
        # receipt check rebuilt it from the context's member list in another order, shares.required differed and
        # the proof was refused, so a partner out of quota stopped the room instead of handing its work on.
        from local_agent_coordinator.room_service import RoomService
        sent = native_room_response_schema("kimi", member_ids=("kimi", "claude", "codex"), provider="kimi",
                                           team_phase="work", can_propose=True)
        self.assertEqual(command_item(sent)["shares"]["required"], ["claude", "codex", "kimi"])
        self.assertEqual(list(command_item(sent)["shares"]["properties"]), ["claude", "codex", "kimi"])
        context = {"members": [{"id": "claude"}, {"id": "codex"}, {"id": "kimi"}]}
        self.assertIs(RoomService._native_receipt_schema_matches(sent, context, "kimi", "work", can_propose=True), True)

    def test_a_checkpoint_that_may_allocate_leaves_summary_nullable(self):
        # Catches the checkpoint schema still forcing a summary onto the allocate command beside it.
        item = command_item(native_room_response_schema("codex", member_ids=self.members, provider="codex",
                                                        team_phase="checkpoint", can_propose=False))
        self.assertEqual(item["summary"]["type"], ["string", "null"])
        review = command_item(native_room_response_schema("codex", member_ids=self.members, provider="codex",
                                                          team_phase="review", can_propose=False))
        self.assertEqual((review["reason"]["type"], review["reason"]["minLength"]), ("string", 1))

    def test_a_call_made_before_allocate_existed_still_matches_its_schema(self):
        # Catches an upgrade orphaning calls in flight: their receipts name the schema the older release sent.
        common = dict(project=True, native=True, collaboration=True, member_ids=self.members, provider="codex")
        old = command_item(response_schema(**common, team_phase="checkpoint", can_propose=False, omit_kinds=frozenset({"allocate"})))
        self.assertEqual(old["type"]["enum"], ["checkpoint"])
        self.assertNotIn("shares", old)
        self.assertEqual((old["summary"]["type"], old["summary"]["minLength"]), ("string", 1))
        stable = command_item(response_schema(**common, stable=True, omit_kinds=frozenset({"allocate"})))
        self.assertNotIn("allocate", stable["type"]["enum"])
        self.assertNotIn("shares", stable)

    def reply(self, commands, *, phase="user", native_call=True):
        value = {"message": "Split settled.", "state": "done", "commands": commands, "base_revision": 0, "files": [], "edits": [],
                 **({"unchanged_paths": [], "resolved_conflicts": []} if native_call else {})}
        return _response(json.dumps(value), "codex", ("claude", "codex"), project=True, team=True, native=native_call,
                         native_phase=phase if native_call else None, can_propose=False)["commands"]

    def test_a_flat_native_allocate_is_unwrapped_to_its_own_fields(self):
        # Catches the null-filled transport shape reaching the store, or shares being dropped on the way.
        flat = {"type": "allocate", "shares": {"claude": 70, "codex": None}, "reason": "Claude holds the larger part.",
                "task_id": None, "id": None, "title": None, "description": None, "acceptance": None, "dependencies": None,
                "write_paths": None, "capability": None}
        self.assertEqual(self.reply([flat]), [{"type": "allocate", "shares": {"claude": 70, "codex": None},
                                              "reason": "Claude holds the larger part."}])

    def test_a_reply_written_before_shares_existed_is_still_read(self):
        # Catches older full-transport replies (every field but shares) being refused after the upgrade.
        from local_agent_coordinator.room_contract import COMMAND_FIELDS
        fields = set().union(*COMMAND_FIELDS.values()) - {"shares"}
        full = {field: None for field in fields} | {"type": "plan_ready"}
        self.assertEqual(self.reply([full], native_call=False), [{"type": "plan_ready"}])


class AllocationService(unittest.IsolatedAsyncioTestCase):
    """Rooms on a fictional project, through real storage and the job loop."""
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    create = pool.PoolService.create
    state = pool.PoolService.state
    statuses = pool.PoolService.statuses
    workers = pool.PoolService.workers
    until_statuses = pool.PoolService.until_statuses
    finished = pool.PoolService.finished

    async def asyncSetUp(self):
        await pool.PoolService.asyncSetUp(self)
        self.fake.handler = self.handler
        self.extra = {}      # (member, phase) -> commands added to that member's next reply in that phase
        self.proposals = {}  # member -> tasks it proposes when it next answers the user

    async def handler(self, agent, context, cwd):
        member, phase = context["self"], context["job"]["job_phase"]
        added = self.extra.pop((member, phase), [])
        if phase == "user" and member in self.proposals:
            added = added + [native.proposal(item["id"], paths=item["paths"], capability=item["capability"],
                                             dependencies=item["dependencies"]) for item in self.proposals.pop(member)]
        reply = await pool.PoolService.handler(self, agent, context, cwd)
        if added:
            fields = set(reply["commands"][0]) if reply["commands"] else None
            if fields is None:
                from local_agent_coordinator.room_response_schema import command_shape
                _, fields = command_shape(team_phase=phase, can_propose=member == context["team"]["plan_author"],
                                          has_peers=True, native=True)
            reply["commands"] += [{field: command.get(field) for field in fields} for command in added]
        return reply

    def allocated(self, state):
        return [event for event in state["events"] if event["kind"] == "workload_allocated"]

    def split(self, state):
        return {row["scope"]: (row["target_percent"], row["source"]) for row in state["workload"]["accounts"]}

    async def test_an_untouched_room_hands_out_in_room_order(self):
        # Catches the feature changing a room where neither the user nor the partners set anything.
        self.plan = [plan("T1"), plan("T2")]
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "codex"})
        self.assertEqual(self.split(state), {"claude": (50, "default"), "codex": (50, "default")})
        self.assertFalse(state["workload"]["configured"])
        self.assertEqual(state["workload"]["user_limits"], {})
        self.assertEqual(self.allocated(state), [])

    async def test_the_partners_allocation_decides_who_is_offered_work_first(self):
        # Catches allocate being accepted and recorded while the pool goes on handing out in room order.
        self.plan = [plan("T1"), plan("T2", dependencies=["T1"])]
        self.extra[("claude", "plan")] = [{"type": "allocate", "shares": {"claude": 10, "codex": 90},
                                           "reason": "Codex carries the build; Claude checks it."}]
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertEqual(self.workers(), {"T1": "codex", "T2": "codex"})
        events = self.allocated(state)
        self.assertEqual(len(events), 1)
        self.assertEqual({key: events[0][key] for key in ("sender", "shares", "previous_shares", "reason", "limited")},
                         {"sender": "claude", "shares": {"claude": 10, "codex": 90}, "previous_shares": {},
                          "reason": "Codex carries the build; Claude checks it.", "limited": []})
        self.assertEqual(self.split(state), {"claude": (10, "partners"), "codex": (90, "partners")})
        self.assertTrue(state["workload"]["configured"])

    async def test_a_room_whose_partners_all_became_unavailable_can_still_be_read(self):
        # Catches the real stop of 2026-11-18: one call ended with its session unknown, the others
        # were cut off with it, and with a split recorded for the round the room page failed on
        # every read. A room nobody can work in must still open, so the user can continue it.
        self.plan = [plan("T1")]
        self.extra[("claude", "plan")] = [{"type": "allocate", "shares": {"claude": 30, "codex": 70}, "reason": "Codex builds."}]
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertEqual(self.split(state), {"claude": (30, "partners"), "codex": (70, "partners")})
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE members SET identity_uncertain=1 WHERE room_id=?", (self.room_id,))
        state = await self.state()
        self.assertEqual([member["id"] for member in state["members"]], ["claude", "codex"])
        self.assertEqual(state["room"]["id"], self.room_id)

    async def test_partners_are_given_the_split_as_facts_and_told_it_is_theirs(self):
        # Catches the split reaching the scheduler but not the partners, who then cannot judge or re-split it.
        self.plan = [plan("T1"), plan("T2")]
        self.extra[("claude", "plan")] = [{"type": "allocate", "shares": {"claude": 40, "codex": 60}, "reason": "Codex has more room."}]
        await self.create(["claude", "codex"], members={"codex": {"workload": 80}})
        await self.finished()
        work = next(call for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work")
        facts = work["context"]["workload"]
        self.assertEqual([(row["members"], row["target_percent"], row["set_by"]) for row in facts["accounts"]],
                         [(["claude"], 40, "partners"), (["codex"], 60, "partners")])
        self.assertEqual(facts["accounts"][1]["user_limit"], {"codex": {"kind": "percent", "value": 80}})
        self.assertNotIn("user_limit", facts["accounts"][0])
        self.assertEqual(set(facts["unfinished_tasks_held"]), {"claude", "codex"})
        self.assertEqual(type(facts["tasks_nobody_has_taken"]), int)
        self.assertEqual(facts["latest_allocation"], {"by": "claude", "shares": {"claude": 40, "codex": 60},
                                                      "reason": "Codex has more room.", "limited_by_user": []})
        self.assertTrue(all(ALLOCATE_INSTRUCTION in call["prompt"] for call in self.fake.calls))
        self.assertIn("needs no confirmation from the user", ALLOCATE_INSTRUCTION)
        self.assertIn("not evenly by head count", ALLOCATE_INSTRUCTION)

    async def test_the_users_ceiling_cuts_the_partners_share_and_keeps_the_member_under_it(self):
        # Catches the partners giving a member more than the user allowed, and the pool then handing it that work.
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        self.extra[("claude", "plan")] = [{"type": "allocate", "shares": {"claude": 20, "codex": 80}, "reason": "Codex builds."}]
        await self.create(["claude", "codex"], members={"codex": {"workload": 30}})
        state = await self.finished()
        event = self.allocated(state)[0]
        self.assertEqual(event["limited"], [{"member": "codex", "asked": 80, "applied": 30, "by": "user_percent"}])
        self.assertEqual(event["shares"], {"claude": 20, "codex": 30})
        self.assertEqual(self.split(state), {"claude": (70, "partners"), "codex": (30, "user")})
        self.assertEqual(state["workload"]["user_limits"], {"codex": {"kind": "percent", "value": 30}})
        # Planning and reviewing already put codex past 30 percent of the round, so it takes no build work.
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "claude", "T3": "claude"})

    async def test_a_setting_the_user_changes_mid_task_decides_the_next_hand_out(self):
        # Catches a mid-task change waiting for every running call to end (or for the next round) before it counts.
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        changed = asyncio.Event()
        async def until_changed():
            return changed.is_set()
        async def until_codex_is_free_again():
            # T2 is delivered and codex has nothing queued, so codex is free while claude still works on T1.
            state = await self.state()
            waiting = [job for job in state["jobs"] if job["provider"] == "codex" and job["status"] in {"queued", "working"}]
            return (await self.statuses()).get("T2") == "review" and not waiting
        self.holds["T2"] = until_changed
        self.holds["T1"] = until_codex_is_free_again
        await self.create(["claude", "codex"])
        for _ in range(500):
            if {("claude", "T1"), ("codex", "T2")} <= set(self.works):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "codex"})
        # Left alone, codex would take T3 the moment it is free. The user caps codex while both calls still run.
        await self.service.send_message(
            self.room_id, "Keep Codex to a small part from here on.", "codex", str(uuid4()), source="user",
            runtime_settings={"permission": "full_access", "members": {"codex": {"model": None, "effort": None, "workload": 5}}})
        self.assertEqual((await self.state())["workload"]["user_limits"], {"codex": {"kind": "percent", "value": 5}})
        changed.set()
        state = await self.finished()
        self.assertEqual(self.workers()["T3"], "claude")
        self.assertEqual(self.split(state)["codex"], (5, "user"))

    async def test_a_new_task_goes_to_the_free_partner_who_has_done_less(self):
        # Catches the reported case: a task proposed mid-run was always taken by its proposer (first in room
        # order and free again once it answered), while a partner with nothing to do never got a turn.
        self.plan = [plan("T1"), plan("T2")]
        released = asyncio.Event()
        async def until_released():
            return released.is_set()
        self.holds["T2"] = until_released
        await self.create(["claude", "codex", "third"])
        for _ in range(500):
            if (await self.statuses()).get("T1") == "accepted":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.workers(), {"T1": "claude", "T2": "codex"})
        self.plan.append(plan("T3"))
        self.proposals["claude"] = [plan("T3")]
        await self.service.send_message(self.room_id, "Also add the third file.", "claude", str(uuid4()), source="user")
        for _ in range(500):
            if "T3" in self.workers():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.workers().get("T3"), "third")
        released.set()
        await self.finished()

    async def assert_corrected(self, shares):
        self.plan = [plan("T1"), plan("T2")]
        self.extra[("claude", "plan")] = [{"type": "allocate", "shares": shares, "reason": "Split."}]
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertIsNone(state["team"]["stop_reason"])
        self.assertEqual(self.allocated(state), [], "the bad allocation was not kept")
        self.assertEqual([job["error"] for job in state["jobs"] if job["error"]], ["invalid_team_allocation"])
        corrections = [call["context"]["job"]["text"] for call in self.fake.calls
                       if "invalid_team_allocation" in (call["context"]["job"]["text"] or "")]
        self.assertEqual(len(corrections), 1, "the partner was asked once to correct its reply")
        self.assertIn("whole number from 0 to 100", corrections[0])
        self.assertEqual(self.split(state), {"claude": (50, "default"), "codex": (50, "default")})

    async def test_an_allocation_leaving_everyone_with_nothing_is_corrected_and_work_goes_on(self):
        # Catches an all-zero allocation stopping the room, or being kept so that nobody may take work.
        await self.assert_corrected({"claude": 0, "codex": 0})

    async def test_an_allocation_naming_a_stranger_is_corrected_and_work_goes_on(self):
        # Catches a share for someone outside the room stopping the round instead of being sent back once.
        await self.assert_corrected({"claude": 50, "nobody": 50})

    async def test_an_allocation_over_a_hundred_is_corrected_and_work_goes_on(self):
        # Catches an out-of-range share from a tool without a strict schema being stored as given.
        await self.assert_corrected({"claude": 150, "codex": 50})

    async def test_a_working_partner_and_a_checkpoint_may_re_split(self):
        # Catches allocate only being accepted while planning, so nobody could adjust once work is under way.
        self.plan = [plan("T1"), plan("T2")]
        self.extra[("claude", "work")] = [{"type": "allocate", "shares": {"claude": 30, "codex": 70}, "reason": "T1 was the small part."}]
        self.extra[("claude", "checkpoint")] = [{"type": "allocate", "shares": {"codex": 60}, "reason": "Closer to even next."}]
        await self.create(["claude", "codex"])
        state = await self.finished()
        events = self.allocated(state)
        self.assertEqual([(event["sender"], event["shares"], event["previous_shares"]) for event in events],
                         [("claude", {"claude": 30, "codex": 70}, {}),
                          ("claude", {"claude": 30, "codex": 60}, {"claude": 30, "codex": 70})])
        self.assertEqual(state["objective"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()


class SpareCapacity(unittest.TestCase):
    """What each member can still take on reaches the partners, and a handed-over task goes to whoever holds least."""

    def test_partners_are_told_how_each_member_is_billed_and_how_much_it_has_left(self):
        # Catches the real room where a pay-per-use partner with most of its budget left did a fifth of
        # the work while two subscriptions ran dry: nothing told the partners who had room.
        from local_agent_coordinator.room_prompts import _workload_facts
        snapshot = {"events": [], "workload": {
            "accounts": [{"members": ["claude"], "target_percent": 50, "actual_percent": 80, "source": "default"},
                         {"members": ["ember"], "target_percent": 50, "actual_percent": 20, "source": "default"}],
            "capacity_left": {"claude": {"billing": "subscription", "left_percent": 8},
                              "ember": {"billing": "pay_per_use", "left_percent": 76}}}}
        facts = _workload_facts(snapshot, [], "run")
        self.assertEqual(facts["capacity_left"], {"claude": {"billing": "subscription", "left_percent": 8},
                                                  "ember": {"billing": "pay_per_use", "left_percent": 76}})
        self.assertIn("capacity_left, when present, says how each member is billed and the percent it has left", ALLOCATE_INSTRUCTION)
        del snapshot["workload"]["capacity_left"]
        self.assertNotIn("capacity_left", _workload_facts(snapshot, [], "run"))

    def test_a_handed_over_task_goes_to_whoever_holds_least_and_a_held_back_member_last(self):
        # Catches a partner's work always going to the first member of the room when it runs out of quota,
        # even when that member already holds three tasks and another holds none.
        from local_agent_coordinator.room_service import RoomService
        team = {"tasks": [{"owner": "claude", "status": "working"}, {"owner": "claude", "status": "assigned"},
                          {"owner": "claude", "status": "accepted"}, {"owner": "codex", "status": "assigned"},
                          {"owner": "kimi", "status": "accepted"}],
                "members": [{"id": "claude", "takes_build_work": True}, {"id": "codex", "takes_build_work": True},
                            {"id": "ember", "takes_build_work": True}, {"id": "kimi", "takes_build_work": False}]}
        self.assertEqual(RoomService._handoff_order(["claude", "codex", "ember", "kimi"], team), ["ember", "codex", "claude", "kimi"])
        self.assertEqual(RoomService._handoff_order(["claude", "codex"], {"tasks": [], "members": team["members"]}), ["claude", "codex"])
