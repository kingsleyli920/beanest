"""Stdio MCP client for one explicitly selected, already-running local room app."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Literal
from pydantic import StrictBool, StrictInt
from uuid import UUID
from urllib.parse import urlencode

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .agent_registry import safe_agent_id
from .message_capacity import MAX_JSON_BODY_BYTES, MessageCapacityError, checked_message_text
from .room_store import TASK_MODES, WORK_MODES


MAX_CONNECTION = 4096
MAX_REQUEST = MAX_JSON_BODY_BYTES
MAX_RESPONSE = 2 * 1024 * 1024
MAX_HEADERS = 16 * 1024
_URL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/?\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_INSTANCE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_REMOTE_CODES = frozenset({
    "invalid_arguments", "invalid_text", "message_too_large", "invalid_target", "invalid_request", "room_not_found",
    "request_conflict", "room_not_active", "session_identity_unknown", "budget_exhausted",
    "deadline_exceeded", "storage_unavailable", "service_stopping", "stale_instance",
    "token_required", "token_scope_rejected", "host_rejected", "origin_rejected", "route_not_found", "operation_failed",
    "operation_timeout", "server_stopping", "body_too_large", "cleanup_unconfirmed",
    "agent_unavailable",
    "invalid_project_mode", "invalid_work_mode", "plan_read_only", "project_requires_first_author", "project_unavailable",
    "project_storage_unavailable", "project_initialization_unconfirmed", "project_commit_unconfirmed",
    "invalid_project_root", "project_unsafe_path", "project_unknown_workspace", "project_corrupt", "project_busy",
    "execution_initialization_unconfirmed", "execution_unavailable",
    "invalid_participants", "invalid_project_request", "invalid_project_path", "invalid_project_files",
    "project_file_unavailable", "project_sensitive_content", "project_limit_exceeded", "project_not_found",
    "project_catalog_corrupt", "project_not_seeded",
    "team_run_conflict", "team_run_not_ready", "team_retry_required", "team_cleanup_unconfirmed", "team_run_limit",
    "user_acceptance_required", "project_export_unavailable", "project_export_corrupt", "invalid_project_revision", "svg_preview_unavailable",
    "invalid_event_cursor", "text_reference_conflict", "text_reference_not_found",
    "event_too_large", "room_state_too_large",
})


class RoomClientError(RuntimeError):
    def __init__(self, code: str, *, remote_state_unknown: bool = False):
        self.code = code
        self.remote_state_unknown = remote_state_unknown
        super().__init__(code)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError


@dataclass(frozen=True)
class _Connection:
    port: int
    token: str = field(repr=False)
    instance_id: str


def _read_connection(path: Path) -> _Connection:
    descriptor = None
    try:
        if not path.is_absolute() or path.is_symlink() or path.parent.is_symlink():
            raise RoomClientError("invalid_connection")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise RoomClientError("connection_permissions")
        if metadata.st_size > MAX_CONNECTION:
            raise RoomClientError("invalid_connection")
        raw = os.read(descriptor, MAX_CONNECTION + 1)
        if len(raw) > MAX_CONNECTION:
            raise RoomClientError("invalid_connection")
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
        if not isinstance(value, dict) or set(value) != {"url", "token", "instance_id"}:
            raise RoomClientError("invalid_connection")
        match = _URL.fullmatch(value["url"]) if isinstance(value["url"], str) else None
        if not match or not 1024 <= int(match[1]) <= 65535:
            raise RoomClientError("invalid_connection")
        if (not isinstance(value["token"], str) or not _TOKEN.fullmatch(value["token"])
                or not isinstance(value["instance_id"], str) or not _INSTANCE.fullmatch(value["instance_id"])):
            raise RoomClientError("invalid_connection")
        return _Connection(int(match[1]), value["token"], value["instance_id"])
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise RoomClientError("connection_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise RoomClientError("invalid_arguments")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise RoomClientError("invalid_arguments") from None
    return value


def _target(value: object) -> str:
    if not isinstance(value, str) or (value not in ("all", "both") and not safe_agent_id(value)):
        raise RoomClientError("invalid_arguments")
    return value


def _message(value: object) -> str:
    """Validate capacity without changing the raw request/idempotency payload."""
    try:
        checked_message_text(value)
    except MessageCapacityError as error:
        code = "message_too_large" if error.code == "message_too_large" else "invalid_arguments"
        raise RoomClientError(code) from None
    return value


def _room_id(value: object) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except ValueError:
        raise RoomClientError("invalid_arguments") from None


class RoomHTTPClient:
    def __init__(self, connection: Path, *, timeout: float = 20,
                 max_response: int = MAX_RESPONSE):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 30
                or type(max_response) is not int or not 1 <= max_response <= MAX_RESPONSE):
            raise RoomClientError("invalid_configuration")
        self.path = Path(connection)
        self.connection = _read_connection(self.path)
        self.timeout, self.max_response = timeout, max_response
        self.paging_supported = False

    def _check_connection(self):
        current = _read_connection(self.path)
        pinned = self.connection
        if (current.port != pinned.port or current.instance_id != pinned.instance_id
                or not secrets.compare_digest(current.token, pinned.token)):
            raise RoomClientError("connection_changed")

    async def _exchange(self, method: str, path: str, body: bytes, *, response_limit=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.connection.port,
                                                       limit=MAX_HEADERS)
        try:
            headers = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{self.connection.port}",
                       "Connection: close", "Accept: application/json",
                       "X-LAC-Token: " + self.connection.token,
                       "X-LAC-Instance: " + self.connection.instance_id]
            if method == "POST":
                headers += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
            writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
            await writer.drain()
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            if len(raw_headers) > MAX_HEADERS:
                raise RoomClientError("invalid_http_response")
            lines = raw_headers.split(b"\r\n")
            status = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", lines[0])
            if not status:
                raise RoomClientError("invalid_http_response")
            fields = {}
            for line in lines[1:-2]:
                key, separator, value = line.partition(b":")
                if not separator or not re.fullmatch(rb"[A-Za-z0-9-]+", key):
                    raise RoomClientError("invalid_http_response")
                key = key.lower()
                if key in fields:
                    raise RoomClientError("invalid_http_response")
                fields[key] = value.strip()
            code = int(status[1])
            if 300 <= code < 400:
                raise RoomClientError("redirect_rejected")
            length = fields.get(b"content-length", b"")
            if b"transfer-encoding" in fields or not re.fullmatch(rb"[0-9]{1,9}", length):
                raise RoomClientError("invalid_http_response")
            if int(length) > (self.max_response if response_limit is None else response_limit):
                raise RoomClientError("response_too_large")
            if fields.get(b"content-type", b"").lower().split(b";", 1)[0] != b"application/json":
                raise RoomClientError("invalid_http_response")
            raw = await reader.readexactly(int(length))
            try:
                result = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise RoomClientError("invalid_http_response") from None
            if not isinstance(result, dict):
                raise RoomClientError("invalid_http_response")
            if code != 200:
                remote = result.get("error")
                remote = remote if isinstance(remote, str) and remote in _REMOTE_CODES else "remote_error"
                raise RoomClientError(remote, remote_state_unknown=method == "POST" and code >= 500)
            if path == "/api/mcp/status" and result.get("instance_id") != self.connection.instance_id:
                raise RoomClientError("stale_instance")
            return result
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _request(self, method: str, path: str, payload=None, *, large_page=False):
        self._check_connection()
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else b""
        if len(body) > MAX_REQUEST:
            raise RoomClientError("request_too_large")
        try:
            # A single explicitly requested history/text item may exceed the
            # compact state budget. Never raise the bound for a whole room.
            response_limit = (MAX_JSON_BODY_BYTES if large_page and self.max_response == MAX_RESPONSE
                              else self.max_response)
            return await asyncio.wait_for(
                self._exchange(method, path, body, response_limit=response_limit), self.timeout)
        except RoomClientError as error:
            if method == "POST" and error.code in {
                "invalid_http_response", "response_too_large", "redirect_rejected"}:
                raise RoomClientError(error.code, remote_state_unknown=True) from None
            raise
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            raise RoomClientError("http_unavailable", remote_state_unknown=method == "POST") from None

    async def status(self):
        result = await self._request("GET", "/api/mcp/status")
        capabilities = result.get("capabilities")
        self.paging_supported = isinstance(capabilities, dict) and capabilities.get("room_paging_v1") is True
        return result

    def _mutation_path(self, path):
        return path + "?response=ack" if self.paging_supported else path

    async def list_rooms(self):
        return await self._request("GET", "/api/mcp/rooms?view=compact" if self.paging_supported else "/api/mcp/rooms")

    async def projects(self):
        return await self._request("GET", "/api/mcp/projects")

    async def partners(self):
        return await self._request("GET", "/api/mcp/partners?view=compact" if self.paging_supported else "/api/mcp/partners")

    async def start(self, goal, target, request_id, *, mode="text", participants=None, project_id=None,
                    collaboration=False, work_mode=None):
        if mode not in TASK_MODES:
            raise RoomClientError("invalid_arguments")
        body = {"goal": _message(goal), "target": _target(target), "request_id": _text(request_id, 512)}
        if mode != "text":
            body["mode"] = mode
        if work_mode is not None:
            if not isinstance(work_mode, str) or work_mode not in WORK_MODES:
                raise RoomClientError("invalid_arguments")
            body["work_mode"] = work_mode
        if participants is not None:
            if not isinstance(participants, list) or not 1 <= len(participants) <= 8:
                raise RoomClientError("invalid_arguments")
            body["participants"] = [_target(item) for item in participants]
            if (len(set(body["participants"])) != len(body["participants"])
                    or any(item in {"all", "both"} for item in body["participants"])):
                raise RoomClientError("invalid_arguments")
        if project_id is not None:
            body["project_id"] = _room_id(project_id)
        if type(collaboration) is not bool:
            raise RoomClientError("invalid_arguments")
        if collaboration:
            body["collaboration"] = True
        return await self._request("POST", self._mutation_path("/api/mcp/rooms"), body)

    async def read(self, room_id):
        path = "/api/mcp/rooms/" + _room_id(room_id)
        return await self._request("GET", path + "/state" if self.paging_supported else path)

    async def events(self, room_id, *, before_seq=None, after_seq=None, through_seq=None,
                     limit=None, max_bytes=None):
        if not self.paging_supported:
            raise RoomClientError("paging_unavailable")
        if before_seq is not None and after_seq is not None:
            raise RoomClientError("invalid_arguments")
        query = {}
        for name, value, minimum, maximum in (
                ("before_seq", before_seq, 0, None), ("after_seq", after_seq, 0, None),
                ("through_seq", through_seq, 0, None), ("limit", limit, 1, 500),
                ("max_bytes", max_bytes, 1, MAX_JSON_BODY_BYTES)):
            if value is None:
                continue
            if type(value) is not int or value < minimum or maximum is not None and value > maximum:
                raise RoomClientError("invalid_arguments")
            query[name] = value
        path = "/api/mcp/rooms/" + _room_id(room_id) + "/events"
        return await self._request("GET", path + ("?" + urlencode(query) if query else ""), large_page=True)

    async def text(self, room_id, kind, sha256, *, run_id=None, objective_id=None,
                   job_id=None, task_id=None, result_version=None, seq=None, execution_id=None):
        if not self.paging_supported:
            raise RoomClientError("paging_unavailable")
        fields_by_kind = {
            "room_goal": set(), "run_goal": {"run_id"}, "objective_goal": {"objective_id"},
            "objective_request": {"objective_id"}, "objective_decision": {"objective_id"},
            "job_text": {"job_id"}, "task_result": {"run_id", "task_id", "result_version"},
            "execution_result": {"execution_id"},
            "event_text": {"seq"},
        }
        identifiers = {key: value for key, value in {
            "run_id": run_id, "objective_id": objective_id, "job_id": job_id,
            "task_id": task_id, "result_version": result_version, "seq": seq,
            "execution_id": execution_id}.items() if value is not None}
        if (not isinstance(kind, str) or kind not in fields_by_kind
                or set(identifiers) != fields_by_kind[kind]
                or not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None):
            raise RoomClientError("invalid_arguments")
        for name, value in identifiers.items():
            if name in {"run_id", "objective_id", "job_id", "execution_id"}:
                identifiers[name] = _room_id(value)
            elif name == "task_id":
                if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is None:
                    raise RoomClientError("invalid_arguments")
            elif type(value) is not int or value < (1 if name == "seq" else 0):
                raise RoomClientError("invalid_arguments")
        query = {"kind": kind, **identifiers, "sha256": sha256}
        path = "/api/mcp/rooms/" + _room_id(room_id) + "/text?" + urlencode(query)
        return await self._request("GET", path, large_page=True)

    async def job(self, room_id, job_id):
        if not self.paging_supported:
            raise RoomClientError("paging_unavailable")
        return await self._request("GET", "/api/mcp/rooms/" + _room_id(room_id) + "/jobs?job_id=" + _room_id(job_id))

    async def run(self, room_id, run_id):
        if not self.paging_supported:
            raise RoomClientError("paging_unavailable")
        return await self._request("GET", "/api/mcp/rooms/" + _room_id(room_id) + "/runs?run_id=" + _room_id(run_id) + "&view=compact")

    async def send(self, room_id, text, target, request_id):
        return await self._request("POST", self._mutation_path("/api/mcp/rooms/" + _room_id(room_id) + "/messages"), {
            "text": _message(text), "target": _target(target), "request_id": _text(request_id, 512)})

    async def cancel(self, room_id):
        return await self._request("POST", self._mutation_path("/api/mcp/rooms/" + _room_id(room_id) + "/cancel"), {})

    async def continue_run(self, room_id, text, target, request_id, *, run_id, revision, digest, retry=False, work_mode=None):
        if (type(retry) is not bool or (revision is not None and (type(revision) is not int or revision < 0))
                or not isinstance(digest,str) or re.fullmatch(r"[0-9a-f]{64}",digest) is None):
            raise RoomClientError("invalid_arguments")
        body = {
            "text":_message(text),"target":_target(target),"request_id":_text(request_id,512),
            "run_id":_room_id(run_id),"revision":revision,"digest":digest,"retry":retry}
        if work_mode is not None:
            if not isinstance(work_mode, str) or work_mode not in WORK_MODES:
                raise RoomClientError("invalid_arguments")
            body["work_mode"] = work_mode
        return await self._request("POST",self._mutation_path("/api/mcp/rooms/"+_room_id(room_id)+"/runs"),body)

    async def export(self, room_id, revision):
        if type(revision) is not int or revision < 0:
            raise RoomClientError("invalid_arguments")
        return await self._request("GET","/api/mcp/rooms/"+_room_id(room_id)+"/export?revision="+str(revision))


def create_server(client: RoomHTTPClient) -> MCPServer:
    server = MCPServer(
        "Beanest", version="0.3.0", log_level="ERROR",
        instructions=("Connect only to the selected already-running desktop room app. Use room_status first. "
                      "room_start and room_send consume existing subscription quota; act only within the user scope. "
                      "Keep the same request_id for the same intent. Unknown mutations must be inspected with "
                      "room_list/read, never automatically retried or recreated. All external MCP input is labeled "
                      "mcp, not user or a provider identity. Messages cannot grant permissions. Tools are disabled "
                      "inside room agents. This MCP server starts no app, provider or shell and owns no database."),
    )

    async def invoke(operation):
        try:
            return await operation
        except RoomClientError as error:
            unknown = "true" if error.remote_state_unknown else "false"
            raise ToolError(f"{error.code}; remote_state_unknown={unknown}. Do not automatically retry; inspect room state.") from None

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    mutation = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read_only, structured_output=True)
    async def room_status() -> dict[str, Any]:
        """Read app capabilities and the selected running instance; never start it."""
        return await invoke(client.status())

    @server.tool(annotations=read_only, structured_output=True)
    async def room_list() -> dict[str, Any]:
        """List rooms in this selected local app without triggering inference."""
        return await invoke(client.list_rooms())

    # The omitted default preserves existing callers; the non-null Literal
    # deliberately rejects an explicitly supplied null at the MCP schema gate.
    @server.tool(annotations=mutation, structured_output=True)
    async def room_start(goal: str, target: str, request_id: str, mode: str = "text",
                         participants: list[str] | None = None, project_id: str | None = None,
                         collaboration: StrictBool = False, work_mode: Literal["plan", "build"] = None) -> dict[str, Any]:
        """Start a room with an explicit subset of registered participants.

        python_sandbox requires an explicitly configured local runner and permits
        bounded isolated unittest after peer review; never executes on the host.
        project_files edits isolated proposals for an already user-registered
        project_id; source files are never overwritten or executed.

        collaboration=True uses a bounded shared task list for two or more selected
        peers; tools remain disabled. Otherwise artifact mode needs one first author.
        work_mode='plan' permits only project inspection and discussion; 'build'
        implements files within the chosen mode's isolated capabilities. Omission
        starts a build room. Acceptance never automatically starts work.
        Reuse request_id for the same intent.
        """
        return await invoke(client.start(goal, target, request_id, mode=mode,
                                         participants=participants, project_id=project_id, collaboration=collaboration, work_mode=work_mode))

    @server.tool(annotations=read_only, structured_output=True)
    async def project_list() -> dict[str, Any]:
        """List only projects explicitly registered by the user; cannot add paths or read arbitrary files."""
        return await invoke(client.projects())

    @server.tool(annotations=read_only, structured_output=True)
    async def partner_list() -> dict[str, Any]:
        """Read coordinator-owned partner monitoring; does not probe credentials or add/enable agents."""
        return await invoke(client.partners())

    @server.tool(annotations=read_only, structured_output=True)
    async def room_read(room_id: str) -> dict[str, Any]:
        """Read authoritative current state without another model call.

        With room_paging_v1, historical messages are read separately using
        room_events; previews and text references are explicitly labeled by
        the host. Older apps retain their original full-room response.
        """
        return await invoke(client.read(room_id))

    @server.tool(annotations=read_only, structured_output=True)
    async def room_events(room_id: str, before_seq: StrictInt | None = None,
                          after_seq: StrictInt | None = None, through_seq: StrictInt | None = None,
                          limit: StrictInt | None = None, max_bytes: StrictInt | None = None) -> dict[str, Any]:
        """Read original visible chat messages, without inference or archiving.

        Call room_status first; requires room_paging_v1. Omit cursors for the
        latest page. Reuse through_seq to keep a stable history boundary;
        before_seq reads earlier messages and after_seq reads newer messages.
        The host reports remaining pages and an oversized single event.
        """
        return await invoke(client.events(room_id, before_seq=before_seq, after_seq=after_seq,
                                           through_seq=through_seq, limit=limit, max_bytes=max_bytes))

    @server.tool(annotations=read_only, structured_output=True)
    async def room_text(room_id: str, kind: Literal["room_goal", "run_goal", "objective_goal", "objective_request", "objective_decision", "job_text", "task_result", "event_text", "execution_result"],
                        sha256: str, run_id: str | None = None, objective_id: str | None = None,
                        job_id: str | None = None, task_id: str | None = None,
                        result_version: StrictInt | None = None, seq: StrictInt | None = None,
                        execution_id: str | None = None) -> dict[str, Any]:
        """Read the exact full original text referenced by current state.

        Copy kind, SHA and identifiers from a host text_ref; never invent a
        replacement when the reference is stale. This cannot read file paths,
        private worker copies, credentials or other applications' sessions.
        """
        return await invoke(client.text(room_id, kind, sha256, run_id=run_id, objective_id=objective_id,
                                         job_id=job_id, task_id=task_id, result_version=result_version,
                                         seq=seq, execution_id=execution_id))

    @server.tool(annotations=read_only, structured_output=True)
    async def room_job(room_id: str, job_id: str) -> dict[str, Any]:
        """Read one exact historical job summary and original-text references; no inference."""
        return await invoke(client.job(room_id, job_id))

    @server.tool(annotations=read_only, structured_output=True)
    async def room_run(room_id: str, run_id: str) -> dict[str, Any]:
        """Read one exact historical run in this chat; does not change or archive it."""
        return await invoke(client.run(room_id, run_id))

    @server.tool(annotations=mutation, structured_output=True)
    async def room_send(room_id: str, text: str, target: str,
                        request_id: str) -> dict[str, Any]:
        """Queue one MCP-sourced message; keep its stable request_id on an uncertain response."""
        return await invoke(client.send(room_id, text, target, request_id))

    @server.tool(annotations=mutation, structured_output=True)
    async def room_continue(room_id: str, text: str, target: str, request_id: str,
                            run_id: str, revision: StrictInt | None, digest: str,
                            retry: StrictBool = False, work_mode: Literal["plan", "build"] = None) -> dict[str, Any]:
        """Continue with an explicit follow-up in this chat, preserving prior runs.

        Copy run_id/revision/digest from current_run. Ready continuation keeps
        native sessions. retry=True is a new attempt with fresh sessions after
        confirmed cleanup; never automatically retry an unknown mutation.
        This does not accept any delivery on the user's behalf.
        Omitted work_mode inherits the current run; explicitly choose build to
        implement a delivered plan. This still cannot execute arbitrary projects.
        """
        return await invoke(client.continue_run(room_id,text,target,request_id,run_id=run_id,
                                               revision=revision,digest=digest,retry=retry,work_mode=work_mode))

    @server.tool(annotations=read_only, structured_output=True)
    async def room_export(room_id: str, revision: StrictInt) -> dict[str, Any]:
        """Download the exact current artifact revision, excluding private chat/session metadata."""
        return await invoke(client.export(room_id,revision))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
                 structured_output=True)
    async def room_cancel(room_id: str) -> dict[str, Any]:
        """Request room cancellation; inspect cancellation_confirmed before claiming it stopped."""
        return await invoke(client.cancel(room_id))

    return server


def main():
    parser = argparse.ArgumentParser(description="MCP client for an already-running local room app")
    parser.add_argument("--connection", type=Path, required=True)
    args = parser.parse_args()
    try:
        server = create_server(RoomHTTPClient(args.connection))
    except RoomClientError as error:
        parser.exit(1, f"Room connection unavailable ({error.code}).\n")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
