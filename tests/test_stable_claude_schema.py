"""Claude keeps one schema across phases; the service keeps every phase rule."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent_coordinator import adapters, native_adapters
from local_agent_coordinator.room_response_schema import (command_shape, native_room_response_schema,
                                                           response_schema, stable_command_shape)
from local_agent_coordinator.room_contract import MAX_RESPONSE_COMMANDS
from local_agent_coordinator.room_service import ResponseValidationError, RoomError, RoomService, _response
from tests import test_native_room_service as native

PHASES = ("plan", "coordinate", "work", "review", "help", "user", "checkpoint", "checkpoint_review")
MEMBERS = ("claude", "codex")
SESSION = "42619ac2-4bfa-446c-a773-5193109566ae"
ALL_KINDS = ["propose", "claim", "complete", "block", "review", "request_help", "plan_ready",
             "checkpoint", "checkpoint_review"]


def claude_bytes(phase, can_propose, members=MEMBERS):
    return adapters._object_text(native_room_response_schema("claude", member_ids=members, provider="claude",
                                                             team_phase=phase, can_propose=can_propose))


def flat(**used):
    """The null-filled command Claude must send under the stable schema."""
    _, fields = stable_command_shape()
    return {**{field: None for field in fields}, **used}


def body(commands):
    return json.dumps({"message": "Current result.", "state": "done", "commands": commands, "base_revision": 1,
                       "files": [], "edits": [], "unchanged_paths": [], "resolved_conflicts": []})


def parse(phase, commands, *, can_propose=True, members=MEMBERS):
    return _response(body(commands), "claude", members, project=True, team=True, native=True,
                     native_phase=phase, can_propose=can_propose, stable_native=True)


class StableSchemaBytes(unittest.TestCase):
    def test_claude_bytes_are_identical_across_every_phase_and_can_propose(self):
        # Catches any phase or plan-author input leaking back into the cached schema.
        for members in (MEMBERS, ("claude",)):
            with self.subTest(members=members):
                seen = {claude_bytes(phase, author, members) for phase in PHASES for author in (True, False)}
                self.assertEqual(len(seen), 1)

    def test_claude_argv_carries_the_same_schema_for_a_resumed_session(self):
        # Catches a difference introduced between the schema object and real CLI argv.
        argv = set()
        for phase in PHASES:
            with patch.object(native_adapters.adapters, "find_claude", return_value="/fake/claude"):
                command = native_adapters.native_command("claude", SESSION, permission="full_access",
                                                         schema_json=claude_bytes(phase, phase == "plan"))
            argv.add(command[command.index("--json-schema") + 1])
        self.assertEqual(len(argv), 1)

    def test_bytes_survive_a_restart_with_a_different_hash_seed(self):
        # Catches set iteration order reaching the bytes, which would miss the cache after every upgrade.
        script = ("from local_agent_coordinator import adapters;"
                  "from local_agent_coordinator.room_response_schema import native_room_response_schema as s;"
                  "print(adapters._object_text(s('claude', member_ids=('claude', 'codex'), provider='claude',"
                  "team_phase='work', can_propose=False)))")
        src = str(Path(__file__).resolve().parents[1] / "src")
        outputs = {subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True,
                                  env={**os.environ, "PYTHONPATH": src, "PYTHONHASHSEED": seed}).stdout
                   for seed in ("1", "2", "77")}
        self.assertEqual(outputs, {claude_bytes("work", False) + "\n"})

    def test_stable_schema_is_the_declared_union_with_no_phase_constraints(self):
        # Catches a union that silently drops a command or keeps a phase-only requirement.
        schema = json.loads(claude_bytes("review", False))
        item = schema["properties"]["commands"]["items"]
        self.assertEqual(item["properties"]["type"]["enum"], ALL_KINDS)
        self.assertEqual(schema["properties"]["commands"]["maxItems"], MAX_RESPONSE_COMMANDS)
        self.assertEqual(schema["properties"]["files"]["maxItems"], 0)
        self.assertEqual(schema["properties"]["edits"]["maxItems"], 0)
        for field in ("reason", "summary"):
            self.assertEqual(item["properties"][field]["type"], ["string", "null"])
            self.assertNotIn("minLength", item["properties"][field])
        solo = json.loads(claude_bytes("review", False, ("claude",)))
        self.assertNotIn("request_help", solo["properties"]["commands"]["items"]["properties"]["type"]["enum"])
        self.assertNotIn("to", solo["properties"]["commands"]["items"]["properties"])

    def test_other_adapters_keep_their_exact_per_phase_schema(self):
        # Catches the stable branch spreading to Codex or Kimi, which have no such cache and rely on the narrow enum.
        for adapter in ("codex", "kimi"):
            for phase in PHASES:
                for author in (True, False):
                    with self.subTest(adapter=adapter, phase=phase, author=author):
                        self.assertEqual(native_room_response_schema(adapter, member_ids=MEMBERS, provider="claude",
                                                                     team_phase=phase, can_propose=author),
                                         response_schema(project=True, native=True, collaboration=True, member_ids=MEMBERS,
                                                         provider="claude", team_phase=phase, can_propose=author))
        self.assertEqual(native_room_response_schema("codex", member_ids=MEMBERS, provider="claude", team_phase="review",
                                                     can_propose=False)["properties"]["commands"]["items"]["properties"]["type"]["enum"],
                         ["block", "review"])

    def test_invalid_phase_still_fails_closed_for_claude(self):
        # Catches an unknown job phase being granted the widest schema because Claude ignores the phase.
        for phase in (None, "ship", 1):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                native_room_response_schema("claude", member_ids=MEMBERS, provider="claude", team_phase=phase, can_propose=True)


class ServerSideRejections(unittest.TestCase):
    def assert_rejected(self, phase, command, key, expected, **options):
        with self.assertRaises(ResponseValidationError) as failure:
            parse(phase, [command], **options)
        self.assertEqual(failure.exception.code, "invalid_team_commands")
        self.assertEqual(failure.exception.diagnostic["commands"][0][key], expected)

    def test_command_type_not_allowed_in_the_jobs_phase(self):
        # Catches the parser inheriting the schema's wider enum instead of the job's phase.
        self.assert_rejected("work", flat(type="propose", id="T1", title="t", description="d", acceptance="a",
                                          dependencies=[], write_paths=[], capability="text"),
                             "allowed_operations", ["block", "complete", "request_help"])
        self.assert_rejected("review", flat(type="complete", task_id="T1", result="done"),
                             "allowed_operations", ["block", "review"])
        self.assert_rejected("checkpoint", flat(type="checkpoint_review", verdict="accept", reason="ok", proposal_digest="a" * 64),
                             "allowed_operations", ["checkpoint"])
        self.assert_rejected("plan", flat(type="review", task_id="T1", verdict="accept", reason="ok"),
                             "allowed_operations", ["claim", "plan_ready", "propose"])

    def test_help_phase_rejects_every_command(self):
        # Catches the help phase gaining commands now that its schema is no longer maxItems 0.
        self.assert_rejected("help", flat(type="claim", task_id="T1"), "allowed_operations", [])

    def test_propose_is_rejected_when_can_propose_is_false(self):
        # Catches a non plan author proposing tasks once the schema always lists propose.
        self.assert_rejected("plan", flat(type="propose", id="T1", title="t", description="d", acceptance="a",
                                          dependencies=[], write_paths=[], capability="text"),
                             "allowed_operations", ["claim", "plan_ready"], can_propose=False)

    def test_solo_room_still_cannot_request_help(self):
        # Catches the roster rule being lost alongside the phase rules.
        self.assert_rejected("work", {"type": "request_help", "task_id": "T1", "to": None, "text": "help",
                                      "capability": "text", "repair_paths": None},
                             "allowed_operations", ["block", "complete"], members=("claude",))

    def test_unused_field_must_be_null(self):
        # Catches widened transport fields letting another phase's values through.
        self.assert_rejected("review", flat(type="review", task_id="T1", verdict="accept", reason="ok", result="wrong field"),
                             "unused_fields", ["result"])
        self.assert_rejected("work", flat(type="complete", task_id="T1", result="done", summary="wrong field"),
                             "unused_fields", ["summary"])

    def test_field_outside_the_union_is_rejected(self):
        # Catches the union becoming an open object.
        self.assert_rejected("work", flat(type="complete", task_id="T1", result="done", invented="x"),
                             "unknown_field_count", 1)

    def test_review_phases_require_a_nonempty_reason(self):
        # Catches the schema's minLength rule disappearing with nothing in its place.
        for value in (None, ""):
            with self.subTest(value=value):
                self.assert_rejected("review", flat(type="review", task_id="T1", verdict="accept", reason=value),
                                     "missing_fields", ["reason"])
                self.assert_rejected("review", flat(type="block", task_id="T1", reason=value, cause="technical"),
                                     "missing_fields", ["reason"])
                self.assert_rejected("checkpoint_review", flat(type="checkpoint_review", verdict="accept", reason=value,
                                                               proposal_digest="a" * 64),
                                     "missing_fields", ["reason"])

    def test_checkpoint_requires_a_nonempty_summary(self):
        # Catches a checkpoint decision being recorded with nothing to show the user.
        for value in (None, ""):
            with self.subTest(value=value):
                self.assert_rejected("checkpoint", flat(type="checkpoint", decision="complete", summary=value,
                                                        scope_reference="Goal"),
                                     "missing_fields", ["summary"])

    def test_reason_stays_optional_outside_the_review_phases(self):
        # Catches the review-only rule being applied to every phase's block command at parse time.
        parsed = parse("work", [flat(type="block", task_id="T1", reason=None, cause="technical")])
        self.assertEqual(parsed["commands"], [{"type": "block", "task_id": "T1", "reason": None, "cause": "technical"}])

    def test_valid_commands_pass_in_null_filled_and_old_per_phase_shapes(self):
        # Catches a restart rejecting responses written under the old narrow schema.
        expected = [{"type": "review", "task_id": "T1", "verdict": "accept", "reason": "Checked."}]
        self.assertEqual(parse("review", [flat(type="review", task_id="T1", verdict="accept", reason="Checked.")])["commands"], expected)
        _, fields = command_shape(team_phase="review", native=True)
        old = {**{field: None for field in fields}, "type": "review", "task_id": "T1", "verdict": "accept", "reason": "Checked."}
        self.assertEqual(parse("review", [old])["commands"], expected)

    def test_non_claude_parsing_is_unchanged(self):
        # Catches the widened transport reaching Codex/Kimi, for which a foreign null field stays unknown.
        foreign = flat(type="complete", task_id="T1", result="done")
        with self.assertRaises(ResponseValidationError) as failure:
            _response(body([foreign]), "codex", MEMBERS, project=True, team=True, native=True,
                      native_phase="work", can_propose=False)
        self.assertEqual(failure.exception.code, "invalid_team_commands")
        self.assertGreater(failure.exception.diagnostic["commands"][0]["unknown_field_count"], 0)
        _, fields = command_shape(team_phase="review", native=True)
        command = {**{field: None for field in fields}, "type": "review", "task_id": "T1", "verdict": "accept"}
        parsed = _response(body([command]), "codex", MEMBERS, project=True, team=True, native=True, native_phase="review", can_propose=False)
        self.assertIsNone(parsed["commands"][0]["reason"])


class ReceiptCompatibility(unittest.TestCase):
    CONTEXT = {"members": [{"id": "claude"}, {"id": "codex"}]}

    def matches(self, schema, phase="work", can_propose=False):
        return RoomService._native_receipt_schema_matches(schema, self.CONTEXT, "claude", phase, can_propose=can_propose)

    def test_old_per_phase_and_new_stable_receipts_are_both_accepted(self):
        # Catches a job launched before the upgrade being unrecoverable afterwards, or the reverse.
        for phase in PHASES:
            with self.subTest(phase=phase):
                legacy = response_schema(project=True, native=True, collaboration=True, member_ids=MEMBERS, provider="claude",
                                         team_phase=phase, can_propose=False)
                self.assertIs(self.matches(legacy, phase), True)
                self.assertIs(self.matches(json.loads(claude_bytes(phase, False)), phase), True)

    def test_a_receipt_from_another_phase_or_roster_is_refused(self):
        # Catches the two-schema allowance turning into accept-anything.
        other_phase = response_schema(project=True, native=True, collaboration=True, member_ids=MEMBERS, provider="claude",
                                      team_phase="review", can_propose=False)
        self.assertIs(self.matches(other_phase, "work"), False)
        other_roster = json.loads(claude_bytes("work", False, ("claude", "kimi")))
        self.assertIs(self.matches(other_roster), False)
        tampered = json.loads(claude_bytes("work", False))
        tampered["properties"]["files"]["maxItems"] = 16
        self.assertIs(self.matches(tampered), False)


class ServiceDispatch(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler

    async def run_room(self):
        created = await native.NativeRoomServiceTests.create(self)
        await self.settle()
        return await self.service.read_room(created["room"]["id"])

    async def test_a_real_room_sends_claude_one_schema_and_codex_per_phase_schemas(self):
        # Catches the dispatcher bypassing the stable helper, the actual cause of the cache rewrites.
        final = await self.run_room()
        self.assertEqual(final["objective"]["status"], "complete")
        claude = [call for call in self.fake.calls if call["agent"] == "claude"]
        codex = [call for call in self.fake.calls if call["agent"] == "codex"]
        self.assertGreaterEqual(len({call["context"]["job"]["job_phase"] for call in claude}), 3)
        self.assertEqual(len({adapters._object_text(call["response_schema"]) for call in claude}), 1)
        self.assertGreater(len({adapters._object_text(call["response_schema"]) for call in codex}), 1)

    async def test_claude_accepts_null_filled_union_replies_end_to_end(self):
        # Catches the finish path parsing a Claude reply with the narrow per-phase transport.
        original = self.handler
        def handler(agent, context, cwd):
            reply = original(agent, context, cwd)
            if agent == "claude":
                reply["commands"] = [flat(**{key: value for key, value in command.items() if value is not None})
                                     for command in reply["commands"]]
            return reply
        self.fake.handler = handler
        final = await self.run_room()
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual([event for event in final["events"] if event["kind"] == "native_response_rejected"], [])

    async def test_empty_review_reason_is_rejected_and_corrected_with_the_field_named(self):
        # Catches the new server check being unreachable from a real Claude review job.
        original = self.handler
        attempts = []
        def handler(agent, context, cwd):
            reply = original(agent, context, cwd)
            if context["job"]["job_phase"] == "review":
                attempts.append(agent)
                if len(attempts) == 1:
                    reply["commands"][0]["reason"] = None
            return reply
        self.fake.handler = handler
        # Codex plans and owns the task, so the independent review goes to Claude.
        created = await self.service.create_room("Update the existing value and preserve notes.", "codex", "req-1",
            mode="native_project", project_id=self.registration["project"]["id"], participants=["claude", "codex"],
            work_mode="build", runtime_settings={"permission": "full_access", "members": {}},
            execution_policy={"mode": "autonomous"})
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(attempts, ["claude", "claude"])
        rejected = [event for event in final["events"] if event["kind"] == "native_response_rejected"]
        self.assertEqual([event["evidence"]["code"] for event in rejected], ["invalid_team_commands"])
        self.assertEqual(rejected[0]["evidence"]["evidence"]["response_validation"]["commands"][0]["missing_fields"], ["reason"])
        corrected = [call for call in self.fake.calls if call["context"]["job"]["job_phase"] == "review"][1]
        self.assertIn("commands[0].reason must be present for review", corrected["prompt"])

    async def test_json_files_in_a_non_work_phase_keep_their_error_code(self):
        # Catches the stable branch skipping the host's ban on files in a Claude reply.
        original = self.handler
        rejected_phase = []
        def handler(agent, context, cwd):
            reply = original(agent, context, cwd)
            if agent == "claude" and context["job"]["job_phase"] in {"plan", "coordinate"} and not rejected_phase:
                rejected_phase.append(context["job"]["job_phase"])
                reply["files"] = [{"path": "main.py", "previous_sha256": None, "content": "VALUE = 3\n"}]
            return reply
        self.fake.handler = handler
        final = await self.run_room()
        self.assertEqual(rejected_phase, ["plan"])
        rejected = [event for event in final["events"] if event["kind"] == "native_response_rejected"]
        self.assertEqual([event["evidence"]["code"] for event in rejected], ["native_json_files_forbidden"])

    def schema_sentence(self, provider, phase, author="claude", members=MEMBERS):
        class Spec:
            adapter = provider
        class Registry:
            @staticmethod
            def get(_):
                return Spec
        class Store:
            @staticmethod
            def member_ids(_):
                return members
        job = {"provider": "claude", "room_id": "r", "mode": "native_project", "team": {"job_phase": phase, "plan_author": author}}
        return RoomService._native_schema_instruction(type("S", (), {"registry": Registry, "store": Store,
            "_uses_stable_native_schema": RoomService._uses_stable_native_schema})(), job)

    def test_prompt_names_the_phase_commands_only_for_claude(self):
        # Catches Claude losing the phase contract that used to live in the schema enum.
        self.assertIn("The only command types allowed now are complete, block, request_help.", self.schema_sentence("claude", "work"))
        self.assertIn("This job_phase is work.", self.schema_sentence("claude", "work"))
        self.assertIn("are propose, claim, plan_ready.", self.schema_sentence("claude", "plan"))
        self.assertIn("are claim, plan_ready.", self.schema_sentence("claude", "plan", author="codex"))
        self.assertIn("No commands are allowed now, so commands must be [].", self.schema_sentence("claude", "help"))
        self.assertIn("reason must be a non-empty string.", self.schema_sentence("claude", "review"))
        self.assertIn("summary must be a non-empty string.", self.schema_sentence("claude", "checkpoint"))
        self.assertEqual(self.schema_sentence("codex", "work"),
                         "Follow only this phase's supplied JSON schema; do not add fields from other phases. "
                         "Null unused fields within that shape. ")


if __name__ == "__main__":
    unittest.main()
