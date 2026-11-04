"""Closed command diagnostics without retaining model values or loosening authority."""
import json
import unittest

from local_agent_coordinator.room_service import _response, ResponseValidationError
from local_agent_coordinator.room_response_schema import command_shape, response_schema
from local_agent_coordinator.team_store import _COMMAND_FIELDS
from tests import test_native_room_service as native


def parse(commands, phase="work"):
    value = {"message":"Current task result.", "state":"done", "commands":commands,
             "base_revision":1, "files":[], "edits":[], "unchanged_paths":[], "resolved_conflicts":[]}
    return _response(json.dumps(value), "claude", ("claude", "codex"), project=True, team=True,
                     native=True, native_phase=phase, can_propose=True)


class CommandValidationTests(unittest.TestCase):
    def test_canonical_and_flat_share_every_allowed_phase_command_contract(self):
        values = {"type":"complete", "id":"T1", "task_id":"T1", "title":"A task", "description":"Work",
                  "acceptance":"Checked", "dependencies":[], "write_paths":[], "capability":"text", "result":"Result",
                  "reason":"Observed", "cause":"technical", "to":"codex", "text":"Question", "repair_paths":None,
                  "verdict":"accept", "decision":"complete", "summary":"Summary", "next_goal":None,
                  "scope_reference":"Original goal", "question":None, "proposal_digest":"a" * 64}
        for phase in ("plan", "coordinate", "work", "review", "help", "user", "checkpoint", "checkpoint_review"):
            kinds, fields = command_shape(team_phase=phase, native=True)
            for kind in kinds:
                with self.subTest(phase=phase, kind=kind):
                    canonical = {field: values[field] for field in _COMMAND_FIELDS[kind]}
                    canonical["type"] = kind
                    flat = {field: canonical.get(field) for field in fields}
                    self.assertEqual(parse([canonical], phase)["commands"], parse([flat], phase)["commands"])

    def test_nonnull_unused_fields_stay_rejected_with_value_free_specific_diagnostics(self):
        _, fields = command_shape(team_phase="work", native=True)
        diagnostic = None
        for value in ("text", "python", "DO NOT EXPOSE THIS VALUE"):
            flat = {field: None for field in fields}
            flat.update(type="complete", task_id="T1", result="Concrete result", capability=value)
            with self.assertRaises(ResponseValidationError) as failure:
                parse([flat])
            current = failure.exception.diagnostic
            self.assertEqual(current["commands"][0]["unused_fields"], ["capability"])
            self.assertNotIn(value, json.dumps(current))
            if diagnostic is not None:
                self.assertEqual(current, diagnostic)
            diagnostic = current
        flat.update(capability=None, to="codex")
        with self.assertRaises(ResponseValidationError) as failure:
            parse([flat])
        self.assertNotEqual(failure.exception.diagnostic, diagnostic)
        self.assertEqual(failure.exception.diagnostic["commands"][0]["unused_fields"], ["to"])

    def test_missing_unknown_and_cross_phase_fields_remain_rejected(self):
        cases = [([{"type":"complete", "result":"No task reference"}], "work", "missing_fields", ["task_id"]),
                 ([{"type":"complete", "task_id":"T1", "result":"Done", "secret-field":"secret-value"}],
                  "work", "unknown_field_count", 1),
                 ([{"type":"complete", "task_id":"T1", "result":"Wrong phase"}], "review", "operation", "unsupported")]
        for commands, phase, key, expected in cases:
            with self.subTest(key=key):
                with self.assertRaises(ResponseValidationError) as failure:
                    parse(commands, phase)
                issue = failure.exception.diagnostic["commands"][0]
                self.assertEqual(issue[key], expected)
                self.assertNotIn("secret", json.dumps(issue))

    def test_native_schema_names_the_only_capability_users(self):
        schema = response_schema(project=True, native=True, collaboration=True, team_phase="work",
                                 member_ids=("claude", "codex"), provider="claude")
        description = schema["properties"]["commands"]["items"]["properties"]["capability"]["description"]
        self.assertIn("Used only by request_help", description)
        self.assertIn("every other command type this field must be null", description)

    def test_receipt_schema_annotation_changes_do_not_mask_description_property_constraints(self):
        import copy
        from local_agent_coordinator.room_service import RoomService
        context = {"members":[{"id":"claude"}, {"id":"codex"}]}
        original = response_schema(project=True, native=True, collaboration=True, team_phase="plan",
                                   member_ids=("claude", "codex"), provider="claude")
        annotation = copy.deepcopy(original)
        annotation["description"] = "New explanation only."
        self.assertTrue(RoomService._native_receipt_schema_matches(annotation, context, "claude", "plan", can_propose=True))
        changed = copy.deepcopy(annotation)
        changed["properties"]["commands"]["items"]["properties"]["description"]["type"] = "number"
        self.assertFalse(RoomService._native_receipt_schema_matches(changed, context, "claude", "plan", can_propose=True))
        changed["properties"]["commands"]["items"]["properties"]["description"] = "Not a schema object"
        self.assertFalse(RoomService._native_receipt_schema_matches(changed, context, "claude", "plan", can_propose=True))


class CommandCorrectionCoreTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler

    async def test_partial_field_correction_is_progress_and_retains_same_session_full_access(self):
        original = self.handler
        work_calls = []
        def handler(agent, context, cwd):
            if context["job"]["job_phase"] != "work":
                return original(agent, context, cwd)
            work_calls.append(context)
            if len(work_calls) == 1:
                (cwd / "main.py").write_text("VALUE = 2\n")
            else:
                self.assertEqual((cwd / "main.py").read_text(), "VALUE = 2\n")
                field = "capability" if len(work_calls) == 2 else "to"
                self.assertIn("commands[0]." + field + " must be null", self.fake.calls[-1]["prompt"])
            value = native.native_reply(context, [{"type":"complete", "task_id":"T1", "result":"Current files checked."}],
                                        unchanged=[{"path":"notes.md", "reason":"Existing notes apply."}])
            if len(work_calls) == 1:
                value["commands"][0]["capability"] = "text"
            elif len(work_calls) == 2:
                value["commands"][0]["to"] = "codex"
            return value
        self.fake.handler = handler
        created = await native.NativeRoomServiceTests.create(self)
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(len(work_calls), 3)
        rejected = [event for event in final["events"] if event["kind"] == "native_response_rejected"]
        self.assertEqual([event["repeated"] for event in rejected], [False, False])
        self.assertEqual([event["evidence"]["evidence"]["response_validation"]["commands"][0]["unused_fields"]
                          for event in rejected], [["capability"], ["to"]])
        calls = [call for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work"]
        self.assertTrue(all(call["permission"] == "full_access" for call in calls))
        self.assertEqual(len({call["session_id"] for call in calls}), 1)
        self.assertFalse(any(member["identity_uncertain"] for member in final["members"]))

    async def semantic_corrections(self, *, repeat_shape):
        original = self.handler
        work_calls = []
        def handler(agent, context, cwd):
            if context["job"]["job_phase"] != "work":
                return original(agent, context, cwd)
            work_calls.append(context)
            if len(work_calls) == 1:
                (cwd / "main.py").write_text("VALUE = 2\n")
            if len(work_calls) == 1 or repeat_shape:
                command = {"type":"request_help", "task_id":"T1", "to":"codex", "capability":None,
                           "text":"First wording." if len(work_calls) == 1 else "Different wording.", "repair_paths":None}
            elif len(work_calls) == 2:
                command = {"type":"block", "task_id":"T1", "reason":"A typed but invalid cause.", "cause":[]}
            else:
                command = {"type":"complete", "task_id":"T1", "result":"Exact current files checked."}
            return native.native_reply(context, [command], unchanged=[{"path":"notes.md", "reason":"Existing notes apply."}])
        self.fake.handler = handler
        created = await native.NativeRoomServiceTests.create(self)
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        rejected = [event for event in final["events"] if event["kind"] == "native_response_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(event["evidence"]["code"] == "invalid_team_commands" for event in rejected))
        self.assertTrue(all("command_shapes" in event["evidence"]["evidence"]["response_validation"] for event in rejected))
        self.assertEqual([event["repeated"] for event in rejected], [False, repeat_shape])
        self.assertFalse(any(member["identity_uncertain"] for member in final["members"]))
        self.assertTrue(all(call["permission"] == "full_access" for call in self.fake.calls))
        if repeat_shape:
            self.assertEqual(len(work_calls), 2)
            self.assertEqual(final["team"]["tasks"][0]["status"], "blocked")
            self.assertEqual(self.service._project(final["room"]["id"]).files_for_paths(["main.py"])[0]["content"], "VALUE = 1\n")
        else:
            self.assertEqual(len(work_calls), 3)
            self.assertEqual(final["objective"]["status"], "complete")
            types = [event["evidence"]["evidence"]["response_validation"]["command_shapes"][0]["field_types"] for event in rejected]
            self.assertEqual(types[0]["capability"], "null")
            self.assertEqual(types[1]["cause"], {"type":"array", "item_types":[]})
        return final

    async def test_semantic_field_type_change_allows_truthful_correction(self):
        await self.semantic_corrections(repeat_shape=False)

    async def test_semantic_same_shape_wording_change_stops_without_replaying_commands(self):
        final = await self.semantic_corrections(repeat_shape=True)
        # A forged or stale binding cannot fall back to a new plan.
        with self.service.store._connection(write=True) as db:
            team = self.service.store._team
            self.assertIsNotNone(team._native_response_failure_source(db, final["room"]["id"], final["current_run"]["id"]))
            db.execute("UPDATE shared_tasks SET assignment_epoch=assignment_epoch+1 WHERE room_id=? AND id='T1'",
                       (final["room"]["id"],))
            self.assertIsNone(team._native_response_failure_source(db, final["room"]["id"], final["current_run"]["id"]))
        await self.denied_continuation(final, "native_recovery_binding_changed")

    async def denied_continuation(self, final, code):
        from local_agent_coordinator.room_store import RoomError
        binding = {"run_id":final["current_run"]["id"], "revision":final["current_run"]["revision"], "digest":final["current_run"]["digest"]}
        before = self.service.store.snapshot(final["room"]["id"])
        with self.assertRaises(RoomError) as denied:
            await self.service.objective_action(final["room"]["id"], "answer", "unsafe-continue", **binding,
                objective_id=final["objective"]["id"], text="Continue", _message_target="claude")
        self.assertEqual(denied.exception.code, code)
        after = self.service.store.snapshot(final["room"]["id"])
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(after["current_run"]["id"], before["current_run"]["id"])

    async def test_unrelated_unknown_execution_cannot_use_local_response_continuation(self):
        final = await self.semantic_corrections(repeat_shape=True)
        room_id, run_id = final["room"]["id"], final["current_run"]["id"]
        with self.service.store._connection(write=True) as db:
            self.assertIsNotNone(self.service.store._team._native_response_failure_source(db, room_id, run_id))
            job = db.execute("SELECT id FROM jobs WHERE room_id=? AND provider='codex' AND status='completed' ORDER BY rowid LIMIT 1", (room_id,)).fetchone()
            self.assertIsNotNone(job)
            db.execute("UPDATE jobs SET status='unknown',error='native_protocol_invalid' WHERE id=?", (job[0],))
            self.assertTrue(self.service.store._native_continuation_requires_recovery(db, room_id))
            self.assertIsNone(self.service.store._team._native_response_failure_source(db, room_id, run_id))
        await self.denied_continuation(final, "native_recovery_required")
