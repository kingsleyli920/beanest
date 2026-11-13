"""Team wire/semantic boundaries; no provider, desktop or source project runs."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_response_schema import response_schema
from local_agent_coordinator.room_server import publish_connection
from local_agent_coordinator.room_contract import MAX_PARTNER_TEXT, MAX_RESPONSE_COMMANDS
from local_agent_coordinator.room_service import MAX_RESPONSE_BYTES, RoomError, RoomService, _response
from local_agent_coordinator.room_store import RoomStore
from local_agent_coordinator.team_store import _COMMAND_FIELDS
from tests import test_room_server as http_fixtures


HAS_MCP = importlib.util.find_spec("mcp") is not None
HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
if HAS_MCP:
    from local_agent_coordinator.room_mcp import RoomClientError, RoomHTTPClient, create_server
if HAS_JSONSCHEMA:
    from jsonschema import Draft202012Validator


MEMBERS = ("claude", "codex")
IDENTITY = "11111111-1111-4111-8111-111111111111"


def proposal():
    return {"type": "propose", "id": "main", "title": "Main deliverable",
            "description": "Implement the requested part.", "acceptance": "A peer checks the concrete result.",
            "dependencies": [], "write_paths": ["main.py"], "capability": "python"}


def response(commands=None, *, project=False, **overrides):
    value = {"message": "Concrete response.", "state": "done", "commands": commands or []}
    if project:
        value.update(base_revision=0, files=[], edits=None)
    return {**value, **overrides}


def flat_command(command):
    fields = set().union(*_COMMAND_FIELDS.values())
    return {**dict.fromkeys(fields), **command}


def native_response(commands=None, *, project=False, **overrides):
    return response([flat_command(command) for command in commands or []], project=project, **overrides)


@unittest.skipUnless(HAS_JSONSCHEMA, "JSON Schema validator is installed with the MCP extra")
class TeamPhaseSchemaTests(unittest.TestCase):
    def validator(self, phase, *, project=False, can_propose=True, members=MEMBERS):
        schema = response_schema(project=project, member_ids=members, provider=members[0],
                                 collaboration=True, team_phase=phase, can_propose=can_propose)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_phase_restrictions_reject_commands_from_other_stages(self):
        commands = {
            "propose": proposal(),
            "claim": {"type": "claim", "task_id": "main"},
            "plan_ready": {"type": "plan_ready"},
            "complete": {"type": "complete", "task_id": "main", "result": "Concrete deliverable."},
            "request_help": {"type": "request_help", "task_id": "main", "to": "codex", "capability": "text", "text": "Check this edge."},
            "review": {"type": "review", "task_id": "main", "verdict": "accept", "reason": "Checked the exact result."},
            "block": {"type": "block", "task_id": "main", "reason": "Missing required information."},
        }
        allowed = {
            "plan": {"propose", "claim", "plan_ready"},
            "coordinate": {"propose", "claim", "plan_ready"},
            "work": {"complete", "request_help", "block"},
            "review": {"review", "block"},
            "help": set(),
            # The plan author's reply to the user may change unstarted work mid-run; the store checks the rest.
            "user": {"claim", "propose"},
        }
        for phase, kinds in allowed.items():
            validator = self.validator(phase)
            self.assertTrue(validator.is_valid(response()))
            for kind, command in commands.items():
                with self.subTest(phase=phase, command=kind):
                    self.assertEqual(validator.is_valid(native_response([command])), kind in kinds)

    def test_non_author_cannot_propose_but_can_claim_and_confirm_the_plan(self):
        for phase in ("plan", "coordinate"):
            with self.subTest(phase=phase):
                validator = self.validator(phase, can_propose=False)
                self.assertFalse(validator.is_valid(native_response([proposal()])))
                self.assertTrue(validator.is_valid(native_response([{"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])))
        # Catches a non-author's user-phase schema leaving out the mid-run change the user may ask any partner for.
        user = self.validator("user", can_propose=False)
        self.assertTrue(user.is_valid(native_response([proposal()])))
        self.assertTrue(user.is_valid(native_response([{"type": "adjust", "task_id": "main", "dependencies": [], "write_paths": []}])))
        self.assertTrue(user.is_valid(native_response([{"type": "claim", "task_id": "main"}])))
        self.assertTrue(self.validator("user").is_valid(native_response(
            [{"type": "adjust", "task_id": "main", "dependencies": [], "write_paths": []}])))
        # Omitting phase still admits every existing team command type.
        broad = self.validator(None)
        self.assertTrue(broad.is_valid(native_response([proposal(), {"type": "complete", "task_id": "main", "result": "Result."}])))

    def test_project_non_work_phases_cannot_return_file_payloads(self):
        files = [{"path": "main.py", "previous_sha256": None, "content": "value = 1\n"}]
        for phase in ("plan", "coordinate", "review", "help", "user"):
            with self.subTest(phase=phase):
                validator = self.validator(phase, project=True)
                self.assertTrue(validator.is_valid(response(project=True)))
                self.assertFalse(validator.is_valid(response(project=True, files=files)))
        for phase in (None, "work"):
            self.assertTrue(self.validator(phase, project=True).is_valid(response(project=True, files=files)))

    def test_team_visible_message_is_nonempty_and_bounded_without_changing_legacy_shape(self):
        validator = self.validator("coordinate", can_propose=False)
        for message, valid in (("", False), ("字", True), ("字" * MAX_PARTNER_TEXT, True), ("字" * (MAX_PARTNER_TEXT + 1), False)):
            with self.subTest(length=len(message)):
                self.assertEqual(validator.is_valid(response(message=message)), valid)
        legacy = response_schema(project=True, member_ids=MEMBERS, provider="claude")
        ignored_team_options = response_schema(project=True, member_ids=MEMBERS, provider="claude",
                                               team_phase="help", can_propose=False)
        self.assertEqual(legacy, ignored_team_options)
        self.assertEqual(legacy["properties"]["message"], {"type": "string"})
        self.assertEqual(legacy["properties"]["files"]["maxItems"], 16)

    def test_phase_and_permission_contexts_are_strict_and_help_has_no_empty_anyof(self):
        valid = {"project": False, "member_ids": MEMBERS, "provider": "claude", "collaboration": True}
        invalid = [{"team_phase": value} for value in (0, True, "planning", "team_work", "", {}, [], ())]
        invalid += [{"can_propose": value} for value in (None, 0, 1, "true", [], {})]
        for options in invalid:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "^invalid_response_schema_context$"):
                response_schema(**valid, **options)
        for members in (MEMBERS, ("claude",)):
            validator = self.validator("help", project=True, members=members)
            self.assertTrue(validator.is_valid(response(project=True)))
            self.assertFalse(validator.is_valid(response([{}], project=True)))
            self.assertNotIn("anyOf", validator.schema["properties"]["commands"]["items"])
            self.assertEqual(validator.schema["properties"]["commands"]["maxItems"], 0)

    def test_flat_item_has_all_canonical_fields_required_and_no_object_union(self):
        item = self.validator("coordinate").schema["properties"]["commands"]["items"]
        fields = set().union(*_COMMAND_FIELDS.values())
        self.assertEqual(set(item["properties"]), fields)
        self.assertEqual(set(item["required"]), fields)
        self.assertIs(item["additionalProperties"], False)
        self.assertNotIn("anyOf", item)
        self.assertNotIn("oneOf", item)
        for field in fields - {"type"}:
            self.assertIn("null", item["properties"][field]["type"])
        validator = self.validator("coordinate", can_propose=False)
        self.assertTrue(validator.is_valid(native_response([{"type": "claim", "task_id": "main"}])))
        self.assertFalse(validator.is_valid(response([{"type": "claim", "task_id": "main"}])))


class TeamProtocolParsingTests(unittest.TestCase):
    def assertCode(self, code, operation):
        with self.assertRaises(RoomError) as error:
            operation()
        self.assertEqual(error.exception.code, code)

    @unittest.skipUnless(HAS_JSONSCHEMA, "JSON Schema validator is installed with the MCP extra")
    def test_native_schema_requires_flat_fields_and_limits_requests_to_peers(self):
        schema = response_schema(project=False, member_ids=MEMBERS, provider="claude", collaboration=True)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        valid = native_response([{"type": "request_help", "task_id": "main", "to": "codex", "capability": "text", "text": "Check an edge case."}])
        self.assertTrue(validator.is_valid(valid))
        missing_field = {key: value for key, value in valid["commands"][0].items() if key != "capability"}
        for invalid in [
            {**valid, "actions": []},
            response([{**valid["commands"][0], "to": "claude"}]),
            native_response([{"type": "claim", "task_id": "main", "owner": "codex"}]),
            response([missing_field]),
            native_response([{"type": "invent_tool", "text": "run something"}]),
        ]:
            with self.subTest(invalid=invalid):
                self.assertFalse(validator.is_valid(invalid))
        self.assertFalse(validator.is_valid(response(project=True)))
        project_schema = response_schema(project=True, member_ids=MEMBERS, provider="claude", collaboration=True)
        self.assertTrue(Draft202012Validator(project_schema).is_valid(response(project=True)))

    def test_team_and_legacy_envelopes_do_not_silently_convert_each_other(self):
        team = response([{"type": "plan_ready"}])
        legacy = {"message": "Review the draft.", "state": "working", "actions": [
            {"type": "request", "to": "codex", "kind": "review", "text": "Check the draft."}]}
        self.assertEqual(_response(json.dumps(team), "claude", MEMBERS, team=True)["commands"], team["commands"])
        self.assertEqual(_response(json.dumps(legacy), "claude", MEMBERS)["action"]["to"], "codex")
        self.assertCode("invalid_agent_response", lambda: _response(json.dumps(team), "claude", MEMBERS))
        self.assertCode("invalid_agent_response", lambda: _response(json.dumps(legacy), "claude", MEMBERS, team=True))

    def test_custom_adapters_still_reach_transactional_command_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RoomStore(Path(directory) / "rooms.sqlite3")
            try:
                room_id, _ = store.create_room("Small goal", "claude", "custom-adapter", collaboration=True)
                job = store.claim(room_id, "claude")
                # A custom adapter does not have to use the native schema. Its
                # parsed command must nevertheless pass the same storage gate.
                parsed = _response(json.dumps(response([{"type": "claim", "task_id": "unproposed"}])),
                                   "claude", MEMBERS, team=True)
                self.assertCode("team_task_not_found", lambda: store.prevalidate_team_result(job["id"], parsed["commands"]))
                self.assertEqual(store.snapshot(room_id)["team"]["tasks"], [])
                self.assertEqual(store.snapshot(room_id)["budget"]["used"], 1)
            finally:
                store.close()

    def test_flat_native_commands_and_custom_canonical_commands_reach_the_same_store_gate(self):
        commands = [{**proposal(), "capability": "text", "write_paths": []},
                    {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}]
        canonical = _response(json.dumps(response(commands)), "claude", MEMBERS, team=True)
        native = _response(json.dumps(native_response(commands)), "claude", MEMBERS, team=True)
        self.assertEqual(native, canonical)
        self.assertEqual(native["commands"], commands)
        with tempfile.TemporaryDirectory() as directory:
            store = RoomStore(Path(directory) / "rooms.sqlite3")
            try:
                room_id, _ = store.create_room("Shared text goal", "claude", "flat-command", collaboration=True)
                job = store.claim(room_id, "claude")
                self.assertTrue(store.prevalidate_team_result(job["id"], native["commands"]))
            finally:
                store.close()

    def test_flat_unused_fields_must_be_null_and_unknown_or_partial_fields_are_rejected(self):
        claim = flat_command({"type": "claim", "task_id": "main"})
        invalid = [
            {**claim, "reason": "This unused value must not be silently discarded."},
            {**claim, "to": "codex"},
            {**claim, "unknown_field": None},
            {key: value for key, value in claim.items() if key != "reason"},
            {"type": "claim", "task_id": "main", "id": None},
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = RoomStore(Path(directory) / "rooms.sqlite3")
            try:
                room_id, _ = store.create_room("A shared goal", "claude", "malformed-flat", collaboration=True)
                job = store.claim(room_id, "claude")

                def decode_and_validate(command):
                    parsed = _response(json.dumps(response([command])), "claude", MEMBERS, team=True)
                    return store.prevalidate_team_result(job["id"], parsed["commands"])

                for command in invalid:
                    with self.subTest(command=command):
                        self.assertCode("invalid_team_commands", lambda: decode_and_validate(command))
                self.assertEqual(store.snapshot(room_id)["team"]["tasks"], [])
            finally:
                store.close()

    def test_flat_text_values_are_redacted_before_becoming_canonical_visible_data(self):
        secret = "fixture-token-abcdefghijklmnop"
        command = {"type": "complete", "task_id": "main", "result": "Bearer " + secret}
        parsed = _response(json.dumps(native_response([command])), "claude", MEMBERS, team=True)
        self.assertEqual(set(parsed["commands"][0]), {"type", "task_id", "result"})
        self.assertNotIn(secret, json.dumps(parsed))
        self.assertIn("[redacted]", parsed["commands"][0]["result"])

    def test_nullable_transport_does_not_authorize_missing_required_command_values(self):
        value = native_response([{"type": "claim", "task_id": None}])
        parsed = _response(json.dumps(value), "claude", MEMBERS, team=True)
        self.assertEqual(parsed["commands"], [{"type": "claim", "task_id": None}])
        with tempfile.TemporaryDirectory() as directory:
            store = RoomStore(Path(directory) / "rooms.sqlite3")
            try:
                room_id, _ = store.create_room("A shared goal", "claude", "null-task", collaboration=True)
                job = store.claim(room_id, "claude")
                self.assertCode("invalid_team_task", lambda: store.prevalidate_team_result(job["id"], parsed["commands"]))
                self.assertEqual(store.snapshot(room_id)["team"]["tasks"], [])
            finally:
                store.close()

    def test_plain_preamble_is_allowed_but_duplicate_values_and_unbounded_commands_fail_closed(self):
        raw = json.dumps(response())
        self.assertEqual(_response("```json\n" + raw + "\n```", "claude", MEMBERS, team=True)["commands"], [])
        self.assertEqual(_response("Before\n" + raw, "claude", MEMBERS, team=True)["commands"], [])
        for invalid in [
            raw + raw,
            '{"message":"first","message":"second","state":"done","commands":[]}',
            json.dumps(response([{"type": "plan_ready"}] * (MAX_RESPONSE_COMMANDS + 1))),
            json.dumps(response(state="accepted")),
        ]:
            with self.subTest(invalid=invalid[:100]):
                self.assertCode("invalid_agent_response", lambda: _response(invalid, "claude", MEMBERS, team=True))

    def test_text_and_project_limits_use_utf8_bytes_and_keep_project_fields_explicit(self):
        value = response([{"type": "complete", "task_id": "main", "result": "界" * 1_500_000}], message="字" * 8000)
        raw = json.dumps(value, ensure_ascii=False)
        self.assertLess(len(raw), MAX_RESPONSE_BYTES)
        self.assertGreater(len(raw.encode("utf-8")), MAX_RESPONSE_BYTES)
        self.assertCode("invalid_agent_response", lambda: _response(raw, "claude", MEMBERS, team=True))
        project = {**response([{"type": "complete", "task_id": "main", "result": "界" * 4000}], message="字" * 8000), "base_revision": 0, "files": []}
        self.assertEqual(_response(json.dumps(project, ensure_ascii=False), "claude", MEMBERS, team=True, project=True)["files"], [])
        self.assertCode("invalid_agent_response", lambda: _response(json.dumps(response()), "claude", MEMBERS, team=True, project=True))
        self.assertCode("invalid_project_files", lambda: _response(json.dumps(response(project=True, base_revision=True)), "claude", MEMBERS, team=True, project=True))
        huge = response(project=True, files=[{"path": "main.py", "previous_sha256": None, "content": "界" * 1_500_000}])
        self.assertCode("invalid_agent_response", lambda: _response(json.dumps(huge, ensure_ascii=False), "claude", MEMBERS, team=True, project=True))

    def test_visible_text_and_nested_command_data_are_redacted_without_rewriting_code(self):
        secret = "fixture-token-abcdefghijklmnop"
        command = {**proposal(), "description": "Authorization: Bearer " + secret,
                   "acceptance": "API_KEY='" + secret + "'"}
        code = "API_KEY='" + secret + "'\n"
        parsed = _response(json.dumps(response([command], project=True, message="Bearer " + secret,
                                               files=[{"path": "main.py", "previous_sha256": None, "content": code}])),
                           "claude", MEMBERS, team=True, project=True)
        self.assertNotIn(secret, parsed["message"] + json.dumps(parsed["commands"]))
        self.assertEqual(parsed["files"][0]["content"], code)
        # Code must be rejected by the artifact boundary, never silently edited
        # by a presentation redactor; that boundary is exercised below.


class TeamProtocolHTTPTests(unittest.TestCase):
    setUp = http_fixtures.RoomHTTPTests.setUp
    tearDown = http_fixtures.RoomHTTPTests.tearDown
    request = http_fixtures.RoomHTTPTests.request

    def test_boolean_flag_crosses_web_and_bound_mcp_routes_without_changing_source(self):
        for path, source in (("/api/rooms", None), ("/api/mcp/rooms", "mcp")):
            for value in (True, False):
                with self.subTest(path=path, value=value):
                    status, _, _ = self.request("POST", path, {"goal": "Shared task", "target": "all",
                        "request_id": str(uuid4()), "collaboration": value},
                        headers={"X-LAC-Instance": self.server.instance_id})
                    self.assertEqual(status, 200)
                    extra = self.service.calls[-1][-1]
                    self.assertIs(extra["collaboration"], value)
                    self.assertEqual(extra.get("source"), source)

    def test_non_boolean_flags_fail_before_service_dispatch(self):
        for path in ("/api/rooms", "/api/mcp/rooms"):
            for value in (0, 1, "true", "false", None, [], {}):
                with self.subTest(path=path, value=value):
                    status, _, body = self.request("POST", path, {"goal": "No dispatch", "target": "all",
                        "request_id": str(uuid4()), "collaboration": value},
                        headers={"X-LAC-Instance": self.server.instance_id})
                    self.assertEqual((status, json.loads(body)), (400, {"error": "invalid_arguments"}))
        self.assertEqual(self.service.calls, [])

    @unittest.skipUnless(HAS_MCP, "Install the MCP extra to exercise the connected client")
    def test_mcp_http_client_preserves_opt_in_and_rejects_invalid_values_before_network(self):
        connection = publish_connection(self.server, self.assets)
        client = RoomHTTPClient(connection)
        asyncio.run(client.start("Shared task", "all", "team", collaboration=True))
        self.assertEqual(self.service.calls[-1][-1], {"source": "mcp", "collaboration": True})
        asyncio.run(client.start("Legacy task", "all", "legacy", collaboration=False))
        self.assertEqual(self.service.calls[-1][-1], {"source": "mcp"})
        before = len(self.service.calls)
        for value in (0, 1, "true", "false", None, [], {}):
            with self.subTest(value=value), self.assertRaises(RoomClientError) as error:
                asyncio.run(client.start("Reject locally", "all", "invalid", collaboration=value))
            self.assertEqual(error.exception.code, "invalid_arguments")
        self.assertEqual(len(self.service.calls), before)


@unittest.skipUnless(HAS_MCP, "Install the MCP extra to exercise native tool argument validation")
class TeamProtocolMCPToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tool_does_not_coerce_strings_or_integers_to_boolean(self):
        class Sink:
            def __init__(self):
                self.values = []

            async def start(self, *args, **kwargs):
                self.values.append(kwargs["collaboration"])
                return {"ok": True}

        sink = Sink()
        server = create_server(sink)
        tool = next(tool for tool in await server.list_tools() if tool.name == "room_start")
        self.assertEqual(tool.input_schema["properties"]["collaboration"]["type"], "boolean")
        arguments = {"goal": "A bounded task", "target": "all", "request_id": "tool-input"}
        for value in (True, False):
            await server.call_tool("room_start", {**arguments, "collaboration": value})
        self.assertEqual(sink.values, [True, False])
        from mcp.server.mcpserver.exceptions import ToolError
        for value in (0, 1, "true", "false", None, [], {}):
            with self.subTest(value=value), self.assertRaises(ToolError):
                await server.call_tool("room_start", {**arguments, "collaboration": value})
        self.assertEqual(sink.values, [True, False])


class TeamProtocolArtifactBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name).resolve()

        async def forbidden_invoke(*args, **kwargs):
            self.fail("This protocol fixture must never invoke an agent")

        self.service = RoomService(root / "rooms.sqlite3", root / "workspaces", invoke=forbidden_invoke)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.directory.cleanup()

    def project_room(self):
        room_id, _ = self.service.store.create_room("A new Python file", "claude", str(uuid4()),
                                                   collaboration=True, mode="python_artifacts")
        self.service._project(room_id, create=True)
        return room_id

    def work(self, room_id):
        store = self.service.store
        author, peer = store.claim(room_id, "claude"), store.claim(room_id, "codex")
        store.finish_job(author["id"], team_commands=[proposal(), {"type": "claim", "task_id": "main"}, {"type": "plan_ready"}])
        store.finish_job(peer["id"], team_commands=[])
        confirmation = store.claim(room_id, "codex")
        store.finish_job(confirmation["id"], team_commands=[{"type": "plan_ready"}])
        job = store.claim(room_id, "claude")
        self.service._prompt(job)
        return job

    def deliver(self, job, value):
        self.service._finish(job, {"status": "completed", "session_id": IDENTITY,
                                  "events": [], "usage": None, "text": json.dumps(value)})

    def test_plan_file_payload_is_rejected_before_artifact_mutation(self):
        room_id = self.project_room()
        job = self.service.store.claim(room_id, "claude")
        self.service._prompt(job)
        self.deliver(job, response(project=True, files=[{"path": "main.py", "previous_sha256": None, "content": "value = 1\n"}]))
        snapshot = self.service.store.snapshot(room_id)
        self.assertEqual(snapshot["jobs"][0]["error"], "team_files_forbidden")
        self.assertEqual(self.service._project(room_id).snapshot()["revision"], 0)

    def test_sensitive_work_files_are_rejected_and_never_enter_chat_or_storage(self):
        room_id = self.project_room()
        job = self.work(room_id)
        secret = "fixture-secret-abcdefghijklmnop"
        self.deliver(job, response([{"type": "complete", "task_id": "main", "result": "Candidate file."}],
            project=True, files=[{"path": "main.py", "previous_sha256": None, "content": "API_KEY='" + secret + "'\n"}]))
        snapshot = self.service.store.snapshot(room_id)
        self.assertEqual(next(item for item in snapshot["jobs"] if item["id"] == job["id"])["error"], "artifact_sensitive_content")
        self.assertEqual(self.service._project(room_id).snapshot()["revision"], 0)
        self.assertNotIn(secret, json.dumps(snapshot))

    def test_text_room_cannot_silently_advertise_python_delivery(self):
        room_id, _ = self.service.store.create_room("Text only", "claude", "text-capability", collaboration=True)
        job = self.service.store.claim(room_id, "claude")
        self.service._prompt(job)
        self.deliver(job, response([proposal()]))
        snapshot = self.service.store.snapshot(room_id)
        self.assertEqual(snapshot["jobs"][0]["error"], "team_capability_unavailable")
        self.assertEqual(snapshot["team"]["tasks"], [])

    def test_work_file_payload_cannot_expand_its_declared_task_scope(self):
        room_id = self.project_room()
        job = self.work(room_id)
        self.deliver(job, response([{"type": "complete", "task_id": "main", "result": "Candidate."}],
            project=True, files=[{"path": "other.py", "previous_sha256": None, "content": "value = 1\n"}]))
        snapshot = self.service.store.snapshot(room_id)
        self.assertEqual(next(item for item in snapshot["jobs"] if item["id"] == job["id"])["error"], "team_scope_conflict")
        self.assertEqual(self.service._project(room_id).snapshot()["revision"], 0)


if __name__ == "__main__":
    unittest.main()
