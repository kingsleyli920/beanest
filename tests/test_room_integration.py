"""Real HTTP, private SQLite and the background service loop; providers stay fake."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import http.client
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.agent_registry import AgentRegistry, AgentSpec, default_registry
from local_agent_coordinator.project_workspace import ProjectWorkspaceError
from local_agent_coordinator.partner_manager import PartnerManager


class PeerCLI:
    """Respond to the actual job context without doing any provider I/O."""

    def __init__(self, *, blocked=()):
        self.blocked = set(blocked)
        self.calls = []
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.cleaned = threading.Event()

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        with self.lock:
            self.calls.append({"agent": agent, "kind": context["job"]["kind"],
                               "session_id": session_id, "thread": threading.get_ident(),
                               "room_id": cwd.parent.name})
        self.started.set()
        try:
            if agent in self.blocked:
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            kind = context["job"]["kind"]
            peer = "codex" if agent == "claude" else "claude"
            action = [{"type": "request", "to": peer, "kind": "review",
                       "text": "Review the same goal and the bound draft."}] if kind == "task" else []
            value = {"message": f"{agent} visible {kind} result", "actions": action, "state": "done"}
            if "project" in context:
                value.update(base_revision=context["proposal_base_revision"], files=[])
                if kind == "task":
                    value["files"] = [{"path": "toy.py", "previous_sha256": None,
                                       "content": "def doubled(value):\n    return value * 2\n"}]
                elif kind == "review":
                    value["files"] = [{"path": "test_peer.py", "previous_sha256": None,
                                       "content": "raise RuntimeError('not executable in this suite')\n"}]
            return {
                "session_id": session_id or str(uuid5(NAMESPACE_URL, str(cwd))),
                "status": "completed", "usage": None, "events": [],
                "text": json.dumps(value),
            }
        finally:
            self.cleaned.set()

    def snapshot_calls(self):
        with self.lock:
            return list(self.calls)


class RoomIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "index.html").write_text(
            '<title>Room test</title><meta name="lac-token" content="__LAC_TOKEN__">')
        self.fake = PeerCLI()
        self.server = None
        self.thread = None
        self.constructor_thread = threading.get_ident()
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.directory.cleanup()

    def start_server(self, registry=None, partners=None):
        # Production constructs in main, then calls async methods on _BridgeLoop's thread.
        execution = {"registry": registry} if registry is not None else {"invoke": self.fake}
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   **execution, partners=partners)
        self.server = create_room_server(self.service, port=0, asset_dir=self.assets)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01})
        self.thread.start()

    def stop_server(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.server.server_close()
        self.assertIsNone(self.server.runner.shutdown_error)
        self.server = None

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Host": f"127.0.0.1:{self.port}", "X-LAC-Token": self.server.mcp_token if path.startswith("/api/mcp/") else self.server.token}
        if path.startswith('/api/mcp/'):
            headers['X-LAC-Instance'] = self.server.instance_id
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            content = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                content = json.loads(content)
            return response.status, content
        finally:
            connection.close()

    def create_room(self, target="claude", request_id="create-request"):
        status, snapshot = self.request("POST", "/api/rooms",
                                        {"goal": "Review a short shared text goal", "target": target,
                                         "request_id": request_id})
        self.assertEqual(status, 200)
        return snapshot

    def settle(self):
        async def wait():
            while True:
                tasks = [task for task in self.service._workers.values() if not task.done()]
                tasks.extend(task for task in self.service._executions.values() if not task.done())
                if not tasks:
                    return
                await asyncio.gather(*tasks)

        async def bounded():
            await asyncio.wait_for(wait(), 2)

        self.server.runner.call(bounded)

    def test_partner_style_registration_and_validation_through_real_http_without_inference(self):
        self.stop_server()
        manager = PartnerManager(self.root / 'partners', default_registry(self.fake))
        self.start_server(registry=manager.registry(), partners=manager)
        status, directory = self.request('GET', '/api/partners')
        self.assertEqual(status, 200)
        self.assertEqual([item['id'] for item in directory['bean_styles']],
                         ['sprout', 'peach', 'sky', 'lavender', 'sunshine', 'cocoa',
                          'mint', 'coral', 'ocean', 'rose'])
        profile = {'id': 'bean', 'name': 'Fictional Bean', 'adapter': 'claude', 'bean_style': 'sky'}
        status, created = self.request('POST', '/api/partners', profile)
        self.assertEqual(status, 200)
        self.assertEqual(next(item for item in created['partners'] if item['id'] == 'bean')['bean_style'], 'sky')
        status, observed = self.request('GET', '/api/status')
        self.assertEqual(status, 200)
        self.assertEqual(observed['bean_styles'], directory['bean_styles'])
        self.assertEqual(next(item for item in observed['agents'] if item['id'] == 'bean')['bean_style'], 'sky')
        self.assertEqual(self.request('POST', '/api/partners', profile)[0], 200)
        status, error = self.request('POST', '/api/partners', {**profile, 'bean_style': 'cocoa'})
        self.assertEqual((status, error['error']), (409, 'partner_identity_conflict'))
        for invalid in ('unregistered-style', '', None, []):
            status, error = self.request('POST', '/api/partners', {**profile, 'id': 'new', 'bean_style': invalid})
            self.assertEqual((status, error['error']), (400, 'invalid_partner_profile'))
        self.assertEqual(self.request('POST', '/api/mcp/partners', {**profile, 'id': 'fromagent'})[0], 404)
        self.assertEqual(len(manager.profiles()), 3)
        self.assertEqual(self.fake.snapshot_calls(), [])

    def test_sandbox_execution_is_automatic_and_version_bound_through_user_and_mcp_http(self):
        class Runner:
            def __init__(self):
                self.calls = []

            async def ready(self):
                return {"available": True}

            async def run(self, snapshot, timeout=15, *, run_id=None):
                self.calls.append((run_id, snapshot["revision"]))
                return {"status": "completed", "exit_code": 0, "stdout": "UNTRUSTED_PROGRAM_OUTPUT",
                        "stderr": "", "truncated": False, "cleanup_confirmed": True,
                        "run_id": run_id, "revision": snapshot["revision"],
                        "snapshot_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                            ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()}

        runner = Runner()
        self.service._runner_instance = runner
        for entry, source in (("/api/rooms", "user"), ("/api/mcp/rooms", "mcp")):
            body = {"goal": "Create a fictional function and review then sandbox its new files",
                    "target": "codex", "request_id": "sandbox-" + source, "mode": "python_sandbox"}
            status, first = self.request("POST", entry, body)
            self.assertEqual(status, 200)
            self.settle()
            status, saved = self.request("GET", "/api/rooms/" + first["room"]["id"])
            self.assertEqual(status, 200)
            self.assertEqual(saved["room"]["mode"], "python_sandbox")
            self.assertEqual(saved["room"]["status"], "completed")
            self.assertEqual(saved["events"][0]["sender"], source)
            self.assertEqual([job["kind"] for job in saved["jobs"]], ["task", "review", "feedback", "execution"])
            self.assertEqual(len(saved["executions"]), 1)
            execution = saved["executions"][0]
            self.assertEqual((execution["revision"], execution["status"]), (2, "completed"))
            self.assertEqual(execution["feedback_job_id"], saved["jobs"][-1]["id"])
            self.assertEqual(execution["result"]["report_trust"], "untrusted")
            self.assertNotIn("UNTRUSTED_PROGRAM_OUTPUT", str([e for e in saved["events"] if e["kind"] == "message"]))
            self.assertEqual(self.request("POST", entry, body)[1]["room"]["id"], first["room"]["id"])
            self.settle()
        self.assertEqual(len(runner.calls), 2)

    def test_artifact_http_and_mcp_entry_preserve_mode_source_and_no_execution(self):
        for entry, source in (("/api/rooms", "user"), ("/api/mcp/rooms", "mcp")):
            body = {"goal": "Create new toy file data; never execute", "target": "codex",
                    "request_id": "artifact-" + source, "mode": "python_artifacts"}
            status, first = self.request("POST", entry, body)
            self.assertEqual(status, 200)
            self.settle()
            status, saved = self.request("GET", "/api/rooms/" + first["room"]["id"])
            self.assertEqual(status, 200)
            self.assertEqual(saved["room"]["mode"], "python_artifacts")
            self.assertEqual(saved["room"]["status"], "completed")
            self.assertEqual(saved["project"]["revision"], 2)
            self.assertEqual(saved["project"]["execution"], "disabled")
            self.assertEqual(saved["events"][0]["sender"], source)
            self.assertEqual({file["owner"] for file in saved["project"]["files"]}, {"claude", "codex"})
            calls = len(self.fake.snapshot_calls())
            self.assertEqual(self.request("POST", entry, body)[1]["room"]["id"], first["room"]["id"])
            self.assertEqual(len(self.fake.snapshot_calls()), calls)
            self.assertEqual(self.request("POST", entry, {**body, "mode": "text"})[0], 409)
            self.assertEqual(self.request("POST", entry, {**body, "project_path": "/fictional/unapproved"})[0], 400)
        self.assertFalse(list((self.root / "project-artifacts").rglob("*.py")))

    def test_an_unknown_role_or_workload_is_named_as_such(self):
        # Catches a bad role or workload choice coming back as an unexplained "operation failed".
        for choice, code in (({"role": "builtin:builder"}, "invalid_role"), ({"workload": "huge"}, "invalid_workload")):
            status, body = self.request("POST", "/api/rooms", {
                "goal": "Review a short shared text goal", "target": "claude", "request_id": "bad-" + code,
                "mode": "text_artifacts", "work_mode": "build",
                "runtime_settings": {"permission": "workspace_write", "members": {"claude": choice}}})
            self.assertEqual((status, body), (400, {"error": code}))

    def test_mcp_room_without_work_mode_is_a_configurable_build_room_and_old_user_callers_stay_legacy(self):
        # Catches: an agent-started room (work_mode is optional over MCP) being stored as an
        # old legacy record, which the page shows as history with every model locked.
        body = {"goal": "Review a short shared text goal", "target": "claude", "request_id": "mcp-no-mode"}
        status, created = self.request("POST", "/api/mcp/rooms", body)
        self.assertEqual(status, 200, created)
        room_id = created["room"]["id"]
        self.settle()
        status, saved = self.request("GET", "/api/rooms/" + room_id)
        self.assertEqual(status, 200)
        self.assertEqual(saved["events"][0]["sender"], "mcp")
        self.assertEqual(saved["room"]["work_mode"], "build")
        self.assertEqual(saved["runtime_configuration"]["desired"], {"work_mode": "build", "runtime_settings": None})
        self.assertTrue(saved["runtime_configuration"]["can_edit"])
        claims = [event["work_mode"] for event in saved["events"] if event["kind"] == "job_claimed"]
        self.assertTrue(claims)
        self.assertEqual(set(claims), {"build"})
        # The omitted field stays out of the request fingerprint: the same retry is the same room.
        calls = len(self.fake.snapshot_calls())
        status, again = self.request("POST", "/api/mcp/rooms", body)
        self.assertEqual((status, again["room"]["id"]), (200, room_id))
        self.assertEqual(len(self.fake.snapshot_calls()), calls)
        # An older user client that never sent the field keeps its recorded legacy behavior.
        status, older = self.request("POST", "/api/rooms", {**body, "request_id": "user-no-mode"})
        self.assertEqual(status, 200, older)
        self.settle()
        self.assertEqual(older["room"]["work_mode"], "legacy")
        self.assertEqual(self.request("GET", "/api/rooms/" + older["room"]["id"])[1]["runtime_configuration"]["desired"]["work_mode"], "legacy")

    def test_artifact_initialization_failure_is_unknown_and_same_id_does_not_dispatch(self):
        body = {"goal": "A fictional initialization failure", "target": "claude",
                "request_id": "failed-project-init", "mode": "python_artifacts"}
        with patch('local_agent_coordinator.room_service.ProjectWorkspace',
                   side_effect=ProjectWorkspaceError('project_commit_unconfirmed')):
            status, failure = self.request('POST', '/api/mcp/rooms', body)
        self.assertEqual(status, 503)
        self.assertEqual(failure['error'], 'project_initialization_unconfirmed')
        self.assertEqual(self.fake.snapshot_calls(), [])
        status, recovered = self.request('POST', '/api/mcp/rooms', body)
        self.assertEqual(status, 200)
        self.assertEqual(recovered['room']['status'], 'unknown')
        self.assertFalse(recovered['project']['available'])
        self.assertEqual(self.fake.snapshot_calls(), [])

    def test_main_constructed_service_routes_both_initial_providers_through_http(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            created = self.create_room(initial, "start-" + initial)
            room_id = created["room"]["id"]
            self.settle()
            status, snapshot = self.request("GET", f"/api/rooms/{room_id}")
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["room"]["status"], "completed")
            self.assertEqual([job["kind"] for job in snapshot["jobs"]], ["task", "review", "feedback"])
            self.assertEqual(snapshot["budget"]["used"], 3)
            messages = [event for event in snapshot["events"] if event["kind"] == "message"]
            self.assertEqual([event["sender"] for event in messages], ["user", initial, peer, initial])
            self.assertEqual(len({event["seq"] for event in snapshot["events"]}), len(snapshot["events"]))
            calls = [call for call in self.fake.snapshot_calls() if call["room_id"] == room_id]
            self.assertEqual([call["agent"] for call in calls], [initial, peer, initial])
            self.assertIsNone(calls[0]["session_id"])
            self.assertIsNotNone(calls[-1]["session_id"])
            self.assertTrue(all(call["thread"] == self.server.runner.thread.ident for call in calls))
            self.assertNotEqual(calls[0]["thread"], self.constructor_thread)

    def test_third_registered_agent_uses_the_same_http_and_background_loop(self):
        self.stop_server()
        defaults = default_registry(self.fake)
        registry = AgentRegistry([*(defaults.get(agent_id) for agent_id in defaults.ids()),
                                  AgentSpec("helper", "Local Helper", partial(self.fake, "helper"), avatar="H")])
        self.start_server(registry)
        status, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["agents"][-1]["id"], "helper")
        self.assertEqual(self.fake.snapshot_calls(), [])
        room_id = self.create_room("helper", "third-http")["room"]["id"]
        self.settle()
        status, snapshot = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(status, 200)
        self.assertEqual([job["provider"] for job in snapshot["jobs"]], ["helper", "claude", "helper"])
        self.assertEqual(snapshot["room"]["status"], "completed")
        messages = [event for event in snapshot["events"] if event["kind"] == "message"]
        self.assertEqual([event["sender"] for event in messages], ["user", "helper", "claude", "helper"])
        self.assertEqual(self.create_room("helper", "third-http")["room"]["id"], room_id)
        self.assertEqual(len(self.fake.snapshot_calls()), 3)

    def test_get_status_home_list_and_reconnected_snapshot_never_invoke(self):
        for path in ("/", "/api/status", "/api/rooms"):
            self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(self.fake.snapshot_calls(), [])
        room_id = self.create_room()["room"]["id"]
        self.settle()
        count = len(self.fake.snapshot_calls())
        self.stop_server()
        self.start_server()
        for path in ("/api/status", "/api/rooms", f"/api/rooms/{room_id}"):
            self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(len(self.fake.snapshot_calls()), count)

    def test_concurrent_same_request_is_once_and_conflicting_retry_is_rejected(self):
        body = {"goal": "Review a short shared text goal", "target": "claude", "request_id": "same-key"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.request("POST", "/api/rooms", body), range(2)))
        self.assertTrue(all(status == 200 for status, _ in replies))
        self.assertEqual(replies[0][1]["room"]["id"], replies[1][1]["room"]["id"])
        self.settle()
        self.assertEqual(len(self.fake.snapshot_calls()), 3)
        status, error = self.request("POST", "/api/rooms", {**body, "goal": "Different goal"})
        self.assertEqual((status, error), (409, {"error": "request_conflict"}))
        _, rooms = self.request("GET", "/api/rooms")
        self.assertEqual(len(rooms["rooms"]), 1)

    def test_http_cancel_waits_for_tracked_fake_cleanup_and_remains_cancelled_after_restart(self):
        self.fake.blocked = {"claude"}
        room_id = self.create_room()["room"]["id"]
        self.assertTrue(self.fake.started.wait(1))
        status, snapshot = self.request("POST", f"/api/rooms/{room_id}/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(snapshot["cancellation_confirmed"])
        self.assertTrue(self.fake.cleaned.is_set())
        self.assertEqual(snapshot["room"]["status"], "cancelled")
        self.stop_server()
        self.fake = PeerCLI()
        self.start_server()
        status, cached = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual((status, cached["room"]["status"]), (200, "cancelled"))
        self.assertEqual(self.fake.snapshot_calls(), [])

    def test_shutdown_interrupts_active_work_and_http_recovery_does_not_replay_it(self):
        self.fake.blocked = {"claude"}
        room_id = self.create_room()["room"]["id"]
        self.assertTrue(self.fake.started.wait(1))
        self.stop_server()
        self.assertTrue(self.fake.cleaned.is_set())
        self.fake = PeerCLI()
        self.start_server()
        status, snapshot = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["room"]["status"], "interrupted")
        self.assertEqual(snapshot["jobs"][0]["status"], "interrupted")
        self.assertEqual(self.fake.snapshot_calls(), [])

    def test_recovery_of_reserved_but_not_dispatched_http_job_never_infers(self):
        # Reproduce the crash window between durable reservation and worker scheduling.
        with patch.object(self.service, "_wake", lambda _room_id: None):
            room_id = self.create_room()["room"]["id"]
        self.assertEqual(self.fake.snapshot_calls(), [])
        self.stop_server()
        self.start_server()
        status, snapshot = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["room"]["status"], "interrupted")
        self.assertTrue(any(event.get("code") == "service_restarted_no_retry" for event in snapshot["events"]))
        self.assertEqual(self.fake.snapshot_calls(), [])


if __name__ == "__main__":
    unittest.main()
