"""Native schema shape and roster validation, without a CLI or validator dependency."""
import json
import unittest

from local_agent_coordinator.room_response_schema import response_schema
from local_agent_coordinator.room_service import _response, RoomError
from local_agent_coordinator.team_store import _COMMAND_FIELDS
from local_agent_coordinator.room_response_schema import command_shape


class RoomResponseSchemaTests(unittest.TestCase):
    def test_repair_paths_are_nullable_structured_help_input_not_cross_phase_authority(self):
        for phase in ("plan", "coordinate", "work", "review", "help", "user", "checkpoint", "checkpoint_review"):
            with self.subTest(phase=phase):
                schema = response_schema(project=True, native=True, collaboration=True, team_phase=phase,
                    member_ids=("claude", "codex"), provider="claude")
                item = schema["properties"]["commands"]["items"]
                self.assert_native_shape(schema)
                if phase == "work":
                    self.assertIn("repair_paths", item["required"])
                    repair = item["properties"]["repair_paths"]
                    self.assertEqual(repair["type"], ["array", "null"])
                    self.assertEqual(repair["items"], {"type": "string"})
                    self.assertIn("canonical ownership", repair["description"])
                    self.assertIn("For every other command, use null", repair["description"])
                else:
                    self.assertNotIn("repair_paths", item["properties"])
        solo = response_schema(project=True, native=True, collaboration=True, team_phase="work",
            member_ids=("claude",), provider="claude")
        self.assertNotIn("repair_paths", solo["properties"]["commands"]["items"]["properties"])

    def test_native_work_parser_preserves_repair_paths_and_rejects_unused_repair_authority(self):
        base = {"message": "Please repair these project files within the current goal.", "state": "working",
                "base_revision": 2, "files": [], "edits": [], "unchanged_paths": [], "resolved_conflicts": []}
        _, fields = command_shape(team_phase="work", native=True)
        command = {field: None for field in fields}
        command.update(type="request_help", task_id="T1", to="codex", capability="text", text="Check the resource references.")
        for paths in (None, [], ["code/runner.tscn"]):
            with self.subTest(paths=paths):
                command["repair_paths"] = paths
                parsed = _response(json.dumps({**base, "commands": [command]}), "claude", ("claude", "codex"),
                    project=True, team=True, native=True, native_phase="work")
                self.assertEqual(parsed["commands"][0].get("repair_paths"), paths)
        complete = {field: None for field in fields}
        complete.update(type="complete", task_id="T1", result="The owned work is ready.")
        parsed = _response(json.dumps({**base, "commands": [complete]}), "claude", ("claude", "codex"),
            project=True, team=True, native=True, native_phase="work")
        self.assertNotIn("repair_paths", parsed["commands"][0])
        complete["repair_paths"] = ["code/runner.tscn"]
        with self.assertRaises(RoomError) as denied:
            _response(json.dumps({**base, "commands": [complete]}), "claude", ("claude", "codex"),
                project=True, team=True, native=True, native_phase="work")
        self.assertEqual(denied.exception.code, "invalid_team_commands")

    def test_native_review_shape_excludes_the_two_observed_cross_phase_failure_fields(self):
        schema = response_schema(project=True, native=True, collaboration=True, team_phase="review",
                                 member_ids=("claude", "codex"), provider="claude")
        fields = schema["properties"]["commands"]["items"]["properties"]
        self.assertEqual(set(fields), {"type", "task_id", "verdict", "reason", "cause"})
        self.assertEqual(fields["reason"]["type"], "string")
        base = {"message": "已核对实际私有版本。", "state": "done", "base_revision": 2, "files": [], "edits": [],
                "unchanged_paths": [], "resolved_conflicts": []}
        valid = {"type": "review", "task_id": "T2", "verdict": "accept", "reason": "Exact code and checks inspected.", "cause": None}
        parsed = _response(json.dumps({**base, "commands": [valid]}), "claude", ("claude", "codex"),
                           project=True, team=True, native=True, native_phase="review")
        self.assertEqual(parsed["commands"], [{key: value for key, value in valid.items() if key != "cause"}])
        # Sanitized shapes from the real failed native review and its correction:
        # findings were put in result, then result+summary remained populated.
        all_fields = set().union(*_COMMAND_FIELDS.values())
        for reason, summary in ((None, None), ("Review findings.", "Review summary.")):
            failed = {field: None for field in all_fields}
            failed.update(valid, result="Review findings in the wrong field.", reason=reason, summary=summary)
            with self.assertRaises(RoomError) as denied:
                _response(json.dumps({**base, "commands": [failed]}), "claude", ("claude", "codex"),
                          project=True, team=True, native=True, native_phase="review")
            self.assertEqual(denied.exception.code, "invalid_team_commands")

    def test_block_causes_are_explicit_nullable_and_only_in_native_block_phases(self):
        expected_causes = {"technical", "user_decision", "permission_required", "external_dependency", "coordination_required", None}
        for phase in ("plan", "coordinate", "work", "review", "help", "user", "checkpoint", "checkpoint_review"):
            with self.subTest(phase=phase):
                schema = response_schema(project=True, native=True, collaboration=True, team_phase=phase,
                    member_ids=("claude", "codex"), provider="claude")
                item = schema["properties"]["commands"]["items"]
                if phase in {"work", "review"}:
                    self.assertIn("cause", item["required"])
                    cause = item["properties"]["cause"]
                    self.assertEqual(cause["type"], ["string", "null"])
                    self.assertEqual(set(cause["enum"]), expected_causes)
                    self.assertIn("never overrides human instructions", cause["description"])
                else:
                    self.assertNotIn("cause", item["properties"])
        # The original flat static transport also carries the nullable field.
        static = response_schema(project=True, collaboration=True, member_ids=("claude", "codex"), provider="claude")
        item = static["properties"]["commands"]["items"]
        self.assertIn("cause", item["required"])
        self.assertEqual(set(item["properties"]["cause"]["enum"]), expected_causes)

    def test_native_phase_shapes_share_parser_schema_and_keep_unused_fields_strict(self):
        for phase in ("plan", "coordinate", "work", "review", "help", "user", "checkpoint", "checkpoint_review"):
            for author in (True, False):
                schema = response_schema(project=True, native=True, collaboration=True, team_phase=phase,
                    member_ids=("claude", "codex"), provider="claude", can_propose=author)
                self.assert_native_shape(schema)
                kinds, fields = command_shape(team_phase=phase, can_propose=author, native=True)
                item = schema["properties"]["commands"]["items"]
                self.assertEqual(set(item["properties"]), fields if kinds else set())
        schema = response_schema(project=True, native=True, collaboration=True, team_phase="checkpoint_review",
                                 member_ids=("claude", "codex"), provider="codex")
        self.assertEqual(set(schema["properties"]["commands"]["items"]["properties"]),
                         {"type", "verdict", "reason", "proposal_digest"})
        # Static clients retain their original full-flat transport contract.
        static = response_schema(project=True, collaboration=True, team_phase="review",
                                 member_ids=("claude", "codex"), provider="claude")
        self.assertIn("result", static["properties"]["commands"]["items"]["properties"])
        value = {"message": "工作已完成。", "state": "done", "base_revision": 1, "files": [], "edits": [],
                 "unchanged_paths": [], "resolved_conflicts": []}
        _, fields = command_shape(team_phase="work", native=True)
        command = {field: None for field in fields}
        command.update(type="complete", task_id="T1", result="Delivered file.", reason="Unused reason is forbidden.")
        with self.assertRaises(RoomError):
            _response(json.dumps({**value, "commands": [command]}), "claude", ("claude", "codex"),
                      project=True, team=True, native=True, native_phase="work")

    def assert_native_shape(self, schema):
        self.assertEqual(json.loads(json.dumps(schema, allow_nan=False)), schema)

        def visit(node):
            if node.get("type") == "object":
                self.assertIs(node["additionalProperties"], False)
                self.assertCountEqual(node["required"], node["properties"])
                for child in node["properties"].values():
                    visit(child)
            if "enum" in node:
                self.assertIsInstance(node["enum"], list)
                self.assertTrue(node["enum"], "native schemas must not contain empty enums")
            if "items" in node:
                visit(node["items"])

        visit(schema)

    def test_default_members_only_target_the_other_member_and_match_text_parser(self):
        for provider, peer in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(provider=provider):
                schema = response_schema(project=False, member_ids=("claude", "codex"), provider=provider)
                self.assert_native_shape(schema)
                properties = schema["properties"]
                self.assertEqual(set(properties), {"message", "actions", "state"})
                self.assertEqual(properties["actions"]["maxItems"], 1)
                action = properties["actions"]["items"]["properties"]
                self.assertEqual(set(action), {"type", "to", "kind", "text"})
                self.assertEqual(action["type"]["enum"], ["request"])
                self.assertEqual(action["to"]["enum"], [peer])
                self.assertEqual(action["kind"]["enum"], ["review", "help"])
                value = {"message": "请独立审核。", "state": "done", "actions": [
                    {"type": "request", "to": peer, "kind": "review", "text": "核对同一目标的草案。"}]}
                self.assertEqual(set(value), set(schema["required"]))
                self.assertEqual(_response(json.dumps(value), provider, ("claude", "codex"))["action"]["to"], peer)

    def test_single_member_has_no_actions_and_no_empty_enum(self):
        schema = response_schema(project=False, member_ids=("helper",), provider="helper")
        self.assert_native_shape(schema)
        self.assertEqual(schema["properties"]["actions"]["maxItems"], 0)
        self.assertNotIn("enum", schema["properties"]["actions"]["items"]["properties"]["to"])

    def test_project_requires_exact_proposal_shape_but_leaves_file_semantics_to_backend(self):
        schema = response_schema(project=True, member_ids=("claude", "codex"), provider="claude")
        self.assert_native_shape(schema)
        self.assertEqual(set(schema["properties"]), {"message", "actions", "state", "base_revision", "files"})
        self.assertEqual(schema["properties"]["base_revision"], {"type": "integer", "minimum": 0})
        files = schema["properties"]["files"]
        self.assertEqual(files["maxItems"], 16)
        self.assertEqual(set(files["items"]["properties"]), {"path", "previous_sha256", "content"})
        self.assertEqual(files["items"]["properties"]["previous_sha256"]["type"], ["string", "null"])
        self.assertEqual(files["items"]["properties"]["path"], {"type": "string"})
        value = {"message": "文字改稿，尚未执行。", "state": "done", "actions": [], "base_revision": 0,
                 "files": [{"path": "docs/example.md", "previous_sha256": None, "content": "短说明。"}]}
        self.assertEqual(set(value), set(schema["required"]))
        self.assertEqual(_response(json.dumps(value), "claude", ("claude", "codex"), project=True)["files"], value["files"])

    def test_aliases_and_eight_member_limit_keep_the_actual_roster(self):
        members = tuple("member_" + str(number) for number in range(8))
        schema = response_schema(project=False, member_ids=members, provider="member_3")
        self.assert_native_shape(schema)
        self.assertEqual(schema["properties"]["actions"]["items"]["properties"]["to"]["enum"],
                         [member for member in members if member != "member_3"])

    def test_each_call_returns_independent_nested_data(self):
        arguments = {"project": True, "member_ids": ("claude", "codex"), "provider": "claude"}
        first = response_schema(**arguments)
        second = response_schema(**arguments)
        first["required"].clear()
        first["properties"]["actions"]["items"]["properties"]["to"]["enum"].append("outside")
        first["properties"]["files"]["items"]["properties"]["previous_sha256"]["type"].clear()
        self.assertEqual(second, response_schema(**arguments))
        self.assert_native_shape(second)
        self.assertEqual(second["properties"]["actions"]["items"]["properties"]["to"]["enum"], ["codex"])

    def test_invalid_context_is_rejected_with_a_safe_stable_error(self):
        valid = {"project": False, "member_ids": ("claude", "codex"), "provider": "claude"}
        invalid = [
            {"project": value} for value in (0, 1, None, "false")
        ] + [
            {"member_ids": value} for value in (
                [], ["claude", "codex"], (), ("claude", "claude"),
                tuple("member_" + str(number) for number in range(9)),
                ("claude", "all"), ("claude", "../outside"), ("claude", "a" * 33),
                ("claude", "helper\n"), ("claude", None), ("claude", []),
            )
        ] + [
            {"provider": value} for value in (None, True, "helper", "all", "../outside")
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, "^invalid_response_schema_context$"):
                    response_schema(**{**valid, **overrides})


if __name__ == "__main__":
    unittest.main()
