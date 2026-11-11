"""Human-readable delivery state derived from recorded work, never chat claims."""
from __future__ import annotations

from copy import deepcopy
import re

from .runtime_settings import read_only
from .execution_policy import task_finished, WITHDRAWN


class _SystemText(str):
    """A display string with source-bound translation parts, never inferred from content."""
    def __new__(cls, text, parts):
        value = super().__new__(cls, text)
        value.parts = parts
        return value

    def __add__(self, other):
        return _SystemText(str(self) + str(other), self.parts + _text_parts(other))

    def __radd__(self, other):
        return _SystemText(str(other) + str(self), _text_parts(other) + self.parts)


def _text_parts(value):
    return value.parts if isinstance(value, _SystemText) else [{"text": str(value)}]


def _system(key, *values):
    params = {f"p{index}": str(value) for index, value in enumerate(values)}
    text = re.sub(r"\{(p\d+)\}", lambda match: params.get(match[1], match[0]), key)
    return _SystemText(text, [{"key": key, "params": params}])


def _listed(items):
    """Raw items joined by a translated separator, so English never shows 、."""
    joined = _SystemText("", [])
    for index, item in enumerate(items):
        joined = (joined + _system("、") if index else joined) + str(item)
    return joined


def _with_display_i18n(view):
    # Only explicitly authored system strings carry metadata. Model questions,
    # task titles, results, and unrecognized provider reasons remain literal.
    def attach(container, field):
        value = container.get(field)
        if isinstance(value, _SystemText):
            container[field + "_i18n"] = {"parts": deepcopy(value.parts)}
            container[field] = str(value)
    attach(view, "summary")
    for task in view.get("tasks", []):
        attach(task, "waiting_for")
    outcome = view["outcome"]
    for field in ("reason", "next_action"):
        attach(outcome, field)
    attach(outcome["validation"], "summary")
    outcome["remaining_i18n"] = [
        {"parts": deepcopy(value.parts)} if isinstance(value, _SystemText) else None
        for value in outcome.get("remaining", [])
    ]
    outcome["remaining"] = [str(value) for value in outcome.get("remaining", [])]
    return view


_REASONS = {
    "all_partners_quota_exhausted": _system("伙伴的额度都用完了，进度已保留。有伙伴额度恢复后会自动继续。"),
    "native_preflight_failed": _system("伙伴的模型服务暂时连不上，提示词没有发出，已有工作不受影响。"),
    "plan_read_only": _system("本轮只讨论方案，未保存文件修改；需要动手时可明确开始新一轮。"),
    "permission_read_only": _system("本轮统一为只读，不能修改文件或运行项目；需要动手时可在下一轮调整权限。"),
    "invalid_team_task": _system("伙伴提交的任务清单缺少有效信息，尚未接受这次交付。"),
    "invalid_agent_response": _system("伙伴回复不符合协作协议，未接受这次交付。"),
    "invalid_team_commands": _system("伙伴提交的任务操作无效，协作已停止。"),
    "invalid_project_edits": _system("伙伴的精确修改格式无效，未接受这批修改。"),
    "project_edit_conflict": _system("待替换文本或文件版本不匹配，未接受这批修改。"),
    "project_edit_ambiguous": _system("待替换文本出现多次，无法确定修改位置；这批修改未接受。"),
    "project_edit_overlap": _system("同一批精确修改的位置重叠，未接受这批修改。"),
    "project_edit_limit": _system("精确修改超过文本容量限制，未接受这批修改。"),
    "team_plan_missing": _system("伙伴尚未提交可执行的任务清单，协作已停止。"),
    "team_plan_stalled": _system("分工仍有未认领或未确认的事项，一次明确协调后仍未解决；请查看任务清单中的负责人和依赖。"),
    "team_revision_limit": _system("这项交付修订后仍未通过互审，需要你决定下一步。"),
    "team_files_forbidden": _system("这次回复超出了当前任务的文件写入权限，改稿未接受。"),
    "subscription_limit_reached": _system("工具报告订阅额度已用完，协作已停止。"),
    "agent_quota_exhausted": _system("当前账号已知额度不足，未继续派发调用；检查账号与额度后再明确尝试。"),
    "agent_quota_unconfirmed": _system("当前登录身份尚未确认，之前记录的额度限制仍保留；未启动新调用，请先检查账号。"),
    "cli_nonzero_exit": _system("本机工具异常退出，尚未确认的工作已停止。"),
    "budget_exhausted": _system("本次调用上限已到，未完成的任务保留在清单里。"),
    "objective_calls_exhausted": _system("你设置的调用预算已用完，未完成的任务保留在清单里。"),
    "objective_deadline_exceeded": _system("你设置的结束时间已到，现有成果和未完成任务已保留。"),
    "objective_no_progress": _system("伙伴连续几次给出相同的证据或没有变化的结果，已暂停，免得空转。"),
    "deadline_exceeded": _system("本次任务期限已到，未继续派发工作。"),
    "capability_unavailable": _system("这项工作需要尚未接入的能力，暂时无法完成。"),
    "team_capability_unavailable": _system("当前执行方式不具备这项工作所需的能力。"),
    "execution_failed": _system("隔离检查没有通过，交付仍需修正。"),
    "execution_unknown": _system("隔离检查或清理结果尚未确认，协作已停止。"),
    "execution_repair_no_change": _system("修复后文件没有实际变化，未重复运行同一份成果。"),
    "execution_repair_budget": _system("剩余调用不足以完成修复和重新互审，协作已停止。"),
    "execution_repair_unavailable": _system("原任务无法安全重开修复，现有文件和失败记录已保留。"),
    "execution_binding_changed": _system("检查记录与当前文件不一致，未采用这份结果继续工作。"),
    "execution_version_stale": _system("检查对应较早的文件版本，需要核对当前成果。"),
    "team_room_stopped": _system("任务已经停止，没有接受迟到的改动。"),
    "team_result_missing": _system("任务约定的交付尚未齐全，当前不能验收。"),
    "project_revision_conflict": _system("改稿版本发生冲突，未覆盖已有文件。"),
    "project_file_conflict": _system("文件内容发生冲突，未覆盖已有文件。"),
    "project_owner_conflict": _system("文件负责人发生冲突，改动未保存。"),
    "project_limit_exceeded": _system("文件大小或数量超过本次限制，最新改稿未保存；已有成果保留。"),
    "artifact_sensitive_content": _system("最新改稿含疑似凭据内容，未保存或转交；请先用虚构占位内容替换。"),
    "team_scope_conflict": _system("任务的修改范围或文件负责人冲突，需要重新确认分工。"),
    "service_restarted": _system("服务曾中断，未确认的工作没有自动重跑。"),
    "cancel_requested": _system("你已停止任务，已保留现有交付。"),
    "cancelled": _system("任务已停止，已保留现有交付。"),
    "interrupted": _system("任务已中断，未确认的工作没有自动重跑。"),
    "native_no_progress": _system("同一工具失败反复出现，授权文件没有变化；工作副本已保留，任务暂停。"),
    "native_progress_unconfirmed": _system("无法确认原生工具是否仍有实际进展；已保留工作副本，等待核对。"),
    "native_side_effects_unconfirmed": _system("伙伴遇到额度限制，原生工具可能已修改私有文件；已保留现场，核对后再重新分派。"),
    "native_cleanup_unconfirmed": _system("原生进程清理未确认，停止新增派发。"),
    "native_changes_not_imported": _system("工作副本有尚不能导入的改动，请查看对应伙伴的文件状态。"),
    "native_review_unobserved": _system("这次审核缺少精确版本或实际工具观察，未当作独立审核通过。"),
}


def collaboration_view(snapshot: dict, *, active=False) -> dict:
    room, project = snapshot["room"], snapshot.get("project") or {}
    team = snapshot.get("team")
    current_run = snapshot.get("current_run") or {}
    current_run_id = current_run.get("id") or (team or {}).get("run_id")
    revision = project.get("revision") if project.get("available") else None
    artifacts = [{"path": file["path"], "sha256": file["sha256"], "revision": revision}
                 for file in project.get("files", [])]
    checks = [execution for execution in snapshot.get("executions", [])
              if execution["revision"] == revision
              and execution["snapshot_sha256"] == project.get("snapshot_sha256")
              and (not current_run_id or execution.get("team_run_id") == current_run_id)]
    verified_execution = next((execution for execution in reversed(checks)
        if execution["status"] == "completed" and (execution.get("result") or {}).get("status") == "completed"
        and type((execution.get("result") or {}).get("exit_code")) is int
        and execution["result"]["exit_code"] == 0 and execution["result"].get("cleanup_confirmed") is True), None)
    validation = {
        "scope": "sandbox_execution" if verified_execution else "not_run",
        "summary": (_system("当前文件版本的隔离进程退出为0，资源清理已确认；程序输出仍需核对。")
                    if verified_execution else _system("没有当前版本的实际运行通过记录。")),
        "revision": revision if verified_execution else None,
    }
    if not team:
        if any(job["kind"] in {"review", "help"} and job["status"] == "completed"
               for job in snapshot["jobs"]) and not verified_execution:
            validation.update(scope="static_review", summary=_system("记录包含静态互审；没有当前版本的完整运行验收。"))
        terminal = room["status"] in {"cancelled", "interrupted", "failed", "budget_exhausted"}
        state = ("unknown" if room["status"] == "unknown" else "stopped" if terminal else
                 "round_finished" if room["status"] == "completed" else
                 "needs_user" if room["status"] == "needs_user" else "in_progress")
        return _with_display_i18n({"mode": "legacy", "phase": "stopped" if terminal else "waiting",
                "summary": _system("历史协作记录，未记录共享任务清单。"), "tasks": [],
                "outcome": {"state": state,
                    "reason": _system("本轮已经回复，目标尚未记录验收。") if state == "round_finished"
                              else _system("这份记录使用早期的草案与互审流程。"),
                    "next_action": _system("查看实际改稿、审核及检查范围后，再决定下一步。"),
                    "validation": validation, "artifacts": artifacts,
                    "remaining": [_system("改动保存在隔离副本；原项目未回写，项目和测试未运行。")]
                                 if room["mode"] in {"project_files", "project_folder"} else []}})

    tasks = []
    remaining = []
    names = {member["id"]: member["name"] for member in snapshot.get("members", [])}
    current_files = {file["path"]: file for file in project.get("files", [])}
    stale = False
    for task in team["tasks"]:
        if task["status"] == WITHDRAWN:
            # Listed so the board stays true, but never remaining, delivered or reviewed work.
            tasks.append({"id": task["id"], "title": task["title"], "owner": task["owner"],
                          "status": task["status"], "waiting_for": "", "result": ""})
            continue
        waiting = (task.get("blocked_reason") or
                   (_system("等待 {p0} 的协助", names.get(task.get("help_to"), task.get("help_to"))) if task["status"] == "waiting_peer" else
                    _system("等待独立审核") if task["status"] == "review" else
                    _system("等待依赖：") + _listed(task["dependencies"]) if task["status"] == "assigned" and task["dependencies"] else ""))
        tasks.append({"id": task["id"], "title": task["title"], "owner": task["owner"],
                      "status": task["status"], "waiting_for": _REASONS.get(waiting, waiting),
                      "result": task.get("result") or ""})
        if not task_finished(task):
            remaining.append(task["title"] + (_system("：") + _REASONS.get(waiting, waiting) if waiting else _system("：尚未交付并通过互审")))
        if task_finished(task):
            for file in task.get("result_files") or []:
                if current_files.get(file["path"], {}).get("sha256") != file["sha256"]:
                    stale = True
                    remaining.append(task["title"] + _system("：交付文件后来发生变化，需要重新核对。"))
    withdrawn = [task for task in tasks if task["status"] == WITHDRAWN]
    tasks_in_scope = tasks
    tasks = [task for task in tasks if task["status"] != WITHDRAWN]
    if not verified_execution and tasks and all(task_finished(task) for task in tasks):
        self_checks = sum(task["status"] == "self_checked" for task in tasks)
        validation.update(scope="self_check" if self_checks == len(tasks) else "mixed_review" if self_checks else "static_review",
                          summary=_system("包含伙伴自检，不能当作独立互审；组合结果仍需实际验收。") if self_checks else
                                  _system("各子任务已记录独立静态互审；组合结果仍需实际验收。"))
    reason_code = team.get("stop_reason")
    reason = _REASONS.get(reason_code, "")
    phase = team["phase"]
    if phase in {"unknown", "interrupted", "cancelled", "stopped"}:
        state = "unknown" if phase == "unknown" else "stopped"
        reason = reason or _system("协作因异常停止，未完成项仍保留。")
        next_action = (_system("不用你操作；有伙伴额度恢复后会自动继续。") if reason_code == "all_partners_quota_exhausted"
                       else _system("点“继续”再试一次；没有发出的工作不会重复。") if reason_code == "native_preflight_failed"
                       else _system("先核对停止原因和现有交付；未确认结果不会自动重跑。"))
    elif stale or room["status"] == "needs_user" or any(task["status"] == "blocked" for task in tasks):
        state = "needs_user"
        reason = reason or _system("有工作尚未完成，需要补充信息或确认处理方式。")
        next_action = _system("查看未完成项，在同一聊天补充所需信息。")
    elif phase == "ready_for_user" and not active:
        objective = snapshot.get("objective")
        checkpoint_finished = objective and objective["status"] in {"complete", "awaiting_continue"}
        if not tasks and withdrawn and not remaining and not checkpoint_finished:
            state = "ready_for_user"
            reason = _system("本轮的任务都被你撤掉了，没有新的交付。")
            next_action = _system("需要做别的，直接在这里说。")
        elif (not tasks and not checkpoint_finished) or remaining:
            state = "needs_user"
            reason = _system("任务清单还有未确认的交付，尚不能验收。")
            next_action = _system("先核对任务清单及实际交付，处理未完成项。")
        elif room["mode"] == "python_sandbox" and not read_only(room.get("work_mode"), room.get("runtime_settings")) and not verified_execution:
            state = "needs_user"
            reason = _system("子任务已互审，但当前版本的隔离检查尚未确认通过。")
            next_action = _system("查看隔离检查记录，确认失败或尚未完成的部分。")
            remaining.append(_system("当前版本的隔离检查未确认通过。"))
        else:
            checked_by_self = sum(task["status"] == "self_checked" for task in tasks)
            # One whole sentence each; three spliced fragments left English without control of spacing.
            state, reason = "ready_for_user", (_system("子任务已交付并自检，等待你验收。") if checked_by_self == len(tasks) and tasks else
                                               _system("子任务已交付并互审和自检，等待你验收。") if checked_by_self else
                                               _system("子任务已交付并互审，等待你验收。"))
            next_action = _system("查看成果和检查范围；可以接受本轮，也可以在这里继续提出修改。")
            if current_run.get("can_continue") is False:
                next_action = _system("查看成果和检查范围；可以接受本轮。此聊天已到轮数上限，新目标请点“新任务”。")
            if current_run.get("accepted_at") is not None:
                state, reason = "accepted", _system("你已接受本轮交付。")
                next_action = (_system("此聊天已到轮数上限，新目标请点“新任务”。")
                               if current_run.get("can_continue") is False else _system("需要继续改进时，直接在这里说。"))
    else:
        state, reason = "in_progress", _system("正在讨论分工。") if phase == "planning" else _system("伙伴正在按共享清单推进。")
        if phase == "working" and any(task.get("capacity_attempts") and task["status"] in {"assigned", "working"}
                                      for task in team["tasks"]):
            reason = _system("文件提案超出容量，原负责人正在精简后重新提交，仍需独立互审。")
        if team.get("execution_repair_id") and phase == "working":
            reason = _system("隔离检查发现问题，伙伴正在按原分工修正并重新互审。")
        next_action = _system("可以继续补充信息；需要你决定时会在这里说明。")
    objective = snapshot.get("objective")
    if objective and objective["status"] == "paused":
        state, reason = "paused", _system("当前调用结束后暂停。") if objective["pause_requested"] else _system("目标已暂停，现有成果和分工保留。")
        next_action = _system("准备好后继续；总目标的截止时间不会重置。")
    elif objective and objective["status"] == "needs_user":
        state, reason = "needs_user", (objective.get("next_step") or {}).get("question") or _system("下一步需要你确认。")
        next_action = _system("查看下一步建议或补充决定后，再明确开始。")
    elif objective and objective["status"] == "checkpoint":
        state, reason, next_action = "in_progress", _system("正在核对总目标是否完成和下一步。"), _system("需要你决定时会提出具体问题。")
    elif objective and objective["status"] in {"complete", "awaiting_continue"}:
        if not objective.get("delivery", {}).get("verified", True):
            state, reason, next_action = "needs_user", _system("目标交付的文件版本有变化，需要核对。"), _system("查看未确认的成果。")
        elif objective["status"] == "awaiting_continue":
            state, reason = "ready_for_user", _system("本阶段已交付，下一步已准备好。")
            next_action = _system("继续下一步将沿用同一目标的剩余时间与调用次数，无需先接受本阶段。")
        elif current_run.get("accepted_at") is None:
            state, reason, next_action = "ready_for_user", _system("本目标已交付，可以查看成果后验收。"), _system("查看本目标的交付、检查范围及下一步建议。")
        if any(task["status"] == "self_checked" for task in tasks):
            reason += _system("含伙伴自检，不代表全部经过独立互审。")
    if room["mode"] == "project_folder":
        validation["summary"] += _system("改动保存在隔离副本；原项目未回写，项目和测试未运行。")
    if room["mode"] == "native_project":
        proofs = [entry for entry in snapshot.get("native_executions", []) if entry.get("phase") == "review"
                  and entry.get("status") == "completed" and entry.get("cleanup_confirmed") is True
                  and (entry.get("tool_results") or 0) > 0 and entry.get("binding")]
        checked = [task for task in team["tasks"] if task_finished(task) and task["status"] != "self_checked"
                   and any(entry["binding"]["task_id"] == task["id"]
                           and entry["binding"]["result_version"] == task["result_version"]
                           and entry["binding"]["revision"] == task["result_revision"]
                           and entry["provider"] != task["owner"] for entry in proofs)]
        if checked:
            validation.update(scope="native_review", revision=revision,
                summary=_system("审核者获得了精确版本的完整私有项目，并有实际原生工具观察；不代表每行已读或注册测试已通过。"))
        else:
            validation.update(scope="self_check" if any(task["status"] == "self_checked" for task in tasks) else "not_run",
                summary=_system("可查看原生工具的实际运行记录；模型结论不能代替测试验收。"))
        validation["summary"] += _system("改动保存在私有副本，豆窝未自动合回原目录；Full Access 不是沙箱。")
    summary = _system("共享任务 {p0}/{p1} 已交付并检查", sum(task_finished(task) for task in tasks), len(tasks))
    if withdrawn:
        summary += _system("，{p0} 项已撤掉", len(withdrawn))
    return _with_display_i18n({"mode": "team", "phase": phase,
            "summary": summary,
            "tasks": tasks_in_scope, "outcome": {"state": state, "reason": reason, "next_action": next_action,
                "validation": validation, "artifacts": artifacts, "remaining": remaining}})
