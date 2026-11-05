"""Exercise the independent room API through a real loopback socket."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from local_agent_coordinator.room_server import create_room_server, publish_connection
from local_agent_coordinator.room_service import RoomError


ROOM = "11111111-1111-4111-8111-111111111111"


class FakeRooms:
    def __init__(self):
        self.calls = []
        self.shutdowns = 0
        self.watchdog_error = None
        self.error = None

    def record(self, *call):
        self.calls.append(call)
        if self.error:
            raise self.error

    async def status(self):
        self.record("status")
        return {"app": "local-agent-rooms", "capabilities": {"tools": False}}

    async def list_rooms(self):
        self.record("list")
        return {"rooms": [{"id": ROOM, "goal": "A small isolated task"}]}

    async def read_room(self, room_id):
        self.record("read", room_id)
        return {"room": {"id": room_id}, "events": [], "members": []}

    async def create_room(self, goal, target, request_id, **extra):
        if target not in ('claude', 'codex', 'all', 'both'):
            raise RoomError('invalid_target')
        self.record("create", goal, target, request_id, *([extra] if extra else []))
        return {"room": {"id": ROOM}}

    async def send_message(self, room_id, text, target, request_id, **extra):
        self.record("message", room_id, text, target, request_id, *([extra] if extra else []))
        return {"room": {"id": room_id}}

    async def start_run(self, room_id, text, target, request_id, **extra):
        self.record("run", room_id, text, target, request_id, extra)
        return {"room": {"id": room_id}}

    async def cancel_room(self, room_id):
        self.record("cancel", room_id)
        return {"room": {"id": room_id, "status": "cancelled"}}

    async def expire_tasks(self):
        pass

    async def shutdown(self):
        self.shutdowns += 1


class RoomHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.assets = Path(self.temp.name)
        (self.assets / "index.html").write_text(
            '<title>Rooms</title><meta name="lac-token" content="__LAC_TOKEN__">')
        (self.assets / "app.js").write_text("'use strict';")
        (self.assets / "style.css").write_text("body { color: black; }")
        self.service = FakeRooms()
        self.server = create_room_server(self.service, port=0, asset_dir=self.assets)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, token=True, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        fields = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            fields["X-LAC-Token"] = self.server.mcp_token if path.startswith("/api/mcp/") else self.server.token
        if data is not None:
            fields.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in fields.items():
                connection.putheader(name, value)
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_work_modes_are_explicit_and_strict_on_create_and_continue(self):
        create = {"goal": "Discuss scope", "target": "claude", "request_id": "mode-create",
                  "mode": "text_artifacts", "work_mode": "plan"}
        status, _, _ = self.request("POST", "/api/rooms", create)
        self.assertEqual(status, 200)
        self.assertEqual(self.service.calls[-1][-1], {"mode": "text_artifacts", "work_mode": "plan"})
        continuation = {"text": "Implement it", "target": "all", "request_id": "mode-run",
            "run_id": ROOM, "revision": 0, "digest": "0" * 64, "retry": False, "work_mode": "build"}
        for prefix in ("/api/rooms/", "/api/mcp/rooms/"):
            status, _, _ = self.request("POST", prefix + ROOM + "/runs", continuation,
                headers={"X-LAC-Instance": self.server.instance_id} if "mcp" in prefix else {})
            self.assertEqual(status, 200)
            self.assertEqual(self.service.calls[-1][-1]["work_mode"], "build")
        calls = len(self.service.calls)
        for invalid in (None, "legacy", "", "Build", True, [], {}):
            for path, body in (("/api/rooms", create), ("/api/rooms/" + ROOM + "/runs", continuation)):
                status, _, _ = self.request("POST", path, {**body, "work_mode": invalid})
                self.assertEqual(status, 400, repr(invalid))
        self.assertEqual(len(self.service.calls), calls)

    def test_opening_home_and_reading_rooms_never_dispatches_a_job(self):
        status, headers, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertNotIn(b"__LAC_TOKEN__", body)
        self.assertIn(self.server.token.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.service.calls, [])
        status, _, _ = self.request("GET", "/api/rooms")
        self.assertEqual(status, 200)
        self.assertEqual(self.service.calls, [("list",)])

    def test_room_api_retains_loopback_token_host_and_origin_checks(self):
        for options, code in [
            ({"token": False}, "token_required"),
            ({"headers": {"Host": "attacker.invalid"}}, "host_rejected"),
            ({"headers": {"Origin": "https://attacker.invalid"}}, "origin_rejected"),
        ]:
            status, _, body = self.request("GET", "/api/status", **options)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["error"], code)
        self.assertEqual(self.service.calls, [])

    def test_create_follow_up_and_cancel_route_to_one_room_service(self):
        status, _, _ = self.request("POST", "/api/rooms",
                                    {"goal": "Review a small list", "target": "both", "request_id": "req1"})
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", f"/api/rooms/{ROOM}/messages",
                                    {"text": "Keep it concise", "target": "codex", "request_id": "req2"})
        self.assertEqual(status, 200)
        status, _, body = self.request("POST", f"/api/rooms/{ROOM}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["room"]["status"], "cancelled")
        self.assertEqual(self.service.calls, [
            ("create", "Review a small list", "both", "req1"),
            ("message", ROOM, "Keep it concise", "codex", "req2"),
            ("cancel", ROOM),
        ])

    def test_invalid_targets_extra_fields_and_queries_have_no_side_effect(self):
        cases = [
            {"goal": "Do something", "target": ["claude"], "request_id": "req1"},
            {"goal": "Do something", "target": "bad/target", "request_id": "req1"},
            {"goal": "Do something", "target": "claude", "request_id": "req1", "workspace": "/private"},
        ]
        for body in cases:
            status, _, result = self.request("POST", "/api/rooms", body)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(result)["error"], "invalid_arguments")
        status, _, _ = self.request("GET", f"/api/rooms/{ROOM}?refresh=1")
        self.assertEqual(status, 400)
        self.assertEqual(self.service.calls, [])

    def test_room_failures_do_not_leak_private_error_text(self):
        self.service.error = RoomError("room_not_found")
        status, _, body = self.request("GET", f"/api/rooms/{ROOM}")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "room_not_found"})
        self.service.error = RuntimeError("SECRET private-path")
        status, _, body = self.request("GET", "/api/rooms")
        self.assertEqual(status, 500)
        self.assertNotIn(b"SECRET", body)

    def test_close_runs_owned_service_cleanup_once(self):
        self.server.server_close()
        self.server.server_close()
        self.assertEqual(self.service.shutdowns, 1)

    def test_mcp_routes_bind_instance_and_cannot_choose_a_sender(self):
        body = {"goal": "External text task", "target": "claude", "request_id": "mcp-one"}
        status, _, result = self.request("POST", "/api/mcp/rooms", body)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(result), {"error": "stale_instance"})
        self.assertEqual(self.service.calls, [])
        headers = {"X-LAC-Instance": self.server.instance_id}
        status, _, _ = self.request("POST", "/api/mcp/rooms", body, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.service.calls[-1], ("create", "External text task", "claude", "mcp-one", {"source": "mcp"}))
        status, _, result = self.request("GET", "/api/mcp/status", headers=headers)
        self.assertEqual(json.loads(result)['instance_id'], self.server.instance_id)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/mcp/rooms", {**body, "source": "codex"}, headers=headers)
        self.assertEqual(status, 400)

    def test_connection_capability_is_private_and_replaces_stale_file(self):
        target = self.assets / 'connection.json'
        target.write_text('stale')
        result = publish_connection(self.server, self.assets)
        self.assertEqual(result, target)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text()), {
            'url': self.server.url, 'token': self.server.mcp_token, 'instance_id': self.server.instance_id})
        self.assertFalse(list(self.assets.glob('.connection-*')))


if __name__ == "__main__":
    unittest.main()


class StartupTests(unittest.TestCase):
    def test_an_occupied_port_names_the_port_without_a_traceback(self):
        # Catches: a busy default port crashing with a traceback (server_close
        # touched a loop that never started) instead of saying what to do.
        import socket
        import subprocess
        import sys
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
            result = subprocess.run([sys.executable, "-m", "local_agent_coordinator.room_server",
                                     "--runtime", str(Path(root) / "rooms"), "--port", str(port)],
                                    capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"Port {port} is already in use", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")
