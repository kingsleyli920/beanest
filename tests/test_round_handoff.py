"""Round handoff and session rotation. Real private trees and SQLite; only the native transport is synthetic."""
import json
import unittest
from uuid import uuid4

from local_agent_coordinator import adapters, round_handoff
from local_agent_coordinator.room_response_schema import native_room_response_schema, response_schema, stable_command_shape
from local_agent_coordinator.room_service import RoomService, _response
from local_agent_coordinator.agent_registry import default_registry
from tests import test_native_room_service as native

native_reply = native.native_reply
MEMBERS = ("claude", "codex")
PHASES = ("plan", "coordinate", "work", "review", "help", "user", "checkpoint", "checkpoint_review")
HANDOFF = {"goal": "Change the value to two.", "done": ["main.py now holds VALUE = 2"],
           "decisions": ["Notes stay untouched"], "unverified": ["Nobody ran the engine"], "key_files": ["main.py"]}
OVER_THRESHOLD = {"input_tokens": 1000, "cache_read_input_tokens": 300000, "cache_creation_input_tokens": 49000, "output_tokens": 500}


def stable_bytes(phase, can_propose=True, members=MEMBERS):
    return adapters._object_text(native_room_response_schema("claude", member_ids=members, provider="claude",
                                                             team_phase=phase, can_propose=can_propose))


def flat(**used):
    _, fields = stable_command_shape()
    return {**{field: None for field in fields}, **used}


def parse(phase, command):
    body = json.dumps({"message": "Current result.", "state": "done", "commands": [command], "base_revision": 1,
                       "files": [], "edits": [], "unchanged_paths": [], "resolved_conflicts": []})
    return _response(body, "claude", MEMBERS, project=True, team=True, native=True, native_phase=phase,
                     can_propose=True, stable_native=True)


CHECKPOINT = dict(type="checkpoint", decision="complete", summary="Done.", next_goal=None, scope_reference="goal", question=None)


class HandoffSchema(unittest.TestCase):
    def test_handoff_is_in_the_one_schema_every_phase_sends(self):
        # Catches a phase-dependent handoff field, which would rewrite the cached prompt on every phase change (#41).
        self.assertEqual({stable_bytes(phase, author) for phase in PHASES for author in (True, False)}, {stable_bytes("work")})
        item = json.loads(stable_bytes("work"))["properties"]["commands"]["items"]
        self.assertEqual(item["properties"]["handoff"]["type"], ["object", "null"])
        self.assertEqual(item["properties"]["handoff"]["properties"]["goal"]["maxLength"], round_handoff.GOAL_MAX_CHARACTERS)
        self.assertEqual(item["properties"]["handoff"]["properties"]["done"]["maxItems"], round_handoff.LIST_MAX_ITEMS)
        self.assertEqual(item["properties"]["handoff"]["properties"]["key_files"]["items"]["maxLength"], round_handoff.KEY_FILE_MAX_CHARACTERS)
        self.assertIn("handoff", item["required"])

    def test_other_adapters_are_not_offered_handoff(self):
        # Catches the field leaking to Codex or Kimi per-phase schemas, whose strict parsers treat it as unknown.
        for adapter in ("codex", "kimi"):
            schema = native_room_response_schema(adapter, member_ids=MEMBERS, provider="claude", team_phase="checkpoint", can_propose=True)
            self.assertNotIn("handoff", schema["properties"]["commands"]["items"]["properties"], adapter)
            self.assertEqual(schema, response_schema(project=True, native=True, collaboration=True, member_ids=MEMBERS,
                                                     provider="claude", team_phase="checkpoint", can_propose=True))


class HandoffParsing(unittest.TestCase):
    def test_task_ending_commands_keep_handoff_and_others_drop_it_without_correction(self):
        # Catches a misplaced handoff triggering an invalid_team_commands correction call, or being lost where it is used.
        kept = parse("checkpoint", flat(**CHECKPOINT, handoff=HANDOFF))
        self.assertEqual(kept["commands"][0]["handoff"], HANDOFF)
        for phase, command in (("review", dict(type="review", task_id="T1", verdict="accept", reason="ok")),
                               ("work", dict(type="complete", task_id="T1", result="Done."))):
            self.assertEqual(parse(phase, flat(**command, handoff=HANDOFF))["commands"][0]["handoff"], HANDOFF, phase)
        dropped = parse("plan", flat(type="claim", task_id="T1", handoff=HANDOFF))
        self.assertNotIn("handoff", dropped["commands"][0])
        self.assertNotIn("handoff", parse("checkpoint", flat(**CHECKPOINT, handoff=None))["commands"][0])

    def test_normalized_handoff_boundaries(self):
        # Catches off-by-one limits and partially trusted shapes reaching storage.
        good = {"goal": "x" * 1000, "done": ["d"] * 12, "decisions": [], "unverified": ["u" * 500], "key_files": ["f" * 240] * 24}
        self.assertEqual(round_handoff.normalized_handoff(good)["goal"], "x" * 1000)
        for name, mutation in {"goal too long": {"goal": "x" * (round_handoff.GOAL_MAX_CHARACTERS + 1)}, "too many items": {"done": ["d"] * (round_handoff.LIST_MAX_ITEMS + 1)},
                               "item too long": {"unverified": ["u" * (round_handoff.ITEM_MAX_CHARACTERS + 1)]}, "too many files": {"key_files": ["f"] * (round_handoff.KEY_FILES_MAX_ITEMS + 1)},
                               "file too long": {"key_files": ["f" * (round_handoff.KEY_FILE_MAX_CHARACTERS + 1)]}, "blank goal": {"goal": "   "},
                               "blank item": {"done": [" "]}, "wrong type": {"done": "main.py"}}.items():
            with self.subTest(name=name):
                self.assertIsNone(round_handoff.normalized_handoff({**good, **mutation}))
        missing = {key: value for key, value in good.items() if key != "key_files"}
        self.assertIsNone(round_handoff.normalized_handoff(missing))
        self.assertIsNone(round_handoff.normalized_handoff({**good, "extra": "x"}))
        self.assertIsNone(round_handoff.normalized_handoff("notes"))

    def test_context_tokens_needs_all_three_counts(self):
        # Catches a partial usage report turning into a small count that wrongly blocks or passes the threshold.
        self.assertEqual(round_handoff.context_tokens({"values": OVER_THRESHOLD}), 350000)
        self.assertIsNone(round_handoff.context_tokens({"values": {"input_tokens": 1000, "cache_read_input_tokens": 5}}))
        self.assertIsNone(round_handoff.context_tokens(None))


class IterationUsage(unittest.TestCase):
    def test_context_is_the_last_api_request_not_the_turn_total(self):
        # Catches the cumulative result.usage driving the threshold: observed real CLI output had two requests,
        # the total summed to 72612 while the last request's context was 36426.
        lines = [json.dumps(event).encode() for event in (
            {"type": "system", "subtype": "init", "session_id": "s"},
            {"type": "assistant", "session_id": "s", "message": {"role": "assistant", "usage": {"input_tokens": 2, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 36184}, "content": [{"type": "text", "text": "a"}]}},
            {"type": "assistant", "session_id": "s", "message": {"role": "assistant", "usage": {"input_tokens": 2, "cache_read_input_tokens": 36184, "cache_creation_input_tokens": 240}, "content": [{"type": "text", "text": "b"}]}},
            {"type": "result", "subtype": "success", "session_id": "s", "usage": {"input_tokens": 4, "cache_read_input_tokens": 36184, "cache_creation_input_tokens": 36424}})]
        result = adapters.parse_events("claude", lines)
        self.assertEqual(result["usage"]["cache_creation_input_tokens"], 36424)
        usage = {"values": result["usage"], "last_iteration": result["last_iteration_usage"]}
        self.assertEqual(round_handoff.context_tokens(usage), 36426)
        self.assertEqual(round_handoff.context_tokens({"values": result["usage"]}), 72612, "without an iteration count, fall back to the total")


class RoundHandoffService(unittest.IsolatedAsyncioTestCase):
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        await native.NativeRoomServiceTests.asyncSetUp(self)
        self.install_usage()
        self.fake.handler = self.handler
        self.round_handoffs = [HANDOFF]
        self.stage_decisions = {}  # objective stage -> decision for the checkpoint author
        self.reject_first_checkpoint = False
        self.written = self.proposals = 0

    def install_usage(self):
        self.checkpoint_usage = OVER_THRESHOLD
        invoke = self.service.native_invoke
        async def with_usage(agent, prompt, cwd, session_id=None, **options):
            result = await invoke(agent, prompt, cwd, session_id=session_id, **options)
            if agent == "claude" and json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])["job"]["job_phase"] == "checkpoint":
                result["usage"] = self.checkpoint_usage
            return result
        self.service.native_invoke = with_usage

    def handler(self, agent, context, cwd):
        phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and agent == board["plan_author"]:
                commands += [native.proposal("T1", paths=["main.py"], capability="python"), {"type": "claim", "task_id": "T1"}]
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            self.written += 1
            (cwd / "main.py").write_text(f"VALUE = {self.written + 1}\n")
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Updated main."}])
        if phase == "review":
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Read the exact tree."}])
        objective = context["objective"]
        if phase == "checkpoint":
            self.proposals += 1
            decision = self.stage_decisions.get(objective["stage"], "complete")
            reply = native_reply(context, [{"type": "checkpoint", "decision": decision, "summary": f"Stage {objective['stage']} result {self.written} proposal {self.proposals}.",
                "next_goal": "Finish the remaining part." if decision == "continue" else None,
                "scope_reference": objective["scope_reference"], "question": None}])
            if agent == "claude":
                handoff = self.round_handoffs.pop(0) if len(self.round_handoffs) > 1 else self.round_handoffs[0]
                reply["commands"][0]["handoff"] = handoff
            return reply
        if phase == "checkpoint_review":
            verdict = "changes_requested" if self.reject_first_checkpoint else "accept"
            self.reject_first_checkpoint = False
            return native_reply(context, [{"type": "checkpoint_review", "verdict": verdict,
                "reason": "Goal and delivery compared.", "proposal_digest": objective["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    async def finished_room(self):
        created = await self.create()
        await self.settle()
        room_id = created["room"]["id"]
        state = await self.service.read_room(room_id)
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        return room_id, state

    async def follow_up(self, room_id, state):
        await self.service.send_message(room_id, "Change the value again.", "all", str(uuid4()), intent="continue",
                                        binding=state["continuation"]["binding"])
        await self.settle()
        return await self.service.read_room(room_id)

    def session(self, state, member="claude"):
        return next(item["session_id"] for item in state["members"] if item["id"] == member)

    def events(self, state, kind):
        return [event for event in state["events"] if event["kind"] == kind]

    def claude_calls(self, phase=None):
        return [call for call in self.fake.calls if call["agent"] == "claude" and (phase is None or call["context"]["job"]["job_phase"] == phase)]

    async def test_followup_after_a_large_round_moves_the_author_to_a_new_session_with_the_handoff(self):
        # Catches the whole chain breaking: recording, rotation, a first-registration refusal on the new session, or a lost handoff.
        self.stage_decisions = {1: "continue"}
        self.round_handoffs = [HANDOFF]
        room_id, first = await self.finished_room()
        old = self.session(first)
        self.assertEqual(first["objective"]["next_step"]["decision"], "complete")
        recorded = self.events(first, "round_handoff_recorded")[-1]
        self.assertEqual(recorded["handoff"], HANDOFF)
        self.assertEqual(recorded["context_tokens"], 350000)
        self.assertEqual((await self.service.read_room_state(room_id))["latest_round_handoff"]["handoff"], HANDOFF)
        before = len(self.claude_calls())
        self.stage_decisions = {1: "continue"}
        final = await self.follow_up(room_id, first)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        rotated = self.events(final, "partner_session_rotated")
        self.assertEqual([(event["provider"], event["context_tokens"]) for event in rotated], [("claude", 350000)])
        self.assertNotEqual(self.session(final), old)
        self.assertIsNotNone(self.session(final))
        records = self.service.store.native_session_cleanup_records(room_id)
        self.assertIn(old, {record["session_id"] for record in records}, "the old session must stay cleanable")
        self.assertIn(self.session(final), {record["session_id"] for record in records})
        new_calls = self.claude_calls()[before:]
        self.assertIsNone(new_calls[0]["session_id"], "the first call of the round starts fresh")
        self.assertEqual(new_calls[0]["context"]["job"]["job_phase"], "checkpoint", "the fresh session runs the assessment checkpoint")
        self.assertEqual(new_calls[0]["context"]["round_handoff"], {"source_run_id": first["current_run"]["id"], **HANDOFF})
        self.assertIn("check it against the repository's current state", new_calls[0]["prompt"])
        self.assertEqual(new_calls[0]["context"]["goal"], first["objective"]["original_goal"])
        self.assertIn("tasks", new_calls[0]["context"]["team"], "the task board is still in the first prompt")
        self.assertTrue(new_calls[0]["cwd"].is_dir())
        self.assertEqual(new_calls[1]["session_id"], self.session(final), "the next job resumes the new session")
        self.assertNotIn("round_handoff", new_calls[1]["context"])
        self.assertTrue(all("round_handoff" not in call["context"] for call in new_calls[1:]))

    async def test_directed_followup_and_a_new_run_also_rotate(self):
        # Catches rotation being wired to only the all-members natural followup path, missing the other user-round entries.
        room_id, state = await self.finished_room()
        old = self.session(state)
        await self.service.send_message(room_id, "Change the value again.", "claude", str(uuid4()), intent="continue",
                                        binding=state["continuation"]["binding"])
        await self.settle()
        directed = await self.service.read_room(room_id)
        self.assertEqual(len(self.events(directed, "partner_session_rotated")), 1)
        self.assertNotEqual(self.session(directed), old)
        self.assertEqual(self.events(directed, "run_started")[-1]["session_rotated"], True)
        new_session = self.session(directed)
        run = directed["current_run"]
        await self.service.start_run(room_id, "Add a follow-up note.", "all", str(uuid4()),
                                     run_id=run["id"], revision=run["revision"], digest=run["digest"])
        await self.settle()
        after_run = await self.service.read_room(room_id)
        self.assertEqual(len(self.events(after_run, "partner_session_rotated")), 2)
        self.assertNotEqual(self.session(after_run), new_session)

    async def test_threshold_is_the_exact_boundary(self):
        # Catches an off-by-one in the 200_000 comparison in either direction.
        self.checkpoint_usage = {**OVER_THRESHOLD, "cache_read_input_tokens": 200_000 - 1 - 1000 - 49000}
        room_id, state = await self.finished_room()
        self.assertEqual(self.events(state, "round_handoff_recorded")[-1]["context_tokens"], 199_999)
        kept = self.session(state)
        # This follow-up stays below the threshold; its own checkpoint now reports exactly the boundary.
        self.checkpoint_usage = {**OVER_THRESHOLD, "cache_read_input_tokens": 200_000 - 1000 - 49000}
        self.assertEqual(self.session(await self.follow_up(room_id, state)), kept)
        state = await self.service.read_room(room_id)
        self.assertEqual(self.events(state, "partner_session_rotated"), [])
        self.assertEqual(self.events(state, "round_handoff_recorded")[-1]["context_tokens"], 200_000)
        self.assertEqual(state["objective"]["status"], "complete")
        again = await self.follow_up(room_id, state)
        self.assertEqual(len(self.events(again, "partner_session_rotated")), 1)
        self.assertNotEqual(self.session(again), kept)

    async def test_missing_handoff_keeps_the_session_and_costs_no_correction(self):
        # Catches a missing handoff being treated as an error (extra call) or still rotating the session.
        self.round_handoffs = [None]
        room_id, state = await self.finished_room()
        self.assertEqual(self.events(state, "round_handoff_recorded"), [])
        self.assertEqual(self.events(state, "format_correction_queued"), [])
        kept, calls = self.session(state), len(self.claude_calls())
        final = await self.follow_up(room_id, state)
        self.assertEqual(self.session(final), kept)
        self.assertEqual(self.events(final, "partner_session_rotated"), [])
        self.assertTrue(all("round_handoff" not in call["context"] for call in self.claude_calls()[calls:]))

    async def test_malformed_handoff_is_noted_without_correction_or_rotation(self):
        # Catches a schema-escaping handoff forcing a format retry or being half-stored.
        self.round_handoffs = [{"goal": "x" * (round_handoff.GOAL_MAX_CHARACTERS + 1), "done": [], "decisions": [], "unverified": [], "key_files": []}]
        room_id, state = await self.finished_room()
        self.assertEqual(len(self.events(state, "round_handoff_invalid")), 1)
        self.assertEqual(self.events(state, "round_handoff_recorded"), [])
        self.assertEqual(self.events(state, "format_correction_queued"), [])
        kept = self.session(state)
        self.assertEqual(self.session(await self.follow_up(room_id, state)), kept)

    async def test_continue_within_a_round_never_rotates(self):
        # Catches rotation firing on the automatic checkpoint-continue path of the same user round.
        self.stage_decisions = {1: "continue"}
        room_id, state = await self.finished_room()
        self.assertEqual(state["objective"]["stage"], 2)
        self.assertEqual(self.events(state, "partner_session_rotated"), [])
        calls = self.claude_calls()
        self.assertEqual({call["session_id"] for call in calls[1:]}, {self.session(state)})
        self.assertTrue(all("round_handoff" not in call["context"] for call in calls))

    async def test_revised_checkpoint_without_a_new_handoff_does_not_rotate(self):
        # Catches rotation trusting a handoff from a checkpoint proposal the reviewer rejected.
        self.reject_first_checkpoint = True
        self.round_handoffs = [HANDOFF, None]
        room_id, state = await self.finished_room()
        self.assertEqual(len(self.events(state, "round_handoff_recorded")), 1)
        kept = self.session(state)
        final = await self.follow_up(room_id, state)
        self.assertEqual(self.session(final), kept)
        self.assertEqual(self.events(final, "partner_session_rotated"), [])

    async def test_a_restart_keeps_the_handoff_and_its_pending_delivery(self):
        # Catches the handoff or the owed first-call delivery existing only in memory.
        room_id, state = await self.finished_room()
        old = self.session(state)
        await self.service.shutdown()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", native_invoke=self.fake, registry=default_registry())
        self.install_usage()
        self.assertEqual((await self.service.read_room_state(room_id))["latest_round_handoff"]["handoff"], HANDOFF)
        before = len(self.claude_calls())
        final = await self.follow_up(room_id, await self.service.read_room(room_id))
        self.assertNotEqual(self.session(final), old)
        self.assertEqual(self.claude_calls()[before]["context"]["round_handoff"]["goal"], HANDOFF["goal"])

    async def test_rotation_gate_refuses_every_unsafe_or_ineligible_state(self):
        # Catches a single gate being dropped. Each row breaks exactly one condition on an otherwise eligible room.
        room_id, state = await self.finished_room()
        store = self.service.store
        with store._connection() as db:
            run_id = store._team.team(db, room_id)["current_run_id"]

        def attempt():
            with store._connection(write=True) as db:
                team = store._team
                objective = team.objectives.current(db, room_id)
                room = store._room(db, room_id)
                result = team._rotate_after_handoff(db, room_id, room, objective, run_id, str(uuid4()))
                db.rollback()
                return result

        def alter(sql, *parameters):
            def apply(db):
                db.execute(sql, parameters)
            return apply

        def mode(db):
            db.execute("UPDATE rooms SET mode='project_folder' WHERE id=?", (room_id,))

        cases = {
            "identity uncertain": alter("UPDATE members SET identity_uncertain=1 WHERE room_id=? AND provider='claude'", room_id),
            "another adapter wrote it": alter("UPDATE members SET adapter='codex' WHERE room_id=? AND provider='claude'", room_id),
            "no current session": alter("UPDATE members SET session_id=NULL WHERE room_id=? AND provider='claude'", room_id),
            "in-flight job": alter("UPDATE jobs SET status='working' WHERE id=(SELECT id FROM jobs WHERE room_id=? AND provider='claude' LIMIT 1)", room_id),
            "unknown job in the ended run": alter("UPDATE jobs SET status='unknown' WHERE id=(SELECT j.id FROM jobs j JOIN team_jobs t ON t.job_id=j.id WHERE j.room_id=? AND j.provider='claude' LIMIT 1)", room_id),
            "objective not complete": alter("UPDATE objectives SET status='needs_user' WHERE room_id=?", room_id),
            "not a native room": mode,
        }
        self.assertTrue(attempt(), "precondition: the unaltered room is eligible")
        for name, apply in cases.items():
            with self.subTest(name=name):
                with store._connection(write=True) as db:
                    apply(db)
                    team, objective = store._team, store._team.objectives.current(db, room_id)
                    result = team._rotate_after_handoff(db, room_id, store._room(db, room_id), objective, run_id, str(uuid4()))
                    session = db.execute("SELECT session_id FROM members WHERE room_id=? AND provider='claude'", (room_id,)).fetchone()[0]
                    db.rollback()
                self.assertFalse(result)
                self.assertEqual(session, None if name == "no current session" else self.session(state))
        with store._connection(write=True) as db:
            result = store._team._rotate_after_handoff(db, room_id, store._room(db, room_id), store._team.objectives.current(db, room_id), str(uuid4()), str(uuid4()))
            db.rollback()
        self.assertFalse(result, "a handoff from another run must not rotate this one")

    async def test_prompt_asks_for_handoff_only_of_claude_checkpoints(self):
        # Catches the writing instruction vanishing, reaching review phases, or reaching Codex.
        await self.finished_room()
        checkpoint = self.claude_calls("checkpoint")[0]
        self.assertIn("fill handoff", checkpoint["prompt"])
        self.assertIn("null is accepted", checkpoint["prompt"])
        for call in self.fake.calls:
            if call["context"]["job"]["job_phase"] != "checkpoint" or call["agent"] != "claude":
                self.assertNotIn("fill handoff", call["prompt"], call["agent"] + call["context"]["job"]["job_phase"])


    async def test_only_long_running_phases_are_asked_to_report_progress(self):
        # Catches the instruction vanishing from work calls, which brought back 76 silent minutes on a real room,
        # or leaking into planning and checkpoint calls that have nothing to report.
        await self.finished_room()
        phases = {call["context"]["job"]["job_phase"]: call["prompt"] for call in self.fake.calls}
        self.assertIn("at least every 15 minutes of work", phases["work"])
        self.assertNotIn("every 15 minutes", phases["checkpoint"])
        self.assertNotIn("every 15 minutes", phases["plan"])


LARGE = {"input_tokens": 1000, "cache_read_input_tokens": 500000, "cache_creation_input_tokens": 49000, "output_tokens": 500}
SMALL = {"input_tokens": 1000, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 4000, "output_tokens": 500}
BROKEN_HANDOFF = {"goal": "Missing every list."}


def usage_totalling(tokens):
    return {"input_tokens": 0, "cache_read_input_tokens": tokens, "cache_creation_input_tokens": 0, "output_tokens": 1}


class TaskHandoffService(unittest.IsolatedAsyncioTestCase):
    """Two or three tasks in one round, driven through real storage and the service's own job loop."""
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        await native.NativeRoomServiceTests.asyncSetUp(self)
        native.no_rebalance(self.service)
        self.fake.handler = self.handler
        self.task_ids = ["T1", "T2"]
        self.usage = lambda phase, task_id: LARGE   # what each Claude call reports having sent
        self.handoff_on = {"T1"}                    # tasks whose work or review result carries a record
        self.handoff_value = HANDOFF
        self.reject_once = set()                    # tasks whose first review asks for changes
        self.independent = False                    # tasks may run in any order
        self.record_once = set()                    # tasks that leave a record only on their first result
        invoke = self.service.native_invoke
        async def with_usage(agent, prompt, cwd, session_id=None, **options):
            result = await invoke(agent, prompt, cwd, session_id=session_id, **options)
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            result["usage"] = self.usage(context["job"]["job_phase"], (context.get("task") or {}).get("id"))
            return result
        self.service.native_invoke = with_usage

    def handler(self, agent, context, cwd):
        phase, board, task = context["job"]["job_phase"], context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and agent == board["plan_author"]:
                for index, task_id in enumerate(self.task_ids):
                    commands += [native.proposal(task_id, paths=[f"{task_id}.py"], capability="python",
                                                 dependencies=[] if self.independent else self.task_ids[:index][-1:]), {"type": "claim", "task_id": task_id}]
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            (cwd / f"{task['id']}.py").write_text(f"VALUE = {len(self.work_calls()) + 1}\n")
            return self.with_record(agent, task, native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Updated the file."}]))
        if phase == "review":
            verdict = "changes_requested" if task["id"] in self.reject_once else "accept"
            self.reject_once.discard(task["id"])
            return self.with_record(agent, task, native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": verdict, "reason": "Read the exact tree."}]))
        if phase == "checkpoint":
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "All done.", "next_goal": None,
                                           "scope_reference": context["objective"]["scope_reference"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Goal and delivery compared.",
                                           "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    def with_record(self, agent, task, reply):
        if agent == "claude" and task["id"] in self.handoff_on:
            reply["commands"][0]["handoff"] = self.handoff_value
            if task["id"] in self.record_once:
                self.handoff_on.discard(task["id"])
        return reply

    def calls(self, phase=None, task_id=None, agent="claude"):
        return [call for call in self.fake.calls if call["agent"] == agent
                and (phase is None or call["context"]["job"]["job_phase"] == phase)
                and (task_id is None or (call["context"].get("task") or {}).get("id") == task_id)]

    def work_calls(self):
        return self.calls("work")

    async def run_room(self, participants=None):
        created = await self.create(participants=participants)
        await self.settle()
        state = await self.service.read_room(created["room"]["id"])
        self.assertEqual(state["objective"]["status"], "complete", state["team"])
        return state

    def events(self, state, kind):
        return [event for event in state["events"] if event["kind"] == kind]

    async def test_next_task_starts_a_new_session_that_carries_only_the_record(self):
        # Catches the record being lost, the old session being resumed, or the record being replayed on later calls.
        self.task_ids = ["T1", "T2", "T3"]
        state = await self.run_room()
        rotated = self.events(state, "partner_session_rotated")
        self.assertEqual([event["scope"] for event in rotated], ["task"])
        self.assertEqual(self.events(state, "task_handoff_recorded")[0]["task_id"], "T1")
        first, second, third = (self.calls("work", task)[0] for task in ("T1", "T2", "T3"))
        self.assertIsNotNone(first["session_id"])
        self.assertIsNone(second["session_id"], "the second task must not resume the large session")
        self.assertEqual({key: second["context"]["round_handoff"][key] for key in HANDOFF}, HANDOFF)
        self.assertIn("previous round or task left", second["prompt"])
        self.assertIsNotNone(third["session_id"], "the new session is resumed afterwards")
        self.assertNotEqual(third["session_id"], first["session_id"])
        self.assertNotIn("round_handoff", third["context"])

    async def test_threshold_is_the_exact_boundary(self):
        # Catches an off-by-one in the between-task size limit.
        for tokens, expected in ((399_999, 0), (400_000, 1)):
            with self.subTest(tokens=tokens):
                await native.NativeRoomServiceTests.asyncTearDown(self)
                await self.asyncSetUp()
                self.usage = lambda phase, task_id: usage_totalling(tokens)
                state = await self.run_room()
                self.assertEqual(len(self.events(state, "partner_session_rotated")), expected)

    async def test_only_a_large_session_is_asked_and_only_claude(self):
        # Catches every call paying for a record, or Codex being given an instruction its schema cannot answer.
        self.usage = lambda phase, task_id: SMALL if phase in {"plan", "coordinate"} else LARGE
        await self.run_room()
        self.assertNotIn("handoff_requested", self.calls("work", "T1")[0]["context"], "the session was still small at this claim")
        self.handoff_on = set()
        await native.NativeRoomServiceTests.asyncTearDown(self)
        await self.asyncSetUp()
        await self.run_room()
        large = self.calls("work", "T1")[0]
        self.assertTrue(large["context"]["handoff_requested"])
        self.assertIn("fill handoff", large["prompt"])
        codex_reviews = self.calls("review", agent="codex")
        self.assertTrue(codex_reviews and all(call["session_id"] for call in codex_reviews), "a resumed Codex session reported a large context")
        for call in self.fake.calls:
            if call["agent"] != "claude":
                self.assertNotIn("handoff_requested", call["context"], call["agent"])

    async def test_no_record_keeps_the_session_without_a_correction_call(self):
        # Catches a skipped record costing an extra model call or dropping the session anyway.
        self.handoff_on = set()
        state = await self.run_room()
        self.assertEqual(self.events(state, "partner_session_rotated"), [])
        sessions = {call["session_id"] for call in self.calls("work")}
        self.assertEqual(len(sessions), 1)
        self.assertNotIn(None, sessions)
        self.assertEqual(len(self.calls("work")), 2)

    async def test_malformed_record_is_noted_and_keeps_the_session(self):
        # Catches a broken record either replacing the session's memory or stopping the task.
        self.handoff_value = BROKEN_HANDOFF
        state = await self.run_room()
        self.assertEqual(len(self.events(state, "round_handoff_invalid")), 1)
        self.assertEqual(self.events(state, "partner_session_rotated"), [])
        self.assertEqual(len({call["session_id"] for call in self.calls("work")}), 1)

    async def test_rework_of_the_same_task_keeps_its_memory(self):
        # Catches a rejected attempt being corrected by a session that no longer remembers it.
        self.reject_once = {"T1"}
        state = await self.run_room()
        attempts = self.calls("work", "T1")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]["session_id"], attempts[0]["session_id"])
        self.assertNotIn("round_handoff", attempts[1]["context"])
        self.assertIsNone(self.calls("work", "T2")[0]["session_id"], "the next task still moves on")
        self.assertEqual(len(self.events(state, "partner_session_rotated")), 1)

    async def test_a_session_that_grows_during_a_peer_reviewed_task_still_moves_on(self):
        # Catches the record being asked for too late: with a peer reviewing, this partner has no later call to write it in.
        def usage(phase, task_id):
            return usage_totalling(250_000) if phase in {"plan", "coordinate"} else usage_totalling(600_000)
        self.usage = usage
        await self.run_room()
        first = self.calls("work", "T1")[0]
        self.assertTrue(first["context"]["handoff_requested"], "asked while still under the rotation size")
        self.assertIsNone(self.calls("work", "T2")[0]["session_id"])

    async def test_a_request_alone_does_not_move_a_session_that_stayed_small(self):
        # Catches the earlier request also lowering the size at which a session is replaced.
        self.usage = lambda phase, task_id: usage_totalling(250_000)
        state = await self.run_room()
        self.assertEqual(self.events(state, "partner_session_rotated"), [])
        self.assertEqual(self.events(state, "task_handoff_recorded"), [])

    async def test_rework_of_an_earlier_task_keeps_the_session_even_after_another_tasks_record(self):
        # Catches a rejected task being reworked by a new session that only knows a different task's record.
        self.independent = True
        self.handoff_on = {"T2"}
        self.reject_once = {"T1"}
        await self.run_room()
        rework = self.calls("work", "T1")[1]
        self.assertIsNotNone(rework["session_id"])
        self.assertNotIn("round_handoff", rework["context"])

    async def test_a_later_result_without_a_record_makes_the_old_record_stale(self):
        # Catches a new session being handed a record written before the rework it never heard about.
        self.handoff_on = {"T1"}
        self.record_once = {"T1"}
        self.reject_once = {"T1"}
        state = await self.run_room()
        self.assertEqual(self.events(state, "partner_session_rotated"), [])
        self.assertIsNotNone(self.calls("work", "T2")[0]["session_id"])

    async def test_self_check_keeps_the_author_session_then_the_next_task_moves(self):
        # Catches a self-check losing the work it reviews, and the accepted review's record going unused.
        self.handoff_on = {"T1"}
        state = await self.run_room(participants=["claude"])
        work, review = self.calls("work", "T1")[0], self.calls("review", "T1")[0]
        self.assertEqual(review["session_id"], work["session_id"])
        self.assertIsNone(self.calls("work", "T2")[0]["session_id"])
        self.assertEqual(self.events(state, "task_handoff_recorded")[-1]["task_id"], "T1")

    async def test_rejected_review_records_nothing(self):
        # Catches a record written while the task is still open being trusted as finished work.
        self.handoff_on = {"T1"}
        self.reject_once = {"T1"}
        state = await self.run_room(participants=["claude"])
        kinds = [event["job_id"] for event in self.events(state, "task_handoff_recorded")]
        reviews = [call for call in self.calls("review", "T1")]
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len(kinds), len(self.calls("work", "T1")) + 1, "both attempts and the accepting review, not the rejecting one")

    async def test_checkpoint_and_last_task_never_rotate(self):
        # Catches a round-end judgement starting without the session that did the work.
        self.task_ids = ["T1"]
        state = await self.run_room()
        self.assertEqual(self.events(state, "partner_session_rotated"), [])
        self.assertEqual(self.calls("checkpoint")[0]["session_id"], self.calls("work", "T1")[0]["session_id"])


if __name__ == "__main__":
    unittest.main()
