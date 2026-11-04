"""Run choices cross real storage, scheduling and HTTP; provider turns stay fake."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator import adapters
from local_agent_coordinator.agent_registry import AgentRegistry, AgentSpec
from local_agent_coordinator.room_service import RoomError
from local_agent_coordinator.runtime_settings import RuntimeSettingsError, normalized_settings
from tests import test_team_runs as runs
from tests import test_team_service as team
from tests import test_work_modes as modes
from tests import test_room_server as http


def choice(permission="workspace_write", model="example", effort="high"):
    return {"permission": permission, "members": {"claude": {"model": model, "effort": effort},
                                                 "codex": {"model": model, "effort": effort}}}


def catalog():
    return {"models": [{"id": "example", "name": "Example", "efforts": ["low", "high"],
                        "default_effort": "low"}], "catalog_source": "test_metadata"}


class RuntimeSettingsServiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = team.TeamServiceTests.asyncSetUp
    asyncTearDown = team.TeamServiceTests.asyncTearDown
    settle = team.TeamServiceTests.settle
    scenario = runs.TeamRunTests.scenario

    async def load_catalog(self):
        with patch.object(adapters, "runtime_options", new=AsyncMock(return_value=catalog())):
            await self.service.refresh_runtime_options("claude")
            await self.service.refresh_runtime_options("codex")

    async def create(self, settings, *, work_mode="build"):
        created = await self.service.create_room("Deliver two independent modules", "all", "runtime-create",
            collaboration=True, mode="python_artifacts", work_mode=work_mode, runtime_settings=settings)
        await self.settle()
        return await self.service.read_room(created["room"]["id"])

    async def test_models_and_efforts_are_passed_on_every_phase_and_next_run_is_frozen_separately(self):
        await self.load_catalog()
        self.scenario(1)
        observed = []
        original = self.service.invoke
        async def invoke(*args, runtime_settings=None, **kwargs):
            observed.append((args[0], copy.deepcopy(runtime_settings)))
            return await original(*args, **kwargs)
        self.service.invoke = invoke
        requested = choice()
        first = await self.create(requested)
        self.assertTrue(first["current_run"]["can_accept"])
        self.assertTrue(observed)
        self.assertTrue(all(value == requested["members"][agent] for agent, value in observed))
        self.assertEqual({call["context"]["job"]["job_phase"] for call in self.provider.calls},
                         {"plan", "coordinate", "work", "review"})
        self.assertEqual(first["room"]["runtime_settings"], first["current_run"]["runtime_settings"])
        first_run = first["current_run"]
        second_settings = choice(effort="low")
        offset = len(observed)
        self.scenario(2)
        await self.service.start_run(first["room"]["id"], "Revise both modules", "all", "runtime-next",
            run_id=first_run["id"], revision=first_run["revision"], digest=first_run["digest"],
            runtime_settings=second_settings)
        await self.settle()
        second = await self.service.read_room(first["room"]["id"])
        self.assertTrue(second["current_run"]["can_accept"])
        self.assertTrue(all(value == second_settings["members"][agent] for agent, value in observed[offset:]))
        self.assertEqual(second["runs"][0]["runtime_settings"]["members"], requested["members"])
        self.assertEqual(second["current_run"]["runtime_settings"]["members"], second_settings["members"])
        # Idempotent replay needs neither a fresh catalog nor a new provider call.
        self.service._runtime_catalogs.clear()
        count = len(observed)
        await self.service.start_run(first["room"]["id"], "Revise both modules", "all", "runtime-next",
            run_id=first_run["id"], revision=first_run["revision"], digest=first_run["digest"],
            runtime_settings=second_settings)
        self.assertEqual(len(observed), count)

    async def test_unsupported_choices_fail_before_room_creation_or_inference(self):
        for settings, error in [(choice(), "runtime_options_required")]:
            with self.assertRaises(RoomError) as caught:
                await self.create(settings)
            self.assertEqual(caught.exception.code, error)
        await self.load_catalog()
        for settings, error in [(choice(model="unknown"), "runtime_model_unsupported"),
                                (choice(effort="ultra"), "runtime_effort_unsupported")]:
            with self.assertRaises(RoomError) as caught:
                await self.create(settings)
            self.assertEqual(caught.exception.code, error)
        self.assertEqual((await self.service.list_rooms())["rooms"], [])
        self.assertEqual(self.provider.calls, [])

    async def test_custom_connector_defaults_work_but_custom_models_are_never_silently_ignored(self):
        original = self.service.registry.get("claude")
        self.service.registry = AgentRegistry([AgentSpec("claude", "Custom", original.invoke, adapter="json_cli"),
                                               self.service.registry.get("codex")])
        with self.assertRaises(RoomError) as caught:
            await self.create(choice())
        self.assertEqual(caught.exception.code, "runtime_settings_unsupported")
        self.assertEqual(self.provider.calls, [])

    async def test_build_read_only_is_enforced_before_files_are_committed(self):
        self.scenario(1)
        settings = choice("read_only", model=None, effort=None)
        snapshot = await self.create(settings)
        self.assertEqual(snapshot["project"]["revision"], 0)
        self.assertEqual(snapshot["project"]["files"], [])
        self.assertFalse(snapshot["current_run"]["can_accept"])
        self.assertTrue(all(call["context"]["available_capabilities"] == ["text"] for call in self.provider.calls))

    async def test_read_only_can_complete_text_work_and_plan_downgrades_requested_writes(self):
        original_plan = modes.WorkModeServiceTests.plan
        def discuss(agent, context):
            # Reuse a real text-task lifecycle; build still has a read-only host.
            rewritten = {**context, "work_mode": "plan"}
            return original_plan(self, agent, rewritten)
        self.provider.handler = discuss
        snapshot = await self.create(choice("read_only", model=None, effort=None))
        self.assertTrue(snapshot["current_run"]["can_accept"])
        self.assertEqual(snapshot["project"]["files"], [])
        self.assertEqual(snapshot["room"]["runtime_settings"]["effective_permission"], "read_only")

    async def test_catalog_get_is_memory_only_explicit_refresh_coalesces_aliases_and_failure_is_honest(self):
        with patch.object(adapters, "runtime_options", new=AsyncMock(return_value=catalog())) as probe:
            self.assertEqual((await self.service.runtime_options())["partners"]["codex"]["state"], "not_checked")
            self.assertEqual(probe.await_count, 0)
            await asyncio.gather(self.service.refresh_runtime_options("codex"), self.service.refresh_runtime_options("codex"))
            self.assertEqual(probe.await_count, 1)
            self.assertEqual((await self.service.runtime_options())["partners"]["codex"]["state"], "ready")
        self.assertEqual(self.provider.calls, [])
        self.service._runtime_catalogs.clear()
        with patch.object(adapters, "runtime_options", new=AsyncMock(side_effect=RuntimeError("private payload"))):
            value = await self.service.refresh_runtime_options("codex")
        self.assertEqual(value["partners"]["codex"]["state"], "unavailable")
        self.assertNotIn("private payload", json.dumps(value))

    async def test_read_only_sandbox_text_delivery_is_ready_without_runner_or_execution(self):
        def discuss(agent, context):
            return modes.WorkModeServiceTests.plan(self, agent, {**context, "work_mode": "plan"})
        self.provider.handler = discuss
        with patch.object(self.service, "_execution_ready", new=AsyncMock(side_effect=AssertionError("No runner check for read-only work"))):
            created = await self.service.create_room("Discuss the private project", "all", "readonly-sandbox",
                mode="python_sandbox", collaboration=True, work_mode="build",
                runtime_settings=choice("read_only", model=None, effort=None))
            await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertTrue(snapshot["current_run"]["can_accept"])
        self.assertEqual(snapshot["collaboration"]["outcome"]["state"], "ready_for_user")
        self.assertEqual(snapshot["project"]["execution"], "disabled")
        self.assertEqual(snapshot["executions"], [])


class RuntimeSettingsValidationTests(unittest.TestCase):
    def test_invalid_shapes_and_effort_without_a_model_are_refused(self):
        invalid = [[], {}, {"permission": [], "members": {}}, {**choice(), "shell": True},
                   {**choice(), "members": {"other": {}}}, choice(model=None),
                   choice(model='model"; shell=true'), choice(effort="HIGH")]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RuntimeSettingsError):
                normalized_settings(value, ("claude", "codex"), "build")

    def test_plan_computes_effective_permission_and_member_defaults_are_explicit(self):
        result = normalized_settings({"permission": "workspace_write", "members": {}}, ("claude", "codex"), "plan")
        self.assertEqual(result["permission"], "workspace_write")
        self.assertEqual(result["effective_permission"], "read_only")
        self.assertEqual(result["members"], {"claude": {"model": None, "effort": None},
                                             "codex": {"model": None, "effort": None}})

    def test_new_and_resumed_native_commands_keep_tools_disabled_and_apply_real_provider_flags(self):
        sid = "11111111-1111-4111-8111-111111111111"
        with patch.object(adapters, "find_codex", return_value="codex"), patch.object(adapters, "find_claude", return_value="claude"):
            for session in (None, sid):
                codex = adapters._command("codex", session, runtime_settings={"model": "example", "effort": "high"})
                self.assertIn('model_reasoning_effort="high"', codex)
                self.assertEqual(codex[codex.index("--model") + 1], "example")
                self.assertIn("read-only", codex)
                if session:
                    self.assertLess(codex.index("resume"), codex.index("--model"))
                claude = adapters._command("claude", session, runtime_settings={"model": "opus", "effort": "xhigh"})
                self.assertEqual(claude[claude.index("--model") + 1], "opus")
                self.assertEqual(claude[claude.index("--effort") + 1], "xhigh")
                self.assertEqual(claude[claude.index("--tools") + 1], "")
                self.assertNotIn("--dangerously-skip-permissions", claude)


class RuntimeOptionsAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_model_rpc_reads_metadata_only_and_sanitizes_supported_efforts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "methods.jsonl"
            script = root / "rpc.py"
            script.write_text('''import sys,json
for line in sys.stdin:
 value=json.loads(line); method=value['method']
 with open(sys.argv[1],'a') as f:f.write(json.dumps(method)+'\\n')
 if method=='initialized':continue
 if method=='initialize':result={}
 elif method=='model/list':result={'data':[{'model':'example','displayName':'Example','supportedReasoningEfforts':[{'reasoningEffort':'low'},{'reasoningEffort':'high'}],'defaultReasoningEffort':'low','hidden':False}], 'nextCursor':None}
 else:raise RuntimeError('unexpected RPC')
 print(json.dumps({'id':value['id'],'result':result}),flush=True)
''')
            result = await adapters._codex_account_rpc([sys.executable, str(script), str(log)], root, models=True)
            self.assertEqual(result[0]["efforts"], ["low", "high"])
            self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                             ["initialize", "initialized", "model/list"])

    async def test_claude_uses_actual_help_aliases_without_claiming_account_entitlements(self):
        help_text = b"--effort <level> Effort for session (low, high)\n--model <model> Provide an alias (e.g. 'opus', or 'sonnet') or full name.\n--next <value> Next\n"
        with patch.object(adapters, "find_claude", return_value="claude"), \
             patch.object(adapters, "_claude_control_rpc", new=AsyncMock(side_effect=adapters.AccountTelemetryError("runtime_catalog_not_exposed"))), \
             patch.object(adapters, "_run", new=AsyncMock(return_value=([help_text], 0))) as probe:
            result = await adapters.runtime_options("claude", Path("/tmp"))
        self.assertEqual([item["id"] for item in result["models"]], ["opus", "sonnet"])
        self.assertEqual(result["models"][0]["efforts"], [])
        self.assertEqual(result["effort_scope"], "unknown")
        self.assertEqual(result["reason"], "provider_model_entitlement_not_exposed")
        self.assertEqual(probe.call_args.args[0], ["claude", "--safe-mode", "--restricted", "--help"])

    async def test_claude_catalog_errors_and_timeouts_do_not_fall_back_to_invented_versions(self):
        for error in (adapters.AccountTelemetryError("account_protocol_invalid"), asyncio.TimeoutError()):
            with self.subTest(error=type(error).__name__), \
                 patch.object(adapters, "find_claude", return_value="claude"), \
                 patch.object(adapters, "_claude_control_rpc", new=AsyncMock(side_effect=error)), \
                 patch.object(adapters, "_run", new=AsyncMock()) as help_probe:
                with self.assertRaises(type(error)):
                    await adapters.runtime_options("claude", Path("/tmp"))
                help_probe.assert_not_awaited()

    async def test_claude_catalog_is_the_initialize_capability_source_not_an_entitlement_claim(self):
        models = [{"id": "opus", "name": "Opus 5.5", "resolved_model": "claude-opus-5-5",
                   "efforts": ["low", "high"], "default_effort": None, "is_default": False}]
        with patch.object(adapters, "find_claude", return_value="claude"), \
             patch.object(adapters, "_claude_control_rpc", new=AsyncMock(return_value=models)) as rpc, \
             patch.object(adapters, "_run", new=AsyncMock()) as help_probe:
            result = await adapters.runtime_options("claude", Path("/tmp"))
        self.assertEqual(result["models"], models)
        self.assertEqual(result["catalog_source"], "claude_sdk_initialize")
        self.assertEqual(result["effort_scope"], "per_model")
        self.assertEqual(result["reason"], "provider_model_entitlement_not_exposed")
        self.assertEqual(rpc.call_args.kwargs["query"], "models")
        help_probe.assert_not_awaited()


class RuntimeSettingsHTTPTests(unittest.TestCase):
    setUp = http.RoomHTTPTests.setUp
    tearDown = http.RoomHTTPTests.tearDown
    request = http.RoomHTTPTests.request

    def test_runtime_settings_cross_the_user_http_boundary_but_mcp_cannot_escalate_them(self):
        create = {"goal": "Bounded work", "target": "all", "request_id": "settings", "runtime_settings": choice()}
        self.assertEqual(self.request("POST", "/api/rooms", create)[0], 200)
        self.assertEqual(self.service.calls[-1][-1]["runtime_settings"], choice())
        count = len(self.service.calls)
        status, _, value = self.request("POST", "/api/mcp/rooms", create,
            headers={"X-LAC-Instance": self.server.instance_id})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(value)["error"], "user_runtime_settings_required")
        self.assertEqual(len(self.service.calls), count)

    def test_refresh_requires_an_explicit_user_request_and_get_only_reads_cache(self):
        async def metadata():
            self.service.calls.append(("runtime-cache",))
            return {"permissions": [], "partners": {}, "scope": "coordinator_owned_only"}
        self.service.runtime_options = metadata
        self.service.refresh_runtime_options = AsyncMock(return_value={"partners": {}})
        self.assertEqual(self.request("GET", "/api/runtime-options")[0], 200)
        self.service.refresh_runtime_options.assert_not_awaited()
        self.assertEqual(self.request("POST", "/api/partners/codex/runtime/refresh", {})[0], 200)
        self.service.refresh_runtime_options.assert_awaited_once_with("codex")
        self.assertEqual(self.request("POST", "/api/mcp/partners/codex/runtime/refresh", {},
            headers={"X-LAC-Instance": self.server.instance_id})[0], 403)
