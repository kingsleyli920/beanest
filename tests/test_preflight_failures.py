"""A native call that fails before its prompt is sent: retry, then step aside.

Real private trees/SQLite with explicitly synthetic transports; no model runs.
Adapters are named only to show the behaviour is the same for each of them.
"""
import asyncio
from collections import Counter
from dataclasses import replace
from functools import partial
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import adapters, room_service
from local_agent_coordinator.agent_registry import AgentRegistry, default_registry, native_spec
from local_agent_coordinator.native_outcome import NATIVE_PREFLIGHT_FAILED, preflight_failed, prompt_never_sent
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.stop_policy import continues_automatically
from test_folder_service import FolderProvider, response
from test_native_room_service import NativeTransport, native_reply
from test_team_store import proposal
from tests import test_kimi_acp as acp_fixture
from wait_support import within

CONFIGURATION_FAILURE = {"phase": "configuration", "rpc_method": "session/set_mode", "rpc_code": -32603, "prompt_sent": False}
ALL_PARTNERS = ["claude", "codex", "kimi"]


def registry(invoke=None):
    defaults = default_registry(invoke)
    kimi = native_spec("kimi") if invoke is None else replace(native_spec("kimi"), invoke=partial(invoke, "kimi"), invoke_structured=None)
    return AgentRegistry([*(defaults.get(agent_id) for agent_id in defaults.ids()), kimi])


def receipt(agent, permission="plan", **overrides):
    """The adapter's real evidence shape for a call that never reached a model."""
    return {"agent": agent, "permission": permission, "tool_calls": 0, "tool_results": 0,
            "side_effects_possible": False, "cleanup_confirmed": True, "exit_code": 0, "receipt_path": None,
            "failure_diagnostic": dict(CONFIGURATION_FAILURE), **overrides}


def refused(agent, permission, *, session_id=None, observation=None, failure_code="native_provider_error", **overrides):
    return {"session_id": session_id, "status": "failed", "failure_code": failure_code, "text": "", "usage": None,
            "events": [], "cleanup_confirmed": True,
            "native_execution": observation if observation is not None else receipt(agent, permission), **overrides}


def never_started_error(agent, permission):
    """Claude and Codex raise before spawning; Kimi returns a receipt. Both are valid."""
    error = RuntimeError(agent + " unavailable")
    error.native_execution = {"agent": agent, "permission": permission, "never_started": True, "tool_calls": 0,
                              "tool_results": 0, "side_effects_possible": False, "cleanup_confirmed": True,
                              "exit_code": None, "receipt_path": None}
    return error


class Fault:
    """Fail `times` matching calls of `agent` the way a real transport would."""
    def __init__(self, agent, times, *, phase=None, raises=False, result=None):
        self.agent, self.times, self.phase, self.raises, self.result = agent, times, phase, raises, result

    def fire(self, agent, phase, permission):
        if self.agent not in {None, agent} or self.times <= 0 or self.phase not in {None, phase}:
            return None
        self.times -= 1
        if self.raises:
            raise never_started_error(agent, permission)
        return self.result(agent, permission) if self.result else refused(agent, permission)


class FaultyNative(NativeTransport):
    def __init__(self):
        super().__init__()
        self.faults, self.attempts, self.held = [], Counter(), {}

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=None, **options):
        phase = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])["job"]["job_phase"]
        self.attempts[agent] += 1
        for fault in self.faults:
            result = fault.fire(agent, phase, options["permission"])
            if result is not None:
                return result
        if self.held.get(agent) is not None:
            hold, release = self.held[agent]
            hold.set()
            await release.wait()
        return await super().__call__(agent, prompt, cwd, session_id=session_id, timeout=timeout, **options)


class FolderFaults(FolderProvider):
    def __init__(self):
        super().__init__()
        self.faults, self.attempts, self.cancelled, self.held = [], Counter(), [], {}

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        phase = context["job"]["job_phase"]
        self.attempts[agent] += 1
        for fault in self.faults:
            result = fault.fire(agent, phase, "plan")
            if result is not None:
                return result
        if self.held.get(agent) and phase == "work":
            started, proof = self.held[agent]
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                self.cancelled.append((agent, session_id))
                proof(error)
                raise
        return await super().__call__(agent, prompt, cwd, session_id=session_id, timeout=timeout)


def folder_handler(agent, context):
    phase, board = context["job"]["job_phase"], context["team"]
    if phase in {"plan", "coordinate"}:
        commands = []
        tasks = {task["id"]: task for task in board["tasks"]}
        if not tasks and agent == board["plan_author"]:
            commands += [proposal("T1"), proposal("T2")]
            tasks = {"T1": {"owner": None}, "T2": {"owner": None}}
        if agent == board["plan_author"] and tasks["T1"]["owner"] is None:
            commands.append({"type": "claim", "task_id": "T1"})
        if agent != board["plan_author"] and tasks and tasks["T2"]["owner"] is None:
            commands.append({"type": "claim", "task_id": "T2"})
        if tasks:
            commands.append({"type": "plan_ready"})
        return response(context, commands=commands)
    if phase == "work":
        return response(context, commands=[{"type": "complete", "task_id": context["task"]["id"], "result": "Delivered the requested text."}])
    if phase == "review":
        return response(context, commands=[{"type": "review", "task_id": context["task"]["id"], "verdict": "accept", "reason": "Checked the result."}])
    if phase == "checkpoint":
        objective = context["objective"]
        return response(context, commands=[{"type": "checkpoint", "decision": "complete", "summary": "All delivered.",
            "next_goal": None, "scope_reference": objective["original_goal"], "question": None}])
    if phase == "checkpoint_review":
        return response(context, commands=[{"type": "checkpoint_review", "verdict": "accept", "reason": "Goal and delivery match.",
            "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
    raise AssertionError(phase)


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("VALUE = 1\n")
        (self.source / "notes.md").write_text("Original notes.\n")
        delay = patch.object(room_service, "_PREFLIGHT_RETRY_DELAY_SECONDS", 0)
        delay.start()
        self.addCleanup(delay.stop)
        self.service = self.make_service()
        self.registration = await self.service.register_project("Example", str(self.source), selection_kind="folder")

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def settle(self):
        async def wait():
            for _ in range(100):
                tasks = [task for task in self.service._workers.values() if not task.done()]
                if not tasks:
                    return
                for result in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
            self.fail("scheduler did not settle")
        await within(wait(), 15)

    def assert_healthy(self, final):
        for member in final["members"]:
            self.assertFalse(member["identity_uncertain"], member["id"])
        self.assertFalse(any(job["status"] == "unknown" for job in final["jobs"]), final["jobs"])
        self.assertNotEqual(final["team"]["phase"], "unknown")


class NativeProjectCases(ServiceCase):
    def make_service(self):
        self.fake = FaultyNative()
        self.fake.handler = self.handler
        return RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", native_invoke=self.fake, registry=registry())

    def handler(self, agent, context, cwd):
        phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and agent == board["plan_author"]:
                commands += [proposal("T1", paths=["main.py", "notes.md"], capability="python"), {"type": "claim", "task_id": "T1"}]
            if board["tasks"] and agent == board["plan_author"] and board["tasks"][0]["owner"] is None:
                commands.append({"type": "claim", "task_id": "T1"})
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            (cwd / "main.py").write_text("VALUE = 2\n")
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Updated main."}],
                                unchanged=[{"path": "notes.md", "reason": "Notes stay correct."}])
        if phase == "review":
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the tree."}])
        if phase == "checkpoint":
            objective = context["objective"]
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Update complete.",
                "next_goal": None, "scope_reference": objective["original_goal"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Matches.",
                "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        raise AssertionError(phase)

    async def create(self, participants=ALL_PARTNERS, failover=True):
        policy = {"mode": "autonomous", "quota_failover": failover}
        created = await self.service.create_room("Update the existing value.", participants[0], str(uuid4()),
            mode="native_project", project_id=self.registration["project"]["id"], participants=participants,
            work_mode="build", runtime_settings={"permission": "full_access", "members": {}}, execution_policy=policy)
        return created["room"]["id"]

    async def final(self, room_id):
        await self.settle()
        return await self.service.read_room(room_id)

    async def test_refused_before_the_prompt_is_retried_and_completes_for_every_adapter(self):
        # Catches: a provider hiccup in setup stopping the whole room, for any
        # partner, instead of being retried without the user noticing.
        for agent, raises in (("claude", True), ("codex", True), ("kimi", False)):
            with self.subTest(agent=agent):
                self.fake.faults = [Fault(agent, 1, raises=raises)]
                self.fake.attempts.clear()
                room_id = await self.create()
                final = await self.final(room_id)
                self.assertEqual(final["objective"]["status"], "complete")
                self.assert_healthy(final)
                self.assertEqual(final["objective"]["retired_partners"], {})
                self.assertFalse(any(event["kind"] == "objective_auto_continued" for event in final["events"]))
                retries = [event for event in final["events"] if event["kind"] == "native_preflight_retry"]
                self.assertEqual([event["sender"] for event in retries], [agent])
                self.assertFalse(any(job["error"] for job in final["jobs"]), final["jobs"])

    async def test_still_refused_retires_only_that_partner_and_the_others_finish(self):
        # Catches: a partner that cannot start ending the objective or leaving
        # its share undone, for any adapter, including the plan author.
        for agent, raises in (("claude", True), ("codex", True), ("kimi", False)):
            with self.subTest(agent=agent):
                self.fake.faults = [Fault(agent, 2, raises=raises)]
                self.fake.attempts.clear()
                room_id = await self.create()
                final = await self.final(room_id)
                self.assertEqual(final["objective"]["status"], "complete")
                self.assertEqual(self.fake.attempts[agent], 2)
                self.assert_healthy(final)
                self.assertEqual(final["objective"]["retired_partners"][agent]["reason"], NATIVE_PREFLIGHT_FAILED)
                self.assertTrue(any(event["kind"] == "partner_retired" and event["provider"] == agent for event in final["events"]))
                self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]))
                self.assertEqual([(job["status"], job["error"]) for job in final["jobs"] if job["provider"] == agent and job["error"]],
                                 [("failed", NATIVE_PREFLIGHT_FAILED)])
                self.assertFalse(any(job["error"] == "invoke_interrupted" for job in final["jobs"]))

    async def test_a_partner_that_cannot_start_in_any_later_phase_is_handed_on(self):
        # Catches: a retired owner, reviewer or checkpoint author leaving its
        # share stuck, so the objective silently never finishes.
        for phase in ("work", "review", "checkpoint", "checkpoint_review"):
            with self.subTest(phase=phase):
                self.fake.faults = [Fault(None, 2, phase=phase)]
                final = await self.final(await self.create())
                self.assertEqual(final["objective"]["status"], "complete")
                self.assertEqual(len(final["objective"]["retired_partners"]), 1)
                self.assert_healthy(final)
                retired = next(iter(final["objective"]["retired_partners"]))
                if phase == "work":
                    self.assertNotEqual(final["team"]["tasks"][0]["owner"], retired)

    async def test_empty_sessions_from_refused_calls_stay_purgeable_but_are_never_resumed(self):
        # Catches: a session created before the refused setup step leaking past
        # a room purge, or a later call resuming a session no prompt ever reached.
        for times, retired in ((1, set()), (2, {"kimi"})):
            with self.subTest(times=times):
                created = iter(("stray-%d-a" % times, "stray-%d-b" % times))
                self.fake.faults = [Fault("kimi", times, phase="plan",
                                          result=lambda agent, permission: refused(agent, permission, session_id=next(created)))]
                room_id = await self.create()
                final = await self.final(room_id)
                self.assertEqual(final["objective"]["status"], "complete")
                self.assertEqual(set(final["objective"]["retired_partners"]), retired)
                with self.service.store._connection() as db:
                    history = {row[0] for row in db.execute(
                        "SELECT session_id FROM native_session_history WHERE room_id=? AND provider='kimi'", (room_id,))}
                strays = {"stray-%d-a" % times, "stray-%d-b" % times}
                self.assertEqual(history & strays, strays if times == 2 else {"stray-1-a"})
                member = next(member for member in final["members"] if member["id"] == "kimi")
                self.assertNotIn(member["session_id"], strays)
                self.assertFalse(any(call["agent"] == "kimi" and call["session_id"] in strays for call in self.fake.calls))

    async def test_a_parallel_partner_is_not_cancelled_by_the_failure(self):
        # Catches: one partner's unsent call killing another partner's real work.
        started, release = asyncio.Event(), asyncio.Event()
        self.fake.held["codex"] = (started, release)
        self.fake.faults = [Fault("kimi", 2, phase="plan")]
        room_id = await self.create()
        await within(started.wait(), 5)
        await within(self.wait_retired(room_id, "kimi"), 5)
        mid = await self.service.read_room(room_id)
        self.assertEqual(mid["team"]["stop_reason"], None)
        self.assertNotEqual(mid["team"]["phase"], "unknown")
        self.assertTrue(any(job["provider"] == "codex" and job["status"] == "working" for job in mid["jobs"]))
        self.fake.held.pop("codex")
        release.set()
        final = await self.final(room_id)
        self.assertFalse(any(job["error"] == "invoke_interrupted" for job in final["jobs"]))
        self.assertEqual(final["objective"]["status"], "complete")

    async def wait_retired(self, room_id, agent):
        while agent not in (await self.service.read_room(room_id))["objective"]["retired_partners"]:
            await asyncio.sleep(.01)

    async def test_a_sole_partner_stops_with_a_clear_reason_and_can_continue(self):
        # Catches: the only partner leaving a dead room, or waiting on quota
        # that was never the problem, instead of a stop the user can press through.
        for phase in (None, "work"):
            with self.subTest(phase=phase):
                self.fake.faults = [Fault("claude", 2, phase=phase, raises=True)]
                room_id = await self.create(["claude"])
                final = await self.final(room_id)
                self.assertEqual((final["team"]["phase"], final["team"]["stop_reason"]), ("stopped", NATIVE_PREFLIGHT_FAILED))
                self.assertEqual(final["objective"]["status"], "stopped")
                self.assert_healthy(final)
                self.assertEqual(final["objective"]["retired_partners"], {})
                self.assertTrue(continues_automatically(final["team"]["stop_reason"]))
                self.assertIn("提示词没有发出", final["collaboration"]["outcome"]["reason"])
                self.assertIn("继续", final["collaboration"]["outcome"]["next_action"])
                snapshot = self.service._snapshot(room_id)
                continuation = self.service._natural_continuation(snapshot)
                self.assertTrue(continuation["can_send"], continuation)
                self.assertEqual(continuation["action"], "restart-objective")
                await self.service._send_natural_action(snapshot, continuation["action"], None, str(uuid4()), continuation["binding"], None)
                healed = await self.final(room_id)
                self.assertEqual(healed["objective"]["status"], "complete")

    async def test_failover_turned_off_stops_without_retiring_anyone(self):
        # Catches: retiring a partner when the user turned failover off.
        self.fake.faults = [Fault("kimi", 2)]
        room_id = await self.create(failover=False)
        final = await self.final(room_id)
        self.assertEqual((final["team"]["phase"], final["team"]["stop_reason"]), ("stopped", NATIVE_PREFLIGHT_FAILED))
        self.assertEqual(final["objective"]["retired_partners"], {})
        self.assert_healthy(final)

    async def test_a_prompt_that_may_have_been_sent_stays_on_the_conservative_path(self):
        # Catches: treating a failure after the send as replayable, which could
        # repeat work the model already did.
        cases = {
            "prompt_sent": lambda agent, permission: refused(agent, permission,
                observation=receipt(agent, permission, failure_diagnostic={**CONFIGURATION_FAILURE, "prompt_sent": True, "phase": "prompt", "rpc_method": "session/prompt"})),
            "tool_ran": lambda agent, permission: refused(agent, permission,
                observation=receipt(agent, permission, tool_calls=1, tool_results=1)),
        }
        for name, build in cases.items():
            with self.subTest(case=name):
                self.fake.faults = [Fault("kimi", 1, phase="plan", result=build)]
                self.fake.attempts.clear()
                room_id = await self.create()
                final = await self.final(room_id)
                self.assertEqual(self.fake.attempts["kimi"], 1, "no retry")
                self.assertEqual(final["team"]["phase"], "unknown")
                self.assertEqual([job["status"] for job in final["jobs"] if job["provider"] == "kimi"], ["unknown"])
                self.assertEqual(final["objective"]["retired_partners"], {})


class FolderCases(ServiceCase):
    """The reported incident: a folder room with three partners and a coordinate call."""

    def make_service(self):
        self.provider = FolderFaults()
        self.provider.handler = folder_handler
        return RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", registry=registry(self.provider))

    async def create(self, participants=ALL_PARTNERS, failover=True):
        created = await self.service.create_room("Update the project.", participants[0], str(uuid4()),
            mode="project_folder", project_id=self.registration["project"]["id"], participants=participants,
            collaboration=True, execution_policy={"mode": "autonomous", "quota_failover": failover})
        return created["room"]["id"]

    async def final(self, room_id):
        await self.settle()
        return await self.service.read_room(room_id)

    async def test_the_reported_configuration_failure_retries_invisibly(self):
        # Catches: the original incident: -32603 from a setup request ending
        # the room with an unknown status and an uncertain session identity.
        for phase in ("plan", "coordinate"):
            with self.subTest(phase=phase):
                self.provider.faults = [Fault("kimi", 1, phase=phase)]
                self.provider.attempts.clear()
                self.provider.calls.clear()
                final = await self.final(await self.create())
                self.assertEqual(final["objective"]["status"], "complete")
                self.assert_healthy(final)
                self.assertEqual(final["objective"]["retired_partners"], {})
                self.assertEqual(self.provider.attempts["kimi"], sum(1 for call in self.provider.calls if call["agent"] == "kimi") + 1)

    async def test_the_reported_configuration_failure_retires_only_the_failing_partner(self):
        # Catches: a provider setup failure cancelling the other partners' calls.
        for phase in ("plan", "coordinate"):
            with self.subTest(phase=phase):
                self.provider.faults = [Fault("kimi", 2, phase=phase)]
                final = await self.final(await self.create())
                self.assertEqual(final["objective"]["status"], "complete")
                self.assert_healthy(final)
                self.assertEqual(list(final["objective"]["retired_partners"]), ["kimi"])
                self.assertFalse(any(job["error"] == "invoke_interrupted" for job in final["jobs"]))

    async def test_an_identity_mismatch_before_the_prompt_stays_unknown(self):
        # Catches: retrying through a real identity problem just because no prompt left.
        self.provider.faults = [Fault("kimi", 1, phase="plan",
                                      result=lambda agent, permission: refused(agent, permission, failure_code="native_session_identity_mismatch"))]
        final = await self.final(await self.create())
        self.assertEqual(self.provider.attempts["kimi"], 1)
        self.assertEqual(final["team"]["phase"], "unknown")
        self.assertEqual(final["objective"]["retired_partners"], {})

    async def test_unconfirmed_cleanup_stays_unknown(self):
        # Catches: treating a possibly-alive process as harmless.
        self.provider.faults = [Fault("kimi", 1, phase="plan",
                                      result=lambda agent, permission: refused(agent, permission, observation=receipt(agent, permission, cleanup_confirmed=False)))]
        final = await self.final(await self.create())
        self.assertEqual(self.provider.attempts["kimi"], 1)
        self.assertEqual(final["team"]["phase"], "unknown")

    async def run_to_cancelled_work(self, proof, *, resume=True):
        """Both partners coordinate, then codex is held in work while claude's work fails."""
        started, cancelled_at = asyncio.Event(), []
        original = self.provider.handler
        def resuming(agent, context):
            return original(agent, context)
        self.provider.handler = resuming
        self.provider.held["codex"] = (started, proof)
        self.provider.faults = [Fault("claude", 2, phase="work", raises=True)]
        room_id = await self.create(["claude", "codex"], failover=False)
        await within(started.wait(), 10)
        return room_id

    async def test_a_cancelled_peer_with_a_known_session_and_confirmed_cleanup_can_continue(self):
        # Catches: a peer cancelled by someone else's stop being marked
        # identity-unknown even though its session and cleanup are both known.
        def confirmed(error):
            error.cleanup_confirmed = True
        room_id = await self.run_to_cancelled_work(confirmed)
        final = await self.final(room_id)
        self.assertEqual([agent for agent, _ in self.provider.cancelled], ["codex"])
        codex_job = [job for job in final["jobs"] if job["provider"] == "codex" and job["error"] == "invoke_interrupted"]
        self.assertEqual([job["status"] for job in codex_job], ["unknown"])
        self.assertIsNotNone(self.provider.cancelled[0][1])
        members = {member["id"]: member for member in final["members"]}
        self.assertFalse(members["codex"]["identity_uncertain"])
        self.assertEqual(members["codex"]["session_id"], self.provider.cancelled[0][1])
        self.assertEqual(final["team"]["phase"], "stopped")
        snapshot = self.service._snapshot(room_id)
        self.assertTrue(self.service._natural_continuation(snapshot)["can_send"])

    async def test_a_cancelled_peer_without_cleanup_proof_stays_uncertain(self):
        # Catches: loosening the identity gate without evidence of cleanup.
        room_id = await self.run_to_cancelled_work(lambda error: None)
        final = await self.final(room_id)
        members = {member["id"]: member for member in final["members"]}
        self.assertTrue(members["codex"]["identity_uncertain"])
        snapshot = self.service._snapshot(room_id)
        self.assertFalse(self.service._natural_continuation(snapshot)["can_send"])


class EvidenceTests(unittest.TestCase):
    def test_only_explicit_evidence_counts_as_never_sent(self):
        # Catches: a missing field being read as proof nothing happened.
        self.assertTrue(prompt_never_sent(receipt("kimi")))
        self.assertTrue(prompt_never_sent({"never_started": True, "tool_calls": 0, "tool_results": 0,
                                           "side_effects_possible": False, "cleanup_confirmed": True}))
        for bad in (None, {}, receipt("kimi", cleanup_confirmed=False), receipt("kimi", tool_calls=1), receipt("kimi", tool_results=1),
                    receipt("kimi", side_effects_possible=True), receipt("kimi", side_effects_possible=None),
                    receipt("kimi", failure_diagnostic={"prompt_sent": True}), receipt("kimi", failure_diagnostic={}),
                    {"tool_calls": 0, "tool_results": 0, "side_effects_possible": False, "cleanup_confirmed": True}):
            self.assertFalse(prompt_never_sent(bad), bad)

    def test_a_completed_call_or_different_resumed_identity_is_never_preflight(self):
        # Catches: a success, or a returned session differing from the resumed one, being retried.
        self.assertFalse(preflight_failed(None, result={**refused("kimi", "plan"), "status": "completed"}))
        self.assertFalse(preflight_failed("sid-a", result=refused("kimi", "plan", session_id="sid-b")))
        self.assertTrue(preflight_failed("sid-a", result=refused("kimi", "plan", session_id=None)))
        self.assertTrue(preflight_failed(None, result=refused("kimi", "plan", session_id="fresh")))
        for code in ("native_session_identity_mismatch", "subscription_limit_reached", "native_cleanup_unconfirmed"):
            self.assertFalse(preflight_failed(None, result=refused("kimi", "plan", failure_code=code)), code)


class CliAdapterEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name).resolve()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_cli_auth_refusal_reports_that_no_model_was_started(self):
        # Catches: Claude and Codex lacking the evidence the Kimi adapter gives.
        for agent, confirmed in (("claude", True), ("codex", True), ("codex", False)):
            with self.subTest(agent=agent, confirmed=confirmed):
                status = {"subscription_confirmed": False, "cleanup_confirmed": confirmed}
                with patch.object(adapters, "_subscription_status", return_value=status) as check, \
                        patch.object(adapters, "_command", return_value=["unused"]):
                    with self.assertRaises(RuntimeError) as caught:
                        await adapters.invoke(agent, "hello", self.cwd, timeout=5)
                check.assert_awaited_once()
                self.assertEqual(preflight_failed(None, error=caught.exception), confirmed)


class KimiWireEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = acp_fixture.KimiACPTests.asyncSetUp
    asyncTearDown = acp_fixture.KimiACPTests.asyncTearDown
    scenario = acp_fixture.KimiACPTests.scenario
    trace = acp_fixture.KimiACPTests.trace
    invoke = acp_fixture.KimiACPTests.invoke

    def inject_error(self, method, code):
        script = self.executable.read_text()
        old = '    if method == "initialize":'
        injected = '    if method == ' + repr(method) + ':\n        send({"id":row["id"],"error":{"code":' + str(code) + ',"message":"failed"}})\n        continue\n' + old
        self.executable.write_text(script.replace(old, injected))

    async def test_a_rejected_setup_request_over_stdio_is_classified_as_never_sent(self):
        # Catches: the adapter's real wire failure not matching what the room service expects.
        self.scenario()
        self.inject_error("session/set_mode", -32603)
        result = await self.invoke()
        self.assertEqual(result["failure_code"], "native_provider_error")
        self.assertEqual(result["native_execution"]["failure_diagnostic"]["prompt_sent"], False)
        self.assertFalse(any(row.get("method") == "session/prompt" for row in self.trace()))
        self.assertTrue(preflight_failed(None, result=result))

    async def test_a_failure_after_the_send_over_stdio_is_not_never_sent(self):
        # Catches: the prompt flag being set too late, so a sent prompt looks unsent.
        self.scenario()
        self.inject_error("session/prompt", -32603)
        result = await self.invoke()
        self.assertEqual(result["native_execution"]["failure_diagnostic"]["prompt_sent"], True)
        self.assertFalse(preflight_failed(None, result=result))


if __name__ == "__main__":
    unittest.main()
