"""Run-scoped provider choices and host permissions, without implicit CLI reads."""
from __future__ import annotations

import re

from .native_setup import NATIVE_IDS
from .roles import RoleError, normalized_role, normalized_workload


PERMISSIONS = (
    {"id": "plan", "name": "Plan", "legacy": False, "description": "使用原生计划或只读模式检查与讨论；可用工具遵循各原生工具的能力。"},
    {"id": "full_access", "name": "Full Access", "legacy": False, "description": "使用原生 Full Access 按用户目标操作文件、命令和联网；源项目写入无需豆窝另行批准。"},
    {"id": "read_only", "name": "只读（旧版）", "legacy": True, "description": "保留已有只读配置的权限映射，不自动提升为新 Plan。"},
    {"id": "workspace_write", "name": "工作区写入（旧版）", "legacy": True, "description": "保留已有工作区写入配置的权限映射，不自动提升为 Full Access。"},
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}\Z")
_EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class RuntimeSettingsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def normalized_settings(value, members, work_mode):
    """Canonical storage form; effective permission is always host-computed."""
    if value is None:
        return None
    if not isinstance(members, (list, tuple)) or any(not isinstance(member, str) for member in members):
        raise RuntimeSettingsError("invalid_runtime_settings")
    if (not isinstance(value, dict) or set(value) - {"permission", "effective_permission", "members"}
            or not {"permission", "members"}.issubset(value)
            or not isinstance(value["permission"], str)
            or value["permission"] not in {item["id"] for item in PERMISSIONS}
            or not isinstance(value["members"], dict)
            or set(value["members"]) - set(members)):
        raise RuntimeSettingsError("invalid_runtime_settings")
    choices = {}
    for identifier in members:
        choice = value["members"].get(identifier, {})
        if not isinstance(choice, dict) or set(choice) - {"model", "effort", "role", "workload"}:
            raise RuntimeSettingsError("invalid_runtime_settings")
        model, effort = choice.get("model"), choice.get("effort")
        if (model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model))
                or effort is not None and (not isinstance(effort, str) or not _EFFORT.fullmatch(effort))
                or effort is not None and model is None):
            raise RuntimeSettingsError("invalid_runtime_settings")
        choices[identifier] = {"model": model, "effort": effort}
        try:
            role, workload = normalized_role(choice.get("role")), normalized_workload(choice.get("workload"))
        except RoleError as error:
            raise RuntimeSettingsError(str(error)) from None
        # Only chosen extras are stored, so a room without them keeps its old shape.
        if role is not None:
            choices[identifier]["role"] = role
        if workload is not None:
            choices[identifier]["workload"] = workload
    permission = value["permission"]
    effective = ("read_only" if permission in {"read_only", "workspace_write"} else "plan") if work_mode == "plan" else permission
    return {"permission": permission,
            "effective_permission": effective,
            "members": choices}


def read_only(work_mode, settings):
    return work_mode == "plan" or bool(settings and settings["effective_permission"] in {"plan", "read_only"})


def read_only_capabilities(room_mode):
    """Task capabilities a read-only room may still plan.

    Local development runs every partner in a read-only mode that can still
    run non-modifying commands (Codex's sandbox, Claude's plan mode, Kimi under
    a write-denying sandbox), so a task that only needs running is allowed
    there; writes stay refused through empty write_paths and no files/edits.
    """
    return frozenset({"text", "python"}) if room_mode == "native_project" else frozenset({"text"})


def native_permission(work_mode, settings, *, inspection=False):
    """Keep a claimed choice frozen; internal recovery retains its old boundary."""
    if inspection:
        return "read_only"
    frozen = (settings or {}).get("effective_permission")
    if frozen is not None:
        return frozen
    return "read_only" if work_mode == "plan" else "workspace_write"


def configuration_update(current, members, *, work_mode=None, runtime_settings=None):
    """An omitted field inherits the last accepted desired configuration."""
    if work_mode is None and runtime_settings is None:
        return None
    if work_mode is not None and work_mode not in {"plan", "build"}:
        raise RuntimeSettingsError("invalid_work_mode")
    mode = current["work_mode"] if work_mode is None else work_mode
    settings = current["runtime_settings"] if runtime_settings is None else runtime_settings
    return {"work_mode": mode, "runtime_settings": normalized_settings(settings, members, mode)}


def model_choice(choice):
    """The part of a member's choice a provider tool understands."""
    return {"model": choice.get("model"), "effort": choice.get("effort")} if choice else None


def validate_choices(settings, specs, catalogs):
    if settings is None:
        return
    for identifier, choice in settings["members"].items():
        if choice["model"] is None:
            continue
        spec = specs.get(identifier)
        if spec is None or not spec.supports_runtime_settings or spec.adapter not in NATIVE_IDS:
            raise RuntimeSettingsError("runtime_settings_unsupported")
        catalog = catalogs.get(spec.adapter, {})
        if catalog.get("state") != "ready":
            raise RuntimeSettingsError("runtime_options_required")
        model = next((item for item in catalog.get("models", []) if item["id"] == choice["model"]), None)
        if model is None:
            raise RuntimeSettingsError("runtime_model_unsupported")
        if choice["effort"] is not None and choice["effort"] not in model["efforts"]:
            raise RuntimeSettingsError("runtime_effort_unsupported")
