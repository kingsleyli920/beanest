"""Actual setup/room HTTP routes with temporary profiles and fictional native peers."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import select
import signal
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator import adapters, kimi_acp
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
from test_kimi_native_setup import configuration


class SetupFixture:
    def __init__(self, *, discover=True):
        self.temporary = tempfile.TemporaryDirectory(prefix="beanest-fake-kimi-setup-")
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "fictional-home"
        self.config = self.home / ".kimi-code" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.release_file = self.root / "release-login"
        self.source = self.root / "fictional-project"
        self.source.mkdir()
        (self.source / "README.md").write_text("# Disposable setup fixture\n", encoding="utf-8")
        self.executable = self.root / "fake-kimi"
        self.executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f'''
            from pathlib import Path
            import os
            import sys
            import time
            assert sys.argv[1:] == ["login"]
            root = Path(__file__).parent
            with (root / "login-starts").open("a") as stream:
                stream.write(str(os.getpid()) + "\\n")
            print("Opening browser for Kimi device login: https://www.kimi.com/code/authorize_device?user_code=TEST-1234",
                  file=sys.stderr, flush=True)
            print("If the browser did not open, paste the URL above and enter code: TEST-1234",
                  file=sys.stderr, flush=True)
            while not (root / "release-login").exists():
                time.sleep(.05)
            config = Path({str(self.config)!r})
            config.write_text({configuration()!r}, encoding="utf-8")
            print("Logged in to managed:kimi-code.", file=sys.stderr, flush=True)
        '''), encoding="utf-8")
        self.executable.chmod(0o700)
        self.calls = []
        self.invoke_handler = None
        self.stack = ExitStack()
        self.server = self.thread = None
        self.closed = False
        original_configuration = kimi_acp.managed_configuration

        def fictional_configuration(path=None):
            return original_configuration(self.config)

        async def held_invoke(*args, **kwargs):
            self.calls.append({"adapter": args[0] if args else None, "synthetic": True,
                               "permission": kwargs.get("permission"), "session_id": kwargs.get("session_id")})
            if self.invoke_handler is not None:
                return await self.invoke_handler(*args, **kwargs)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                error.native_execution = {"cleanup_confirmed": True, "side_effects_possible": False,
                                          "never_started": True, "tool_calls": 0, "tool_results": 0, "receipt_path": None}
                raise

        async def fake_kimi_status(*args, **kwargs):
            return {"ready": fictional_configuration()["state"] == "ready", "version": "0.0.0"}

        async def metadata_run(command, *args, **kwargs):
            if "--version" in command:
                return [b"Fictional native CLI 0.0.0"], 0
            if "auth" in command:
                return [json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                    "apiProvider": "firstParty", "subscriptionType": "pro"}).encode()], 0
            if command[1:] == ["login", "status"]:
                return [b"Logged in using ChatGPT"], 0
            raise AssertionError("fixture_disallows_external_cli")

        async def catalog(adapter, *args, **kwargs):
            models = fictional_configuration()["models"] if adapter == "kimi" else [{
                "id": "fictional-model", "name": "Fictional model", "efforts": ["high"],
            }]
            return {"models": models, "catalog_source": "fictional_fixture",
                    "effort_scope": "per_model", "reason": "fixture_metadata_only"}

        async def account(adapter, *args, **kwargs):
            ready = adapter != "kimi" or fictional_configuration()["state"] == "ready"
            return {"state": "ready" if ready else "unavailable", "email": None, "plan": None,
                    "auth_method": ({"claude": "claude.ai", "codex": "chatgpt", "kimi": "kimi-code"}[adapter]
                                    if ready else None),
                    "source": "fictional_fixture", "scope": "default_cli_login", "reason": None,
                    "quota": {"state": "unavailable", "windows": [], "source": "fictional_fixture",
                              "scope": "provider_account", "reason": "provider_quota_not_exposed"}}

        def disallowed(*args, **kwargs):
            raise AssertionError("fixture_disallows_external_cli")

        for name in ("find_kimi", "find_claude", "find_codex"):
            self.stack.enter_context(patch.object(adapters, name, return_value=str(self.executable)))
        self.stack.enter_context(patch.object(adapters, "_environment", return_value={
            "HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        }))
        for name, value in {
            "invoke": held_invoke, "_run": metadata_run, "_status": disallowed,
            "kimi_status": fake_kimi_status, "runtime_options": catalog, "account_status": account,
            "_codex_account_rpc": disallowed, "_claude_control_rpc": disallowed,
            "_claude_usage_rpc": disallowed,
        }.items():
            self.stack.enter_context(patch.object(adapters, name, value))
        self.stack.enter_context(patch.object(kimi_acp, "managed_configuration", fictional_configuration))
        try:
            registry = default_registry(held_invoke)
            self.partners = PartnerManager(self.root / "partners", registry, startup_discovery=True)
            self.partners.add(id="kimi", name="Kimi Code", adapter="kimi", avatar="K")
            self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                       registry=registry, partners=self.partners, native_invoke=held_invoke)
            self.server = create_room_server(self.service, port=0)
            self.port = self.server.server_address[1]
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
            self.thread.start()
            code, initial = (self.request("POST", "/api/partners/discover", {}) if discover
                             else self.request("GET", "/api/partners"))
            if code != 200:
                raise AssertionError(("fixture_discovery_failed", code, initial.get("error")))
            self.initial = initial
        except BaseException:
            self.close()
            raise

    def request(self, method, path, value=None, *, mcp=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"X-LAC-Token": self.server.mcp_token if mcp else self.server.token}
        if mcp:
            headers["X-LAC-Instance"] = self.server.instance_id
        if value is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=None if value is None else json.dumps(value), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def bootstrap(self):
        """The real GET envelope contains all three selectable synthetic profiles."""
        code, result = self.request("GET", "/api/partners")
        if code != 200:
            raise AssertionError("fixture_partners_unavailable")
        return result

    def release_login(self):
        self.release_file.touch()

    def hold_login(self):
        self.release_file.unlink(missing_ok=True)

    def info(self):
        return {"url": f"http://127.0.0.1:{self.port}/", "port": self.port,
                "instance_id": self.server.instance_id, "root": str(self.root),
                "source": str(self.source), "release_file": str(self.release_file),
                "real_cli_calls": 0, "real_model_calls": 0}

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.server is not None:
                self.server.shutdown()
                if self.thread is not None:
                    self.thread.join(3)
                self.server.server_close()
        finally:
            self.stack.close()
            self.temporary.cleanup()


def fixture_main():
    fixture = SetupFixture()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print(json.dumps(fixture.info(), ensure_ascii=False), flush=True)
    try:
        while not stop.is_set():
            available, _, _ = select.select([sys.stdin], [], [], .25)
            if not available:
                continue
            command = sys.stdin.readline()
            if not command or command.strip() == "stop":
                break
            if command.strip() == "release":
                fixture.release_login()
                print('{"fixture_command":"released"}', flush=True)
            elif command.strip() == "hold":
                fixture.hold_login()
                print('{"fixture_command":"held"}', flush=True)
            elif command.strip() == "status":
                profiles = fixture.bootstrap()
                print(json.dumps({"setup": {item["id"]: item["setup"]["state"]
                                            for item in profiles["supported_adapters"]},
                                  "synthetic_calls": len(fixture.calls), "real_model_calls": 0}), flush=True)
    finally:
        fixture.close()
        print(json.dumps({"closed": True, "temporary_removed": not fixture.root.exists(),
                          "synthetic_calls": len(fixture.calls), "real_model_calls": 0}), flush=True)


class KimiSetupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SetupFixture(discover=False)
        self.addCleanup(self.fixture.close)
        self.service, self.server = self.fixture.service, self.fixture.server
        self.port, self.root, self.source = self.fixture.port, self.fixture.root, self.fixture.source

    def tearDown(self):
        self.doCleanups()

    def request(self, method, path, value=None, *, expected=200, mcp=False):
        code, result = self.fixture.request(method, path, value, mcp=mcp)
        self.assertEqual(code, expected, {"status": code, "error": result.get("error")})
        return result

    def setup_state(self):
        result = self.request("GET", "/api/partners")
        return next(item["setup"]["state"] for item in result["supported_adapters"] if item["id"] == "kimi")

    def wait_login(self, state):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            operation = self.request("GET", "/api/native-adapters/kimi/login")["operation"]
            if operation["state"] == state:
                return operation
            if operation["state"] in {"completed", "failed", "cancelled"}:
                self.fail(f"expected login {state}: {operation['state']}/{operation['reason']}")
            time.sleep(.02)
        self.fail("fake login did not reach " + state)

    def register_project(self):
        result = self.request("POST", "/api/projects", {
            "name": "Fictional native setup project", "path": str(self.source), "selection_kind": "folder",
        })
        return result["project"]["id"]

    def create_body(self, project_id, members):
        return {"goal": "Inspect the disposable setup project.", "request_id": str(uuid4()),
                "target": members[0], "participants": members, "mode": "native_project",
                "project_id": project_id, "work_mode": "build",
                "runtime_settings": {"permission": "full_access", "members": {}},
                "execution_policy": {"mode": "autonomous"}}

    def wait_room(self, room_id, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = self.request("GET", f"/api/rooms/{room_id}")
            if predicate(snapshot):
                return snapshot
            time.sleep(.02)
        self.fail("HTTP fixture did not settle: " + json.dumps({
            "room": snapshot["room"]["status"],
            "objective": (snapshot.get("objective") or {}).get("status"),
            "jobs": [{key: job[key] for key in ("status", "error")} for job in snapshot["jobs"]],
        }))

    def test_passive_get_login_operation_identity_scopes_and_ready_completion(self):
        with patch("asyncio.create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as spawn:
            self.assertEqual(self.setup_state(), "unknown")
            self.assertIsNone(self.request("GET", "/api/native-adapters/kimi/login")["operation"])
            self.request("GET", "/api/runtime-options")
            self.request("GET", "/api/rooms")
            self.assertEqual(self.setup_state(), "unknown")
            spawn.assert_not_called()
        self.assertFalse((self.root / "login-starts").exists())

        self.request("POST", "/api/partners/discover", {})
        self.assertEqual(self.setup_state(), "login_required")
        first = self.request("POST", "/api/native-adapters/kimi/login", {})["operation"]
        waiting = self.wait_login("waiting")
        self.assertEqual(waiting["verification_uri"],
                         "https://www.kimi.com/code/authorize_device?user_code=TEST-1234")
        repeated = self.request("POST", "/api/native-adapters/kimi/login", {})["operation"]
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(len((self.root / "login-starts").read_text().splitlines()), 1)
        for method, body in (("GET", None), ("POST", {}), ("DELETE", {"operation_id": first["id"]})):
            with self.subTest(mcp_method=method):
                denied = self.request(method, "/api/mcp/native-adapters/kimi/login", body,
                                      expected=403, mcp=True)
                self.assertEqual(denied["error"], "user_native_login_required")
        self.fixture.release_login()
        completed = self.wait_login("completed")
        self.assertIsNone(completed["verification_uri"])
        self.assertIsNone(completed["user_code"])
        self.assertEqual(self.setup_state(), "ready")

        self.fixture.hold_login()
        second = self.request("POST", "/api/native-adapters/kimi/login", {})["operation"]
        self.wait_login("waiting")
        self.assertNotEqual(second["id"], first["id"])
        stale = self.request("DELETE", "/api/native-adapters/kimi/login", {"operation_id": first["id"]}, expected=409)
        self.assertEqual(stale["error"], "login_operation_changed")
        current = self.request("GET", "/api/native-adapters/kimi/login")["operation"]
        self.assertEqual((current["id"], current["state"]), (second["id"], "waiting"))
        cancelled = self.request("DELETE", "/api/native-adapters/kimi/login", {"operation_id": second["id"]})["operation"]
        self.assertEqual((cancelled["state"], cancelled["reason"]), ("cancelled", "login_cancelled"))
        self.assertEqual(len((self.root / "login-starts").read_text().splitlines()), 2)
        self.assertEqual(self.fixture.calls, [])

    def test_native_kimi_three_member_room_and_legacy_json_profiles_remain_distinct(self):
        self.fixture.config.write_text(configuration(), encoding="utf-8")
        self.request("POST", "/api/partners/discover", {})
        connection = {"protocol": "beanest-json-v1", "executable": str(self.fixture.executable),
                      "args": ["--json"], "billing": "existing_subscription"}
        for identifier, old_connection in (("kimi_old_null", None), ("kimi_old_json", connection)):
            self.request("POST", "/api/partners", {"id": identifier, "name": identifier,
                         "adapter": "kimi", "connection": old_connection})
        profiles = {item["id"]: item for item in self.request("GET", "/api/partners")["partners"]}
        self.assertNotIn("connection", profiles["kimi"])
        self.assertEqual((profiles["kimi"]["provider"], profiles["kimi"]["connection_state"]), ("moonshot", "builtin"))
        self.assertIsNone(profiles["kimi_old_null"]["connection"])
        self.assertEqual(profiles["kimi_old_null"]["connection_state"], "not_configured")
        self.assertEqual(profiles["kimi_old_json"]["connection"], connection)
        self.assertEqual(profiles["kimi_old_json"]["usage_source"], "configured_cli")
        project_id = self.register_project()
        for identifier in ("kimi_old_null", "kimi_old_json"):
            with self.subTest(legacy=identifier):
                rejected = self.request("POST", "/api/rooms", self.create_body(project_id, [identifier]), expected=409)
                self.assertEqual(rejected["error"], "native_adapter_unsupported")
        self.assertEqual(self.fixture.calls, [])

        created = self.request("POST", "/api/rooms", self.create_body(project_id, ["kimi", "claude", "codex"]))
        room_id = created["room"]["id"]
        snapshot = self.wait_room(room_id, lambda value: bool(self.fixture.calls))
        self.assertEqual({member["id"] for member in snapshot["members"]}, {"kimi", "claude", "codex"})
        self.assertEqual(snapshot["room"]["mode"], "native_project")
        self.assertEqual(snapshot["team"]["plan_author"], "kimi")
        self.assertTrue(all(call["adapter"] in {"kimi", "claude", "codex"} for call in self.fixture.calls))
        cancelled = self.request("POST", f"/api/rooms/{room_id}/cancel", {})
        self.assertTrue(cancelled["cancellation_confirmed"])
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in cancelled["jobs"]))

    def test_native_permission_failure_surfaces_the_original_question_and_stops_dispatch(self):
        self.fixture.config.write_text(configuration(), encoding="utf-8")
        question = "是否允许读取临时项目说明？\n允许一次 / 拒绝"
        opaque_sid = "session_00000000-0000-4000-8000-000000000034"

        async def native_question(adapter, prompt, cwd, session_id=None, **options):
            self.assertEqual(adapter, "kimi")
            self.assertIsNone(session_id)
            return {"session_id": opaque_sid, "status": "failed", "failure_code": "native_permission_required",
                    "native_question": question, "text": "", "usage": None, "events": [],
                    "cleanup_confirmed": True,
                    "native_execution": {"agent": "kimi", "permission": options["permission"],
                        "tool_calls": 0, "tool_results": 0, "side_effects_possible": False,
                        "cleanup_confirmed": True, "exit_code": -15, "duration_seconds": .01}}
        self.fixture.invoke_handler = native_question
        project_id = self.register_project()
        created = self.request("POST", "/api/rooms", self.create_body(project_id, ["kimi"]))
        room_id = created["room"]["id"]
        final = self.wait_room(room_id, lambda value: value["objective"]["status"] == "needs_user"
                               and not any(job["status"] in {"queued", "working"} for job in value["jobs"]))
        self.assertEqual(final["objective"]["stop_reason"], "objective_decision_required")
        self.assertEqual(final["objective"]["next_step"]["question"], question)
        self.assertTrue(final["objective"]["can_answer"])
        self.assertEqual(len(final["jobs"]), 1)
        self.assertEqual((final["jobs"][0]["status"], final["jobs"][0]["error"]), ("failed", "native_permission_required"))
        messages = [event for event in final["events"] if event.get("native_question")]
        self.assertEqual([(event["sender"], event["text"]) for event in messages], [("kimi", question)])
        self.assertEqual(final["members"][0]["session_id"], opaque_sid)
        self.assertFalse(final["members"][0]["identity_uncertain"])
        self.assertTrue(self.service.store.stage_cleanup_confirmed(room_id))
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.fixture.calls[0]["permission"], "full_access")
        self.assertFalse(final["team"]["tasks"])
        self.request("GET", f"/api/rooms/{room_id}/state")
        self.assertEqual(len(self.fixture.calls), 1)


if __name__ == "__main__":
    unittest.main()

