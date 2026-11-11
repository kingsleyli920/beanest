"""Loopback-only Web entry point for the existing bounded task bridge."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import CancelledError as FutureCancelled, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import signal
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .bridge import Bridge, BridgeConfig, BridgeError
from .message_capacity import MAX_JSON_BODY_BYTES
from .paradev import ParadevError
from .task_store import StoreError


MAX_BODY = MAX_JSON_BODY_BYTES
ASSET_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/view-account.js": ("view-account.js", "text/javascript; charset=utf-8"),
    "/view-manager.js": ("view-manager.js", "text/javascript; charset=utf-8"),
    "/view-setup.js": ("view-setup.js", "text/javascript; charset=utf-8"),
    "/view-timeline.js": ("view-timeline.js", "text/javascript; charset=utf-8"),
    "/boot.js": ("boot.js", "text/javascript; charset=utf-8"),
    "/i18n-en-a.js": ("i18n-en-a.js", "text/javascript; charset=utf-8"),
    "/i18n-en-b.js": ("i18n-en-b.js", "text/javascript; charset=utf-8"),
    "/i18n-en-c.js": ("i18n-en-c.js", "text/javascript; charset=utf-8"),
    "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
    "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
TASK_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TASK_ROUTE = re.compile(rf"/api/tasks/({TASK_ID})(?:/(reply|cancel))?")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; media-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
       "object-src 'none'")
# Only known codes may cross the HTTP boundary, even if an upstream exception changes.
ERROR_CODES = frozenset({
    "invalid_prompt", "agent_not_supported", "invalid_request", "request_conflict",
    "task_not_found", "task_cancelled", "task_not_idle", "task_deadline_exceeded",
    "deadline_exceeded", "task_budget_exceeded", "turn_budget_exceeded",
    "total_turn_budget_exceeded", "parent_depth_exceeded", "session_conflict",
    "session_not_owned", "storage_error", "invalid_result", "invalid_error_code",
    "invalid_provider_status", "invalid_provider_source", "invalid_provider_sequence",
    "invalid_page", "cli_unavailable", "command_timeout", "output_limit",
    "cli_cleanup_failed", "authentication_required", "not_found", "rate_limited",
    "cli_error", "invalid_json", "invalid_response", "journal_reset",
})


class _HTTPError(Exception):
    def __init__(self, status: int, code: str, details: dict | None = None):
        self.status = status
        self.code = code
        self.details = details or {}


class _BridgeLoop:
    """Keep all bridge operations and the expiry watchdog on one event loop."""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.loop = asyncio.new_event_loop()
        self.operations: set[asyncio.Task] = set()
        self.closing = False
        config = getattr(getattr(bridge, "config", None), "paradev", None)
        self.operation_timeout = 3 * getattr(config, "command_timeout", 20) + 5
        self.shutdown_timeout = 15
        self.shutdown_error: str | None = None
        self.watchdog = None
        self.thread = threading.Thread(target=self._run, name="lac-bridge", daemon=True)
        self.thread.start()
        self.call(self._start_watchdog)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        self.loop.close()

    async def _start_watchdog(self):
        self.watchdog = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            try:
                await self.bridge.expire_tasks()
                self.bridge.watchdog_error = None
            except (BridgeError, ParadevError, StoreError) as error:
                self.bridge.watchdog_error = error.code if error.code in ERROR_CODES else "operation_failed"
            except Exception:
                self.bridge.watchdog_error = "operation_failed"
            await asyncio.sleep(2)

    async def _invoke(self, operation):
        if self.closing:
            raise _HTTPError(503, "server_stopping")
        task = asyncio.current_task()
        self.operations.add(task)
        try:
            return await operation()
        finally:
            self.operations.discard(task)

    def call(self, operation):
        if self.closing:
            raise _HTTPError(503, "server_stopping")
        future = asyncio.run_coroutine_threadsafe(self._invoke(operation), self.loop)
        # Cancellation can involve create/send, close, then list: allow their CLI bounds.
        try:
            return future.result(timeout=self.operation_timeout)
        except FutureTimeout:
            future.cancel()
            raise _HTTPError(504, "operation_timeout") from None
        except FutureCancelled:
            raise _HTTPError(503, "server_stopping") from None

    async def _shutdown(self):
        self.closing = True
        if self.watchdog:
            self.watchdog.cancel()
        operations = list(self.operations)
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        if self.watchdog:
            await asyncio.gather(self.watchdog, return_exceptions=True)
        # Closing a page does not call this; only process/server shutdown does.
        try:
            await asyncio.wait_for(self.bridge.shutdown(), self.shutdown_timeout)
        except asyncio.TimeoutError:
            # A cancelled remote stop is not confirmation that the agent stopped.
            self.shutdown_error = "shutdown_timeout"
        except Exception:
            self.shutdown_error = "shutdown_failed"

    def close(self):
        if self.closing:
            return
        self.closing = True
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        try:
            future.result()
        except Exception:
            pass  # No raw provider error or private path is written to stderr.
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()


class LocalUIServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, bridge: Bridge, *, port: int = 8768, asset_dir: Path | None = None,
                 handler_class: type[BaseHTTPRequestHandler] | None = None):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid local port")
        self.bridge = bridge
        self.asset_dir = asset_dir or Path(__file__).parent / "ui"
        # One server instance serves one UI version, even while a checkout is
        # being edited. Missing assets stay unavailable until the next start.
        self.asset_contents = {}
        for name in {route[0] for route in ASSET_ROUTES.values()}:
            try:
                self.asset_contents[name] = (self.asset_dir / name).read_bytes()
            except OSError:
                self.asset_contents[name] = None
        self.token = secrets.token_urlsafe(32)
        self._closed = False
        # Bind before starting the loop, so an occupied port cannot leak a worker thread.
        super().__init__(("127.0.0.1", port), handler_class or _Handler)
        try:
            self.runner = _BridgeLoop(bridge)
        except BaseException:
            super().server_close()
            raise

    def authenticate_api(self, token: str, path: str) -> str:
        """Return the credential's principal; derived servers may narrow scope."""
        if not secrets.compare_digest(token.encode("utf-8"), self.token.encode("ascii")):
            raise _HTTPError(403, "token_required")
        return "user"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def server_close(self):
        if self._closed:
            return
        self._closed = True
        super().server_close()
        # socketserver closes a socket that failed to bind before the loop exists.
        if hasattr(self, "runner"):
            self.runner.close()

    def handle_error(self, request, client_address):
        pass  # A malformed/disconnected HTTP client must not print a private traceback.


class _Handler(BaseHTTPRequestHandler):
    server: LocalUIServer
    server_version = "LocalAgentCoordinator"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass  # Requests may contain private task IDs; never emit raw request/error logs.

    def _response(self, status: int, body: bytes, content_type: str):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def _json(self, status: int, value):
        body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        self._response(status, body, "application/json; charset=utf-8")

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler parse errors must not reflect raw request text.
        self.close_connection = True
        self._json(code, {"error": "invalid_http_request"})

    def _header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise _HTTPError(400, "invalid_headers")
        return values[0] if values else None

    def _check_access(self, *, api: bool):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self._header("Host") not in hosts:
            raise _HTTPError(403, "host_rejected")
        origin = self._header("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in hosts}:
            raise _HTTPError(403, "origin_rejected")
        if api:
            token = self._header("X-LAC-Token") or ""
            self.principal = self.server.authenticate_api(token, urlsplit(self.path).path)

    def _body(self) -> dict:
        if self._header("Transfer-Encoding") is not None:
            raise _HTTPError(400, "invalid_body_length")
        length = self._header("Content-Length")
        if length is None or not re.fullmatch(r"[0-9]+", length):
            raise _HTTPError(411, "body_length_required")
        if len(length) > len(str(MAX_BODY)) or int(length) > MAX_BODY:
            raise _HTTPError(413, "body_too_large")
        content_type = self._header("Content-Type") or ""
        if content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise _HTTPError(415, "json_required")
        raw = self.rfile.read(int(length))
        if len(raw) != int(length):
            raise _HTTPError(400, "invalid_json")
        try:
            def reject_duplicates(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            def reject_constant(value):
                raise ValueError("nonfinite JSON number")

            value = json.loads(raw, object_pairs_hook=reject_duplicates,
                               parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise _HTTPError(400, "invalid_json") from None
        if not isinstance(value, dict):
            raise _HTTPError(400, "invalid_arguments")
        return value

    @staticmethod
    def _text(value, maximum: int) -> bool:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
            return False
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
        return True

    async def _dispatch(self, path: str, query: str, body: dict | None):
        bridge = self.server.bridge
        if self.command == "GET":
            if path == "/api/status" and not query:
                return await bridge.status()
            if path == "/api/tasks" and not query:
                return bridge.list_tasks()
            match = TASK_ROUTE.fullmatch(path)
            if match and match[2] is None:
                params = parse_qs(query, keep_blank_values=True)
                if query and (set(params) != {"refresh"} or params["refresh"] not in (["0"], ["1"])):
                    raise _HTTPError(400, "invalid_arguments")
                return await bridge.read(match[1], refresh=params.get("refresh", ["0"])[0] == "1")
            raise _HTTPError(404, "route_not_found")
        if query:
            raise _HTTPError(400, "invalid_arguments")
        if path == "/api/tasks":
            fields = {"agent", "prompt", "request_id"}
            if (not fields <= body.keys() or body.keys() - fields - {"parent_task_id"}
                    or body["agent"] not in ("claude", "codex")
                    or not self._text(body["prompt"], 8000)
                    or not self._text(body["request_id"], 512)):
                raise _HTTPError(400, "invalid_arguments")
            parent = body.get("parent_task_id")
            if parent is not None and (not isinstance(parent, str) or not re.fullmatch(TASK_ID, parent)):
                raise _HTTPError(400, "invalid_arguments")
            return await bridge.start(body["agent"], body["prompt"], body["request_id"], parent)
        match = TASK_ROUTE.fullmatch(path)
        if match and match[2] == "reply":
            if set(body) != {"prompt"} or not self._text(body["prompt"], 8000):
                raise _HTTPError(400, "invalid_arguments")
            return await bridge.reply(match[1], body["prompt"])
        if match and match[2] == "cancel":
            if body:
                raise _HTTPError(400, "invalid_arguments")
            return await bridge.cancel(match[1])
        raise _HTTPError(404, "route_not_found")

    def _handle(self):
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment:
                raise _HTTPError(400, "invalid_path")
            api = target.path.startswith("/api/") or target.path == "/api"
            self._check_access(api=api)
            if self.command not in {"GET", "POST"}:
                raise _HTTPError(405, "method_not_allowed")
            if api:
                body = self._body() if self.command == "POST" else None
                result = self.server.runner.call(lambda: self._dispatch(target.path, target.query, body))
                self._json(200, result)
                return
            if self.command != "GET" or target.query:
                raise _HTTPError(404, "route_not_found")
            if target.path not in ASSET_ROUTES:
                raise _HTTPError(404, "route_not_found")
            name, content_type = ASSET_ROUTES[target.path]
            content = self.server.asset_contents.get(name)
            if content is None:
                raise _HTTPError(503, "assets_unavailable")
            if name == "index.html":
                if b"__LAC_TOKEN__" not in content:
                    raise _HTTPError(503, "assets_unavailable")
                content = content.replace(b"__LAC_TOKEN__", self.server.token.encode("ascii"))
            self._response(200, content, content_type)
        except _HTTPError as error:
            self.close_connection = True
            self._json(error.status, {**error.details, "error": error.code})
        except (BridgeError, ParadevError, StoreError) as error:
            code = error.code if error.code in ERROR_CODES else "operation_failed"
            status = 404 if code == "task_not_found" else 409
            result = {"error": code}
            task_id = getattr(error, "task_id", None)
            if isinstance(task_id, str) and re.fullmatch(TASK_ID, task_id):
                result["task_id"] = task_id
            self._json(status, result)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self._json(500, {"error": "operation_failed"})

    do_GET = _handle
    do_POST = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle


def create_server(bridge: Bridge, *, port: int = 8768,
                  asset_dir: Path | None = None) -> LocalUIServer:
    """Bind an unserved loopback server; caller owns serve_forever/server_close."""
    return LocalUIServer(bridge, port=port, asset_dir=asset_dir)


def main():
    parser = argparse.ArgumentParser(description="Local Agent Coordinator Web workbench")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="Open the local workbench in the default browser")
    args = parser.parse_args()
    try:
        if not args.config.is_absolute():
            raise ValueError("Configuration path must be absolute")
        server = create_server(Bridge(BridgeConfig.load(args.config)), port=args.port)
    except (OSError, ValueError, StoreError):
        parser.exit(1, "Local UI configuration, state or port unavailable. Check the selected config.\n")
    print(server.url, flush=True)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        if args.open_browser:
            try:
                webbrowser.open(server.url)
            except (webbrowser.Error, OSError):
                print("Browser unavailable; open the printed local URL.", file=sys.stderr)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        server.server_close()
        if server.runner.shutdown_error:
            print("Local UI stopped; remote cleanup was not confirmed.", file=sys.stderr)


if __name__ == "__main__":
    main()
