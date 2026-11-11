"""Independent peer-room HTTP surface over the shared loopback safety shell.

The existing task bridge keeps its routes and assets. Only HTTP validation,
token handling and event-loop lifecycle are shared; room execution uses the
direct subscription CLI service and never starts a Paradev host.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import time
from uuid import uuid4
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, quote

from .room_service import RoomError, RoomService
from .room_store import TASK_MODES, WORK_MODES, checked_message
from . import room_projection, room_media
from .project_catalog import ProjectCatalogError
from .partner_manager import PartnerError
from .ui_server import LocalUIServer, TASK_ID, _Handler, _HTTPError


ROOM_ROUTE = re.compile(rf"/api/rooms/({TASK_ID})(?:/(messages(?:/receipt)?|members|state|events|text|jobs|code-status|delivery-repository|cancel|runs|accept|fork|export|preview|objective|continue|pause|resume|answer|file|restart-objective|native-handoff|recover-native|reassess-objective))?")
TARGET_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
PARTNER_ROUTE = re.compile(r"/api/partners/([a-z][a-z0-9_-]{0,31})/(check|enabled|connection|appearance|account-source|workload)")
RUNTIME_REFRESH_ROUTE = re.compile(r"/api/partners/([a-z][a-z0-9_-]{0,31})/runtime/refresh")
AVAILABILITY_RECHECK_ROUTE = re.compile(r"/api/partners/([a-z][a-z0-9_-]{0,31})/availability/recheck")
ACCOUNT_REFRESH_ROUTE = re.compile(r"/api/partners/([a-z][a-z0-9_-]{0,31})/account/refresh")
LIFECYCLE_ROUTE = re.compile(rf"/api/(projects|rooms|partners)/({TASK_ID}|[a-z][a-z0-9_-]{{0,31}})/lifecycle")
PROJECT_NAME_ROUTE = re.compile(rf"/api/projects/({TASK_ID})/name")
ERROR_STATUS = {
    "native_login_unsupported":400, "native_install_unsupported":400, "invalid_workload_level":400, "login_operation_changed":409, "user_native_login_required":403,
    "invalid_delivery_repository":409, "delivery_repository_unavailable":409, "user_delivery_repository_required":403,
    "invalid_code_workspace":400, "user_code_status_required":403,
    "media_not_found":404, "media_unavailable":404, "media_invalid_path":400,
    "media_unsafe_path":409, "media_changed":409, "media_binding_changed":409,
    "media_ambiguous_reference":409, "user_media_required":403,
    "media_access_expired":403, "media_range_invalid":416,
    "user_outbox_required": 403, "outbox_not_found": 404, "outbox_scope_conflict": 409,
    "outbox_in_flight": 409, "outbox_unconfirmed": 409, "outbox_retire_unavailable": 409,
    "outbox_intervening_control": 409, "outbox_preparation_unconfirmed": 409,
    "outbox_legacy_unconfirmed": 409, "outbox_cleanup_unconfirmed": 409, "outbox_receipt_unconfirmed": 409,
    "message_too_large": 413, "invalid_event_cursor": 400, "text_reference_conflict": 409,
    "text_reference_not_found": 404, "event_too_large": 413, "room_state_too_large": 413,
    "user_continuation_required": 403, "objective_followup_unavailable": 409,
    "invalid_goal": 400, "invalid_target": 400, "invalid_request": 400,
    "invalid_prompt": 400, "invalid_text": 400, "invalid_message": 400,
    "invalid_configuration": 400, "invalid_state_path": 400,
    "invalid_workspace": 400, "invalid_agent_response": 409,
    "invalid_project_mode": 400, "invalid_work_mode": 400, "plan_read_only": 409, "project_requires_first_author": 400,
    "project_initialization_unconfirmed": 503, "project_commit_unconfirmed": 503,
    "execution_initialization_unconfirmed": 503, "execution_unavailable": 409,
    "project_unavailable": 409, "project_storage_unavailable": 503,
    "invalid_project_root": 409, "project_unsafe_path": 409,
    "project_unknown_workspace": 409, "project_corrupt": 409, "project_busy": 409,
    "invalid_agent_action": 409,
    "agent_unavailable": 409,
    "room_not_found": 404, "request_conflict": 409,
    "room_cancelled": 409, "room_not_running": 409, "room_closed": 409,
    "room_interrupted": 409, "room_budget_exhausted": 409,
    "room_not_active": 409, "session_identity_unknown": 409,
    "budget_exhausted": 409, "runtime_in_use": 409,
    "storage_error": 503, "storage_unavailable": 503,
    "service_closed": 503, "service_stopping": 503, "deadline_exceeded": 409,
    "invalid_participants": 400, "invalid_project_request": 400,
    "member_limit_reached": 409, "invitation_mode_unsupported": 409, "user_member_invitation_required": 403,
    "invalid_project_path": 400, "invalid_project_files": 400,
    "project_file_unavailable": 409, "project_sensitive_content": 409,
    "project_limit_exceeded": 400, "project_not_found": 404,
    "project_capture_limit": 413, "project_source_changed": 409, "project_capture_cancelled": 409,
    "project_catalog_corrupt": 503, "project_not_seeded": 409,
    "unsafe_partner_storage": 503, "invalid_partner_storage": 503,
    "invalid_partner_profile": 400, "unsupported_partner_adapter": 400,
    "partner_identity_conflict": 409, "partner_limit_reached": 409,
    "invalid_partner_enabled": 400, "unknown_partner": 404,
    "invalid_account_source": 400, "partner_account_source_busy": 409, "partner_account_home_changed": 409,
    "invalid_check_clock": 503, "partner_management_unavailable": 409,
    "invalid_partner_connection": 400, "invalid_project_revision": 400,
    "project_export_unavailable": 409, "project_export_corrupt": 409, "svg_preview_unavailable":409,
    "team_run_conflict":409, "team_run_not_ready":409, "team_retry_required":409,
    "team_cleanup_unconfirmed":409, "team_run_limit":409, "user_acceptance_required":403,
    "fork_source_unavailable":409, "user_fork_required":403,
    "invalid_lifecycle_action":400, "project_inactive":409, "partner_inactive":409,
    "room_archived":409, "room_removed":409, "room_lifecycle_busy":409,
    "user_lifecycle_required":403,
    "lifecycle_operation_not_found":404, "room_deleted":410,
    "message_request_not_found":404, "user_message_receipt_required":403,
    "project_deletion_not_found":404, "project_deletion_pending":409,
    "purge_files_incomplete":409, "purge_workspace_busy":409,
    "purge_dependency_unconfirmed":409, "purge_reference_conflict":409,
    "user_account_required":403, "account_adapter_unsupported":409,
    "invalid_runtime_settings":400, "permission_read_only":409, "runtime_settings_unsupported":409,
    "runtime_options_required":409, "runtime_model_unsupported":400, "runtime_effort_unsupported":400,
    "user_runtime_settings_required":403,
    "agent_quota_exhausted":409, "agent_quota_unconfirmed":409, "quota_state_corrupt":503,
    "availability_recheck_unavailable":409,
    "invalid_execution_policy":400, "user_execution_policy_required":403,
    "objective_unavailable":409, "objective_conflict":409, "objective_already_started":409,
    "objective_continue_unavailable":409, "objective_binding_changed":409,
    "objective_not_running":409, "objective_not_paused":409,
    "objective_calls_exhausted":409, "objective_deadline_exceeded":409, "objective_stage_limit":409,
    "all_partners_quota_exhausted":409,
    "partner_lifecycle_operation_not_found":404, "partner_lifecycle_busy":409,
    "partner_disabled":409, "partner_archived":409, "partner_removed":409,
    "partner_unavailable":409, "partner_lifecycle_pending":409,
    "partner_cleanup_unconfirmed":409, "partner_handoff_binding_changed":409,
    "partner_configuration_unavailable":503,
    "partner_receiver_busy":409, "no_available_partner":409,
    "objective_answer_required":409, "objective_answer_unavailable":409,
    "objective_restart_unavailable":409, "objective_restart_unsafe":409,
    "native_permission_required":409, "user_native_permission_required":403,
    "native_handoff_unavailable":409, "native_handoff_unconfirmed":503,
    "native_adapter_unsupported":409,
    "native_recovery_unavailable":409, "native_recovery_cleanup_unconfirmed":409,
    "native_recovery_workspace_unavailable":409, "native_recovery_unpublished_changes":409,
    "native_recovery_binding_changed":409,
    "native_recovery_required":409,
    "native_recovery_diagnostic_unconfirmed":409,
    "objective_reassessment_unavailable":409,
}


class RoomUIServer(LocalUIServer):
    def __init__(self, service: RoomService, *, port: int = 8769,
                 asset_dir: Path | None = None):
        self.service = service
        self.instance_id = str(uuid4())
        self.mcp_token = secrets.token_urlsafe(32)
        self.media_access = {}
        super().__init__(service, port=port,
                         asset_dir=asset_dir or Path(__file__).parent / "room_ui",
                         handler_class=_RoomHandler)

    def authenticate_api(self, token: str, path: str) -> str:
        mcp_path = path.startswith("/api/mcp/")
        if secrets.compare_digest(token.encode("utf-8"), self.mcp_token.encode("ascii")):
            if not mcp_path:
                raise _HTTPError(403, "token_scope_rejected")
            return "mcp"
        principal = super().authenticate_api(token, path)
        if mcp_path:
            raise _HTTPError(403, "token_scope_rejected")
        return principal


class _RoomHandler(_Handler):
    server: RoomUIServer

    def _handle(self):
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._json(400, {"error": "invalid_path"})
            return
        if not target.path.startswith("/room-media/"):
            return super()._handle()
        stream = None
        try:
            if target.scheme or target.netloc or target.fragment:
                raise _HTTPError(400, "invalid_path")
            self._check_access(api=False)
            if self.command not in {"GET", "HEAD"}:
                raise _HTTPError(405, "method_not_allowed")
            if not re.fullmatch(r"/room-media/[A-Za-z0-9_-]{43}", target.path):
                raise _HTTPError(404, "media_not_found")
            fields = parse_qs(target.query, keep_blank_values=True)
            if fields and fields != {"download": ["1"]}:
                raise _HTTPError(400, "invalid_arguments")
            access = self.server.media_access.get(target.path.rsplit("/", 1)[1])
            if not access or access["expires_at"] <= time.time():
                raise _HTTPError(403, "media_access_expired")
            stream, metadata = self.server.service.open_room_media(access["room_id"], access["asset"])
            if room_media._identity(os.fstat(stream.fileno())) != metadata["identity"]:
                raise room_media.MediaError("media_changed")
            etag = '"' + metadata["sha256"] + '"'
            if self._header("If-None-Match") == etag and not self._header("Range"):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, no-cache")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            requested = self._header("Range")
            if self._header("If-Range") not in {None, etag}:
                requested = None
            try:
                status, start, end = room_media.parse_range(requested, metadata["bytes"])
            except room_media.MediaError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{metadata['bytes']}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", metadata["mime"])
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            disposition = "attachment" if fields or metadata["kind"] == "file" else "inline"
            self.send_header("Content-Disposition", disposition + "; filename*=UTF-8''" + quote(metadata["name"], safe=""))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{metadata['bytes']}")
            self.end_headers()
            if self.command == "GET":
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    data = stream.read(min(256 * 1024, remaining))
                    if not data or room_media._identity(os.fstat(stream.fileno())) != metadata["identity"]:
                        self.close_connection = True
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except _HTTPError as error:
            self._json(error.status, {"error": error.code})
        except (room_media.MediaError, RoomError) as error:
            code = error.code if error.code in ERROR_STATUS else "media_unavailable"
            self._json(ERROR_STATUS.get(code, 404), {"error": code})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        finally:
            if stream:
                stream.close()

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle

    def do_DELETE(self):
        """The sole DELETE operation uses the same auth/body validation shell."""
        try:
            target = urlsplit(self.path)
            if (target.scheme or target.netloc or target.query or target.fragment
                    or re.fullmatch(r"/api/(?:mcp/)?native-adapters/[a-z][a-z0-9_-]{0,31}/(?:login|install)", target.path) is None):
                raise _HTTPError(405, "method_not_allowed")
            self._check_access(api=True)
            body = self._body()
            result = self.server.runner.call(lambda: self._dispatch(target.path, "", body))
            self._json(200, result)
        except _HTTPError as error:
            self._json(error.status, {"error": error.code})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    @staticmethod
    def _message_text(value):
        checked_message(value)
        return True

    def _message_fields(self, body: dict, text_field: str, *, allow_mode=False) -> None:
        expected = {text_field, "target", "request_id"}
        if allow_mode and "mode" in body:
            expected.add("mode")
        if allow_mode:
            expected.update(set(body) & {"participants", "project_id", "collaboration", "work_mode", "runtime_settings", "execution_policy"})
        if (set(body) != expected
                or not self._message_text(body[text_field])
                or not isinstance(body["target"], str)
                or not TARGET_ID.fullmatch(body["target"])
                or not self._text(body["request_id"], 512)):
            raise _HTTPError(400, "invalid_arguments")
        if allow_mode and body.get("mode", "text") not in TASK_MODES:
            raise _HTTPError(400, "invalid_arguments")
        if allow_mode and "work_mode" in body and (not isinstance(body["work_mode"], str) or body["work_mode"] not in WORK_MODES):
            raise _HTTPError(400, "invalid_arguments")
        if allow_mode and "collaboration" in body and type(body["collaboration"]) is not bool:
            raise _HTTPError(400, "invalid_arguments")

    async def _dispatch(self, path: str, query: str, body: dict | None):
        try:
            fields = parse_qs(query, keep_blank_values=True)
            canonical = "/api/" + path[len("/api/mcp/"):] if path.startswith("/api/mcp/") else path
            ack = fields.pop("response", None)
            outbox_scope = fields.pop("outbox_scope", None)
            view = fields.pop("view", None)
            self.compact_view = view == ["compact"]
            if view is not None and (not self.compact_view or not (
                    canonical.startswith("/api/partners") or self.command == "GET" and
                    (canonical == "/api/rooms" or canonical.endswith("/runs")))):
                raise _HTTPError(400, "invalid_arguments")
            if ack is not None and (ack != ["ack"] or self.command != "POST" or not (
                    canonical == "/api/rooms" or ROOM_ROUTE.fullmatch(canonical))):
                raise _HTTPError(400, "invalid_arguments")
            submission = None
            if outbox_scope is not None:
                if self.principal == "mcp":
                    raise _HTTPError(403, "user_outbox_required")
                if (len(outbox_scope) != 1 or self.command != "POST" or not (canonical == "/api/rooms" or ROOM_ROUTE.fullmatch(canonical))):
                    raise _HTTPError(400, "invalid_arguments")
                submission = self.server.service.begin_outbox_submission(outbox_scope[0], canonical, body, instance_id=self.server.instance_id)
                if submission.get("ack"):
                    return submission["ack"]
            try:
                result = await self._dispatch_room(path, urlencode(fields, doseq=True), body)
            finally:
                if submission:
                    self.server.service.finish_outbox_submission(submission)
            # Directory responses use a list; runtime catalogs are keyed by
            # profile ID and must retain their independent model/effort shape.
            if self.compact_view and isinstance(result, dict) and isinstance(result.get("partners"), list):
                result = room_projection.compact_partners(result)
            if ack and isinstance(result, dict) and isinstance(result.get("room"), dict):
                room = result["room"]
                return {"acknowledged": True, **({"request_id": body["request_id"]} if body and "request_id" in body else {}),
                        "room_id": room["id"], "state_url": "/api/rooms/" + room["id"] + "/state",
                        "status": room["status"], "cancellation_confirmed": result.get("cancellation_confirmed"),
                        "current_run_id": (result.get("current_run") or {}).get("id"),
                        "objective_id": (result.get("objective") or {}).get("id")}
            return result
        except (RoomError, ProjectCatalogError, PartnerError) as error:
            # Error text may contain private paths. Only stable, known codes
            # cross this boundary; an unexpected code remains generic.
            code = getattr(error, "code", "operation_failed")
            if code not in ERROR_STATUS:
                raise _HTTPError(409, "operation_failed") from None
            raise _HTTPError(ERROR_STATUS[code], code) from None

    async def _dispatch_room(self, path: str, query: str, body: dict | None):
        service = self.server.service
        mcp = self.principal == "mcp"
        if mcp != path.startswith('/api/mcp/'):
            raise _HTTPError(403, "token_scope_rejected")
        if mcp:
            if self.headers.get('X-LAC-Instance') != self.server.instance_id:
                raise _HTTPError(409, 'stale_instance')
            path = '/api/' + path[len('/api/mcp/'):]
        login = re.fullmatch(r"/api/native-adapters/([a-z][a-z0-9_-]{0,31})/(login|install)", path)
        if login:
            operate = service.native_login_operation if login[2] == "login" else service.native_install_operation
            if mcp:
                raise _HTTPError(403, "user_native_login_required")
            if query:
                raise _HTTPError(400, "invalid_arguments")
            if self.command == "GET":
                return await operate(login[1])
            if self.command == "POST" and not body:
                return await operate(login[1], "start")
            if self.command == "DELETE" and isinstance(body, dict) and set(body) == {"operation_id"} and self._text(body["operation_id"], 128):
                return await operate(login[1], "cancel", body["operation_id"])
            raise _HTTPError(400, "invalid_arguments")
        media = re.fullmatch(rf"/api/rooms/({TASK_ID})/media(?:/(resolve|attach|[0-9a-f]{{64}}/access))?", path)
        if media:
            if mcp:
                raise _HTTPError(403, "user_media_required")
            room_id, action = media[1], media[2]
            if self.command == "GET" and action is None and not query:
                return await service.list_room_media(room_id)
            if self.command == "GET" and action == "resolve":
                fields = parse_qs(query, keep_blank_values=True)
                if set(fields) != {"reference"} or len(fields["reference"]) != 1:
                    raise _HTTPError(400, "invalid_arguments")
                return await service.resolve_room_media(room_id, fields["reference"][0])
            if self.command == "POST" and not query:
                if action == "attach" and set(body) == {"path", "reference", "sha256", "source"}:
                    return await service.attach_room_media(room_id, **body)
                if action and action.endswith("/access") and set(body) == {"sha256"}:
                    asset = await service.access_room_media(room_id, action[:-7], body["sha256"])
                    now = time.time()
                    self.server.media_access = {key: value for key, value in self.server.media_access.items()
                                                if value["expires_at"] > now}
                    token = secrets.token_urlsafe(32)
                    expires = now + 20 * 60
                    self.server.media_access[token] = {"room_id": room_id, "asset": asset, "expires_at": expires}
                    return {"asset_id": asset["id"], "sha256": asset["sha256"], "url": "/room-media/" + token,
                            "expires_at": expires, **{key: asset[key] for key in ("mime", "bytes", "name")}}
            raise _HTTPError(400, "invalid_arguments")
        outbox = re.fullmatch(r"/api/outbox(?:/([^/]+)(/retire)?)?", path)
        if outbox:
            if mcp:
                raise _HTTPError(403, "user_outbox_required")
            instance = self.server.instance_id
            request_id = unquote(outbox[1]) if outbox[1] else None
            if self.command == "GET":
                fields = parse_qs(query, keep_blank_values=True)
                if ("scope" not in fields or any(len(values) != 1 for values in fields.values())
                        or set(fields) - ({"scope"} if request_id else {"scope", "after_seq", "through_seq", "limit"})
                        or outbox[2]):
                    raise _HTTPError(400, "invalid_arguments")
                scope = fields.pop("scope")[0]
                if request_id:
                    return await service.read_outbox(scope, request_id, instance_id=instance)
                if any(re.fullmatch(r"0|[1-9][0-9]{0,18}", values[0]) is None for values in fields.values()):
                    raise _HTTPError(400, "invalid_arguments")
                return await service.list_outbox(scope, instance_id=instance, **{key: int(values[0]) for key, values in fields.items()})
            if self.command == "POST" and not query:
                if not request_id and {"scope", "request_id", "kind", "room_id", "body"} <= set(body) and not (
                        set(body) - {"scope", "request_id", "kind", "room_id", "body", "legacy_unknown", "source_seq"}):
                    if "legacy_unknown" in body and body["legacy_unknown"] is not True:
                        raise _HTTPError(400, "invalid_arguments")
                    return await service.stage_outbox(**body, instance_id=instance)
                if request_id and outbox[2] and set(body) == {"scope", "fingerprint", "disposition"}:
                    return await service.retire_outbox(body["scope"], request_id, body["fingerprint"], body["disposition"], instance_id=instance)
            raise _HTTPError(400, "invalid_arguments")
        match = ROOM_ROUTE.fullmatch(path)
        if self.command == "POST" and match and match[2] == "delivery-repository":
            if mcp:
                raise _HTTPError(403, "user_delivery_repository_required")
            if query or not isinstance(body, dict) or set(body) != {"workspace_id"}:
                raise _HTTPError(400, "invalid_arguments")
            return await service.bind_delivery_repository(match[1], workspace_id=body["workspace_id"])
        if self.command == "GET" and match and match[2] == "code-status":
            if mcp:
                raise _HTTPError(403, "user_code_status_required")
            fields = parse_qs(query, keep_blank_values=True)
            if (set(fields) - {"workspace", "refresh_remote"} or any(len(value) != 1 or not value[0] for value in fields.values())
                    or fields.get("refresh_remote", ["1"])[0] != "1"):
                raise _HTTPError(400, "invalid_arguments")
            return await service.read_code_status(match[1], workspace=fields.get("workspace", [None])[0],
                refresh_remote=fields.get("refresh_remote") == ["1"])
        if self.command == "GET" and match and match[2] in {"state", "events", "text", "jobs"}:
            fields = parse_qs(query, keep_blank_values=True)
            if any(len(values) != 1 for values in fields.values()):
                raise _HTTPError(400, "invalid_arguments")
            parameters = {key: values[0] for key, values in fields.items()}
            if match[2] == "state":
                if fields:
                    raise _HTTPError(400, "invalid_arguments")
                return await service.read_room_state(match[1])
            if match[2] == "events":
                if set(fields) - {"before_seq", "after_seq", "through_seq", "limit", "max_bytes"} or any(
                        re.fullmatch(r"0|[1-9][0-9]{0,18}", value) is None for value in parameters.values()):
                    raise _HTTPError(400, "invalid_event_cursor")
                return await service.read_room_events(match[1], **{key: int(value) for key, value in parameters.items()})
            if match[2] == "jobs":
                if set(parameters) != {"job_id"} or re.fullmatch(TASK_ID, parameters["job_id"]) is None:
                    raise _HTTPError(400, "invalid_arguments")
                return await service.read_room_job(match[1], parameters["job_id"])
            for key in ("seq", "result_version"):
                if key in parameters:
                    if re.fullmatch(r"0|[1-9][0-9]{0,18}", parameters[key]) is None:
                        raise _HTTPError(400, "invalid_arguments")
                    parameters[key] = int(parameters[key])
            return await service.read_room_text(match[1], parameters)
        if self.command == "GET" and path.startswith("/api/partners/lifecycle-operations/"):
            if mcp:
                raise _HTTPError(403, "user_lifecycle_required")
            request_id = unquote(path[len("/api/partners/lifecycle-operations/"):])
            if query or not self._text(request_id, 512):
                raise _HTTPError(400, "invalid_arguments")
            return await service.read_partner_operation(request_id)
        if self.command == "GET" and path == "/api/project-deletions":
            if mcp:
                raise _HTTPError(403, "user_lifecycle_required")
            fields = parse_qs(query, keep_blank_values=True)
            if set(fields) != {"request_id"} or len(fields["request_id"]) != 1 or not self._text(fields["request_id"][0], 512):
                raise _HTTPError(400, "invalid_arguments")
            return await service.read_project_deletion(fields["request_id"][0])
        operation = re.fullmatch(rf"/api/room-lifecycle-operations/({TASK_ID})", path)
        if self.command == "GET" and (operation or path == "/api/room-lifecycle-operations"):
            if mcp:
                raise _HTTPError(403, "user_lifecycle_required")
            if operation:
                if query:
                    raise _HTTPError(400, "invalid_arguments")
                return await service.read_lifecycle_operation(operation[1])
            fields = parse_qs(query, keep_blank_values=True)
            if set(fields) != {"request_id"} or len(fields["request_id"]) != 1 or not self._text(fields["request_id"][0], 512):
                raise _HTTPError(400, "invalid_arguments")
            return await service.read_lifecycle_operation(request_id=fields["request_id"][0])
        if self.command == "POST" and match and match[2] == "recover-native":
            if mcp:
                raise _HTTPError(403, "user_native_permission_required")
            if (set(body) not in ({"request_id", "run_id", "revision", "digest"},
                                 {"request_id", "run_id", "revision", "digest", "user_message"})
                    or not self._text(body.get("request_id"), 512)
                    or "user_message" in body and not self._message_text(body["user_message"])
                    or not isinstance(body.get("run_id"), str) or re.fullmatch(TASK_ID, body["run_id"]) is None
                    or type(body.get("revision")) is not int or body["revision"] < 1
                    or not isinstance(body.get("digest"), str) or re.fullmatch(r"[0-9a-f]{64}", body["digest"]) is None):
                raise _HTTPError(400, "invalid_arguments")
            return await service.recover_native(match[1], **body)
        if self.command == "POST" and match and match[2] == "native-handoff":
            if mcp:
                raise _HTTPError(403, "user_native_permission_required")
            if (set(body) != {"request_id", "run_id", "revision", "digest", "execution_policy", "runtime_settings"}
                    or not self._text(body.get("request_id"), 512)
                    or not isinstance(body.get("run_id"), str) or re.fullmatch(TASK_ID, body["run_id"]) is None
                    or type(body.get("revision")) is not int or body["revision"] < 1
                    or not isinstance(body.get("digest"), str) or re.fullmatch(r"[0-9a-f]{64}", body["digest"]) is None
                    or not isinstance(body.get("runtime_settings"), dict)):
                raise _HTTPError(400, "invalid_arguments")
            return await service.native_handoff(match[1], **body)
        if self.command == "GET" and match and match[2] == "file":
            fields = parse_qs(query, keep_blank_values=True)
            if (set(fields) != {"run_id", "revision", "digest", "path", "sha256"}
                    or any(len(values) != 1 for values in fields.values())
                    or re.fullmatch(TASK_ID, fields["run_id"][0]) is None
                    or re.fullmatch(r"0|[1-9][0-9]{0,9}", fields["revision"][0]) is None
                    or any(re.fullmatch(r"[0-9a-f]{64}", fields[name][0]) is None for name in ("digest", "sha256"))
                    or not self._text(fields["path"][0], 1024)):
                raise _HTTPError(400, "invalid_arguments")
            return await service.read_delivery_file(match[1], **{name: values[0] for name, values in fields.items() if name != "revision"},
                                                    revision=int(fields["revision"][0]))
        if self.command == "GET" and match and match[2] == "runs":
            fields = parse_qs(query,keep_blank_values=True)
            if (set(fields) != {"run_id"} or len(fields["run_id"]) != 1
                    or re.fullmatch(TASK_ID,fields["run_id"][0]) is None):
                raise _HTTPError(400,"invalid_arguments")
            if self.compact_view:
                return await service.read_run(match[1], fields["run_id"][0], compact=True)
            return await service.read_run(match[1], fields["run_id"][0])
        if self.command == "GET" and match and match[2] in {"export","preview"}:
            fields = parse_qs(query,keep_blank_values=True)
            expected = {"revision","path"} if match[2] == "preview" else {"revision"}
            if (set(fields) != expected or any(len(values) != 1 for values in fields.values())
                    or re.fullmatch(r"0|[1-9][0-9]{0,9}",fields.get("revision",[""])[0]) is None):
                raise _HTTPError(400,"invalid_arguments")
            revision = int(fields["revision"][0])
            if match[2] == "preview":
                return await service.preview_room(match[1],revision,fields["path"][0])
            return await service.export_room(match[1],revision)
        if query:
            raise _HTTPError(400, "invalid_arguments")
        runtime_refresh = RUNTIME_REFRESH_ROUTE.fullmatch(path)
        if path == "/api/runtime-options" and self.command == "GET":
            return await service.runtime_options()
        if runtime_refresh and self.command != "GET":
            if mcp:
                raise _HTTPError(403, "user_runtime_settings_required")
            if body:
                raise _HTTPError(400, "invalid_arguments")
            return await service.refresh_runtime_options(runtime_refresh[1])
        if mcp and body and "runtime_settings" in body:
            raise _HTTPError(403, "user_runtime_settings_required")
        if path == "/api/availability/refresh" and self.command != "GET":
            if mcp:
                raise _HTTPError(403, "user_account_required")
            if body:
                raise _HTTPError(400, "invalid_arguments")
            return await service.refresh_availability()
        recheck = AVAILABILITY_RECHECK_ROUTE.fullmatch(path)
        if recheck and self.command != "GET":
            if mcp:
                raise _HTTPError(403, "user_account_required")
            if body:
                raise _HTTPError(400, "invalid_arguments")
            return await service.request_availability_recheck(recheck[1])
        account_refresh = ACCOUNT_REFRESH_ROUTE.fullmatch(path)
        if account_refresh and self.command != "GET":
            if mcp:
                raise _HTTPError(403, "user_account_required")
            if body:
                raise _HTTPError(400, "invalid_arguments")
            return await service.refresh_partner_account(account_refresh[1])
        lifecycle = LIFECYCLE_ROUTE.fullmatch(path)
        if lifecycle and self.command != "GET":
            if mcp:
                raise _HTTPError(403, "user_lifecycle_required")
            kind, identifier = lifecycle.groups()
            if (set(body) not in ({"action"}, {"action", "request_id"}) or not isinstance(body["action"], str)
                    or body["action"] not in {"archive", "restore", "remove"}
                    or "request_id" in body and (kind not in {"rooms", "projects", "partners"} or not self._text(body["request_id"], 512)
                                                  or kind == "projects" and body["action"] != "remove")
                    or kind in {"projects", "rooms"} and re.fullmatch(TASK_ID, identifier) is None
                    or kind == "partners" and TARGET_ID.fullmatch(identifier) is None):
                raise _HTTPError(400, "invalid_arguments")
            operation = {"projects": service.set_project_lifecycle,
                         "partners": service.set_partner_lifecycle,
                         "rooms": service.set_room_lifecycle}[kind]
            return await operation(identifier, body["action"], **({"request_id": body["request_id"]} if "request_id" in body else {}))
        # A display name is user configuration, like registering the project.
        project_name = PROJECT_NAME_ROUTE.fullmatch(path)
        if project_name and self.command != "GET":
            if mcp:
                raise _HTTPError(403, "user_lifecycle_required")
            if not isinstance(body, dict) or set(body) != {"name"} or not isinstance(body["name"], str):
                raise _HTTPError(400, "invalid_arguments")
            return await service.rename_project(project_name[1], body["name"])
        if self.command == "GET":
            if path == "/api/status":
                status = await service.status()
                return {**status, 'instance_id': self.server.instance_id} if mcp else status
            if path == "/api/rooms":
                return await service.list_rooms(compact=True) if self.compact_view else await service.list_rooms()
            if path == "/api/projects":
                return await service.list_projects()
            if path == "/api/partners":
                partners = await service.list_partners()
                if mcp:
                    partners = {**partners,"partners":[{key:value for key,value in item.items() if key not in {"connection", "account", "account_source"}}
                                                       for item in partners["partners"]]}
                return partners
            match = ROOM_ROUTE.fullmatch(path)
            if match and match[2] is None:
                return await service.read_room(match[1])
            raise _HTTPError(404, "route_not_found")
        if path == "/api/rooms":
            self._message_fields(body, "goal", allow_mode=True)
            extra = {"source": "mcp"} if mcp else {}
            if "mode" in body:
                extra["mode"] = body["mode"]
            if mcp and body.get("execution_policy") is not None:
                raise _HTTPError(403, "user_execution_policy_required")
            for field in ("participants", "project_id", "collaboration", "work_mode", "runtime_settings", "execution_policy"):
                if field in body:
                    extra[field] = body[field]
            return await service.create_room(body["goal"], body["target"], body["request_id"], **extra)
        # Project paths and executable profiles are user configuration. An
        # agent connected through MCP cannot enlarge those permissions itself.
        if not mcp and path == "/api/projects":
            folder = body.get("selection_kind") == "folder"
            fields = {"name", "path", "selection_kind"} if folder else {"name", "path", "files"}
            if (set(body) != fields or not self._text(body["name"], 80)
                    or not self._text(body["path"], 4096)
                    or not folder and not isinstance(body["files"], list)):
                raise _HTTPError(400, "invalid_arguments")
            return await service.register_project(body["name"], body["path"], body.get("files"),
                                                   selection_kind="folder" if folder else "files")
        if not mcp and path == "/api/partners":
            if (not {"id", "name", "adapter"}.issubset(body)
                    or set(body) - {"id", "name", "adapter", "avatar", "bean_style", "connection"}):
                raise _HTTPError(400, "invalid_arguments")
            return await service.add_partner(**body)
        if not mcp and path == "/api/partners/discover":
            if body:
                raise _HTTPError(400,"invalid_arguments")
            return await service.refresh_partners()
        partner = PARTNER_ROUTE.fullmatch(path)
        if not mcp and partner:
            if partner[2] == "appearance" and set(body) == {"bean_style"} and isinstance(body["bean_style"], str):
                return await service.set_partner_appearance(partner[1], body["bean_style"])
            if partner[2] == "workload" and set(body) == {"level"} and isinstance(body["level"], str):
                return await service.set_partner_workload(partner[1], body["level"])
            if partner[2] == "connection" and set(body) == {"connection"} and isinstance(body["connection"],dict):
                return await service.configure_partner(partner[1],body["connection"])
            if partner[2] == "check" and not body:
                return await service.check_partner(partner[1])
            if partner[2] == "account-source" and set(body) == {"source"} and isinstance(body["source"], str):
                return await service.set_partner_account_source(partner[1], body["source"])
            if (partner[2] == "enabled" and set(body) in ({"enabled"}, {"enabled", "request_id"}) and type(body["enabled"]) is bool
                    and ("request_id" not in body or self._text(body["request_id"], 512))):
                return await service.set_partner_enabled(partner[1], body["enabled"], **({"request_id":body["request_id"]} if "request_id" in body else {}))
            raise _HTTPError(400, "invalid_arguments")
        match = ROOM_ROUTE.fullmatch(path)
        if match and match[2] == "members":
            if mcp:
                raise _HTTPError(403, "user_member_invitation_required")
            if set(body) != {"request_id", "participants"} or not self._text(body.get("request_id"), 512):
                raise _HTTPError(400, "invalid_arguments")
            return await service.invite_members(match[1], body["participants"], body["request_id"])
        if match and match[2] in {"objective", "restart-objective", "continue", "pause", "resume", "answer", "reassess-objective"}:
            if mcp:
                raise _HTTPError(403, "user_execution_policy_required")
            action = match[2]
            expected = {"request_id", "objective_id"} if action in {"pause", "resume"} else {"request_id", "run_id", "revision", "digest"}
            if action in {"objective", "restart-objective"}:
                expected.add("execution_policy")
            if action == "answer":
                expected.update({"text", "objective_id"})
            if set(body) != expected or not self._text(body.get("request_id"), 512):
                raise _HTTPError(400, "invalid_arguments")
            identifier = body.get("objective_id") if action in {"pause", "resume"} else body.get("run_id")
            if not isinstance(identifier, str) or re.fullmatch(TASK_ID, identifier) is None:
                raise _HTTPError(400, "invalid_arguments")
            if action == "answer" and (not self._message_text(body.get("text"))
                    or not isinstance(body.get("objective_id"), str) or re.fullmatch(TASK_ID, body["objective_id"]) is None):
                raise _HTTPError(400, "invalid_arguments")
            if action not in {"pause", "resume"} and (type(body["revision"]) is not int and body["revision"] is not None
                    or type(body["revision"]) is int and body["revision"] < 0
                    or not isinstance(body["digest"], str) or re.fullmatch(r"[0-9a-f]{64}", body["digest"]) is None):
                raise _HTTPError(400, "invalid_arguments")
            return await service.objective_action(match[1], action, **body)
        if match and match[2] in {"runs","accept","fork"}:
            if mcp and match[2] == "accept":
                raise _HTTPError(403,"user_acceptance_required")
            if mcp and match[2] == "fork":
                raise _HTTPError(403,"user_fork_required")
            expected = {"request_id","run_id","revision","digest"}
            if match[2] == "runs":
                expected |= {"text","target","retry"}
                if "work_mode" in body:
                    expected.add("work_mode")
                if "runtime_settings" in body:
                    expected.add("runtime_settings")
                if "execution_policy" in body:
                    expected.add("execution_policy")
            if match[2] == "fork":
                expected |= {"text","target"}
            if (set(body) != expected or not self._text(body.get("request_id"),512)
                    or not isinstance(body.get("run_id"),str) or re.fullmatch(TASK_ID,body["run_id"]) is None
                    or (body["revision"] is not None and (type(body["revision"]) is not int or body["revision"] < 0))
                    or not isinstance(body["digest"],str) or re.fullmatch(r"[0-9a-f]{64}",body["digest"]) is None):
                raise _HTTPError(400,"invalid_arguments")
            if "work_mode" in body and (not isinstance(body["work_mode"], str) or body["work_mode"] not in WORK_MODES):
                raise _HTTPError(400,"invalid_arguments")
            extra = {key:body[key] for key in ("run_id","revision","digest")}
            if "work_mode" in body:
                extra["work_mode"] = body["work_mode"]
            if "runtime_settings" in body:
                extra["runtime_settings"] = body["runtime_settings"]
            if "execution_policy" in body:
                if mcp and body["execution_policy"] is not None:
                    raise _HTTPError(403, "user_execution_policy_required")
                extra["execution_policy"] = body["execution_policy"]
            if match[2] == "accept":
                return await service.accept_run(match[1],body["request_id"],**extra)
            if (not self._message_text(body["text"]) or not isinstance(body["target"],str)
                    or TARGET_ID.fullmatch(body["target"]) is None
                    or (match[2] == "runs" and type(body["retry"]) is not bool)):
                raise _HTTPError(400,"invalid_arguments")
            if match[2] == "fork":
                if type(body["revision"]) is not int or body["revision"] < 1:
                    raise _HTTPError(400,"invalid_arguments")
                return await service.fork_room(match[1],body["text"],body["target"],body["request_id"],**extra)
            return await service.start_run(match[1],body["text"],body["target"],body["request_id"],
                                           **extra,retry=body["retry"],source="mcp" if mcp else "user")
        if match and match[2] in {"messages", "messages/receipt"}:
            receipt = match[2] == "messages/receipt"
            if receipt and mcp:
                raise _HTTPError(403, "user_message_receipt_required")
            natural = "intent" in body or "binding" in body
            settings_fields = set(body) & {"work_mode", "runtime_settings"}
            if mcp and settings_fields:
                raise _HTTPError(403, "user_runtime_settings_required")
            if ("work_mode" in body and body["work_mode"] not in WORK_MODES
                    or "runtime_settings" in body and not isinstance(body["runtime_settings"], dict)):
                raise _HTTPError(400, "invalid_arguments")
            if natural:
                if mcp:
                    raise _HTTPError(403, "user_continuation_required")
                binding = body.get("binding")
                if (set(body) != {"text", "target", "request_id", "intent", "binding"} | settings_fields or body.get("intent") != "continue"
                        or not isinstance(binding, dict) or set(binding) != {"run_id", "revision", "digest"}
                        or not isinstance(binding["run_id"], str) or re.fullmatch(TASK_ID, binding["run_id"]) is None
                        or binding["revision"] is not None and (type(binding["revision"]) is not int or binding["revision"] < 0)
                        or not isinstance(binding["digest"], str) or re.fullmatch(r"[0-9a-f]{64}", binding["digest"]) is None):
                    raise _HTTPError(400, "invalid_arguments")
            self._message_fields({key: value for key, value in body.items() if key not in {"intent", "binding", "work_mode", "runtime_settings"}}, "text")
            extra = {"source": "mcp"} if mcp else {}
            extra.update({key: body[key] for key in settings_fields})
            if natural:
                extra.update(intent=body["intent"], binding=body["binding"])
            if receipt:
                return await service.read_message_receipt(match[1], body["text"], body["target"], body["request_id"],
                                                          **{key: value for key, value in extra.items() if key != "source"})
            return await service.send_message(match[1], body["text"], body["target"],
                                              body["request_id"], **extra)
        if match and match[2] == "cancel":
            if body:
                raise _HTTPError(400, "invalid_arguments")
            return await service.cancel_room(match[1])
        raise _HTTPError(404, "route_not_found")


def create_room_server(service: RoomService, *, port: int = 8769,
                       asset_dir: Path | None = None) -> RoomUIServer:
    """Bind without model calls. Caller owns serve_forever / server_close."""
    return RoomUIServer(service, port=port, asset_dir=asset_dir)


def publish_connection(server: RoomUIServer, runtime: Path) -> Path:
    """Publish a private capability for explicitly configured local MCP clients."""
    target = runtime / 'connection.json'
    temporary = runtime / ('.connection-' + str(uuid4()))
    payload = {'url': server.url, 'token': server.mcp_token, 'instance_id': server.instance_id}
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(payload, output)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description="Beanest local agent rooms")
    parser.add_argument("--runtime", type=Path, default=Path("runtime/rooms"))
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    service = None
    server = None
    try:
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        service = RoomService(state_path=runtime / "rooms.sqlite3",
                              workspace=runtime / "workspaces")
        server = create_room_server(service, port=args.port)
        connection = publish_connection(server, runtime)
    except (OSError, ValueError, RoomError, ProjectCatalogError) as error:
        # A failed bind must release this service's runtime ownership too.
        if server is not None:
            server.server_close()
        elif service is not None:
            import asyncio
            asyncio.run(service.shutdown())
        if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
            parser.exit(1, f"Port {args.port} is already in use. Stop the other program or pass --port with a free port.\n")
        parser.exit(1, "Local rooms could not start. Check the runtime directory or port.\n")

    # Desktop startup reads only this first line; no auth token is printed.
    print(server.url, flush=True)
    previous = signal.getsignal(signal.SIGTERM)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        # An obsolete capability is never silently retargeted to a future service.
        try:
            connection.unlink(missing_ok=True)
        finally:
            server.server_close()
        if server.runner.shutdown_error:
            print("Local rooms stopped; child cleanup was not confirmed.", file=sys.stderr)
            raise SystemExit(2)


if __name__ == "__main__":
    main()
