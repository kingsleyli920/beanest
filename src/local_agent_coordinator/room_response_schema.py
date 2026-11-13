"""Small native structured-output shape; the service still validates semantics.

This does not authorize an action or replace owner, revision, secret and byte
checks. In particular, file paths stay strings so imported text projects need
not pretend to be Python projects.
"""
from __future__ import annotations

from .agent_registry import safe_agent_id
from .block_contract import BLOCK_CAUSES
from .round_handoff import handoff_schema
from .room_contract import COMMAND_FIELDS as _COMMAND_FIELDS, MAX_PARTNER_TEXT, MAX_RESPONSE_COMMANDS, MAX_ROOM_MEMBERS, MAX_TASK_PATHS


_TEAM_PHASES = frozenset({"plan", "coordinate", "work", "review", "help", "user", "checkpoint", "checkpoint_review"})
_PHASE_COMMANDS = {
    "plan": {"propose", "claim", "plan_ready"},
    "coordinate": {"propose", "claim", "plan_ready"},
    "work": {"complete", "request_help", "block", "extend"},
    "review": {"review", "block"},
    "help": set(),
    # Any partner answering the user may change the unstarted part of a working
    # board (new tasks, adjust); the service refuses it in any other team phase.
    "user": {"claim", "propose", "adjust"},
    "checkpoint": {"checkpoint"},
    "checkpoint_review": {"checkpoint_review"},
}
PLAN_AUTHOR_COMMANDS = frozenset({"propose", "adjust"})
# Command kinds added after release; a call made by an older release sent a
# schema without them, and its receipt must still prove itself after upgrade.
ADDED_COMMAND_KINDS = ("adjust", "extend")
# Per-phase schemas require these as non-empty strings. A stable schema leaves
# them nullable, so the service enforces the same rule from the job's phase.
PHASE_NONEMPTY_TEXT = {
    "review": frozenset({"reason"}),
    "checkpoint_review": frozenset({"reason"}),
    "checkpoint": frozenset({"summary"}),
}

# Sits on the transport object only: no stored command contains it, the
# per-phase schemas never offer it, and the service reads it for checkpoints.
STABLE_ONLY_FIELD = "handoff"


def stable_native_schema(adapter):
    """Whether this adapter's resumed sessions need one byte-identical schema.

    Claude puts --json-schema ahead of the cached prompt, so a schema that
    changes with the team phase rewrites the whole session cache on resume.
    """
    return adapter == "claude"


def stable_command_shape(*, has_peers=True):
    """Every command a room member could ever send, independent of phase.

    Only the roster narrows it, and the roster already bounds the schema.
    """
    kinds = [kind for kind in _COMMAND_FIELDS if has_peers or kind != "request_help"]
    return kinds, set().union(*(_COMMAND_FIELDS[kind] for kind in kinds)) | {STABLE_ONLY_FIELD}


def command_shape(*, team_phase=None, can_propose=True, has_peers=True, native=False):
    """One transport shape shared by schema generation and strict parsing."""
    if native and (not isinstance(team_phase, str) or team_phase not in _TEAM_PHASES):
        raise ValueError("invalid_response_schema_context")
    allowed = set(_COMMAND_FIELDS) if team_phase is None else _PHASE_COMMANDS[team_phase]
    kinds = [kind for kind in _COMMAND_FIELDS if kind in allowed
             and (kind not in PLAN_AUTHOR_COMMANDS or can_propose or team_phase == "user")
             and (has_peers or kind not in {"review", "request_help"} or team_phase == "review" and kind == "review")]
    fields = set().union(*(_COMMAND_FIELDS[kind] for kind in kinds)) if native else set().union(*_COMMAND_FIELDS.values())
    return kinds, fields


def native_room_response_schema(adapter, *, member_ids, provider, team_phase, can_propose):
    """The schema one native team call sends, chosen by its real adapter."""
    common = dict(project=True, native=True, collaboration=True, member_ids=member_ids, provider=provider)
    if not stable_native_schema(adapter):
        return response_schema(**common, team_phase=team_phase, can_propose=can_propose)
    if type(team_phase) is not str or team_phase not in _TEAM_PHASES or type(can_propose) is not bool:
        raise ValueError("invalid_response_schema_context")
    return response_schema(**common, stable=True)


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def response_schema(*, project: bool, member_ids: tuple[str, ...], provider: str,
                    collaboration: bool = False, team_phase: str | None = None,
                    can_propose: bool = True, folder: bool = False, native: bool = False,
                    stable: bool = False, omit_kinds: frozenset = frozenset()) -> dict:
    """Return an independent schema for one room member's JSON response.

    The bounded roster must include the responding member. A single-member
    room permits no actions, without emitting an invalid empty enum. Team callers
    may restrict the response to the recorded job phase; omitting it permits all
    existing team command types. Storage still validates task permissions.
    A stable native schema depends only on the roster: the service applies the
    job's phase rules to the response instead.
    """
    if (type(project) is not bool or type(collaboration) is not bool or type(can_propose) is not bool
            or type(folder) is not bool or (folder and not project)
            or type(native) is not bool or native and (not project or not collaboration)
            or type(stable) is not bool or stable and (not native or team_phase is not None)
            or (team_phase is not None and (type(team_phase) is not str or team_phase not in _TEAM_PHASES))
            or type(member_ids) is not tuple
            or not 1 <= len(member_ids) <= MAX_ROOM_MEMBERS
            or any(type(member) is not str or not safe_agent_id(member) for member in member_ids)
            or len(set(member_ids)) != len(member_ids)
            or type(provider) is not str or not safe_agent_id(provider)
            or provider not in member_ids):
        raise ValueError("invalid_response_schema_context")

    peers = [member for member in member_ids if member != provider]
    recipient = {"type": "string"}
    if peers:
        recipient["enum"] = peers
    action = _object({
        "type": {"type": "string", "enum": ["request"]},
        "to": recipient,
        "kind": {"type": "string", "enum": ["review", "help"]},
        "text": {"type": "string"},
    })
    properties = {
        "message": {"type": "string"},
        "actions": {"type": "array", "maxItems": 1 if peers else 0, "items": action},
        "state": {"type": "string", "enum": ["done", "working", "needs_user"]},
    }
    if collaboration:
        kinds, fields = (stable_command_shape(has_peers=bool(peers)) if stable else
                         command_shape(team_phase=team_phase, can_propose=can_propose, has_peers=bool(peers), native=native))
        if omit_kinds and native:
            # The schema an older release sent, before these command kinds existed.
            kinds = [kind for kind in kinds if kind not in omit_kinds]
            fields = set().union(*(_COMMAND_FIELDS[kind] for kind in kinds)) | ({STABLE_ONLY_FIELD} if stable else set())
        command_properties = {"type": {"type": "string", "enum": kinds}}
        command_properties.update({field: {"type": ["string", "null"]}
                                   for field in sorted(fields - {"type"})})
        for field in {"id", "task_id"} & fields:
            command_properties[field].update(pattern=r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                description=("REQUIRED non-null task identifier when type is propose (id) or claim/complete/"
                             "block/review/request_help/adjust/extend (task_id), for example T1. Null only when unused."))
        command_properties["type"]["description"] = (
            "Choose an allowed operation. All fields used by that operation must be non-null; "
            "propose requires id such as T1. Optional request_help repair_paths may be null "
            "for ordinary assistance; otherwise only unused fields are null.")
        if stable:
            command_properties["type"]["description"] += (
                " This schema is shared by every phase: the host accepts only the operations "
                "the prompt names for the current job_phase.")
            command_properties[STABLE_ONLY_FIELD] = handoff_schema()
        for field in {"dependencies", "write_paths", "repair_paths"} & fields:
            command_properties[field] = {"type": ["array", "null"], "items": {"type": "string"}}
        if "repair_paths" in fields:
            command_properties["repair_paths"]["description"] = (
                "For request_help, optional exact project-relative paths needing engineering repair "
                "within the existing goal. Null or [] requests ordinary assistance. The host checks "
                "paths and canonical ownership, then schedules owner work and review; this field "
                "does not authorize the helper to edit arbitrary files or expand permissions. "
                "For every other command, use null."
            )
        for field, values in {"capability": ["text", "python", "image", None], "verdict": ["accept", "changes_requested", None],
                              "decision": ["continue", "complete", "needs_user", None], "to": [*peers, None]}.items():
            if field in fields:
                command_properties[field] = {"type": ["string", "null"], "enum": values}
        if "cause" in fields:
            command_properties["cause"] = {
                "type": ["string", "null"],
                "enum": [*sorted(BLOCK_CAUSES), None],
                "description": (
                    "For block, classify the actual cause using concrete evidence. "
                    "technical means an engineering obstacle within existing authority; "
                    "user_decision needs a human choice; permission_required needs permission; "
                    "external_dependency needs an external change. This label grants no authority "
                    "and never overrides human instructions. Null preserves an unknown legacy "
                    "cause or marks an unused field; never guess a cause from wording."
                ),
            }
        if native:
            for field, description in {"result": "Concrete complete result only; review findings belong in reason.",
                                       "reason": "Required review/block findings; do not use result or summary instead.",
                                       "summary": "Checkpoint summary only; do not use for review findings."}.items():
                if field in fields:
                    command_properties[field]["description"] = description
            for field in PHASE_NONEMPTY_TEXT.get(team_phase, ()):
                command_properties[field].update(type="string", minLength=1)
        if native:
            for field in fields - {"type", STABLE_ONLY_FIELD}:
                users = [kind for kind in kinds if field in _COMMAND_FIELDS[kind]]
                rule = ("Used only by " + ", ".join(users) + "; for every other command type this field must be null. ")
                command_properties[field]["description"] = rule + command_properties[field].get("description", "")
        properties.pop("actions")
        properties["message"] = {"type": "string", "minLength": 1, "maxLength": MAX_PARTNER_TEXT}
        # Fixed fields avoid native generators losing fields across object-union
        # branches. The service requires unused fields to be null, then restores
        # the canonical command before transactional permission checks.
        properties["commands"] = {"type": "array", "maxItems": MAX_RESPONSE_COMMANDS if kinds else 0,
                                  "items": _object(command_properties) if kinds else _object({})}
    if project:
        properties.update({
            "base_revision": {"type": "integer", "minimum": 0},
            "files": {"type": "array", "maxItems": 0 if collaboration and team_phase not in (None, "work") else 16,
                      "items": _object({
                "path": {"type": "string"},
                "previous_sha256": {"type": ["string", "null"]},
                "content": {"type": "string"},
            })},
        })
        if collaboration or folder:
            properties["edits"] = {"type": ["array", "null"],
                "maxItems": 16 if team_phase in (None, "work") else 0,
                "items": _object({
                    "path": {"type": "string"},
                    "base_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"},
                })}
    if folder:
        request = _object({
            "kind": {"type": "string", "enum": ["list", "read", "search"]},
            "path": {"type": "string"},
            "query": {"type": ["string", "null"]},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        })
        request["type"] = ["object", "null"]
        properties["project_request"] = request
    if native:
        properties["files"]["maxItems"] = 0
        properties["edits"]["maxItems"] = 0
        properties["unchanged_paths"] = {"type": "array", "maxItems": MAX_TASK_PATHS,
            "items": _object({"path": {"type": "string"}, "reason": {"type": "string", "minLength": 1}})}
        properties["resolved_conflicts"] = {"type": "array", "maxItems": MAX_TASK_PATHS, "items": {"type": "string"}}
    return _object(properties)
