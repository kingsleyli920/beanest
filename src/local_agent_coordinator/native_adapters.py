"""Subscription CLI turns with explicitly selected native tool permissions.

Full access is genuinely unsandboxed. ``cwd`` selects a working directory, not a
security boundary; callers must arrange private workspaces and file ownership.
Tool receipts report observed execution, never infer tests from assistant prose.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import signal
import shlex
import re
import subprocess
import tempfile
import time
import uuid

from . import adapters, job_progress

_PERMISSIONS = {'plan', 'read_only', 'workspace_write', 'full_access'}
_CODEX_TOOLS = {'command_execution', 'file_change', 'mcp_tool_call', 'web_search',
                'todo_list', 'plan', 'context_compaction', 'image_generation',
                'tool_call', 'function_call', 'collab_tool_call'}
def _claude_tool_name_valid(name):
    """Validate wire names; native permissions decide which tools may execute."""
    return (isinstance(name, str)
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]{0,255}', name) is not None)


_MAX_LINE = 2 * 1024 * 1024
_MAX_RECEIPT = 64 * 1024 * 1024
_PUBLIC_PROGRESS_CACHE_SIZE = 256
_MAX_PUBLIC_PROGRESS_BYTES = 16 * 1024
ACTIVITY_OPERATIONS = frozenset({
    'read_file', 'edit_file', 'write_file', 'search_files', 'run_command',
    'search_web', 'fetch_web', 'update_plan', 'delegate', 'generate_image', 'use_tool',
})
ACTIVITY_EXECUTABLES = frozenset({'rg', 'git', 'python', 'python3', 'godot', 'node', 'npm', 'npx', 'pytest'})
_TOOL_OPERATIONS = {
    'Read': 'read_file', 'Edit': 'edit_file', 'MultiEdit': 'edit_file',
    'NotebookEdit': 'edit_file', 'Write': 'write_file', 'file_change': 'edit_file',
    'Glob': 'search_files', 'Grep': 'search_files',
    'Bash': 'run_command', 'PowerShell': 'run_command', 'command_execution': 'run_command',
    'WebSearch': 'search_web', 'web_search': 'search_web', 'WebFetch': 'fetch_web',
    'TodoWrite': 'update_plan', 'todo_list': 'update_plan', 'plan': 'update_plan',
    'TaskCreate': 'update_plan', 'TaskUpdate': 'update_plan',
    'Task': 'delegate', 'Agent': 'delegate', 'collab_tool_call': 'delegate',
    'image_generation': 'generate_image',
}


def _activity_summary(tool, value):
    """Return labels from finite vocabularies, never native argument content.

    An executable label describes only a requested command's leading program.
    It does not establish purpose, successful execution, or a passed test.
    Paths and file names are deliberately excluded, including relative names.
    """
    operation = _TOOL_OPERATIONS.get(tool, 'use_tool')
    summary = {'operation': operation}
    command = value.get('command') if isinstance(value, dict) else None
    if operation != 'run_command' or not isinstance(command, str) or len(command) > 8192:
        return summary
    try:
        words = shlex.split(command)
        # Codex commonly reports its shell wrapper. Unwrap only this exact
        # simple shape; never interpret shell code or inspect its output.
        if (len(words) == 3 and words[0].rsplit('/', 1)[-1] in {'sh', 'bash', 'zsh'}
                and words[1] in {'-c', '-lc'}):
            command = words[2]
            words = shlex.split(command)
    except ValueError:
        return summary
    if (not words or any(character in command for character in '\n\r\x00`$|;&<>')
            or '\\' in words[0]):
        return summary
    executable = words[0].rsplit('/', 1)[-1]
    if executable in ACTIVITY_EXECUTABLES:
        summary['executable'] = executable
    return summary


def _structured_final_text(text):
    """Recognize complete coordinator envelopes, not JSON as a content type."""
    candidate = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', candidate, re.DOTALL)
    if fenced is not None:
        candidate = fenced[1]
    try:
        value = adapters._strict_json(candidate)
    except (ValueError, UnicodeError, RecursionError):
        return False
    if not isinstance(value, dict) or not isinstance(value.get('message'), str):
        return False
    # Older simple turns use this closed response shape. Team turns use the
    # versioned file envelope. Neither rule suppresses ordinary JSON examples,
    # arrays, partial snippets or code blocks merely for being structured text.
    if set(value) == {'message', 'action'} and value['action'] == 'finish':
        return True
    required = {'message', 'state', 'commands', 'base_revision', 'files', 'edits',
                'unchanged_paths', 'resolved_conflicts'}
    return (set(value) == required and isinstance(value['state'], str)
            and type(value['base_revision']) is int
            and all(isinstance(value[key], list) for key in
                    ('commands', 'files', 'edits', 'unchanged_paths', 'resolved_conflicts'))
            and all(isinstance(command, dict) and isinstance(command.get('type'), str)
                    for command in value['commands']))
# Official Claude Code 2.1.316's finite supplier ceiling, not a Beanest goal
# duration or an unlimited command.
# Avoid its short foreground/auto-background defaults in Beanest-owned turns;
# the host's explicit task deadline and cancellation remain independent.
_CLAUDE_BASH_TIMEOUT_MS = '2147483647'
# A background command or monitor wakes the model later in a new turn of the
# same process, so one native call would end with several result events and be
# rejected as incomplete. Long checks run in the foreground instead.
_CLAUDE_BASH_ENV = {
    'BASH_DEFAULT_TIMEOUT_MS': _CLAUDE_BASH_TIMEOUT_MS,
    'BASH_MAX_TIMEOUT_MS': _CLAUDE_BASH_TIMEOUT_MS,
    'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1',
}
_CLAUDE_WAKEUP_TOOLS = 'Monitor,CronCreate,CronDelete,CronList,ScheduleWakeup'


def _nonnegative_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _claude_metadata_valid(event):
    """Validate known non-execution notices without retaining their content."""
    subtype = event.get('subtype')
    if subtype == 'vcs_state_changed':
        # Native schema calls this a repository-cache hint, never a completed
        # tool or proof of a commit/merge. Future kind strings are supported.
        if (not all(isinstance(event.get(key), str) for key in ('kind', 'cwd', 'session_id'))
                or 'branch' in event and not isinstance(event['branch'], str)):
            return False
        try:
            uuid.UUID(event['uuid'])
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
        return True
    if subtype == 'ui_invalidate':
        # Installed Claude Code 2.1.316's internal SDK notice asks remote
        # surfaces to redraw ui.render. It is neither a tool nor permission.
        required = {'type', 'subtype', 'event', 'uuid', 'session_id'}
        if (not required <= set(event) or set(event) - required - {'prompts_screened', 'instances'}
                or event['event'] != 'ui.render' or not isinstance(event['session_id'], str)
                or 'prompts_screened' in event and event['prompts_screened'] is not True):
            return False
        try:
            uuid.UUID(event['uuid'])
        except (ValueError, TypeError, AttributeError):
            return False
        if 'instances' in event:
            fields = {'surface', 'component', 'instance_id'}
            if not isinstance(event['instances'], list) or any(
                not isinstance(item, dict) or set(item) != fields
                or not all(isinstance(item[key], str) for key in fields)
                for item in event['instances']):
                return False
        return True
    if subtype == 'background_tasks_changed':
        # This is the SDK's complete live-task inventory, not a tool result or
        # a test outcome. The CLI still owns task execution and notifications.
        fields = {'type', 'subtype', 'tasks', 'uuid', 'session_id'}
        if (set(event) != fields or not isinstance(event['tasks'], list)
                or not isinstance(event['session_id'], str)):
            return False
        try:
            uuid.UUID(event['uuid'])
        except (ValueError, TypeError, AttributeError):
            return False
        required = {'task_id', 'task_type', 'description'}
        optional_text = {'run_id', 'subagent_type', 'parent_task_id'}
        optional_bool = {'ambient', 'awaited'}  # awaited is in installed 2.1.316.
        for task in event['tasks']:
            if (not isinstance(task, dict) or not required <= set(task)
                    or set(task) - required - optional_text - optional_bool
                    or not all(isinstance(task[key], str) for key in required)
                    or any(not isinstance(task[key], str) for key in optional_text & set(task))
                    or any(type(task[key]) is not bool for key in optional_bool & set(task))):
                return False
        return True
    if subtype == 'informational':
        return (isinstance(event.get('content'), str)
                and isinstance(event.get('level'), str)
                and event['level'] in {'info', 'notice', 'suggestion', 'warning'}
                and ('prevent_continuation' not in event or type(event['prevent_continuation']) is bool))
    if subtype == 'api_retry':
        return (all(_nonnegative_number(event[key]) for key in
                    ('attempt', 'max_retries', 'retry_delay_ms') if key in event)
                and (event.get('error_status') is None or type(event['error_status']) is int)
                and ('error_category' not in event or isinstance(event['error_category'], str)))
    if subtype == 'thinking_tokens':
        return all(_nonnegative_number(event.get(key)) for key in
                   ('estimated_tokens', 'estimated_tokens_delta'))
    if subtype == 'files_persisted':
        return (isinstance(event.get('files'), list) and isinstance(event.get('failed'), list)
                and isinstance(event.get('processed_at'), str))
    if subtype == 'task_updated':
        patch = event.get('patch')
        return (isinstance(event.get('task_id'), str) and isinstance(patch, dict)
                and ('status' not in patch or isinstance(patch['status'], str) and patch['status'] in
                     {'pending', 'running', 'completed', 'failed', 'killed'}))
    return None


_CLAUDE_STREAM_SYSTEM_SUBTYPES = frozenset({'init', 'permission_denied', 'compact_boundary', 'status',
                                            'task_started', 'task_progress', 'task_notification'})


def _claude_future_system_notice(event):
    """A new Claude Code system subtype is a notice, not execution or a result.

    Claude Code adds system notices between releases (``notification`` in
    2.1.319, ``ui_invalidate`` in 2.1.316). Tool execution only arrives as
    assistant tool_use / user tool_result blocks and the turn's outcome only as
    ``result``, so an unrecognized notice cannot carry authority. Rejecting it
    discarded completed turns and stopped every room after each CLI update.
    Known subtypes keep their strict shape checks.
    """
    subtype = event.get('subtype')
    return (event.get('type') == 'system' and isinstance(subtype, str)
            and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', subtype) is not None
            and subtype not in _CLAUDE_STREAM_SYSTEM_SUBTYPES
            and _claude_metadata_valid(event) is None)


def _claude_permission_denied_valid(event):
    """Official top-level advisory only; it never completes a tool call."""
    required = {'type', 'subtype', 'tool_name', 'tool_use_id', 'message', 'uuid', 'session_id'}
    optional = {'decision_reason_type', 'decision_reason_code', 'decision_reason'}
    if (not required <= set(event) or set(event) - required - optional
            or event['type'] != 'system' or event['subtype'] != 'permission_denied'
            or not isinstance(event['tool_name'], str)
            or not _claude_tool_name_valid(event['tool_name']) or event['tool_name'] == 'StructuredOutput'
            or not isinstance(event['tool_use_id'], str)
            or re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', event['tool_use_id']) is None
            or not isinstance(event['message'], str) or not isinstance(event['session_id'], str)
            or any(not isinstance(event[key], str) for key in optional if key in event)
            or 'decision_reason_code' in event and event['decision_reason_code'] not in {
                'outside_reads_blocked', 'memory_paused', 'classifier_transcript_too_long'}):
        return False
    try:
        return str(uuid.UUID(event['uuid'])) == event['uuid']
    except (ValueError, TypeError, AttributeError):
        return False


def _protocol_diagnostic(raw):
    """Record bounded protocol shape when rejected, never payload values."""
    value = {'type': 'native_protocol_diagnostic'}
    try:
        event = adapters._strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        return value
    if isinstance(event, dict):
        for source, target in (('type', 'event_type'), ('subtype', 'event_subtype')):
            label = event.get(source)
            if isinstance(label, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', label):
                value[target] = label
        if event.get('type') == 'system' and event.get('subtype') == 'permission_denied':
            fields = {'type', 'subtype', 'tool_name', 'tool_use_id', 'message', 'uuid', 'session_id',
                      'agent_id', 'decision_reason_type', 'decision_reason_code', 'decision_reason'}
            value['event_fields'] = sorted(set(event) & fields)
            value['field_types'] = {key: type(event[key]).__name__ for key in sorted(set(event) & fields)}
            value['unknown_field_count'] = len(set(event) - fields)
        if event.get('type') == 'user':
            # Names are allowlisted too: arbitrary object keys may themselves
            # contain secrets. Only container types and known booleans survive.
            fields = {'type', 'message', 'parent_tool_use_id', 'session_id', 'uuid',
                      'timestamp', 'isSynthetic', 'isReplay'}
            value['event_fields'] = sorted(set(event) & fields)
            value['unknown_field_count'] = len(set(event) - fields)
            for key in ('isSynthetic', 'isReplay'):
                if type(event.get(key)) is bool:
                    value[key] = event[key]
            message = event.get('message')
            value['message_type'] = type(message).__name__
            if isinstance(message, dict):
                value['message_fields'] = sorted(set(message) & {'role', 'content'})
                value['message_unknown_field_count'] = len(set(message) - {'role', 'content'})
                content = message.get('content')
                value['content_type'] = type(content).__name__
                if isinstance(content, list):
                    value['block_count'] = len(content)
                    value['blocks'] = []
                    for block in content[:8]:
                        shape = {'container': type(block).__name__}
                        if isinstance(block, dict):
                            block_fields = {'type', 'text', 'tool_use_id', 'is_error', 'content'}
                            shape['fields'] = sorted(set(block) & block_fields)
                            shape['unknown_field_count'] = len(set(block) - block_fields)
                            if isinstance(block.get('type'), str) and block['type'] in {'text', 'tool_result'}:
                                shape['type'] = block['type']
                            elif isinstance(block.get('type'), str):
                                shape['type'] = 'unknown'
                        value['blocks'].append(shape)
    return value


def _claude_synthetic_user_text(event):
    """Recognize official synthetic context text, never tool execution.

    Claude Code 2.1.316 emits user-role compact summaries/context furniture.
    This closed shape does not authorize a tool result or become final text.
    The caller must additionally match its already established session.
    """
    required = {'type', 'message', 'parent_tool_use_id', 'session_id', 'isSynthetic'}
    if (not required <= set(event) or set(event) - required - {'uuid', 'timestamp', 'isReplay'}
            or event['type'] != 'user'
            or event['isSynthetic'] is not True or event['parent_tool_use_id'] is not None
            or not isinstance(event['session_id'], str)
            or 'isReplay' in event and type(event['isReplay']) is not bool
            or 'timestamp' in event and not isinstance(event['timestamp'], str)):
        return False
    if 'uuid' in event:
        try:
            uuid.UUID(event['uuid'])
        except (ValueError, TypeError, AttributeError):
            return False
    message = event['message']
    if not isinstance(message, dict) or set(message) != {'role', 'content'} or message['role'] != 'user':
        return False
    content = message['content']
    return isinstance(content, str) or (isinstance(content, list) and bool(content) and all(
        isinstance(block, dict) and set(block) == {'type', 'text'} and block['type'] == 'text'
        and isinstance(block['text'], str) for block in content))


class _ClaudeReceiptDiagnostics:
    """Describe format-stage boundaries without retaining response content.

    These observations never participate in stream acceptance. Known schema
    keys and hashes permit a later audit without storing arbitrary model keys,
    structured values, assistant text or deliberation.
    """
    def __init__(self, schema):
        properties = schema.get('properties') if isinstance(schema, dict) else None
        self.schema_keys = set(properties) if isinstance(properties, dict) else set()
        self.event_index = 0
        self.init_count = 0

    @staticmethod
    def _identifier(value):
        return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value) else None

    def _shape(self, value):
        kind = ('null' if value is None else 'boolean' if type(value) is bool else
                'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else
                'string' if isinstance(value, str) else 'number')
        result = {'type': kind, 'sha256': hashlib.sha256(json.dumps(
            value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()}
        if isinstance(value, dict):
            known = sorted(set(value) & self.schema_keys)
            result.update(keys=known[:32], unknown_key_count=len(set(value) - self.schema_keys),
                          keys_truncated=len(known) > 32)
        return result

    def observe(self, raw, stream):
        self.event_index += 1
        try:
            event = adapters._strict_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            return []
        if not isinstance(event, dict):
            return []
        base = {'event_index': self.event_index, 'after_terminal_index': stream.terminal_count}
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            self.init_count += 1
            return [{**base, 'type': 'native_format_init', 'init_count': self.init_count,
                     'session_id': _session_identifier(event.get('session_id'))}]
        if kind == 'result':
            subtype = event.get('subtype')
            item = {**base, 'type': 'native_format_terminal', 'terminal_index': stream.terminal_count + 1,
                    'event_type': 'result', 'subtype': subtype if subtype in {
                        'success', 'error_during_execution', 'error_max_turns', 'error_max_budget_usd',
                        'error_max_structured_output_retries'} else 'unknown',
                    'is_error': event.get('is_error') if type(event.get('is_error')) is bool else None,
                    'session_id': _session_identifier(event.get('session_id')), 'init_count': self.init_count,
                    'structured_output_present': 'structured_output' in event,
                    'ordinary_tool_calls': stream.calls, 'ordinary_tool_results': stream.results,
                    'ordinary_tools_closed': set(stream.tools) == stream.finished_tools}
            if 'structured_output' in event:
                item['structured_output'] = self._shape(event['structured_output'])
            return [item]
        message = event.get('message')
        content = message.get('content', []) if isinstance(message, dict) else []
        observations = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if kind == 'assistant' and block.get('type') == 'tool_use' and block.get('name') == 'StructuredOutput':
                observations.append({**base, 'type': 'native_format_call',
                    'tool_use_id': self._identifier(block.get('id')), 'input_present': 'input' in block,
                    **({'input': self._shape(block['input'])} if 'input' in block else {})})
            elif (kind == 'user' and block.get('type') == 'tool_result'
                  and isinstance(block.get('tool_use_id'), str) and block['tool_use_id'] in stream.format_tools):
                identifier = block['tool_use_id']
                observations.append({**base, 'type': 'native_format_ack', 'tool_use_id': self._identifier(identifier),
                    'is_error': block.get('is_error') if type(block.get('is_error')) is bool else None,
                    'is_error_present': 'is_error' in block, 'paired': True,
                    'duplicate': identifier in stream.finished_format_tools})
        return observations


def _session_identifier(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) and str(uuid.UUID(value)) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _receipt_event(agent, raw, *, format_tool_ids=None):
    """Retain tool evidence and terminal metadata, never model deliberation.

    These receipts are still private: real tool arguments and results can
    contain project data. Chat/final text lives only in the normal result.
    """
    try:
        event = adapters._strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        return {'type': 'protocol_invalid'}
    if not isinstance(event, dict):
        return {'type': 'protocol_invalid'}
    kind = event.get('type')
    if not isinstance(kind, str):
        return {'type': 'protocol_invalid'}
    if agent == 'codex':
        if kind in {'item.started', 'item.updated', 'item.completed'}:
            item = event.get('item')
            if isinstance(item, dict) and item.get('type') == 'error':
                return {'type': kind, 'item': {'type': 'error',
                    'code': adapters._provider_error_code(item.get('message'))}}
            if not isinstance(item, dict) or item.get('type') not in _CODEX_TOOLS:
                return None
            fields = {'id', 'type', 'status', 'command', 'aggregated_output',
                      'exit_code', 'changes', 'server', 'tool', 'arguments', 'result'}
            return {'type': kind, 'item': {k: v for k, v in item.items() if k in fields}}
        if kind == 'thread.started':
            return {'type': kind, 'thread_id': event.get('thread_id')}
        if kind in {'turn.started', 'turn.completed', 'turn.failed', 'error'}:
            return {'type': kind, 'usage': adapters._usage(event.get('usage')),
                    **({'code': 'codex_stream_diagnostic'} if kind == 'error' else {})}
        return None
    if _claude_future_system_notice(event):
        # Labels only: a future notice's body may quote prompts or paths.
        return {'type': 'native_protocol_notice', 'event_type': 'system', 'event_subtype': event['subtype']}
    if kind == 'system' and event.get('subtype') == 'permission_denied':
        if _claude_permission_denied_valid(event):
            # Human-readable denial text may contain paths or private context.
            return {key: event[key] for key in ('type', 'subtype', 'tool_name', 'tool_use_id', 'session_id', 'uuid')}
        return None
    if kind == 'tool_progress':
        # Keep liveness diagnostics even when strict stream validation rejects
        # them. Never retain arbitrary extra fields, output or nested payloads.
        value = {'type': kind}
        invalid = []
        for key in ('tool_use_id', 'tool_name'):
            item = event.get(key)
            if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', item):
                value[key] = item
            else:
                invalid.append(key)
        if _nonnegative_number(event.get('elapsed_time_seconds')):
            value['elapsed_time_seconds'] = event['elapsed_time_seconds']
        else:
            invalid.append('elapsed_time_seconds')
        if 'heartbeat' in event:
            if type(event['heartbeat']) is bool:
                value['heartbeat'] = event['heartbeat']
            else:
                invalid.append('heartbeat')
        if invalid:
            value['invalid_fields'] = invalid
        return value
    if kind == 'assistant':
        error_metadata = None
        if event.get('isApiErrorMessage') is True:
            # Retain only structural provider labels for quota diagnosis; no
            # error prose, reset text, request IDs or deliberation is persisted.
            value = {'type': kind, 'isApiErrorMessage': True}
            for key in ('session_id', 'sessionId'):
                try:
                    value[key] = str(uuid.UUID(event[key]))
                except (KeyError, ValueError, TypeError, AttributeError):
                    pass
            value['parent_present'] = any(event.get(key) is not None for key in
                ('parent_tool_use_id', 'parentToolUseID'))
            value['sidechain'] = event.get('isSidechain') is True
            for key, allowed in (('error', {'rate_limit'}), ('apiError', {'usage_limit_reached'})):
                if isinstance(event.get(key), str) and event[key] in allowed:
                    value[key] = event[key]
            error_metadata = value
        message = event.get('message')
        content = message.get('content', []) if isinstance(message, dict) else []
        blocks = [{k: block[k] for k in ('type', 'id', 'name', 'input') if k in block}
                  for block in content if isinstance(block, dict)
                  and block.get('type') == 'tool_use'
                  and _claude_tool_name_valid(block.get('name'))
                  and block.get('name') != 'StructuredOutput'] if isinstance(content, list) else []
        if error_metadata is not None:
            if blocks:
                error_metadata['message'] = {'content': blocks}
            return error_metadata
        return {'type': kind, 'message': {'content': blocks}} if blocks else None
    if kind == 'user':
        message = event.get('message')
        content = message.get('content', []) if isinstance(message, dict) else []
        blocks = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get('type') != 'tool_result':
                continue
            if (isinstance(block.get('tool_use_id'), str)
                    and block['tool_use_id'] in (format_tool_ids or set())):
                continue
            value = {k: block[k] for k in ('type', 'tool_use_id', 'is_error') if k in block}
            output = block.get('content')
            if isinstance(output, str):
                value['content'] = output
            elif isinstance(output, list):
                value['content'] = [{'type': 'text', 'text': item['text']} for item in output
                                    if isinstance(item, dict) and item.get('type') == 'text'
                                    and isinstance(item.get('text'), str)]
            blocks.append(value)
        return {'type': kind, 'message': {'content': blocks}} if blocks else None
    if kind == 'system' and event.get('subtype') == 'init':
        return {'type': kind, 'subtype': 'init', 'session_id': event.get('session_id')}
    if kind == 'result':
        value = {k: event[k] for k in ('type', 'subtype', 'session_id', 'is_error') if k in event}
        for key, allowed in (('error', {'rate_limit'}), ('apiError', {'usage_limit_reached'})):
            if isinstance(event.get(key), str) and event[key] in allowed:
                value[key] = event[key]
        value['usage'] = adapters._usage(event.get('usage'))
        if adapters._claude_native_error_code(event, event.get('session_id')) == 'subscription_limit_reached':
            value['quota_failure'] = 'provider_quota_exhausted'
        return value
    return None


def claude_quota_recovery_proof(cwd, session_id, *, claimed_at, finished_at, validate_prompt, receipt_dir, home=None):
    """Bind a legacy terminal-only refusal to official owned-session metadata.

    A result string alone is insufficient historical evidence. Require this
    exact host prompt, a native API quota error and the unique zero-tool receipt.
    Never export provider prose, native API parameters or user instructions.
    """
    from datetime import datetime
    import stat
    session_id = str(uuid.UUID(session_id))
    path = adapters.native_home('claude', home) / 'projects' / re.sub(r'[^a-zA-Z0-9]', '-', str(cwd)) / (session_id + '.jsonl')
    if any(part.is_symlink() for part in (path, *path.parents)):
        return None
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024 * 1024:
            return None
        active, prompt_hash, quota, prompt_uuid, error_uuid = False, None, False, None, None
        digest = hashlib.sha256()
        for raw in stream:
            if len(raw) > adapters.MAX_OUTPUT or not raw.endswith(b'\n'):
                return None
            row = adapters._strict_json(raw)
            if not isinstance(row, dict):
                return None
            # These are native transcript indexes/accounting, not new turns.
            # Recognize their complete shapes; unknown or executable tails fail.
            metadata = False
            if active and row.get('sessionId') == session_id:
                keys = set(row)
                kind = row.get('type')
                if (kind == 'last-prompt' and keys == {'type', 'lastPrompt', 'leafUuid', 'sessionId'}
                        and row.get('leafUuid') is not None and row.get('leafUuid') in {prompt_uuid, error_uuid}
                        and isinstance(row.get('lastPrompt'), str) and len(row['lastPrompt']) <= 500):
                    metadata = True
                elif not quota and row == {'type': 'mode', 'mode': 'normal', 'sessionId': session_id}:
                    metadata = True
                elif not quota and kind == 'session-owner' and keys == {'type', 'sessionId', 'accountUuid', 'organizationUuid'}:
                    metadata = all(str(uuid.UUID(row[key])) == row[key] for key in ('accountUuid', 'organizationUuid'))
                elif (not quota and kind == 'atis-latch' and keys == {'type', 'sessionId', 'atis'}
                      and isinstance(row.get('atis'), str) and len(row['atis']) <= 512):
                    metadata = True
                elif quota and kind == 'cost-state':
                    numbers = {'totalCostUSD', 'totalAPIDuration', 'totalAPIDurationWithoutRetries', 'totalToolDuration',
                               'totalLinesAdded', 'totalLinesRemoved', 'totalDuration', 'startTime'}
                    metadata = (keys == numbers | {'type', 'sessionId', 'modelUsage', 'hasUnknownModelCost'}
                        and all(_nonnegative_number(row[key]) for key in numbers)
                        and isinstance(row['modelUsage'], dict) and type(row['hasUnknownModelCost']) is bool)
            if metadata:
                digest.update(raw)
                continue
            if quota:
                return None
            timestamp = row.get('timestamp')
            try:
                timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError, AttributeError):
                if active:
                    return None
                continue
            if not claimed_at <= timestamp <= finished_at:
                if active:
                    return None
                continue
            if row.get('sessionId') != session_id or row.get('isSidechain') is True:
                if active:
                    return None
                continue
            message = row.get('message') or {}
            if row.get('type') == 'user':
                if active:
                    return None
                content = message.get('content')
                text = content if isinstance(content, str) else (content[0].get('text')
                    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get('type') == 'text' else None)
                if not isinstance(text, str) or not validate_prompt(text):
                    return None
                active = True
                prompt_uuid = row.get('uuid')
                prompt_hash = hashlib.sha256(text.encode()).hexdigest()
            elif active and row.get('type') == 'assistant':
                if (row.get('isApiErrorMessage') is not True or row.get('error') != 'rate_limit'
                        or row.get('apiError') != 'usage_limit_reached'
                        or adapters._claude_native_error_code(row, session_id) != 'subscription_limit_reached'):
                    return None
                quota = True
                error_uuid = row.get('uuid')
            elif active and row.get('type') != 'api-request':
                return None
            if active:
                digest.update(raw)
        after = os.fstat(stream.fileno())
    if (not quota or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)):
        return None
    directory = Path(receipt_dir)
    if any(part.is_symlink() for part in (directory, *directory.parents)):
        return None
    receipts = []
    for entry in directory.iterdir():
        if entry.suffix != '.jsonl' or entry.is_symlink():
            continue
        info = entry.stat()
        if not claimed_at <= info.st_mtime <= finished_at:
            continue
        with os.fdopen(os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
                return None
            raw = stream.read(16385)
            final = os.fstat(stream.fileno())
        if ((initial.st_ino, initial.st_size, initial.st_mtime_ns) != (final.st_ino, final.st_size, final.st_mtime_ns)
                or len(raw) > 16384 or len(raw) != initial.st_size):
            return None
        rows = [adapters._strict_json(line) for line in raw.splitlines()]
        # A label-only future notice is neither a tool nor a second terminal.
        rows = [row for row in rows if not (isinstance(row, dict) and row.get('channel') == 'stdout'
                and isinstance(row.get('data'), dict) and row['data'].get('type') == 'native_protocol_notice'
                and 'diagnostics' not in row)]
        if len(rows) != 2 or any(not isinstance(row, dict) or row.get('channel') != 'stdout' for row in rows):
            continue
        init, end = (row.get('data', {}) for row in rows)
        if (init != {'type': 'system', 'subtype': 'init', 'session_id': session_id}
                or end.get('session_id') != session_id or end.get('type') != 'result'
                or end.get('subtype') != 'success' or end.get('is_error') is not True):
            continue
        terminals = [item for row in rows for item in row.get('diagnostics', []) if item.get('type') == 'native_format_terminal']
        if (len(terminals) != 1 or terminals[0].get('ordinary_tool_calls') != 0
                or terminals[0].get('ordinary_tool_results') != 0 or terminals[0].get('ordinary_tools_closed') is not True
                or terminals[0].get('terminal_index') != 1 or terminals[0].get('init_count') != 1):
            return None
        receipts.append(hashlib.sha256(raw).hexdigest())
    if len(receipts) != 1:
        return None
    return {'sha256': digest.hexdigest(), 'prompt_sha256': prompt_hash, 'native_receipt_sha256': receipts[0],
            'reason': 'provider_quota_exhausted'}


def native_command(agent, session_id=None, *, permission='read_only',
                   schema_json=None, schema_path=None, runtime_settings=None):
    """Build argv from this installation's documented CLI flags; never a shell."""
    if permission not in _PERMISSIONS:
        raise ValueError('invalid native permission')
    choice = runtime_settings or {}
    if (not isinstance(choice, dict) or set(choice) - {'model', 'effort'}
            or any(v is not None and (not isinstance(v, str) or not v or len(v) > 200
                   or any(ord(c) < 32 for c in v)) for v in choice.values())):
        raise ValueError('invalid runtime settings')
    if session_id is not None:
        try:
            uuid.UUID(session_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('session_id must be an explicit UUID') from None
    if agent == 'codex':
        sandbox = {'plan': 'read-only', 'read_only': 'read-only',
                   'workspace_write': 'workspace-write', 'full_access': 'danger-full-access'}[permission]
        command = [adapters.find_codex(), 'exec', '--json', '--skip-git-repo-check',
                   '-s', sandbox, '-c', 'forced_login_method="chatgpt"',
                   '-c', 'approval_policy="never"', '-c', 'model_provider="openai"']
        if permission in {'read_only', 'workspace_write'}:
            # Historical modes retain their original bounds. New native modes
            # inherit the user's rules, tools and integrations without overrides.
            command += ['--ignore-user-config', '--ignore-rules', '-c', 'mcp_servers={}']
            for feature in ('hooks', 'plugins', 'apps', 'remote_plugin', 'multi_agent', 'multi_agent_v2'):
                command += ['--disable', feature]
        if schema_json is not None:
            if schema_path is None:
                raise ValueError('Codex response schema requires a private file')
            command += ['--output-schema', schema_path]
        if session_id:
            command += ['resume', session_id]
        if choice.get('model') is not None:
            command += ['--model', choice['model']]
        if choice.get('effort') is not None:
            command += ['-c', 'model_reasoning_effort=' + json.dumps(choice['effort'])]
        return command + ['-']
    if agent == 'claude':
        settings = json.dumps({'apiKeyHelper': '', 'env': _CLAUDE_BASH_ENV}, separators=(',', ':'))
        command = [adapters.find_claude(), '--settings', settings,
                   '-p', '--output-format', 'stream-json', '--verbose',
                   '--permission-prompts', 'none']
        if permission in {'plan', 'full_access'}:
            command += ['--permission-mode', 'plan' if permission == 'plan' else 'bypassPermissions',
                        '--disallowedTools=' + _CLAUDE_WAKEUP_TOOLS]
        else:
            # Compatibility only: existing restricted requests must not gain
            # native capabilities merely because the application was upgraded.
            command += ['--safe-mode', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                        '--disable-slash-commands', '--no-chrome', '--restricted', '--permission-mode',
                        'acceptEdits' if permission == 'workspace_write' else 'dontAsk',
                        '--tools', 'Read,Glob,Grep,Edit,Write,NotebookEdit'
                        if permission == 'workspace_write' else 'Read,Glob,Grep']
        if schema_json is not None:
            command += ['--json-schema', schema_json]
        if session_id:
            command += ['--resume', session_id]
        if choice.get('model') is not None:
            command += ['--model', choice['model']]
        if choice.get('effort') is not None:
            command += ['--effort', choice['effort']]
        return command
    raise ValueError('agent must be codex or claude')


def _processes():
    """Read process identities so cleanup never targets a reused PID by name."""
    # lstart is localized: zh/ja dates such as "三 10月/14" split into fewer
    # fields, silently dropping processes from cleanup. Parse the C locale.
    completed = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,lstart=,stat='],
                               capture_output=True, timeout=2, check=False,
                               env={**adapters._environment(), 'LC_ALL': 'C'})
    if completed.returncode:
        raise RuntimeError('native_process_inspection_failed')
    result = {}
    for line in completed.stdout.decode('utf-8', 'replace').splitlines():
        parts = line.split()
        if len(parts) >= 9:
            try:
                result[int(parts[0])] = (int(parts[1]), int(parts[2]),
                                        ' '.join(parts[3:8]), parts[8])
            except ValueError:
                continue
    return result


def _remember(pid, owned, table):
    changed = True
    while changed:
        changed = False
        for child, (parent, group, birth, _state) in table.items():
            if child == pid or group == pid or parent in owned:
                if child not in owned:
                    owned[child] = birth
                    changed = True


async def _cleanup(process, owned):
    """Terminate the launch group and observed detached children, then verify."""
    confirmed = False
    try:
        table = await asyncio.to_thread(_processes)
        _remember(process.pid, owned, table)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            table = await asyncio.to_thread(_processes)
            # A session/group is ours only while a recorded identity matches.
            if any(pid in owned and row[2] == owned[pid] and row[1] == process.pid
                   for pid, row in table.items()):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
            for pid, birth in owned.items():
                row = table.get(pid)
                if row and row[2] == birth and row[1] != process.pid and 'Z' not in row[3]:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), 1)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.05)
        table = await asyncio.to_thread(_processes)
        confirmed = not any(row[2] == owned.get(pid) and 'Z' not in row[3]
                            for pid, row in table.items())
    except (OSError, subprocess.SubprocessError, RuntimeError):
        # Best effort group cleanup is still required if inspection fails;
        # absence cannot then be certified.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return confirmed


async def _settle_cleanup(operation):
    """A repeated cancel cannot interrupt owned process cleanup halfway."""
    task = asyncio.create_task(operation)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    return task.result(), interrupted


class _Stream:
    def __init__(self, agent, structured, callback, progress_fingerprint=None, *, expected_session=None):
        self.agent, self.structured, self.callback = agent, structured, callback
        self.expected_session = expected_session
        self.public_progress_ids = OrderedDict()
        self.final_lines = []
        self.events = []
        self.calls = self.results = 0
        self.failure = None
        self.tools = {}
        self.session_id = None
        self.possible_effects = False
        self.finished_tools = set()
        self.format_tools = set()
        self.finished_format_tools = set()
        self.terminal_count = 0
        self.codex_closed_tools_at_terminal = None
        self.progress_fingerprint = progress_fingerprint
        self.tool_inputs = {}
        self.tool_summaries = {}
        self.failed_evidence = {}

    def remember_input(self, identifier, tool, value):
        # Only hashes survive the event; native arguments never enter public
        # progress callbacks or the repeated-failure state.
        self.tool_inputs[identifier] = hashlib.sha256(json.dumps(
            [tool, value], sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    async def failed_tool(self, identifier, result):
        input_digest = self.tool_inputs.get(identifier)
        if self.progress_fingerprint is None or input_digest is None:
            return
        signature = hashlib.sha256(json.dumps(
            [input_digest, result], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        try:
            fingerprint = self.progress_fingerprint()
            if inspect.isawaitable(fingerprint):
                fingerprint = await fingerprint
            if not isinstance(fingerprint, str) or re.fullmatch(r'[0-9a-f]{64}', fingerprint) is None:
                raise ValueError('unconfirmed progress fingerprint')
        except Exception:
            # Unknown file state cannot prove stagnation, nor justify silently
            # continuing an unmonitored failure loop.
            self.failure = 'native_progress_unconfirmed'
            return
        previous, count = self.failed_evidence.get(signature, (None, 0))
        count = count + 1 if previous == fingerprint else 1
        self.failed_evidence[signature] = (fingerprint, count)
        if count >= 3:
            self.failure = 'native_no_progress'

    def identity(self, value):
        try:
            uuid.UUID(value)
        except (ValueError, TypeError, AttributeError):
            self.failure = 'native_session_identity_mismatch'
            return
        if self.session_id is not None and self.session_id != value:
            self.failure = 'native_session_identity_mismatch'
        self.session_id = value

    async def activity(self, tool, state, *, identifier=None, exit_code=None, is_error=None,
                       elapsed_time_seconds=None, heartbeat=None):
        # Only finite protocol summaries and numeric status leave this parser.
        # Native arguments/output, URLs, paths and file names stay private.
        event = {'type': 'native_tool', 'kind': tool, 'state': state,
                 'description': {'started': '正在使用工具', 'running': '工具仍在运行', 'completed': '工具已返回',
                                 'failed': '工具返回失败'}[state]}
        event.update(self.tool_summaries.get(identifier, _activity_summary(tool, None)))
        if type(exit_code) is int:
            event['exit_code'] = exit_code
        if type(is_error) is bool:
            event['is_error'] = is_error
        if _nonnegative_number(elapsed_time_seconds):
            event['elapsed_time_seconds'] = elapsed_time_seconds
        if type(heartbeat) is bool:
            event['heartbeat'] = heartbeat
        self.events.append(event)
        if self.callback is not None:
            returned = self.callback(dict(event))
            if inspect.isawaitable(returned):
                await returned

    async def public_progress(self, identifier, text, event, item):
        """Publish complete public speech; never infer speech from tool data.

        Missing identity or an ambiguous frame only suppresses this optional
        display. It must not change legacy parsing or the turn's final outcome.
        The host separately validates current ownership, epoch and cancellation.
        """
        if (self.failure is not None or self.session_id is None
                or self.expected_session is not None and self.session_id != self.expected_session
                or not isinstance(identifier, str)
                or re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', identifier) is None
                or identifier in self.public_progress_ids
                or not isinstance(text, str) or not text.strip()):
            return
        for frame in (event, item):
            if any(frame.get(key) is not None for key in
                   ('parent_tool_use_id', 'parentToolUseID', 'parent_thread_id', 'parent_session_id')):
                return
            if frame.get('isReplay') is True or frame.get('isSynthetic') is True or frame.get('isSidechain') is True:
                return
            if any(key in frame and frame[key] != self.session_id for key in
                   ('session_id', 'sessionId', 'thread_id')):
                return
        # Structured terminal protocol is handled only by the canonical final
        # parser. Do not leak a JSON candidate even when mislabelled commentary.
        if _structured_final_text(text):
            return
        try:
            encoded = text.encode('utf-8')
        except UnicodeError:
            return
        value = {'type': 'native_progress', 'item_id': identifier,
                 'session_id': self.session_id, 'text': text}
        if len(encoded) > _MAX_PUBLIC_PROGRESS_BYTES:
            value.update(text=encoded[:_MAX_PUBLIC_PROGRESS_BYTES].decode('utf-8', errors='ignore'),
                         truncated=True, original_characters=len(text))
        self.public_progress_ids[identifier] = None
        if len(self.public_progress_ids) > _PUBLIC_PROGRESS_CACHE_SIZE:
            # Bound memory, not the number of public messages in useful work.
            # The host retains durable job/item/body deduplication across this
            # rolling cache and process restarts.
            self.public_progress_ids.popitem(last=False)
        if self.callback is not None:
            returned = self.callback(value)
            if inspect.isawaitable(returned):
                await returned

    async def consume(self, raw):
        try:
            event = adapters._strict_json(raw)
            if not isinstance(event, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            self.failure = 'native_protocol_invalid'
            self.possible_effects = True
            return
        kind = event.get('type')
        if not isinstance(kind, str):
            self.failure = 'native_protocol_invalid'
            self.possible_effects = True
            return
        if self.agent == 'codex':
            if kind in {'thread.started', 'turn.started', 'turn.completed', 'turn.failed', 'error'}:
                self.final_lines.append(raw)
                if kind == 'thread.started':
                    self.identity(event.get('thread_id'))
                if kind in {'turn.completed', 'turn.failed'}:
                    self.terminal_count += 1
                    if kind == 'turn.completed' and not set(self.tools) - self.finished_tools:
                        self.codex_closed_tools_at_terminal = frozenset(self.tools)
                return
            if kind not in {'item.started', 'item.updated', 'item.completed'}:
                self.failure = 'native_protocol_unknown'
                self.possible_effects = True
                return
            item = event.get('item')
            if not isinstance(item, dict):
                self.failure = 'native_protocol_invalid'
                return
            tool = item.get('type')
            if tool in {'agent_message', 'reasoning', 'error'}:
                self.final_lines.append(raw)
                if (kind == 'item.completed' and tool == 'agent_message'
                        # exec's AgentMessageItem has text but no phase;
                        # reasoning is a separate item type. Some versions
                        # add phase, which must explicitly denote public speech.
                        and ('phase' not in item or item['phase'] == 'commentary')
                        and item.get('role', 'assistant') == 'assistant'):
                    await self.public_progress(item.get('id'), item.get('text'), event, item)
                return
            if tool not in _CODEX_TOOLS:
                self.failure = 'native_tool_unknown'
                self.possible_effects = True
                return
            self.possible_effects = True
            identifier = item.get('id')
            if not isinstance(identifier, str):
                self.failure = 'native_protocol_invalid'
                return
            inputs = {key: item[key] for key in ('command', 'changes', 'server', 'tool', 'arguments') if key in item}
            if inputs:
                self.remember_input(identifier, tool, inputs)
            if kind == 'item.started':
                if identifier not in self.tools:
                    self.tools[identifier] = tool
                    self.tool_summaries[identifier] = _activity_summary(tool, inputs)
                    self.calls += 1
                    await self.activity(tool, 'started', identifier=identifier)
            elif kind == 'item.completed':
                if identifier in self.finished_tools:
                    self.failure = 'native_protocol_invalid'
                    return
                self.finished_tools.add(identifier)
                if identifier not in self.tools:
                    self.tools[identifier] = tool
                    self.tool_summaries[identifier] = _activity_summary(tool, inputs)
                    self.calls += 1
                self.results += 1
                failed = (item.get('status') in {'failed', 'declined'}
                          or tool == 'command_execution' and type(item.get('exit_code')) is int
                          and item['exit_code'] != 0)
                await self.activity(tool, 'failed' if failed else 'completed',
                                    identifier=identifier, exit_code=item.get('exit_code'), is_error=failed)
                if failed:
                    await self.failed_tool(identifier, {key: item[key] for key in
                        ('status', 'exit_code', 'aggregated_output', 'result', 'error') if key in item})
        elif kind == 'tool_progress':
            identifier = event.get('tool_use_id')
            tool = self.tools.get(identifier) if isinstance(identifier, str) else None
            if tool is None and event.get('heartbeat') is True and isinstance(identifier, str):
                # Claude Code 2.1.316 foreground heartbeats append a counter
                # to the original tool ID. This alias is liveness only: it
                # must never register a new tool or authorize a tool result.
                alias = re.fullmatch(r'(.+)-heartbeat-([0-9]+)', identifier)
                if alias is not None:
                    identifier = alias[1]
                    tool = self.tools.get(identifier)
            if (tool is None or event.get('tool_name') != tool
                    or not _nonnegative_number(event.get('elapsed_time_seconds'))
                    or 'heartbeat' in event and type(event['heartbeat']) is not bool):
                self.failure = 'native_protocol_invalid'
                self.possible_effects = True
                return
            if 'session_id' in event:
                self.identity(event['session_id'])
            if identifier not in self.finished_tools and self.failure is None:
                await self.activity(tool, 'running', elapsed_time_seconds=event['elapsed_time_seconds'],
                                    identifier=identifier, heartbeat=event.get('heartbeat'))
        elif kind == 'system':
            # Native system init/compaction/status carry no tool evidence.
            if event.get('subtype') == 'init':
                self.identity(event.get('session_id'))
                self.final_lines.append(raw)
            elif event.get('subtype') == 'permission_denied':
                if not _claude_permission_denied_valid(event):
                    self.failure = 'native_protocol_invalid'
                elif self.session_id is None or event['session_id'] != self.session_id:
                    self.failure = 'native_session_identity_mismatch'
                elif self.tools.get(event['tool_use_id']) != event['tool_name']:
                    self.failure = 'native_protocol_invalid'
                # Advisory denial is not a tool_result, completion or authority.
                # Keep waiting for the actual result; readonly argv is unchanged.
            elif event.get('subtype') not in {'compact_boundary', 'status', 'task_started',
                                               'task_progress', 'task_notification'}:
                valid = _claude_metadata_valid(event)
                if valid is None and not _claude_future_system_notice(event):
                    self.failure = 'native_protocol_unknown'
                elif valid is None:
                    # Recorded as a label-only notice in the receipt. A
                    # session_id still has to match this owned session.
                    if 'session_id' in event:
                        self.identity(event['session_id'])
                elif not valid:
                    self.failure = 'native_protocol_invalid'
                elif 'session_id' in event:
                    self.identity(event['session_id'])
        elif kind == 'assistant':
            if event.get('isApiErrorMessage') is True:
                for key in ('session_id', 'sessionId'):
                    if key in event and (self.session_id is None or event[key] != self.session_id):
                        self.failure = 'native_session_identity_mismatch'
            self.final_lines.append(raw)
            message = event.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), list):
                self.failure = 'native_protocol_invalid'
                return
            public_message = (event.get('isApiErrorMessage') is not True
                              and event.get('session_id') == self.session_id
                              and 'parent_tool_use_id' in event and event['parent_tool_use_id'] is None
                              and message.get('role') == 'assistant')
            try:
                public_id = str(uuid.UUID(event.get('uuid'))) if public_message else None
            except (ValueError, TypeError, AttributeError):
                public_id = None
            if public_id is not None:
                for index, block in enumerate(message['content']):
                    if isinstance(block, dict) and block.get('type') == 'text':
                        await self.public_progress(public_id + ':' + str(index), block.get('text'), event, message)
            for block in message['content']:
                if not isinstance(block, dict):
                    self.failure = 'native_protocol_invalid'
                elif block.get('type') == 'tool_use':
                    tool = block.get('name')
                    if tool != 'StructuredOutput':
                        self.possible_effects = True
                    if not _claude_tool_name_valid(tool):
                        self.failure = 'native_tool_unknown'
                        continue
                    identifier = block.get('id')
                    if not isinstance(identifier, str):
                        self.failure = 'native_protocol_invalid'
                        continue
                    if tool == 'StructuredOutput':
                        if identifier in self.tools:
                            self.failure = 'native_protocol_invalid'
                        else:
                            self.format_tools.add(identifier)
                        continue
                    if identifier in self.format_tools:
                        self.failure = 'native_protocol_invalid'
                        continue
                    if identifier in self.tools and self.tools[identifier] != tool:
                        self.failure = 'native_protocol_invalid'
                        continue
                    if identifier not in self.tools:
                        self.tools[identifier] = tool
                        self.tool_summaries[identifier] = _activity_summary(tool, block.get('input'))
                        if 'input' in block:
                            self.remember_input(identifier, tool, block['input'])
                        self.calls += 1
                        await self.activity(tool, 'started', identifier=identifier)
                elif block.get('type') not in {'text', 'thinking', 'redacted_thinking'}:
                    self.failure = 'native_protocol_unknown'
        elif kind == 'user':
            if _claude_synthetic_user_text(event):
                if self.session_id is None or event['session_id'] != self.session_id:
                    self.failure = 'native_session_identity_mismatch'
                # Do not echo synthetic context into final output, usage,
                # tool receipts, progress, or host authority.
                return
            message = event.get('message', {})
            content = message.get('content', []) if isinstance(message, dict) else []
            if not isinstance(content, list) or not content:
                self.failure = 'native_protocol_invalid'
                return
            if any(not isinstance(block, dict) or block.get('type') != 'tool_result' for block in content):
                self.failure = 'native_protocol_unknown'
                return
            for block in content:
                identifier = block.get('tool_use_id')
                if not isinstance(identifier, str):
                    self.failure = 'native_protocol_invalid'
                    continue
                if identifier in self.format_tools:
                    if identifier in self.finished_format_tools:
                        self.failure = 'native_protocol_invalid'
                    else:
                        self.finished_format_tools.add(identifier)
                    continue
                tool = self.tools.get(identifier)
                if tool is None:
                    self.failure = 'native_protocol_invalid'
                    continue
                if block['tool_use_id'] in self.finished_tools:
                    self.failure = 'native_protocol_invalid'
                    continue
                self.finished_tools.add(block['tool_use_id'])
                self.results += 1
                failed = block.get('is_error') is True
                await self.activity(tool, 'failed' if failed else 'completed', identifier=identifier, is_error=failed)
                if failed:
                    await self.failed_tool(identifier, {key: block[key] for key in ('is_error', 'content') if key in block})
        elif kind == 'result':
            self.terminal_count += 1
            self.identity(event.get('session_id'))
            self.final_lines.append(raw)
        elif kind not in {'rate_limit_event'}:
            self.failure = 'native_protocol_unknown'
            self.possible_effects = True

    def result(self, exit_code, expected_session, *, allow_codex_nonterminal_diagnostics=False):
        allow_diagnostics = (allow_codex_nonterminal_diagnostics and self.agent == 'codex' and self.structured
            and exit_code == 0 and self.failure is None and self.terminal_count == 1
            and self.session_id is not None and (expected_session is None or self.session_id == expected_session)
            and self.codex_closed_tools_at_terminal == frozenset(self.tools)
            and not set(self.tools) - self.finished_tools)
        result = adapters.parse_events(self.agent, self.final_lines, structured=self.structured,
            allow_codex_nonterminal_diagnostics=allow_diagnostics)
        # This parser's sanitized tool receipts replace the legacy no-tools
        # marker. Provider error and quota classifications remain intact.
        result['events'] = [e for e in result['events'] if e['type'] != 'tool_observed'] + self.events
        actual = result.get('session_id')
        if self.terminal_count != 1:
            self.failure = self.failure or 'protocol_turn_incomplete'
        if result['status'] == 'completed' and set(self.tools) - self.finished_tools:
            self.failure = self.failure or 'native_tool_result_missing'
        if actual is None or expected_session is not None and actual != expected_session:
            self.failure = self.failure or 'native_session_identity_mismatch'
        if self.failure:
            result.update(status='failed', text='', failure_code=self.failure)
        elif exit_code and result.get('failure_code') != 'subscription_limit_reached':
            result.update(status='failed', failure_code='cli_nonzero_exit')
        elif result['status'] != 'completed' and 'failure_code' not in result:
            result['failure_code'] = 'protocol_turn_incomplete'
        return result


async def invoke_native(agent: str, prompt: str, cwd: Path, session_id: str | None = None,
                        timeout: float | None = None, *, permission='read_only',
                        response_schema: dict | None = None, runtime_settings: dict | None = None,
                        on_activity=None, receipt_dir: Path | None = None,
                        progress_fingerprint=None, progress_file: Path | None = None) -> dict:
    """Run one cancellable native turn; no implicit time or turn-count budget.

    Cancellation raises CancelledError *after* owned cleanup. Other failures
    return a sanitized receipt; tool activity makes side effects possible even
    if a quota failure or invalid final response follows. Callers must never
    blindly replay such a failed turn.

    An optional sync/async progress_fingerprint returns a SHA256 of only the
    task's authorized working files. Three identical failed tools with an
    unchanged fingerprint stop the turn; successful tools and heartbeats do
    not consume a count. This is not a general semantic stall detector.
    """
    if agent == 'kimi':
        from .kimi_acp import invoke_native as invoke_kimi
        return await invoke_kimi(prompt, cwd, session_id, timeout, permission=permission,
                                 response_schema=response_schema, runtime_settings=runtime_settings,
                                 on_activity=on_activity, receipt_dir=receipt_dir,
                                 progress_fingerprint=progress_fingerprint, progress_file=progress_file,
                                 sandbox=True)

    def not_started(error, cleanup_confirmed):
        error.native_execution = {'agent': agent, 'permission': permission,
            'never_started': True, 'tool_calls': 0, 'tool_results': 0,
            'side_effects_possible': False, 'cleanup_confirmed': cleanup_confirmed,
            'exit_code': None, 'receipt_path': None}
    try:
        if timeout is not None and (type(timeout) not in (int, float)
                                   or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('timeout must be positive or None')
        if not isinstance(prompt, str) or len(prompt.encode('utf-8')) > adapters.MAX_PROMPT_BYTES:
            raise ValueError('prompt exceeds size limit')
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError('cwd must be a directory')
        schema_json = adapters._object_text(response_schema) if response_schema is not None else None
        if schema_json is not None and (response_schema.get('type') != 'object'
                                        or len(schema_json.encode()) > 64 * 1024):
            raise ValueError('response schema must be a bounded object schema')
        # Validation has not launched either authentication or inference.
        native_command(agent, session_id, permission=permission, runtime_settings=runtime_settings)
    except BaseException as error:
        not_started(error, True)
        raise
    started = time.monotonic()
    status = None
    try:
        status = await asyncio.wait_for(adapters._subscription_status(agent, cwd), timeout=timeout)
        if not status.get('subscription_confirmed') or status.get('cleanup_confirmed') is not True:
            raise RuntimeError(f'{agent} subscription authentication is unconfirmed; inference refused')
    except BaseException as error:
        # The producer proves authentication-child cleanup. A cancelled await
        # alone never establishes that the status subprocess has exited.
        cause = error.__cause__ if isinstance(error, asyncio.TimeoutError) else None
        confirmed = (status.get('cleanup_confirmed') is True if status is not None else
                     getattr(error, 'cleanup_confirmed', False) is True
                     or isinstance(cause, asyncio.CancelledError) and getattr(cause, 'cleanup_confirmed', False) is True)
        not_started(error, confirmed)
        raise
    stream = _Stream(agent, response_schema is not None, on_activity, progress_fingerprint,
                     expected_session=session_id)
    process = None
    owned = {}
    cleanup_confirmed = False
    receipt_path = None
    receipt = None
    schema_directory = None
    receipt_bytes = 0
    tasks = []
    exit_code = None
    cancelled_error = None
    try:
        schema_path = None
        if schema_json is not None and agent == 'codex':
            schema_directory = tempfile.TemporaryDirectory(prefix='beanest-native-schema-')
            path = Path(schema_directory.name) / 'schema.json'
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as file:
                file.write(schema_json)
            schema_path = str(path)
        command = native_command(agent, session_id, permission=permission, schema_json=schema_json,
                                 schema_path=schema_path, runtime_settings=runtime_settings)
        if receipt_dir is not None:
            directory = Path(receipt_dir)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or directory.stat().st_mode & 0o077:
                raise ValueError('receipt directory must be private')
            path = directory / (str(uuid.uuid4()) + '.jsonl')
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            receipt = os.fdopen(descriptor, 'wb')
            receipt_path = str(path)
        environment = adapters._environment()
        if agent == 'claude':
            environment.update(_CLAUDE_BASH_ENV)
        if progress_file is not None:
            environment[job_progress.ENVIRONMENT_VARIABLE] = str(progress_file)
        process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=_MAX_LINE + 1)

        receipt_diagnostics = _ClaudeReceiptDiagnostics(response_schema) if agent == 'claude' else None

        async def record(channel, raw, *, rejected=False):
            nonlocal receipt_bytes
            if receipt:
                evidence = (_protocol_diagnostic(raw) if rejected else
                            _receipt_event(agent, raw, format_tool_ids=stream.format_tools))
                diagnostics = (receipt_diagnostics.observe(raw, stream)
                               if receipt_diagnostics is not None and not rejected else [])
                rows = []
                if evidence is not None:
                    row = {'channel': channel, 'data': evidence}
                    if diagnostics:
                        row['diagnostics'] = diagnostics
                    rows.append(row)
                elif diagnostics:
                    rows.append({'channel': 'diagnostic', 'data': diagnostics})
                for row in rows:
                    value = json.dumps(row, ensure_ascii=False).encode() + b'\n'
                    receipt_bytes += len(value)
                    if receipt_bytes > _MAX_RECEIPT:
                        raise RuntimeError('native_receipt_size_limit')
                    receipt.write(value)
                receipt.flush()

        async def stdout():
            while raw := await process.stdout.readline():
                if len(raw) > _MAX_LINE:
                    raise RuntimeError('native_protocol_line_limit')
                await record('stdout', raw)
                await stream.consume(raw)
                if stream.failure:
                    await record('stdout', raw, rejected=True)
                    raise RuntimeError(stream.failure)

        async def stderr():
            # Exit status and sanitized protocol errors are sufficient for
            # diagnosis; raw stderr may contain credentials or prompt text.
            while await process.stderr.read(65536):
                pass

        async def write():
            try:
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        async def track():
            while process.returncode is None:
                _remember(process.pid, owned, await asyncio.to_thread(_processes))
                await asyncio.sleep(0.1)

        tasks = [asyncio.create_task(stdout()), asyncio.create_task(stderr()), asyncio.create_task(write())]
        tracker = asyncio.create_task(track())
        async def collect():
            await asyncio.gather(*tasks)
            return await process.wait()
        try:
            remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
            exit_code = await asyncio.wait_for(collect(), timeout=remaining)
            result = stream.result(exit_code, session_id)
        except asyncio.TimeoutError:
            # An interrupted process may have acted before its next event was
            # flushed. Absence of a tool event cannot prove an effect-free turn.
            stream.possible_effects = True
            result = {'session_id': stream.session_id or session_id, 'text': '', 'usage': None,
                      'events': stream.events, 'status': 'timeout', 'failure_code': 'deadline_exceeded'}
        except (OSError, RuntimeError, ValueError):
            stream.possible_effects = True
            result = {'session_id': stream.session_id or session_id, 'text': '', 'usage': None,
                      'events': stream.events, 'status': 'failed',
                      'failure_code': stream.failure or 'native_process_or_protocol_failure'}
        finally:
            tracker.cancel()
            await asyncio.gather(tracker, return_exceptions=True)
    except asyncio.CancelledError as error:
        cancelled_error = error
        raise
    finally:
        if process is not None:
            cleanup = asyncio.create_task(_cleanup(process, owned))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup_confirmed = cleanup.result()
            for task in tasks:
                task.cancel()
            settled = asyncio.gather(*tasks, return_exceptions=True)
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError:
                    cancelled = True
            if receipt:
                receipt.close()
            if schema_directory:
                schema_directory.cleanup()
            if cancelled or cancelled_error is not None:
                error = cancelled_error or asyncio.CancelledError()
                error.native_execution = {'agent': agent, 'permission': permission,
                    'tool_calls': stream.calls, 'tool_results': stream.results,
                    'side_effects_possible': True, 'cleanup_confirmed': cleanup_confirmed,
                    'exit_code': process.returncode, 'receipt_path': receipt_path}
                raise error
        else:
            if receipt:
                receipt.close()
            if schema_directory:
                schema_directory.cleanup()
    if agent == 'codex' and response_schema is not None and exit_code == 0 and cleanup_confirmed:
        result = stream.result(exit_code, session_id, allow_codex_nonterminal_diagnostics=True)
    result['native_execution'] = {'agent': agent, 'permission': permission,
        'auth_method': status.get('authMethod'),
        'tool_calls': stream.calls, 'tool_results': stream.results,
        'side_effects_possible': stream.possible_effects, 'cleanup_confirmed': cleanup_confirmed,
        'exit_code': exit_code, 'receipt_path': receipt_path,
        'duration_seconds': round(time.monotonic() - started, 3)}
    if result.get('failure_code') == 'subscription_limit_reached':
        result['native_execution']['failure_diagnostic'] = {
            'phase': 'prompt', 'reason': 'provider_quota_exhausted', 'prompt_sent': True}
    result['cleanup_confirmed'] = cleanup_confirmed
    if not cleanup_confirmed:
        result.update(status='failed', failure_code='native_cleanup_unconfirmed')
    return result
