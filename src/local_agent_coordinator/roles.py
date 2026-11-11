"""What each partner is for in one room, as a set of abilities.

A role only narrows who is asked first. When no available member has the
ability a step needs, everyone is asked as before, so a room never waits on a
role nobody can fill. A member without a role can do everything.
"""
from __future__ import annotations

import re

ABILITIES = ("plan", "work", "review", "checkpoint")
ALL = frozenset(ABILITIES)
# Taking part in planning means either writing the plan or claiming work from it.
COORDINATION = frozenset({"plan", "work"})

BUILTIN = {
    "builder": {"abilities": ("work",),
                "duty": "Implement tasks from the shared plan. Leave plan writing and reviewing to the others."},
    "reviewer": {"abilities": ("review",),
                 "duty": "Review delivered tasks independently. Do not claim implementation tasks."},
    "planner": {"abilities": ("plan", "checkpoint"),
                "duty": "Write the shared plan and assess progress at checkpoints. Do not claim implementation tasks."},
}
WORKLOADS = ("low", "normal", "high")
_CUSTOM_ID = re.compile(r"custom:[a-z0-9][a-z0-9-]{0,39}\Z")
NAME_LIMIT = 20
DUTY_LIMIT = 300


class RoleError(ValueError):
    pass


def normalized_role(value):
    """Canonical stored role, or None for the default all-round member."""
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise RoleError("invalid_role")
    identifier = value["id"]
    if identifier == "all":
        return None
    if identifier in BUILTIN:
        if set(value) - {"id", "name", "duty", "abilities"}:
            raise RoleError("invalid_role")
        return {"id": identifier, "abilities": list(BUILTIN[identifier]["abilities"])}
    if not _CUSTOM_ID.fullmatch(identifier) or set(value) != {"id", "name", "duty", "abilities"}:
        raise RoleError("invalid_role")
    name, duty, abilities = value["name"], value["duty"], value["abilities"]
    if (not isinstance(name, str) or not name.strip() or len(name) > NAME_LIMIT
            or not isinstance(duty, str) or len(duty) > DUTY_LIMIT
            or not isinstance(abilities, list) or not abilities
            or any(item not in ABILITIES for item in abilities) or len(set(abilities)) != len(abilities)
            or any(ord(character) < 32 for character in name + duty.replace("\n", ""))):
        raise RoleError("invalid_role")
    return {"id": identifier, "name": name.strip(), "duty": duty.strip(),
            "abilities": [item for item in ABILITIES if item in abilities]}


def normalized_workload(value):
    if value is None:
        return None
    if value not in WORKLOADS:
        raise RoleError("invalid_workload")
    return value


def member_abilities(settings):
    """Abilities of members that have a role; members without one are absent."""
    members = (settings or {}).get("members") or {}
    return {member: frozenset(choice["role"]["abilities"]) for member, choice in members.items()
            if isinstance(choice, dict) and choice.get("role")}


def can(abilities, member, needed):
    """Whether member has any of the needed abilities."""
    needed = {needed} if isinstance(needed, str) else set(needed)
    return bool(abilities.get(member, ALL) & needed)


def prefer(candidates, needed, abilities):
    """Candidates able to do the step, in their given order; all of them when none is."""
    able = [member for member in candidates if can(abilities, member, needed)]
    return able or list(candidates)


def workload_levels(settings):
    members = (settings or {}).get("members") or {}
    return {member: choice["workload"] for member, choice in members.items()
            if isinstance(choice, dict) and choice.get("workload")}


def prompt_role(settings, member):
    """What the member is told about its own role, or None without one."""
    role = (((settings or {}).get("members") or {}).get(member) or {}).get("role")
    if not role:
        return None
    builtin = BUILTIN.get(role["id"])
    return {"id": role["id"], "abilities": role["abilities"],
            "duty": builtin["duty"] if builtin else role["duty"],
            **({"name": role["name"]} if not builtin else {})}
