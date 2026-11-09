"""Read-only wire projections. Authorization is calculated before these helpers."""
from __future__ import annotations

import hashlib
import json

from .message_capacity import MAX_MESSAGE_UTF8_BYTES, MAX_JSON_BODY_BYTES

STATE_MAX_BYTES = 2 * 1024 * 1024
EVENT_DEFAULT_BYTES = 256 * 1024
EVENT_DEFAULT_LIMIT = 100
EVENT_MAX_LIMIT = 500
TEXT_PREVIEW_CHARACTERS = 240


def wire_bytes(value):
    # Match the HTTP shell's actual serializer, including escaping controls.
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def transport_limits():
    return {"max_message_utf8_bytes": MAX_MESSAGE_UTF8_BYTES, "max_request_utf8_bytes": MAX_JSON_BODY_BYTES,
            "event_page_default_bytes": EVENT_DEFAULT_BYTES, "event_page_max_bytes": MAX_JSON_BODY_BYTES,
            "event_page_default_limit": EVENT_DEFAULT_LIMIT, "event_page_max_limit": EVENT_MAX_LIMIT}


def text_fields(field, text, kind, room_id, **identity):
    if not isinstance(text, str):
        return {field: text}
    return {field + "_preview": text[:TEXT_PREVIEW_CHARACTERS],
            field + "_truncated": len(text) > TEXT_PREVIEW_CHARACTERS, field + "_characters": len(text),
            field + "_text_ref": {"kind": kind, "room_id": room_id,
                                  "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), **identity}}


def replace_text(record, field, kind, room_id, **identity):
    return {key: value for key, value in record.items() if key != field} | text_fields(field, record.get(field), kind, room_id, **identity)


def compact_rooms(result):
    rooms = []
    for room in result["rooms"]:
        value = replace_text(room, "goal", "room_goal", room["id"])
        if room.get("objective_goal") is not None:
            value = replace_text(value, "objective_goal", "objective_goal", room["id"], objective_id=room["objective_id"])
        rooms.append(value)
    return {**result, "projection": "room_list_v1", "rooms": rooms}


def compact_partners(result):
    partners = []
    for partner in result["partners"]:
        monitor = partner.get("monitor") or {}
        tasks = []
        for task in monitor.get("current_tasks", []):
            value = replace_text(task, "room_goal", "room_goal", task["room_id"])
            value = replace_text(value, "current_task", "job_text", task["room_id"], job_id=task["job_id"])
            tasks.append(value)
        partners.append({**partner, "monitor": {**monitor, "current_tasks": tasks}})
    return {**result, "projection": "partner_list_v1", "partners": partners}


def compact_run(value, room_id):
    run = value["run"]
    team = value.get("team")
    if team:
        team = {**team, "tasks": [replace_text(task, "result", "task_result", room_id, run_id=run["id"],
            task_id=task["id"], result_version=task["result_version"]) for task in team["tasks"]]}
    return {**value, "projection": "run_state_v1", "room_id": room_id, "team": team,
            "run": replace_text(run, "goal", "run_goal", room_id, run_id=run["id"])}


def _original_goal_copies(value, original, room_id, objective_id):
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if isinstance(item, str) and len(original) > 8192 and item == original:
            result.update(text_fields(key, item, "objective_goal", room_id, objective_id=objective_id))
        else:
            result[key] = _original_goal_copies(item, original, room_id, objective_id) if isinstance(item, dict) else item
    return result


def room_state(snapshot, roles, permission_for, *, job_ids=None):
    """Retain the host's complete eligibility verdict; omit repeated raw data."""
    room_id = snapshot["room"]["id"]
    events = snapshot["events"]
    through = events[-1]["seq"] if events else 0
    result = {**snapshot, "projection": "room_state_v1", "through_seq": through,
              "event_count": len(events), "events": []}
    result["room"] = replace_text(snapshot["room"], "goal", "room_goal", room_id)
    result["jobs"] = [replace_text(job, "text", "job_text", room_id, job_id=job["id"]) for job in snapshot["jobs"]]
    current = snapshot.get("current_run")
    referenced_runs = {current["id"]} if current else set()
    def collect_run_refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"run_id", "source_run_id", "inherited_from_run"} and isinstance(item, str):
                    referenced_runs.add(item)
                elif isinstance(item, (dict, list)):
                    collect_run_refs(item)
        elif isinstance(value, list):
            for item in value:
                collect_run_refs(item)
    collect_run_refs(snapshot.get("objective"))
    collect_run_refs((current or {}).get("source_plan"))
    result.update(run_count=len(snapshot["runs"]), runs_projection="current_and_referenced")
    result["runs"] = [replace_text(run, "goal", "run_goal", room_id, run_id=run["id"])
                      for run in snapshot["runs"] if run["id"] in referenced_runs]
    if current:
        result["current_run"] = replace_text(current, "goal", "run_goal", room_id, run_id=current["id"])
    objective = snapshot.get("objective")
    if objective:
        value = replace_text(objective, "original_goal", "objective_goal", room_id, objective_id=objective["id"])
        if objective.get("continuation") and isinstance(objective["continuation"].get("user_request"), str):
            value["continuation"] = replace_text(objective["continuation"], "user_request", "objective_request", room_id,
                                                  objective_id=objective["id"])
        if objective.get("user_decision"):
            value["user_decision"] = replace_text(objective["user_decision"], "text", "objective_decision", room_id,
                                                   objective_id=objective["id"])
        result["objective"] = _original_goal_copies(value, objective["original_goal"], room_id, objective["id"])
    for section in ("team", "collaboration"):
        if snapshot.get(section) and "tasks" in snapshot[section]:
            versions = {task["id"]: task["result_version"] for task in (snapshot.get("team") or {}).get("tasks", [])}
            result[section] = {**snapshot[section], "tasks": [replace_text(task, "result", "task_result", room_id,
                run_id=current["id"], task_id=task["id"], result_version=versions[task["id"]])
                for task in snapshot[section]["tasks"]]}
    if snapshot.get("project"):
        result["project"] = {**snapshot["project"], "files": [
            {**{key: value for key, value in file.items() if key != "content"}, "content_available": isinstance(file.get("content"), str)}
            for file in snapshot["project"].get("files", [])]}

    claimed, cleanup, activities, progress, flags = {}, {}, {}, {}, {}
    latest_cache_miss = latest_handoff = None
    for event in events:
        job_id = event.get("job_id")
        if not job_id:
            continue
        if event["kind"] == "native_cache_miss":
            latest_cache_miss = event
        elif event["kind"] == "round_handoff_recorded":
            latest_handoff = event
        elif event["kind"] == "job_claimed":
            claimed[job_id] = event
        elif event["kind"] == "job_cleanup":
            cleanup[job_id] = event.get("cleanup_confirmed")
        elif event["kind"] == "native_tool_activity":
            activities[job_id] = event
        elif event["kind"] == "message" and event.get("native_progress"):
            progress[job_id] = event
        elif event["kind"] in {"native_partial_inspection_queued", "partner_handoff_inspection", "native_readonly_discussion"}:
            flags.setdefault(job_id, set()).add(event["kind"])
    result["latest_native_cache_miss"] = latest_cache_miss
    result["latest_round_handoff"] = latest_handoff
    observations = {item["job_id"]: item for item in snapshot.get("native_executions", [])}
    selected = set(job_ids) if job_ids is not None else {job["id"] for job in snapshot["jobs"] if job["status"] in {"queued", "working"}}
    if job_ids is None:
        last = {job["provider"]: job["id"] for job in snapshot["jobs"] if job["status"] not in {"queued", "working"}}
        selected.update(last.values())
    result.update(job_count=len(snapshot["jobs"]), jobs_projection="active_and_latest")
    live, jobs = [], []
    for job in snapshot["jobs"]:
        if job["id"] not in selected:
            continue
        claim = claimed.get(job["id"])
        # Older claims predate frozen choices; current room settings are not
        # evidence of the mode or model used by that historical invocation.
        mode = claim.get("work_mode", "legacy") if claim else snapshot["room"]["work_mode"]
        settings = claim.get("runtime_settings") if claim else snapshot["room"]["runtime_settings"]
        phase_flags = flags.get(job["id"], set())
        permission_job = {"work_mode": mode, "runtime_settings": settings,
                          "native_partial_inspection": "native_partial_inspection_queued" in phase_flags,
                          "partner_handoff_inspection": "partner_handoff_inspection" in phase_flags}
        observed = observations.get(job["id"], {})
        message = progress.get(job["id"])
        public = ({"seq": message["seq"], "sender": message["sender"], "created_at": message["created_at"],
                   **text_fields("text", message["text"], "event_text", room_id, seq=message["seq"]),
                   "producer_truncated": message.get("truncated") is True,
                   "original_characters": message.get("original_characters", len(message["text"]))} if message else None)
        view = {**{key: job[key] for key in ("id", "provider", "kind", "status")},
            **dict.fromkeys(("phase", "run_id", "task_id", "result_version", "assignment_epoch")), **roles.get(job["id"], {}),
            "work_mode": mode, "runtime_settings": settings, "claim_seq": claim["seq"] if claim else None,
            "claimed_at": claim["created_at"] if claim else None, "cleanup_confirmed": cleanup.get(job["id"]),
            "permission": observed.get("permission") or permission_for(permission_job),
            "permission_observed": observed.get("permission") is not None,
            "partner_handoff_inspection": permission_job["partner_handoff_inspection"],
            "native_partial_inspection": permission_job["native_partial_inspection"],
            "native_readonly_discussion": "native_readonly_discussion" in phase_flags,
            "latest_native_activity": activities.get(job["id"]), "latest_public_progress": public}
        jobs.append({**replace_text(job, "text", "job_text", room_id, job_id=job["id"]), **view})
        if job["status"] in {"queued", "working"}:
            live.append(view)
    result["jobs"] = jobs
    result["live"] = {"jobs": live}
    latest_native = {entry["provider"]: entry["job_id"] for entry in snapshot.get("native_executions", [])}
    native_ids = selected | set(latest_native.values())
    result.update(native_execution_count=len(snapshot.get("native_executions", [])), native_executions_projection="active_and_latest")
    result["native_executions"] = [entry for entry in snapshot.get("native_executions", []) if entry["job_id"] in native_ids]
    executions = snapshot.get("executions", [])
    result.update(execution_count=len(executions), executions_projection="active_and_latest")
    result["executions"] = []
    for execution in executions:
        if execution["status"] not in {"queued", "working"} and execution is not executions[-1]:
            continue
        view = {key: value for key, value in execution.items() if key != "result"}
        raw = execution.get("result")
        view["result_present"] = raw is not None
        summary = {} if isinstance(raw, dict) else None
        if summary is not None:
            for key in ("status", "reason", "exit_code", "cleanup_confirmed", "passed", "failed"):
                item = raw.get(key)
                if key not in raw or not isinstance(item, (str, int, float, bool, type(None))):
                    continue
                if isinstance(item, str) and len(item) > TEXT_PREVIEW_CHARACTERS:
                    summary.update({key + "_preview": item[:TEXT_PREVIEW_CHARACTERS], key + "_truncated": True})
                else:
                    summary[key] = item
        view["result_summary"] = summary
        if raw is not None:
            view.update(text_fields("result", json.dumps(raw, ensure_ascii=True, allow_nan=False), "execution_result", room_id,
                                    execution_id=execution["id"]))
        result["executions"].append(view)
    return result
