"""Real stdio MCP and loopback HTTP; no desktop, room engine or provider is started."""
import asyncio
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlsplit, parse_qs


HAS_MCP = importlib.util.find_spec("mcp") is not None
if HAS_MCP:
    from mcp import Client, StdioServerParameters
    from local_agent_coordinator.room_mcp import RoomClientError, RoomHTTPClient


ROOM_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"


class FakeHTTP(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.instance_id = str(uuid4())
        self.mode = "normal"
        self.requests = []
        self.creates = 0
        self.room = None
        self.paging = False
        self.page_events = []
        self.projects = [{"id": PROJECT_ID, "name": "Example score project", "path": "/explicit/example/project",
                          "files": ["src/score.ts"], "created_at": 1}]
        self.partners = [{"id": value, "name": value.title(), "enabled": True,
                          "health": {"state": "unknown"}, "monitor": {"state": "idle"}}
                         for value in ("claude", "codex", "helper", "writer")]
        self.release = threading.Event()
        super().__init__(("127.0.0.1", 0), Handler)

    def discovery(self):
        return {"url": f"http://127.0.0.1:{self.server_port}/", "token": self.token,
                "instance_id": self.instance_id}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_request(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))) if self.command == "POST" else None
        server.requests.append((self.command, self.path, body))
        if self.headers.get("X-LAC-Token") != server.token:
            self.reply(403, {"error": "token_required"})
            return
        if self.headers.get("X-LAC-Instance") != server.instance_id:
            self.reply(409, {"error": "stale_instance"})
            return
        if server.mode == "hang":
            server.release.wait(2)
            return
        if server.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/PRIVATE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if server.mode == "large":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10000")
            self.end_headers()
            return
        if server.mode == "error":
            self.reply(500, {"error": "Bearer PRIVATE_PROVIDER_DETAIL " + server.token})
            return
        if server.mode == "invalid_participants":
            self.reply(400, {"error": "invalid_participants"})
            return
        if server.mode == "project_not_found":
            self.reply(404, {"error": "project_not_found"})
            return
        if server.mode == "project_author_required":
            self.reply(400, {"error": "project_requires_first_author"})
            return
        if server.mode == "project_initialization_unknown":
            self.reply(503, {"error": "project_initialization_unconfirmed"})
            return
        if server.mode == "execution_initialization_unknown":
            self.reply(503, {"error": "execution_initialization_unconfirmed"})
            return
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        if server.paging and path == "/api/mcp/status":
            self.reply(200, {"app": "local-agent-rooms", "instance_id": server.instance_id,
                             "capabilities": {"room_paging_v1": True}})
            return
        if server.paging and path == "/api/mcp/rooms" and query == {"view": ["compact"]}:
            self.reply(200, {"projection": "room_list_v1", "rooms": [{"id": ROOM_ID, "goal_preview": "Goal"}]})
            return
        if server.paging and path == "/api/mcp/partners" and query == {"view": ["compact"]}:
            self.reply(200, {"projection": "partner_list_v1", "partners": server.partners})
            return
        if server.paging and path == "/api/mcp/rooms/" + ROOM_ID + "/jobs":
            self.reply(200, {"job": {"id": query["job_id"][0], "status": "completed"}})
            return
        if server.paging and path == "/api/mcp/rooms/" + ROOM_ID + "/runs" and self.command == "GET":
            self.reply(200, {"run": {"id": query["run_id"][0], "goal_preview": "Earlier original"}})
            return
        if server.paging and path == "/api/mcp/rooms/" + ROOM_ID + "/state":
            self.reply(200, {"projection": "room_state_v1", "room": {"id": ROOM_ID},
                             "through_seq": 4, "cancellation_confirmed": False})
            return
        if server.paging and path == "/api/mcp/rooms/" + ROOM_ID + "/events":
            self.reply(200, {"events": server.page_events, "through_seq": 4,
                             "has_before": False, "has_after": False, "oversize": True})
            return
        if server.paging and path == "/api/mcp/rooms/" + ROOM_ID + "/text":
            self.reply(200, {"room_id": ROOM_ID, "kind": query["kind"][0],
                             "sha256": query["sha256"][0], "text": "Exact original", "truncated": False})
            return
        if server.paging and self.command == "POST" and query == {"response": ["ack"]}:
            if path == "/api/mcp/rooms":
                server.creates += 1
            if server.mode == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.reply(200, {"acknowledged": True, "room_id": ROOM_ID,
                             "request_id": body.get("request_id"), "state_url": "/api/rooms/" + ROOM_ID + "/state"})
            return
        if self.path == "/api/mcp/status":
            self.reply(200, {"app": "local-agent-rooms", "instance_id": server.instance_id})
        elif self.path == "/api/mcp/projects" and self.command == "GET":
            self.reply(200, {"projects": server.projects})
        elif self.path == "/api/mcp/partners" and self.command == "GET":
            self.reply(200, {"partners": server.partners, "scope": "coordinator_owned_only"})
        elif self.path == "/api/mcp/rooms" and self.command == "GET":
            self.reply(200, {"rooms": [server.room["room"]] if server.room else []})
        elif self.path == "/api/mcp/rooms" and self.command == "POST":
            if server.room is None:
                server.creates += 1
                server.room = {"room": {"id": ROOM_ID, "status": "running"},
                               "events": [{"sender": "mcp", "text": body["goal"]}]}
            if server.mode == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.reply(200, server.room)
        elif self.path == "/api/mcp/rooms/" + ROOM_ID:
            self.reply(200, server.room)
        elif self.path == "/api/mcp/rooms/" + ROOM_ID + "/messages":
            if server.mode == "message_ack":
                self.reply(200, {"accepted": True})
            else:
                server.room["events"].append({"sender": "mcp", "text": body["text"]})
                self.reply(200, server.room)
        elif self.path == "/api/mcp/rooms/" + ROOM_ID + "/runs" and server.mode == "message_ack":
            self.reply(200, {"accepted": True})
        elif self.path == "/api/mcp/rooms/" + ROOM_ID + "/cancel":
            server.room["room"]["status"] = "cancelled"
            self.reply(200, {**server.room, "cancellation_confirmed": True})
        else:
            self.reply(404, {"error": "route_not_found"})

    do_GET = handle_request
    do_POST = handle_request


@unittest.skipUnless(HAS_MCP, "Install the MCP extra to run real stdio tests")
class RoomMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.servers = []
        self.http = self.start_http()
        self.connection = self.root / "connection.json"
        self.write_connection(self.http.discovery())

    def tearDown(self):
        for server, thread in self.servers:
            server.release.set()
            server.shutdown()
            thread.join(2)
            server.server_close()
        self.directory.cleanup()

    def start_http(self):
        server = FakeHTTP()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.servers.append((server, thread))
        return server

    def write_connection(self, value):
        self.connection.write_text(json.dumps(value))
        self.connection.chmod(0o600)

    def parameters(self):
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "local_agent_coordinator.room_mcp", "--connection", str(self.connection)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})

    async def test_real_stdio_tools_roundtrip_and_client_disconnect_does_not_stop_app(self):
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({tool.name for tool in tools},
                             {"room_status", "room_list", "room_start", "room_read", "room_send", "room_cancel",
                              "project_list", "partner_list", "room_continue", "room_export", "room_events", "room_text",
                              "room_job", "room_run"})
            for tool in tools:
                schema = tool.input_schema
                self.assertTrue(all(name not in schema.get("properties", {})
                                    for name in ("sender", "source", "workspace", "session_id", "command")))
            status = await client.call_tool("room_status", {})
            self.assertFalse(status.is_error)
            self.assertEqual(status.structured_content["instance_id"], self.http.instance_id)
            arguments = {"goal": "A text goal", "target": "claude", "request_id": "stable-start"}
            first = await client.call_tool("room_start", arguments)
            repeated = await client.call_tool("room_start", arguments)
            self.assertEqual(first.structured_content["room"]["id"], ROOM_ID)
            self.assertEqual(repeated.structured_content["room"]["id"], ROOM_ID)
            self.assertEqual(self.http.creates, 1)
            starts = [body for method, path, body in self.http.requests
                      if method == "POST" and path == "/api/mcp/rooms"]
            self.assertEqual(starts, [arguments, arguments])
            sent = await client.call_tool("room_send", {"room_id": ROOM_ID, "text": "Clarification",
                                                       "target": "codex", "request_id": "stable-message"})
            self.assertEqual(sent.structured_content["events"][-1]["sender"], "mcp")
            self.assertFalse((await client.call_tool("room_read", {"room_id": ROOM_ID})).is_error)
            self.assertEqual(len((await client.call_tool("room_list", {})).structured_content["rooms"]), 1)
            cancelled = await client.call_tool("room_cancel", {"room_id": ROOM_ID})
            self.assertTrue(cancelled.structured_content["cancellation_confirmed"])
        self.assertTrue(all(path.startswith("/api/mcp/") for _, path, _ in self.http.requests))
        self.assertEqual(self.http.requests[-1][2], {})
        self.assertEqual((await RoomHTTPClient(self.connection).status())["app"], "local-agent-rooms")

    async def test_unknown_mutation_is_not_retried_and_remote_diagnostics_are_not_exposed(self):
        self.http.mode = "drop"
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            result = await client.call_tool("room_start", {"goal": "Goal", "target": "both", "request_id": "lost"})
            self.assertTrue(result.is_error)
            wire = str(result)
            self.assertIn("remote_state_unknown=true", wire)
            self.assertEqual(len(self.http.requests), 1)
            self.assertEqual(self.http.creates, 1)
            self.http.mode = "error"
            result = await client.call_tool("room_send", {"room_id": ROOM_ID, "text": "Info",
                                                         "target": "both", "request_id": "error"})
            self.assertTrue(result.is_error)
            self.assertIn("remote_error", str(result))
            self.assertNotIn("PRIVATE_PROVIDER_DETAIL", str(result))
            self.assertNotIn(self.http.token, str(result))
            self.assertEqual(len(self.http.requests), 2)

    async def test_artifact_mode_and_recoverable_or_unknown_project_errors_cross_stdio(self):
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            arguments = {"goal": "A new file proposal", "target": "claude", "request_id": "artifact",
                         "mode": "python_artifacts"}
            result = await client.call_tool("room_start", arguments)
            self.assertFalse(result.is_error)
            self.assertEqual(self.http.requests[-1][2]["mode"], "python_artifacts")
            self.http.mode = "project_author_required"
            result = await client.call_tool("room_start", {**arguments, "target": "all", "request_id": "all"})
            self.assertTrue(result.is_error)
            self.assertIn("project_requires_first_author", str(result))
            self.assertIn("remote_state_unknown=false", str(result))
            self.http.mode = "project_initialization_unknown"
            result = await client.call_tool("room_start", {**arguments, "request_id": "unknown"})
            self.assertTrue(result.is_error)
            self.assertIn("project_initialization_unconfirmed", str(result))
            self.assertIn("remote_state_unknown=true", str(result))
            self.assertEqual(len(self.http.requests), 3, "no automatic retry")

    async def test_sandbox_mode_and_unknown_initialization_cross_stdio_without_retry(self):
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            arguments = {"goal": "Create a new isolated toy and run stdlib tests", "target": "codex",
                         "request_id": "sandbox", "mode": "python_sandbox"}
            result = await client.call_tool("room_start", arguments)
            self.assertFalse(result.is_error)
            self.assertEqual(self.http.requests[-1][2]["mode"], "python_sandbox")
            self.http.mode = "execution_initialization_unknown"
            result = await client.call_tool("room_start", {**arguments, "request_id": "sandbox-unknown"})
            self.assertTrue(result.is_error)
            self.assertIn("execution_initialization_unconfirmed", str(result))
            self.assertIn("remote_state_unknown=true", str(result))
            self.assertEqual(len(self.http.requests), 2)


    async def test_project_and_partner_catalogues_are_readonly_real_stdio_tools(self):
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("project_list", "partner_list"):
                annotations = tools[name].annotations
                self.assertTrue(annotations.read_only_hint)
                self.assertFalse(annotations.destructive_hint)
                self.assertFalse(annotations.open_world_hint)
                self.assertEqual(tools[name].input_schema.get("properties", {}), {})
            projects = await client.call_tool("project_list", {})
            partners = await client.call_tool("partner_list", {})
            self.assertFalse(projects.is_error)
            self.assertFalse(partners.is_error)
            self.assertEqual(projects.structured_content, {"projects": self.http.projects})
            self.assertEqual(partners.structured_content["partners"], self.http.partners)
            self.assertEqual(partners.structured_content["scope"], "coordinator_owned_only")
            self.assertNotIn("project_register", tools)
            self.assertNotIn("partner_add", tools)
            self.assertNotIn("partner_enable", tools)
            self.assertNotIn("partner_check", tools)
        self.assertEqual([(method, path) for method, path, _ in self.http.requests],
                         [("GET", "/api/mcp/projects"), ("GET", "/api/mcp/partners")])
        self.assertEqual(self.http.creates, 0)

    async def test_selected_participants_and_project_id_cross_stdio_unchanged(self):
        arguments = {"goal": "Review the selected score file", "target": "codex", "request_id": "selected-project",
                     "mode": "project_files", "participants": ["helper", "codex"], "project_id": PROJECT_ID}
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            result = await client.call_tool("room_start", arguments)
            self.assertFalse(result.is_error)
            self.assertEqual(self.http.requests, [("POST", "/api/mcp/rooms", arguments)])
            self.assertEqual(result.structured_content["room"]["id"], ROOM_ID)
            # A later user clarification has no participant/project mutation fields.
            message = {"room_id": ROOM_ID, "text": "Keep the scope small", "target": "helper", "request_id": "clarify"}
            result = await client.call_tool("room_send", message)
            self.assertFalse(result.is_error)
            self.assertEqual(self.http.requests[-1][2], {key: message[key] for key in ("text", "target", "request_id")})
            self.assertNotIn("participants", self.http.requests[-1][2])
            self.assertNotIn("project_id", self.http.requests[-1][2])
        self.assertEqual(self.http.creates, 1)

    async def test_work_mode_crosses_stdio_and_rejects_invalid_explicit_values(self):
        create = {"goal": "Discuss the implementation", "target": "codex", "request_id": "work-plan",
                  "mode": "text_artifacts", "work_mode": "plan"}
        continuation = {"room_id": ROOM_ID, "text": "Implement it", "target": "all", "request_id": "work-build",
            "run_id": PROJECT_ID, "revision": 0, "digest": "0" * 64, "retry": False, "work_mode": "build"}
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            result = await client.call_tool("room_start", create)
            self.assertFalse(result.is_error)
            self.assertEqual(self.http.requests[-1][2], create)
            # This HTTP fixture does not implement runs, but the actual stdio
            # transport must preserve its explicit mode and stable request.
            await client.call_tool("room_continue", continuation)
            self.assertEqual(self.http.requests[-1][2], {key: value for key, value in continuation.items() if key != "room_id"})
            count = len(self.http.requests)
            for invalid in (None, "legacy", "", True, [], {}):
                result = await client.call_tool("room_start", {**create, "work_mode": invalid})
                self.assertTrue(result.is_error, repr(invalid))
                result = await client.call_tool("room_continue", {**continuation, "work_mode": invalid})
                self.assertTrue(result.is_error, repr(invalid))
            self.assertEqual(len(self.http.requests), count)

    async def test_selected_room_rejections_cross_stdio_once_without_auto_retry(self):
        arguments = {"goal": "Review a selected file", "target": "codex", "request_id": "selected-rejected",
                     "mode": "project_files", "participants": ["codex", "helper"], "project_id": PROJECT_ID}
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            for number, (mode, code, unknown) in enumerate([
                ("invalid_participants", "invalid_participants", "false"),
                ("project_not_found", "project_not_found", "false"),
                ("project_initialization_unknown", "project_initialization_unconfirmed", "true"),
            ], 1):
                self.http.mode = mode
                result = await client.call_tool("room_start", {**arguments, "request_id": f"reject-{number}"})
                self.assertTrue(result.is_error)
                self.assertIn(code, str(result))
                self.assertIn("remote_state_unknown=" + unknown, str(result))
                self.assertEqual(len(self.http.requests), number, "one transport attempt per explicit call")
        self.assertEqual(self.http.creates, 0)

    async def test_invalid_participant_shapes_and_project_ids_do_not_dispatch_http(self):
        arguments = {"goal": "Goal", "target": "codex", "request_id": "invalid-selection"}
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            for participants in ([], ["codex", "codex"], ["all"], ["both"], [None], [{}], [[]], "codex",
                                 ["bad/id"], ["codex"] * 9):
                result = await client.call_tool("room_start", {**arguments, "participants": participants})
                self.assertTrue(result.is_error, repr(participants))
            for project_id in ("../project", "not-a-uuid", "", 3):
                result = await client.call_tool("room_start", {**arguments, "mode": "project_files", "project_id": project_id})
                self.assertTrue(result.is_error, repr(project_id))
        self.assertEqual(self.http.requests, [])

    async def test_client_invalid_participant_values_are_stable_errors_before_network(self):
        client = RoomHTTPClient(self.connection)
        for participants in ([], ["codex", "codex"], ["all"], ["both"], [None], [{}], [[]], "codex", ["bad/id"]):
            with self.subTest(participants=participants):
                with self.assertRaises(RoomClientError) as error:
                    await client.start("Goal", "codex", "invalid-client-selection", participants=participants)
                self.assertEqual(error.exception.code, "invalid_arguments")
                self.assertFalse(error.exception.remote_state_unknown)
        self.assertEqual(self.http.requests, [])

    async def test_missing_or_replaced_discovery_never_switches_to_another_service(self):
        other = self.start_http()
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            self.assertFalse((await client.call_tool("room_status", {})).is_error)
            count = len(self.http.requests)
            self.connection.unlink()
            unavailable = await client.call_tool("room_list", {})
            self.assertTrue(unavailable.is_error)
            self.assertIn("connection_unavailable", str(unavailable))
            self.write_connection(other.discovery())
            changed = await client.call_tool("room_start", {"goal": "Goal", "target": "codex", "request_id": "changed"})
            self.assertTrue(changed.is_error)
            self.assertIn("connection_changed", str(changed))
            self.assertEqual(len(self.http.requests), count)
            self.assertEqual(other.requests, [])

    async def test_paged_app_uses_small_ack_and_explicit_original_history(self):
        self.http.paging = True
        self.http.page_events = [{"seq": 4, "sender": "user", "text": "\x01" * (1024 * 1024)}]
        client = RoomHTTPClient(self.connection)
        await client.status()
        text = "  " + "协作\n" * 4000 + "  "
        ack = await client.send(ROOM_ID, text, "codex", "stable-long")
        self.assertTrue(ack["acknowledged"])
        self.assertEqual(self.http.requests[-1], ("POST", "/api/mcp/rooms/" + ROOM_ID + "/messages?response=ack",
                         {"text": text, "target": "codex", "request_id": "stable-long"}))
        state = await client.read(ROOM_ID)
        self.assertEqual(state["projection"], "room_state_v1")
        self.assertNotIn("events", state)
        page = await client.events(ROOM_ID, after_seq=0, through_seq=4, limit=1)
        self.assertEqual(page["events"], self.http.page_events)
        self.assertIn("after_seq=0&through_seq=4&limit=1", self.http.requests[-1][1])
        self.assertEqual(len(self.http.requests), 4)

    async def test_paged_unknown_ack_is_not_retried_or_recreated(self):
        self.http.paging = True
        client = RoomHTTPClient(self.connection)
        await client.status()
        self.http.mode = "drop"
        with self.assertRaises(RoomClientError) as error:
            await client.start("Goal", "codex", "stable")
        self.assertTrue(error.exception.remote_state_unknown)
        self.assertEqual(self.http.creates, 1)
        self.assertEqual(len(self.http.requests), 2)

    async def test_paged_room_list_explicitly_uses_compact_view(self):
        client = RoomHTTPClient(self.connection)
        self.assertEqual(await client.list_rooms(), {"rooms": []})
        self.assertEqual(self.http.requests[-1][1], "/api/mcp/rooms")
        self.http.paging = True
        await client.status()
        result = await client.list_rooms()
        self.assertEqual(result["projection"], "room_list_v1")
        self.assertEqual(self.http.requests[-1][1], "/api/mcp/rooms?view=compact")

    async def test_compact_partners_and_typed_historical_details(self):
        client = RoomHTTPClient(self.connection)
        await client.partners()
        self.assertEqual(self.http.requests[-1][1], "/api/mcp/partners")
        self.http.paging = True
        await client.status()
        self.assertEqual((await client.partners())["projection"], "partner_list_v1")
        self.assertEqual(self.http.requests[-1][1], "/api/mcp/partners?view=compact")
        self.assertEqual((await client.job(ROOM_ID, PROJECT_ID))["job"]["id"], PROJECT_ID)
        self.assertEqual((await client.run(ROOM_ID, PROJECT_ID))["run"]["id"], PROJECT_ID)
        self.assertEqual(self.http.requests[-1][1], "/api/mcp/rooms/" + ROOM_ID + "/runs?run_id=" + PROJECT_ID + "&view=compact")
        self.assertFalse((await client.text(ROOM_ID, "execution_result", "a" * 64,
                                            execution_id=PROJECT_ID))["truncated"])
        count = len(self.http.requests)
        for operation in (lambda: client.job(ROOM_ID, "../wrong"), lambda: client.run(ROOM_ID, "../wrong"),
                          lambda: client.text(ROOM_ID, "execution_result", "a" * 64, execution_id="../wrong")):
            with self.assertRaises(RoomClientError):
                await operation()
        self.assertEqual(len(self.http.requests), count)

    async def test_real_stdio_paging_tools_preserve_large_original_event(self):
        self.http.paging = True
        original = "\x01" * (1024 * 1024)
        self.http.page_events = [{"seq": 4, "sender": "user", "text": original}]
        async with Client(self.parameters(), read_timeout_seconds=20) as client:
            self.assertFalse((await client.call_tool("room_status", {})).is_error)
            ack = await client.call_tool("room_send", {"room_id": ROOM_ID, "text": "继续",
                                                       "target": "codex", "request_id": "stdio-page"})
            self.assertTrue(ack.structured_content["acknowledged"])
            state = await client.call_tool("room_read", {"room_id": ROOM_ID})
            self.assertEqual(state.structured_content["projection"], "room_state_v1")
            page = await client.call_tool("room_events", {"room_id": ROOM_ID, "through_seq": 4, "limit": 1})
            self.assertFalse(page.is_error)
            self.assertEqual(page.structured_content["events"][0]["text"], original)
            text = await client.call_tool("room_text", {"room_id": ROOM_ID, "kind": "event_text",
                                                        "seq": 4, "sha256": "a" * 64})
            self.assertEqual(text.structured_content["text"], "Exact original")
            job = await client.call_tool("room_job", {"room_id": ROOM_ID, "job_id": PROJECT_ID})
            self.assertEqual(job.structured_content["job"]["id"], PROJECT_ID)
            run = await client.call_tool("room_run", {"room_id": ROOM_ID, "run_id": PROJECT_ID})
            self.assertEqual(run.structured_content["run"]["id"], PROJECT_ID)
            bad = await client.call_tool("room_events", {"room_id": ROOM_ID, "limit": True})
            self.assertTrue(bad.is_error)

    async def test_full_text_requires_exact_typed_reference_and_no_arbitrary_path(self):
        self.http.paging = True
        client = RoomHTTPClient(self.connection)
        await client.status()
        digest = "a" * 64
        result = await client.text(ROOM_ID, "task_result", digest,
                                   run_id=PROJECT_ID, task_id="T1", result_version=2)
        self.assertEqual(result["text"], "Exact original")
        self.assertIn("kind=task_result&run_id=" + PROJECT_ID + "&task_id=T1&result_version=2&sha256=" + digest,
                      self.http.requests[-1][1])
        count = len(self.http.requests)
        for kind, sha, ids in (("path", digest, {}), ("room_goal", digest, {"job_id": ROOM_ID}),
                               ("run_goal", digest, {}), ("room_goal", "../secret", {}),
                               ("event_text", digest, {"seq": True}),
                               ("task_result", digest, {"run_id": PROJECT_ID, "task_id": "../x", "result_version": 1})):
            with self.subTest(kind=kind, ids=ids), self.assertRaises(RoomClientError):
                await client.text(ROOM_ID, kind, sha, **ids)
        self.assertEqual(len(self.http.requests), count)

    async def test_history_cursors_and_custom_response_bound_remain_strict(self):
        client = RoomHTTPClient(self.connection, max_response=512)
        with self.assertRaises(RoomClientError) as error:
            await client.events(ROOM_ID)
        self.assertEqual(error.exception.code, "paging_unavailable")
        self.http.paging = True
        await client.status()
        count = len(self.http.requests)
        for arguments in ({"before_seq": 1, "after_seq": 0}, {"limit": True},
                          {"through_seq": -1}, {"max_bytes": 8 * 1024 * 1024 + 1}):
            with self.subTest(arguments=arguments), self.assertRaises(RoomClientError):
                await client.events(ROOM_ID, **arguments)
        self.assertEqual(len(self.http.requests), count)
        self.http.page_events = [{"seq": 1, "text": "a" * 1024}]
        with self.assertRaises(RoomClientError) as error:
            await client.events(ROOM_ID)
        self.assertEqual(error.exception.code, "response_too_large")
        self.assertFalse(error.exception.remote_state_unknown)

    async def test_explicit_history_page_still_rejects_over_wire_budget(self):
        self.http.paging = True
        self.http.page_events = [{"seq": 1, "text": "a" * (8 * 1024 * 1024)}]
        client = RoomHTTPClient(self.connection)
        await client.status()
        with self.assertRaises(RoomClientError) as error:
            await client.events(ROOM_ID)
        self.assertEqual(error.exception.code, "response_too_large")
        self.assertFalse(error.exception.remote_state_unknown)
        self.assertEqual(len(self.http.requests), 2)

    async def test_connection_permissions_owner_symlink_and_url_are_checked_before_network(self):
        self.connection.chmod(0o644)
        with self.assertRaises(RoomClientError) as error:
            RoomHTTPClient(self.connection)
        self.assertEqual(error.exception.code, "connection_permissions")
        self.connection.chmod(0o600)
        with patch("local_agent_coordinator.room_mcp.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(RoomClientError):
                RoomHTTPClient(self.connection)
        link = self.root / "link.json"
        link.symlink_to(self.connection)
        with self.assertRaises(RoomClientError):
            RoomHTTPClient(link)
        for url in ("http://localhost:8000/", "https://127.0.0.1:8000/", "http://127.0.0.1:8000/path",
                    "http://127.0.0.1:8000/?query=1", "http://user@127.0.0.1:8000/"):
            self.write_connection({**self.http.discovery(), "url": url})
            with self.assertRaises(RoomClientError):
                RoomHTTPClient(self.connection)
        self.connection.write_bytes(b"x" * 4097)
        with self.assertRaises(RoomClientError):
            RoomHTTPClient(self.connection)
        self.connection.write_text("[" * 1500 + "]" * 1500)
        with self.assertRaises(RoomClientError):
            RoomHTTPClient(self.connection)
        fifo = self.root / "discovery-fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(RoomClientError):
            RoomHTTPClient(fifo)
        self.assertEqual(self.http.requests, [])

    async def test_redirect_response_limit_and_total_timeout_are_bounded(self):
        client = RoomHTTPClient(self.connection, timeout=0.03, max_response=512)
        for mode, code in (("redirect", "redirect_rejected"), ("large", "response_too_large"),
                           ("hang", "http_unavailable")):
            self.http.mode = mode
            with self.assertRaises(RoomClientError) as error:
                await client.status()
            self.assertEqual(error.exception.code, code)
            self.assertFalse(error.exception.remote_state_unknown)
        self.http.release.set()
        self.assertEqual(len(self.http.requests), 3)

    async def test_local_argument_and_body_limits_never_dispatch_http(self):
        client = RoomHTTPClient(self.connection)
        for operation in (lambda: client.start("Goal", "../other", "id"),
                          lambda: client.read("../../private"),
                          lambda: client.start("\ud800", "claude", "id"),
                          lambda: client.start("x" * (1024 * 1024 + 1), "claude", "id")):
            with self.assertRaises(RoomClientError):
                await operation()
        self.assertEqual(self.http.requests, [])

    async def test_long_messages_cross_http_without_changing_raw_text(self):
        client = RoomHTTPClient(self.connection)
        text = "  " + "继续协作🫘\n" * 4000 + "  "
        await client.start(text, "claude", "long-start")
        self.http.mode = "message_ack"
        await client.send(ROOM_ID, text, "all", "long-send")
        await client.continue_run(ROOM_ID, text, "all", "long-run", run_id=ROOM_ID,
                                  revision=1, digest="a" * 64)
        self.assertEqual(len(self.http.requests), 3)
        for _, _, body in self.http.requests:
            self.assertEqual(body.get("goal", body.get("text")), text)

    async def test_message_capacity_is_raw_utf8_and_rejects_before_dispatch(self):
        client = RoomHTTPClient(self.connection)
        text = "🫘" * (1024 * 1024 // 4 + 1)
        for operation in (lambda: client.start(text, "claude", "large-start"),
                          lambda: client.send(ROOM_ID, text, "all", "large-send"),
                          lambda: client.continue_run(ROOM_ID, text, "all", "large-run",
                              run_id=ROOM_ID, revision=1, digest="a" * 64)):
            with self.assertRaises(RoomClientError) as error:
                await operation()
            self.assertEqual(error.exception.code, "message_too_large")
            self.assertFalse(error.exception.remote_state_unknown)
        self.assertEqual(self.http.requests, [])

    async def test_json_escaping_fits_valid_message_and_whole_body_stays_bounded(self):
        client = RoomHTTPClient(self.connection)
        self.http.mode = "message_ack"
        text = "\x01" * (1024 * 1024)
        await client.send(ROOM_ID, text, "all", "escaped-message")
        self.assertEqual(self.http.requests[-1][2]["text"], text)
        with self.assertRaises(RoomClientError) as error:
            await client._request("POST", "/api/mcp/rooms", {"extra": "x" * (8 * 1024 * 1024)})
        self.assertEqual(error.exception.code, "request_too_large")
        self.assertFalse(error.exception.remote_state_unknown)
        self.assertEqual(len(self.http.requests), 1)

    async def test_registered_agent_targets_are_not_frozen_in_mcp_schema(self):
        async with Client(self.parameters(), read_timeout_seconds=10) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("room_start", "room_send"):
                target = tools[name].input_schema["properties"]["target"]
                self.assertEqual(target["type"], "string")
                self.assertNotIn("enum", target)
            result = await client.call_tool("room_start", {
                "goal": "A configured third adapter", "target": "helper", "request_id": "third-agent"})
            self.assertFalse(result.is_error)
            self.assertEqual(self.http.requests[-1][2]["target"], "helper")
            result = await client.call_tool("room_send", {
                "room_id": ROOM_ID, "text": "For all room members", "target": "all", "request_id": "all-members"})
            self.assertFalse(result.is_error)

    async def test_server_rejects_stale_instance_without_mutating_or_retrying(self):
        client = RoomHTTPClient(self.connection)
        self.http.instance_id = str(uuid4())
        with self.assertRaises(RoomClientError) as error:
            await client.start("Goal", "claude", "stable")
        self.assertEqual(error.exception.code, "stale_instance")
        self.assertFalse(error.exception.remote_state_unknown)
        self.assertEqual(len(self.http.requests), 1)
        self.assertEqual(self.http.creates, 0)


if __name__ == "__main__":
    unittest.main()
