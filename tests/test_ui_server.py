"""Real loopback HTTP tests with a fake bridge; no model or external service."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4
import os
import re
import selectors

from local_agent_coordinator.bridge import BridgeError
from local_agent_coordinator.task_store import StoreError
from local_agent_coordinator.ui_server import create_server, main, MAX_BODY, _HTTPError


TASK = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PARENT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.threads = set()
        self.watchdog_error = None
        self.expiry_seen = threading.Event()
        self.start_entered = threading.Event()
        self.start_finished = threading.Event()
        self.start_gate = asyncio.Event()
        self.block_start = False
        self.start_cancelled = False
        self.shutdowns = 0
        self.block_shutdown = False
        self.shutdown_cancelled = False
        self.error = None

    def record(self, operation, *args):
        self.calls.append((operation, *args))
        self.threads.add(threading.get_ident())
        if self.error is not None:
            raise self.error

    async def status(self):
        self.record("status")
        return {"connected": True, "usage": None}

    def list_tasks(self):
        self.record("list")
        return {"tasks": [{"id": TASK}], "cached": True}

    async def start(self, agent, prompt, request_id, parent_task_id=None):
        self.record("start", agent, prompt, request_id, parent_task_id)
        self.start_entered.set()
        try:
            if self.block_start:
                await self.start_gate.wait()
        except asyncio.CancelledError:
            self.start_cancelled = True
            raise
        finally:
            self.start_finished.set()
        return {"task": {"id": TASK}, "cached": True}

    async def read(self, task_id, refresh=True):
        self.record("read", task_id, refresh)
        return {"task": {"id": task_id}, "cached": not refresh}

    async def reply(self, task_id, prompt):
        self.record("reply", task_id, prompt)
        return {"task": {"id": task_id}}

    async def cancel(self, task_id):
        self.record("cancel", task_id)
        return {"task": {"id": task_id}, "cancellation_confirmed": False}

    async def expire_tasks(self):
        self.expiry_seen.set()

    async def shutdown(self):
        self.shutdowns += 1
        if self.block_shutdown:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.shutdown_cancelled = True
                raise


class LocalUIServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.assets = Path(self.temporary.name)
        (self.assets / "index.html").write_text('<meta name="lac-token" content="__LAC_TOKEN__">')
        (self.assets / "app.js").write_text("'use strict';")
        (self.assets / "markdown.js").write_text("'use strict'; // Reader asset")
        (self.assets / "style.css").write_text("body { color: black; }")
        self.bridge = FakeBridge()
        self.server = create_server(self.bridge, port=0, asset_dir=self.assets)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, body=None, *, token=True, headers=None):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        fields = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            fields["X-LAC-Token"] = self.server.token
        if data is not None:
            fields.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in fields.items():
                connection.putheader(key, value)
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, *args, **kwargs):
        status, headers, body = self.request(*args, **kwargs)
        return status, headers, json.loads(body)

    def test_static_whitelist_token_and_security_headers(self):
        status, headers, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertNotIn(b"__LAC_TOKEN__", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertIn("img-src 'self' data:", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path in ("/app.js", "/markdown.js", "/style.css", "/index.html"):
            self.assertEqual(self.request("GET", path, token=False)[0], 200)
        for path in ("/../bridge.json", "/%2e%2e/bridge.json", "/app.js/anything", "/favicon.ico"):
            self.assertEqual(self.json_request("GET", path, token=False)[0], 404)
        self.assertEqual(self.bridge.calls, [])

    def test_all_apis_require_process_token(self):
        for method, path, body in (("GET", "/api/status", None), ("GET", "/api/tasks", None),
                                   ("GET", f"/api/tasks/{TASK}", None),
                                   ("POST", "/api/tasks", {}),
                                   ("POST", f"/api/tasks/{TASK}/reply", {}),
                                   ("POST", f"/api/tasks/{TASK}/cancel", {})):
            status, _, result = self.json_request(method, path, body, token=False)
            self.assertEqual((status, result), (403, {"error": "token_required"}))
        for token in ("wrong", "caf\xe9"):
            self.assertEqual(self.json_request("GET", "/api/tasks", headers={"X-LAC-Token": token})[0], 403)
        self.assertEqual(self.bridge.calls, [])

    def test_api_credential_hook_rejects_scope_before_bridge_dispatch(self):
        authenticate = self.server.authenticate_api
        observed = []
        def scoped(token, path):
            principal = authenticate(token, path)
            observed.append((principal, path))
            if path == "/api/tasks":
                raise _HTTPError(403, "token_scope_rejected")
            return principal
        with patch.object(self.server, "authenticate_api", side_effect=scoped):
            status, _, result = self.json_request("POST", "/api/tasks", {})
            self.assertEqual((status, result), (403, {"error":"token_scope_rejected"}))
        self.assertEqual(observed, [("user", "/api/tasks")])
        self.assertEqual(self.bridge.calls, [])

    def test_host_and_origin_checked_before_bridge(self):
        for host in ("evil.example", "127.0.0.1", f"127.0.0.1:{self.port + 1}",
                     f"127.0.0.1:{self.port}.evil.example"):
            self.assertEqual(self.json_request("GET", "/api/tasks", headers={"Host": host})[0], 403)
        for origin in ("null", "https://evil.example", f"http://localhost:{self.port + 1}",
                       f"http://127.0.0.1:{self.port}/"):
            self.assertEqual(self.json_request("GET", "/api/tasks", headers={"Origin": origin})[0], 403)
        self.assertEqual(self.bridge.calls, [])
        for host in ("127.0.0.1", "localhost"):
            self.assertEqual(self.request("GET", "/api/tasks", headers={
                "Host": f"{host}:{self.port}", "Origin": f"http://{host}:{self.port}"})[0], 200)

    def test_read_routes_dispatch_on_one_loop_with_explicit_refresh(self):
        self.assertTrue(self.bridge.expiry_seen.wait(1))
        self.assertEqual(self.json_request("GET", "/api/status")[2], {"connected": True, "usage": None})
        self.assertEqual(self.json_request("GET", "/api/tasks")[2]["tasks"], [{"id": TASK}])
        self.assertTrue(self.json_request("GET", f"/api/tasks/{TASK}")[2]["cached"])
        self.assertFalse(self.json_request("GET", f"/api/tasks/{TASK}?refresh=1")[2]["cached"])
        self.assertTrue(self.json_request("GET", f"/api/tasks/{TASK}?refresh=0")[2]["cached"])
        self.assertEqual(self.bridge.calls, [("status",), ("list",), ("read", TASK, False),
                                             ("read", TASK, True), ("read", TASK, False)])
        self.assertEqual(self.bridge.threads, {self.server.runner.thread.ident})

    def test_mutations_dispatch_only_documented_fields(self):
        status, _, result = self.json_request("POST", "/api/tasks", {
            "agent": "claude", "prompt": "整理文字", "request_id": "stable-request", "parent_task_id": PARENT})
        self.assertEqual(status, 200)
        self.assertEqual(result["task"]["id"], TASK)
        self.assertEqual(self.json_request("POST", f"/api/tasks/{TASK}/reply", {"prompt": "追加"})[0], 200)
        self.assertFalse(self.json_request("POST", f"/api/tasks/{TASK}/cancel", {})[2]["cancellation_confirmed"])
        self.assertEqual(self.bridge.calls, [("start", "claude", "整理文字", "stable-request", PARENT),
                                             ("reply", TASK, "追加"), ("cancel", TASK)])

    def test_unknown_methods_paths_queries_and_commands_rejected(self):
        for method in ("OPTIONS", "PUT", "PATCH", "DELETE", "HEAD"):
            self.assertEqual(self.request(method, "/api/tasks")[0], 405)
        for path in ("/api", "/api/commands", "/api/tasks/native-session-id", f"/api/tasks/{TASK}/shell"):
            self.assertEqual(self.json_request("GET", path)[0], 404)
        for path in (f"/api/tasks/{TASK}?refresh=true", f"/api/tasks/{TASK}?refresh=1&refresh=0",
                     f"/api/tasks/{TASK}?before=secret"):
            self.assertEqual(self.json_request("GET", path)[0], 400)
        self.assertEqual(self.json_request("POST", "/api/tasks?command=run", {})[0], 400)
        self.assertEqual(self.bridge.calls, [])

    def test_start_and_reply_reject_ambiguous_or_extra_arguments(self):
        valid = {"agent": "codex", "prompt": "text", "request_id": "stable"}
        for value in ({}, {**valid, "agent": "deepseek"}, {**valid, "prompt": " "},
                      {**valid, "prompt": "x" * 8001}, {**valid, "prompt": "a\0b"},
                      {**valid, "prompt": "\ud800"},
                      {**valid, "request_id": 3}, {**valid, "command": "echo hello"},
                      {**valid, "parent_task_id": "native-session"}):
            self.assertEqual(self.json_request("POST", "/api/tasks", value)[0], 400)
        for value in ({}, {"prompt": "text", "mode": "full-access"}, {"prompt": False}):
            self.assertEqual(self.json_request("POST", f"/api/tasks/{TASK}/reply", value)[0], 400)
        self.assertEqual(self.json_request("POST", f"/api/tasks/{TASK}/cancel", {"force": True})[0], 400)
        self.assertEqual(self.bridge.calls, [])

    def test_body_requires_bounded_length_json_object_and_unique_fields(self):
        cases = [(None, {}, 411), (b"{}", {"Content-Type": "text/plain"}, 415),
                 (b"", {"Content-Length": str(MAX_BODY + 1)}, 413),
                 (b"{}", {"Transfer-Encoding": "chunked"}, 400),
                 (b"{", {}, 400), (b"\xff", {}, 400), ([], {}, 400),
                 (b'{"agent":"claude","agent":"codex"}', {}, 400),
                 (b'{"value":NaN}', {}, 400)]
        for body, headers, expected in cases:
            with self.subTest(body=body, headers=headers):
                self.assertEqual(self.json_request("POST", "/api/tasks", body, headers=headers)[0], expected)
        self.assertEqual(self.bridge.calls, [])

    def test_duplicate_host_or_token_headers_rejected(self):
        for header, value in (("Host", f"localhost:{self.port}"), ("X-LAC-Token", self.server.token),
                              ("Origin", f"http://localhost:{self.port}")):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                connection.putrequest("GET", "/api/tasks", skip_host=True)
                connection.putheader("Host", f"127.0.0.1:{self.port}")
                connection.putheader("X-LAC-Token", self.server.token)
                if header == "Origin":
                    connection.putheader("Origin", f"http://localhost:{self.port}")
                connection.putheader(header, value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertEqual(json.loads(response.read()), {"error": "invalid_headers"})
            finally:
                connection.close()
        self.assertEqual(self.bridge.calls, [])

    def test_transport_accepts_large_wire_json_without_changing_decoded_text(self):
        # Exercise the shared real HTTP parser, independently of room message
        # validation. The legacy bridge's own 8000-character policy is unchanged.
        for index, text in enumerate(("豆" * 6000, "\x01" * 8000)):
            payload = {"agent": "codex", "prompt": text, "request_id": f"wire-{index}"}
            raw = json.dumps(payload).encode()
            self.assertGreater(len(raw), 16 * 1024)
            self.assertEqual(self.json_request("POST", "/api/tasks", raw)[0], 200)
            self.assertIn(text, self.bridge.calls[-1])
        payload = json.dumps({"agent": "codex", "prompt": "wire boundary", "request_id": "wire-max"}).encode()
        raw = b" " * (MAX_BODY - len(payload)) + payload
        self.assertEqual(len(raw), MAX_BODY)
        self.assertEqual(self.json_request("POST", "/api/tasks", raw)[0], 200)

    def test_errors_expose_only_fixed_codes_and_valid_task_id(self):
        stderr = io.StringIO()
        for error, expected in ((StoreError("task_not_found"), (404, {"error": "task_not_found"})),
                                (BridgeError("command_timeout", TASK),
                                 (409, {"error": "command_timeout", "task_id": TASK})),
                                (BridgeError("RAW_PRIVATE_ERROR", "RAW_PRIVATE_ID"),
                                 (409, {"error": "operation_failed"})),
                                (RuntimeError("RAW_PRIVATE_STDERR"), (500, {"error": "operation_failed"}))):
            self.bridge.error = error
            with redirect_stderr(stderr):
                status, _, body = self.json_request("GET", "/api/tasks")
            self.assertEqual((status, body), expected)
        self.assertEqual(stderr.getvalue(), "")

    def test_page_disconnect_keeps_work_and_server_alive(self):
        self.bridge.block_start = True
        body = json.dumps({"agent": "claude", "prompt": "text", "request_id": "disconnected"}).encode()
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        connection.sendall((f"POST /api/tasks HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                            f"X-LAC-Token: {self.server.token}\r\nContent-Type: application/json\r\n"
                            f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        self.assertTrue(self.bridge.start_entered.wait(1))
        connection.close()
        self.server.runner.loop.call_soon_threadsafe(self.bridge.start_gate.set)
        self.assertTrue(self.bridge.start_finished.wait(1))
        self.assertFalse(self.bridge.start_cancelled)
        self.assertEqual(self.bridge.shutdowns, 0)
        self.assertEqual(self.json_request("GET", "/api/status")[0], 200)

    def test_server_close_cancels_inflight_then_shuts_bridge_once(self):
        self.bridge.block_start = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.json_request, "POST", "/api/tasks", {
                "agent": "codex", "prompt": "text", "request_id": "closing"})
            self.assertTrue(self.bridge.start_entered.wait(1))
            self.server.shutdown()
            self.server.server_close()
            self.assertTrue(self.bridge.start_cancelled)
            self.assertEqual(self.bridge.shutdowns, 1)
            self.assertFalse(self.server.runner.thread.is_alive())
            self.assertEqual(result.result(timeout=2)[0], 503)
        self.server.server_close()
        self.assertEqual(self.bridge.shutdowns, 1)

    def test_request_timeout_cancels_operation_without_claiming_remote_stop(self):
        self.bridge.block_start = True
        self.server.runner.operation_timeout = 0.05
        status, _, result = self.json_request("POST", "/api/tasks", {
            "agent": "claude", "prompt": "text", "request_id": "timed-out"})
        self.assertEqual((status, result), (504, {"error": "operation_timeout"}))
        self.assertTrue(self.bridge.start_finished.wait(1))
        self.assertTrue(self.bridge.start_cancelled)
        self.assertEqual(self.bridge.shutdowns, 0)
        self.assertNotIn("cancellation_confirmed", result)
        self.assertEqual(self.json_request("GET", "/api/status")[0], 200)

    def test_shutdown_has_total_stop_budget_and_reports_unconfirmed_cleanup(self):
        self.bridge.block_shutdown = True
        self.server.runner.shutdown_timeout = 0.05
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue(self.bridge.shutdown_cancelled)
        self.assertEqual(self.server.runner.shutdown_error, "shutdown_timeout")
        self.assertFalse(self.server.runner.thread.is_alive())

    def test_assets_freeze_per_instance_and_missing_new_version_fails_safely(self):
        original_index = self.request("GET", "/", token=False)[2]
        original_app = self.request("GET", "/app.js", token=False)[2]
        (self.assets / "index.html").write_text("missing injection marker")
        (self.assets / "app.js").unlink()
        (self.assets / "style.css").write_text("body { color: green; }")
        self.assertEqual(self.request("GET", "/", token=False)[2], original_index)
        self.assertEqual(self.request("GET", "/app.js", token=False)[2], original_app)
        self.assertEqual(self.request("GET", "/style.css", token=False)[2], b"body { color: black; }")

        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.bridge = FakeBridge()
        self.server = create_server(self.bridge, port=0, asset_dir=self.assets)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.assertEqual(self.json_request("GET", "/", token=False)[2], {"error": "assets_unavailable"})
        self.assertEqual(self.json_request("GET", "/app.js", token=False)[0], 503)
        self.assertEqual(self.request("GET", "/style.css", token=False)[2], b"body { color: green; }")

    def test_cli_prints_only_local_url_and_sigterm_closes_cleanly(self):
        repo = self.assets / "repo"
        (repo / "scripts").mkdir(parents=True)
        marker = repo / "cli-was-called"
        (repo / "scripts/dev-cli.ts").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(1)\n")
        config = self.assets / "bridge.json"
        config.write_text(json.dumps({"paradev_repo": str(repo), "bun": sys.executable,
                                      "workspace_id": str(uuid4()),
                                      "state_path": str(self.assets / "private/tasks.sqlite")}))
        process = subprocess.Popen(
            [sys.executable, "-m", "local_agent_coordinator.ui_server", "--config", str(config), "--port", "0"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "CLI did not emit its local URL")
            url = process.stdout.readline().strip()
            self.assertRegex(url, r"^http://127\.0\.0\.1:[0-9]+/$")
            port = urlsplit(url).port
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            token = re.search(rb'<meta name="lac-token" content="([A-Za-z0-9_-]+)">', response.read())[1].decode()
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/tasks", headers={"X-LAC-Token": token})
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read()), {"tasks": [], "cached": True})
            connection.close()
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual((stdout, stderr), ("", ""))
            self.assertNotIn(token, url)
            self.assertFalse(marker.exists(), "Cached UI startup must not invoke the provider CLI")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_cli_opens_browser_only_with_explicit_flag(self):
        for open_browser in (False, True):
            with self.subTest(open_browser=open_browser):
                server = SimpleNamespace(url="http://127.0.0.1:8768/",
                                         serve_forever=Mock(side_effect=KeyboardInterrupt),
                                         server_close=Mock(), runner=SimpleNamespace(shutdown_error=None))
                arguments = ["lac-ui", "--config", str(self.assets / "bridge.json")]
                if open_browser:
                    arguments.append("--open")
                stdout = io.StringIO()
                with (patch.object(sys, "argv", arguments),
                      patch("local_agent_coordinator.ui_server.BridgeConfig.load"),
                      patch("local_agent_coordinator.ui_server.Bridge"),
                      patch("local_agent_coordinator.ui_server.create_server", return_value=server),
                      patch("local_agent_coordinator.ui_server.webbrowser.open") as browser,
                      redirect_stdout(stdout)):
                    main()
                if open_browser:
                    browser.assert_called_once_with(server.url)
                else:
                    browser.assert_not_called()
                server.serve_forever.assert_called_once_with()
                server.server_close.assert_called_once_with()
                self.assertEqual(stdout.getvalue(), server.url + "\n")


if __name__ == "__main__":
    unittest.main()
