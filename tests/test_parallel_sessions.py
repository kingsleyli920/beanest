"""One partner works on several tasks at once when ready tasks outnumber free partners.

Fictional projects and partner mixes, on real SQLite and the real job loop;
only the native transport is synthetic (see test_task_pool).
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator import parallel_sessions, room_service, team_store, ui_server
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.room_store import RoomError
from test_kimi_native_setup import configuration
from test_kimi_setup_http import SetupFixture
import test_task_pool as pool

plan = pool.plan


class ParallelSessions(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the cases below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.limits = {"claude": 3, "codex": 3, "third": 3, "ember": 3}
        # Not in room order, so "most room left" and "first in the room" give different answers.
        self.capacity = {"claude": {"billing": "subscription", "left_percent": 40},
                         "codex": {"billing": "subscription", "left_percent": 60},
                         "third": {"billing": "subscription", "left_percent": 20},
                         "ember": {"billing": "pay_per_use", "left_percent": 90}}
        self.service.store.parallel_limits = lambda: self.limits
        self.service.store.account_capacity = lambda: self.capacity
        # Each partner on its own account, so nobody's quota is another's.
        self.profiles.update({member: (member, "normal", False) for member in self.limits})
        self.running = self.peak = 0
        self.reviews = {}     # task id -> the partner whose review call it was
        pool_handler = self.fake.handler

        async def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "review":
                self.reviews[context["task"]["id"]] = context["self"]
            if phase != "work":
                return await pool_handler(agent, context, cwd)
            self.running += 1
            self.peak = max(self.peak, self.running)
            try:
                return await pool_handler(agent, context, cwd)
            finally:
                self.running -= 1
        self.fake.handler = handler

    def started(self, count):
        async def condition():
            return len(self.works) >= count
        return condition

    def hold_until_started(self, count):
        for task in self.plan:
            self.holds[task["id"]] = self.started(count)

    @staticmethod
    def public(state):
        """What the page and every client receive: the HTTP layer folds each response."""
        return parallel_sessions.fold(state)

    async def test_six_ready_tasks_among_four_partners_all_run_at_once(self):
        # The user's case. Catches the two tasks beyond the four partners waiting for someone to finish,
        # the extra sessions going to anyone but the partners with most room left, and a parallel
        # session showing up as a member, an owner or a sender of its own.
        self.plan = [plan(f"T{number}") for number in range(1, 7)]
        self.hold_until_started(6)
        await self.create(["claude", "codex", "third", "ember"])
        for _ in range(500):
            if len(self.works) >= 6:
                break
            await asyncio.sleep(0.01)
        state = await self.finished()
        self.assertEqual(self.peak, 6, "all six work calls were running together")
        # Work calls start side by side, so their order is not fixed; who did what is.
        # The fifth goes to ember (90% left, 45 per session against codex's 30); the sixth to codex
        # (30 per session, level with ember's 30 for a third session, and codex runs fewer).
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("codex", "T2"), ("codex", "T6"),
                                              ("ember", "T4"), ("ember", "T5"), ("third", "T3")])
        public = self.public(state)
        self.assertEqual([member["id"] for member in public["members"]], ["claude", "codex", "ember", "third"])
        self.assertEqual({task["id"]: task["owner"] for task in public["team"]["tasks"]},
                         {"T1": "claude", "T2": "codex", "T3": "third", "T4": "ember", "T5": "ember", "T6": "codex"})
        self.assertNotIn("~x", repr(public))
        taken = {event["task_id"]: event.get("parallel_session") for event in state["events"] if event["kind"] == "task_taken"}
        self.assertEqual(taken, {"T1": None, "T2": None, "T3": None, "T4": None, "T5": 2, "T6": 2})
        self.assertEqual({task["id"]: task["status"] for task in state["team"]["tasks"]}, {f"T{number}": "accepted" for number in range(1, 7)})

    async def test_the_partner_row_lists_what_it_is_doing_only_while_it_does_several_things(self):
        # Catches the page having nothing to show for a partner doing two tasks (or showing a second
        # partner), and a finished parallel session still listed once the rush is over.
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        release = asyncio.Event()
        async def until_released():
            return release.is_set()
        for task in self.plan:
            self.holds[task["id"]] = until_released
        self.limits = {"claude": 3, "codex": 1}
        await self.create(["claude", "codex"])
        for _ in range(500):
            if len(self.works) >= 3:
                break
            await asyncio.sleep(0.01)
        during = self.public(await self.state())
        claude, codex = (next(member for member in during["members"] if member["id"] == name) for name in ("claude", "codex"))
        self.assertEqual([(item["session"], item["task_id"], item["phase"], item["status"]) for item in claude["parallel_work"]],
                         [(1, "T1", "work", "working"), (2, "T3", "work", "working")])
        self.assertEqual(claude["parallel_work"][1]["task_title"], next(task["title"] for task in during["team"]["tasks"] if task["id"] == "T3"))
        self.assertEqual(codex["parallel_work"], [])
        self.assertEqual(len(during["members"]), 2)
        release.set()
        after = self.public(await self.finished())
        self.assertEqual([member["parallel_work"] for member in after["members"]], [[], []])
        self.assertEqual([member["id"] for member in after["members"]], ["claude", "codex"])

    async def test_a_message_to_everyone_reaches_the_partners_while_one_does_several_tasks(self):
        # Catches "send to all" being refused with agent_unavailable (seen in a real room: the hidden
        # second session was counted as a recipient the host has no tool for), and a parallel session
        # being addressable as if it were a partner of its own.
        self.limits = {"claude": 3, "codex": 1}
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        release = asyncio.Event()
        async def until_released():
            return release.is_set()
        for task in self.plan:
            self.holds[task["id"]] = until_released
        await self.create(["claude", "codex"])
        for _ in range(500):
            if len(self.works) >= 3:
                break
            await asyncio.sleep(0.01)
        self.assertEqual([session["id"] for session in (await self.state())["parallel_sessions"]], ["claude~x2"])
        await self.service.send_message(self.room_id, "Everyone: keep the mouse where it is.", "all", "to-everyone", source="user")
        state = await self.state()
        message = next(event for event in state["events"] if event["kind"] == "message" and event.get("text") == "Everyone: keep the mouse where it is.")
        self.assertEqual((message["sender"], message["recipient"]), ("user", "all"))
        with self.assertRaises(RoomError) as refused:
            await self.service.send_message(self.room_id, "Only the second session.", "claude~x2", "to-a-session", source="user")
        self.assertEqual(str(refused.exception), "invalid_target")
        release.set()
        final = await self.finished()
        self.assertEqual({task["id"]: task["status"] for task in final["team"]["tasks"]}, {"T1": "accepted", "T2": "accepted", "T3": "accepted"})

    async def test_a_limit_of_one_keeps_every_partner_to_one_task_at_a_time(self):
        # Catches the user's limit being ignored: with 1 nobody may work on two tasks at once.
        self.limits = {member: 1 for member in self.limits}
        self.plan = [plan(f"T{number}") for number in range(1, 7)]
        self.hold_until_started(4)
        await self.create(["claude", "codex", "third", "ember"])
        state = await self.finished()
        self.assertEqual(self.peak, 4)
        self.assertEqual(state["parallel_sessions"], [])

    async def test_a_partner_never_runs_more_tasks_at_once_than_its_limit(self):
        # Catches the limit counting only the extra sessions (limit 2 giving three at once) or not at all.
        self.limits = {"claude": 2, "codex": 1}
        self.plan = [plan(f"T{number}") for number in range(1, 6)]
        self.hold_until_started(3)
        await self.create(["claude", "codex"])
        await self.finished()
        self.assertEqual(self.peak, 3)

    async def test_its_own_parallel_work_is_reviewed_by_another_partner(self):
        # Catches claude reviewing what claude's second session made: to the user they are one partner,
        # so that would be a partner approving its own work under the name of an independent review.
        self.limits = {"claude": 3, "codex": 1}
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        self.hold_until_started(3)
        await self.create(["claude", "codex"])
        state = await self.finished()
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("claude", "T3"), ("codex", "T2")])
        self.assertEqual(self.reviews, {"T1": "codex", "T2": "claude", "T3": "codex"})
        kinds = {task["id"]: task["review_kind"] for task in state["team"]["tasks"]}
        self.assertEqual(kinds, {"T1": "independent_review", "T2": "independent_review", "T3": "independent_review"})

    async def test_a_partner_low_on_quota_starts_no_parallel_session(self):
        # Catches the partner with the best-looking number being given more sessions although its account
        # is down to its reserve (the same flag a pay-per-use partner near its spending limit carries).
        self.profiles["claude"] = ("claude", "normal", True)
        self.plan = [plan(f"T{number}") for number in range(1, 5)]
        self.hold_until_started(3)
        await self.create(["claude", "codex"])
        await self.finished()
        self.assertEqual(sorted(self.works), [("codex", "T1"), ("codex", "T2"), ("codex", "T3"), ("codex", "T4")])
        self.assertEqual(self.peak, 3)

    async def test_a_partner_past_the_users_ceiling_starts_no_parallel_session(self):
        # Catches a partner the user capped at a share of the round working around the cap in extra
        # sessions: claude may carry 1% and its planning calls already took more than that.
        self.plan = [plan(f"T{number}") for number in range(1, 5)]
        self.hold_until_started(3)
        await self.create(["claude", "codex"], members={"claude": {"workload": 1}})
        state = await self.finished()
        self.assertEqual(sorted(self.works), [("codex", "T1"), ("codex", "T2"), ("codex", "T3"), ("codex", "T4")])
        self.assertEqual([session["id"] for session in state["parallel_sessions"]], ["codex~x2", "codex~x3"])

    async def test_a_parallel_session_out_of_quota_gives_back_only_its_own_task(self):
        # Catches one session's quota refusal cancelling its partner's other running task, and the host
        # starting yet another session on the account that just ran out.
        self.limits = {"claude": 3, "codex": 1}
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        refused = []
        async def guard(members):
            # The quota check names the partner; which of its sessions asks is the worker it runs in.
            session = next((key[1] for key, worker in self.service._workers.items() if worker is asyncio.current_task()), None)
            if session == "claude~x2" and members == ["claude"] and not refused:
                refused.append(session)
                raise RoomError("agent_quota_exhausted")
        self.service._guard_availability = guard
        release = asyncio.Event()
        async def until_released():
            return release.is_set()
        async def until_returned():
            # The event, not the task's status: the returned task is taken again at once.
            return any(event["kind"] == "task_returned_to_pool" for event in (await self.state())["events"])
        self.holds.update(T1=until_released, T2=until_returned)
        await self.create(["claude", "codex"])
        for _ in range(500):
            if ("codex", "T3") in self.works:   # codex finished T2 and took what the session gave back
                break
            await asyncio.sleep(0.01)
        state = await self.state()
        returned = [(event["task_id"], event["previous_owner"]) for event in state["events"] if event["kind"] == "task_returned_to_pool"]
        self.assertEqual(returned, [("T3", "claude~x2")])
        self.assertEqual(list(state["objective"]["retired_partners"]), ["claude~x2"])
        self.assertEqual(self.public(state)["objective"]["retired_partners"], {}, "the partner itself is not shown as out")
        self.assertEqual({task["id"]: task["status"] for task in state["team"]["tasks"]}["T1"], "working",
                         "claude's own task kept running")
        release.set()
        await self.finished()
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("codex", "T2"), ("codex", "T3")])
        self.assertEqual([session["id"] for session in (await self.state())["parallel_sessions"]], ["claude~x2"],
                         "no third session was started on the account that ran out")

    async def test_the_round_recovers_with_parallel_sessions_after_one_call_dies(self):
        # Catches the host's own recovery losing a parallel session's task, or handing it to someone
        # else while the session that started it still holds its private files.
        self.plan = [plan(f"T{number}") for number in range(1, 7)]
        self.hold_until_started(6)
        died = []
        inner = self.service.native_invoke
        async def invoke(agent, prompt, cwd, **options):
            result = await inner(agent, prompt, cwd, **options)
            if '"job_phase": "work"' in prompt and '"id": "T5"' in prompt.split('"task":', 1)[1][:200] and not died:
                died.append(True)
                return {**result, "status": "failed", "failure_code": "cli_nonzero_exit", "text": ""}
            return result
        self.service.native_invoke = invoke
        # The whole round stops when a task's call has died too often to be done again in place;
        # allowing none puts this single death there.
        with patch.object(team_store, "CONFINED_CALL_LIMIT", 0):
            await self.create(["claude", "codex", "third", "ember"])
            await self.settle()
        stopped = self.service._snapshot(self.room_id)
        self.assertEqual((stopped["team"]["stop_reason"], stopped["stop_kind"]), ("cli_nonzero_exit", "auto"))
        with patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0):
            self.service._schedule_auto_continue(self.room_id, stopped)
            self.service._schedule_auto_continue(self.room_id, stopped)
        await self.service._auto_continues[self.room_id]["task"]
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete")
        self.assertEqual({task["id"]: (task["status"], task["owner"]) for task in state["team"]["tasks"]},
                         {"T1": ("accepted", "claude"), "T2": ("accepted", "codex"), "T3": ("accepted", "third"),
                          "T4": ("accepted", "ember"), "T5": ("accepted", "ember~x2"), "T6": ("accepted", "codex~x2")})

    async def test_a_dead_call_in_a_parallel_session_is_done_again_by_that_session(self):
        # Catches one session's dead call stopping the round and cancelling the five other calls,
        # and its task going to another session while the one that started it holds its files.
        self.plan = [plan(f"T{number}") for number in range(1, 7)]
        async def until_t5_was_handed_out_again():
            return len(self.works) >= 6 and sum(1 for _, task_id in self.works if task_id == "T5") >= 2
        for task in self.plan:
            self.holds[task["id"]] = until_t5_was_handed_out_again
        self.holds["T5"] = self.started(6)
        died = []
        inner = self.service.native_invoke
        async def invoke(agent, prompt, cwd, **options):
            result = await inner(agent, prompt, cwd, **options)
            if '"job_phase": "work"' in prompt and '"id": "T5"' in prompt.split('"task":', 1)[1][:200] and not died:
                died.append(True)
                return {**result, "status": "failed", "failure_code": "cli_nonzero_exit", "text": ""}
            return result
        self.service.native_invoke = invoke
        await self.create(["claude", "codex", "third", "ember"])
        await self.settle()
        state = await self.state()
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        confined = [event for event in state["events"] if event["kind"] == "call_failure_confined"]
        self.assertEqual([(event["provider"], event["task_id"], len(event["running_job_ids"])) for event in confined],
                         [("ember~x2", "T5", 5)])
        self.assertEqual([event for event in state["events"] if event["kind"] == "objective_stopped"], [])
        self.assertEqual([job["error"] for job in state["jobs"] if job["error"]], ["cli_nonzero_exit"])
        self.assertEqual(sorted(self.works), [("claude", "T1"), ("codex", "T2"), ("codex", "T6"), ("ember", "T4"),
                                              ("ember", "T5"), ("ember", "T5"), ("third", "T3")])
        self.assertEqual({task["id"]: (task["status"], task["owner"]) for task in state["team"]["tasks"]},
                         {"T1": ("accepted", "claude"), "T2": ("accepted", "codex"), "T3": ("accepted", "third"),
                          "T4": ("accepted", "ember"), "T5": ("accepted", "ember~x2"), "T6": ("accepted", "codex~x2")})

    async def test_a_parallel_sessions_calls_count_as_its_partners_work(self):
        # Catches a second ledger row for the session: the partner would look half as busy as it is,
        # and the user's ceiling and the partners' shares would be measured against the wrong number.
        self.limits = {"claude": 3, "codex": 1}
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        self.hold_until_started(3)
        await self.create(["claude", "codex"])
        state = await self.finished()
        with self.service.store._connection() as db:
            rows = {row[0]: row[1] for row in db.execute(
                "SELECT provider,calls FROM partner_phase_load WHERE room_id=? AND phase='work'", (self.room_id,))}
        self.assertEqual(rows, {"claude": 2, "codex": 1})
        accounts = {tuple(row["members"]): row["calls"] for row in state["workload"]["accounts"]}
        self.assertEqual(sorted(accounts), [("claude",), ("codex",)])


class Folding(unittest.TestCase):
    def test_a_parallel_session_reads_as_its_partner_everywhere(self):
        # Catches an id leaking through a nested field, a dictionary key, prose, or a list of names.
        value = {"members": [{"id": "claude", "name": "Claude Code"}, {"id": "claude~x2", "name": "Claude Code"}],
                 "tasks": [{"id": "T5", "owner": "claude~x2", "reviewer": "codex"}],
                 "retired": {"claude~x3": {"reason": "agent_quota_exhausted"}, "codex": {"reason": "x"}},
                 "holders": ["codex", "claude~x2", "claude~x3"], "both": ["claude", "claude~x2"],
                 "text": "T5 (owner claude~x2, now working on T9) and HEAD~2, main~x01, a~x2b",
                 "parallel_sessions": [{"id": "claude~x2"}]}
        self.assertEqual(parallel_sessions.fold(value), {
            "members": [{"id": "claude", "name": "Claude Code"}],
            "tasks": [{"id": "T5", "owner": "claude", "reviewer": "codex"}],
            "retired": {"codex": {"reason": "x"}},
            "holders": ["codex", "claude"], "both": ["claude"],
            "text": "T5 (owner claude, now working on T9) and HEAD~2, main~x01, a~x2b"})

    def test_every_http_response_is_folded_before_it_is_sent(self):
        # Catches the one place every response leaves through sending a session's id to the page or a client.
        sent = []

        class Response:
            def _response(self, status, body, content_type):
                sent.append((status, json.loads(body)))
        ui_server._Handler._json(Response(), 200, {"members": [{"id": "claude"}], "jobs": [{"provider": "claude~x2"}],
                                                   "events": [{"sender": "claude~x3", "text": "by claude~x3"}]})
        ui_server._Handler._json(Response(), 200, {"text": "no session here: main~2"})
        self.assertEqual(sent, [(200, {"members": [{"id": "claude"}], "jobs": [{"provider": "claude"}],
                                       "events": [{"sender": "claude", "text": "by claude", "parallel_session": 3}]}),
                                (200, {"text": "no session here: main~2"})])

    def test_what_a_parallel_session_said_carries_its_number_beside_the_partners_name(self):
        # Catches the reader having no way to tell a partner's several pieces of work apart in the
        # conversation, the partner's own messages being numbered too, and a number already recorded
        # on the event (a taken task) being replaced.
        folded = parallel_sessions.fold({"events": [
            {"kind": "message", "sender": "claude", "recipient": "all", "text": "one"},
            {"kind": "message", "sender": "claude~x2", "recipient": "all", "text": "two"},
            {"kind": "message", "sender": "codex", "recipient": "claude~x4", "text": "three"},
            {"kind": "task_taken", "sender": "system", "provider": "claude~x3", "parallel_session": 3}]})
        self.assertEqual(folded, {"events": [
            {"kind": "message", "sender": "claude", "recipient": "all", "text": "one"},
            {"kind": "message", "sender": "claude", "recipient": "all", "text": "two", "parallel_session": 2},
            {"kind": "message", "sender": "codex", "recipient": "claude", "text": "three"},
            {"kind": "task_taken", "sender": "system", "provider": "claude", "parallel_session": 3}]})

    def test_ids(self):
        # Catches an upper bound on session numbers (it was 8), a session 0, or a partner id read as a session.
        self.assertEqual((parallel_sessions.lane_id("claude", 1), parallel_sessions.lane_id("claude", 8),
                          parallel_sessions.lane_id("claude", 12)), ("claude", "claude~x8", "claude~x12"))
        self.assertRaises(ValueError, parallel_sessions.lane_id, "claude", 0)
        self.assertEqual([parallel_sessions.partner_of(value) for value in ("claude~x2", "claude", "claude~x9", "claude~x12", "claude~x01", "claude-x2", None)],
                         ["claude", "claude", "claude", "claude", "claude~x01", "claude-x2", None])
        self.assertEqual([parallel_sessions.lane_number(value) for value in ("claude~x12", "claude~x100")], [12, 100])
        self.assertEqual(parallel_sessions.fold_text("claude~x12 said hi"), "claude said hi")
        self.assertEqual([parallel_sessions.is_lane(value) for value in ("claude~x2", "claude", "claude~x1", "Claude~x2", "claude~x0")],
                         [True, False, False, False, False])


async def _unused(*_args, **_kwargs):
    raise AssertionError("no tool runs in these tests")


class PartnerLimit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "partners"
        self.manager = PartnerManager(self.root, default_registry(_unused), clock=lambda: 42)

    def test_the_limit_starts_at_three_and_the_users_choice_is_kept(self):
        # Catches the setting not being saved, or changing one partner changing another.
        self.assertEqual(self.manager.parallel_limits(), {"claude": 3, "codex": 3})
        self.assertEqual(self.manager.set_parallel_limit("claude", 6)["parallel_limit"], 6)
        reloaded = PartnerManager(self.root, default_registry(_unused))
        self.assertEqual(reloaded.parallel_limits(), {"claude": 6, "codex": 3})
        self.assertEqual(reloaded.set_parallel_limit("claude", 1)["parallel_limit"], 1)
        self.assertEqual(json.loads((self.root / "profiles.json").read_text())["profiles"][0]["parallel_limit"], 1)

    def test_any_whole_number_from_one_up_is_accepted(self):
        # Catches a limit that would start no session at all, a non-number, or an upper bound of Beanest's own (it was 8).
        self.assertEqual(self.manager.set_parallel_limit("claude", 20)["parallel_limit"], 20)
        self.assertEqual(self.manager.set_parallel_limit("claude", 3)["parallel_limit"], 3)
        for value in (0, -1, "3", 3.0, True, None):
            with self.subTest(value=value), self.assertRaisesRegex(PartnerError, "invalid_parallel_limit"):
                self.manager.set_parallel_limit("claude", value)
        self.assertEqual(self.manager.parallel_limits()["claude"], 3)
        with self.assertRaisesRegex(PartnerError, "unknown_partner"):
            self.manager.set_parallel_limit("absent", 2)

    def test_a_tool_reached_through_the_users_own_command_stays_at_one(self):
        # Catches the host starting a second copy of a command nobody said can run twice.
        self.manager.add(id="helper", name="Helper", adapter="local_json")
        self.assertEqual(self.manager.parallel_limits()["helper"], 1)
        with self.assertRaisesRegex(PartnerError, "parallel_sessions_unsupported"):
            self.manager.set_parallel_limit("helper", 4)

    def test_a_parallel_session_is_its_partner_to_the_partner_directory(self):
        # Catches a session being looked up as a partner of its own: unknown, or with another login or quota.
        self.assertEqual(self.manager.get("claude~x2")["id"], "claude")
        self.assertEqual(self.manager.account_scope("claude~x3"), "claude")
        self.assertEqual(self.manager.account_home("claude~x2"), self.manager.account_home("claude"))
        self.assertEqual(self.manager.registry().get("codex~x2").id, "codex")
        self.assertNotIn("claude~x2", self.manager.registry().ids())
        with self.assertRaisesRegex(PartnerError, "unknown_partner"):
            self.manager.get("absent~x2")


class PartnerLimitHTTP(unittest.TestCase):
    def test_the_setting_is_changed_over_http_and_bad_bodies_change_nothing(self):
        # Catches the page's control having no route, or a malformed body being saved.
        fixture = SetupFixture(discover=False)
        self.addCleanup(fixture.close)
        fixture.config.write_text(configuration(), encoding="utf-8")
        code, value = fixture.request("POST", "/api/partners/kimi/parallel-limit", {"limit": 5})
        self.assertEqual(code, 200)
        self.assertEqual(next(item for item in value["partners"] if item["id"] == "kimi")["parallel_limit"], 5)
        self.assertEqual(fixture.request("POST", "/api/partners/kimi/parallel-limit", {"limit": 9})[0], 200)
        self.assertEqual(fixture.request("POST", "/api/partners/kimi/parallel-limit", {"limit": 5})[0], 200)
        for body in ({"limit": 0}, {"limit": "5"}, {}, {"limit": 2, "level": "high"}):
            with self.subTest(body=body):
                self.assertEqual(fixture.request("POST", "/api/partners/kimi/parallel-limit", body)[0], 400)
        self.assertEqual(fixture.request("POST", "/api/partners/absent/parallel-limit", {"limit": 2})[0], 404)
        code, value = fixture.request("GET", "/api/partners")
        self.assertEqual(next(item for item in value["partners"] if item["id"] == "kimi")["parallel_limit"], 5)


if __name__ == "__main__":
    unittest.main()
