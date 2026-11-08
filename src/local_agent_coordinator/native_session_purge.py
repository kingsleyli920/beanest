"""Scope official local-session deletion without editing provider indexes.

The caller freezes room ownership, sharing and time bounds before removing its
own database rows, and supplies a managed command runner. This module neither
runs a model nor falls back to unlinking transcripts or rewriting provider DBs.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import signal
import subprocess
from uuid import UUID

from .room_purge import _open_absolute
from .adapters import _environment


class _Pending(Exception):
    pass


def run_official_cleanup(argv, environment):
    """Bound one official administration command, not an Agent work turn.

    No shell, model prompt, remote endpoint, permission bypass or raw command
    output is accepted. The group is cleaned even after a failed/slow command.
    """
    valid_codex = (len(argv) == 4 and argv[1:3] == ['delete', '--force'])
    valid_claude = (len(argv) == 4 and argv[1:3] == ['purge', '--yes'])
    if not (valid_codex or valid_claude) or not Path(argv[0]).is_absolute():
        raise ValueError('unsupported cleanup command')
    if valid_codex:
        _uuid(argv[3])
        expected = 'CODEX_HOME'
    else:
        _absolute(argv[3])
        expected = 'CLAUDE_CONFIG_DIR'
    if set(environment) != {expected}:
        raise ValueError('unsupported cleanup environment')
    home = _absolute(environment[expected])
    env = {**_environment(), expected: str(home)}
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    code = None
    reaped = True
    try:
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                continue
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            reaped = False
    if not reaped:
        return {'exit_code': None, 'cleanup_confirmed': False}
    try:
        os.killpg(process.pid, 0)
        clean = False
    except ProcessLookupError:
        clean = True
    return {'exit_code': code, 'cleanup_confirmed': clean}


def _uuid(value):
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise _Pending('invalid_session_identity') from None
    if str(parsed) != value:
        raise _Pending('invalid_session_identity')
    return parsed


def _absolute(value):
    if not isinstance(value, (str, Path)):
        raise _Pending('invalid_owned_path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or path == Path(path.anchor):
        raise _Pending('invalid_owned_path')
    return path


def _room_cwd(runtime, workspace, room_id, value):
    path = _absolute(value)
    try:
        parts = path.relative_to(runtime).parts
    except ValueError:
        raise _Pending('unowned_session_cwd') from None
    native = ((len(parts) == 4 and parts[:2] == ('native-workspaces', room_id) and parts[-1] == 'project')
              or (len(parts) == 5 and parts[:2] == ('native-transfers', room_id)
                  and parts[-2:] == ('receiver', 'project')))
    static = path.parent.parent == workspace and path.parent.name == room_id
    if not native and not static:
        raise _Pending('unowned_session_cwd')
    try:
        descriptor = _open_absolute(path)
    except FileNotFoundError:
        # A prior runtime cleanup may already have removed a dedicated cwd.
        # Existing ancestors were still opened without following symlinks.
        pass
    except OSError:
        raise _Pending('unsafe_owned_session_cwd') from None
    else:
        os.close(descriptor)
    return str(path)


def _timestamp(value):
    if not isinstance(value, str):
        raise _Pending('session_time_unconfirmed')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise _Pending('session_time_unconfirmed') from None


def _within(target, value):
    low, high = target['created_after'], target['created_before']
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _Pending('session_time_unconfirmed')
    if not low <= value <= high:
        raise _Pending('session_time_mismatch')


def _metadata(path, *, claude=False, allow_sidechain=False):
    """Read only the exact candidate; retain no conversation body in receipts."""
    parent = _open_absolute(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'r', encoding='utf-8') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _Pending('unsafe_session_file')
            for _ in range(128 if claude else 1):
                line = source.readline(2 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line.encode('utf-8')) > 2 * 1024 * 1024:
                    raise _Pending('session_metadata_unconfirmed')
                value = json.loads(line)
                if not isinstance(value, dict):
                    continue
                if claude and isinstance(value.get('sessionId'), str) and isinstance(value.get('cwd'), str):
                    if value.get('isSidechain') is True and not allow_sidechain:
                        raise _Pending('session_sidechain_unconfirmed')
                    result = {'id': value['sessionId'], 'cwd': value['cwd'], 'timestamp': value.get('timestamp')}
                    if allow_sidechain:
                        result.update(sidechain=value.get('isSidechain') is True,
                                      parent_session_id=value.get('parentSessionId'))
                    return result
                if not claude and value.get('type') == 'session_meta' and isinstance(value.get('payload'), dict):
                    return {key: value['payload'].get(key) for key in ('id', 'cwd', 'timestamp', 'source')}
    finally:
        os.close(parent)
    raise _Pending('session_metadata_unconfirmed')


def _check_metadata(target, metadata):
    if metadata['id'] != target['session_id'] or metadata['cwd'] not in target['owned_cwds']:
        raise _Pending('session_identity_or_cwd_mismatch')
    _within(target, _timestamp(metadata['timestamp']))


def _codex_plan(home, target):
    identifier = _uuid(target['session_id'])
    if identifier.version != 7:
        raise _Pending('codex_session_date_unconfirmed')
    databases = list(home.glob('state_*.sqlite'))
    if len(databases) != 1:
        raise _Pending('codex_metadata_index_unconfirmed')
    database = databases[0]
    # Pin all ancestors and reject a substituted/symlink provider index. Read
    # only this exact ID and its explicit spawn edges, never titles/messages.
    parent = _open_absolute(database.parent)
    try:
        entry = os.stat(database.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise _Pending('unsafe_provider_index')
        with closing(sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT id,rollout_path,cwd,source,created_at FROM threads WHERE id=?',
                             (target['session_id'],)).fetchone()
            descendants = [item[0] for item in db.execute(
                'WITH RECURSIVE child(id) AS (SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id=? '
                'UNION SELECT e.child_thread_id FROM thread_spawn_edges e JOIN child c ON e.parent_thread_id=c.id) '
                'SELECT id FROM child', (target['session_id'],))]
        latest = os.stat(database.name, dir_fd=parent, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (latest.st_dev, latest.st_ino):
            raise _Pending('provider_index_changed')
    finally:
        os.close(parent)
    if row is None:
        # Missing index alone cannot prove the transcript is gone. Check only
        # exact UUID filenames in the UUIDv7 UTC day and timezone boundary days.
        day = datetime.fromtimestamp((identifier.int >> 80) / 1000, timezone.utc).date()
        for offset in (-1, 0, 1):
            folder = home / 'sessions' / (day + timedelta(days=offset)).strftime('%Y/%m/%d')
            if any(folder.glob('rollout-*-' + str(identifier) + '.jsonl*')):
                raise _Pending('codex_index_missing_for_transcript')
        if any((home / 'archived_sessions').glob('rollout-*-' + str(identifier) + '.jsonl*')):
            raise _Pending('codex_index_missing_for_transcript')
        return {'missing': True, 'children': descendants}
    if row['cwd'] not in target['owned_cwds'] or row['source'] != 'exec':
        raise _Pending('session_identity_or_cwd_mismatch')
    # Codex's index stores whole seconds, while room creation and rollout
    # metadata retain fractions. A session started later in the same second
    # must not fail the index check; the rollout still uses the exact bounds.
    index_target = {**target, 'created_after': math.floor(target['created_after'])}
    _within(index_target, row['created_at'])
    path = _absolute(row['rollout_path'])
    day = datetime.fromtimestamp((identifier.int >> 80) / 1000, timezone.utc).date()
    days = {(day + timedelta(days=offset)).strftime('%Y/%m/%d') for offset in (-1, 0, 1)}
    allowed = (path.parent == home / 'archived_sessions'
               or any(path.parent == home / 'sessions' / value for value in days))
    if not allowed or not re.fullmatch(r'rollout-.+-' + re.escape(str(identifier)) + r'\.jsonl', path.name):
        raise _Pending('unowned_rollout_path')
    try:
        meta = _metadata(path)
    except FileNotFoundError:
        meta = None  # Official delete must still remove the orphaned index row.
    if meta is not None:
        _check_metadata(target, meta)
        if meta['source'] != 'exec':
            raise _Pending('session_source_unconfirmed')
    return {'missing': False, 'children': descendants, 'path': path}


def _claude_project(home, cwd):
    return home / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', cwd)


def _audit_claude_companion(project, sid, target):
    """Audit only a validated main session's exact companion namespace.

    Tool-result bytes are opaque and are never read. Sidechain JSONL files
    require an explicit parent-session relationship and the same owned cwd.
    No project-level subagents directory is granted by this convention.
    """
    directory = project / sid
    root = _open_absolute(directory)
    device = os.fstat(root).st_dev
    def walk(descriptor, path, area=None):
        for name in os.listdir(descriptor):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != device or area is None and name not in {'tool-results', 'subagents'}:
                    raise _Pending('session_companion_unconfirmed')
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise _Pending('session_companion_changed')
                    walk(child, path / name, area or name)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and area is not None:
                if area == 'subagents' and name.endswith('.jsonl'):
                    meta = _metadata(path / name, claude=True, allow_sidechain=True)
                    if (not meta['sidechain'] or meta['cwd'] not in target['owned_cwds']
                            or meta['id'] != sid and meta.get('parent_session_id') != sid):
                        raise _Pending('session_sidechain_unconfirmed')
                    _within(target, _timestamp(meta['timestamp']))
                elif area == 'subagents':
                    raise _Pending('session_sidechain_unconfirmed')
            else:
                raise _Pending('unsafe_session_companion')
    try:
        walk(root, directory)
    finally:
        os.close(root)


def purge_native_sessions(runtime_root, workspace_root, room_id, targets, *, codex_home,
                          claude_home, codex_cli=None, claude_cli=None, run_command=run_official_cleanup):
    """Delete verified dedicated sessions through official provider commands.

    Each frozen target carries adapter/session_id/owned_cwds/created_after/
    created_before/shared_room_ids. The runner owns subprocess cleanup and must
    return {exit_code: int, cleanup_confirmed: bool}. No output is persisted.
    Shared, unregistered, changed or unsupported evidence remains pending; the
    caller must not erase its only ownership evidence while pending is nonempty.
    """
    runtime, workspace = _absolute(runtime_root), _absolute(workspace_root)
    _uuid(room_id)
    if workspace.parent != runtime:
        raise ValueError('workspace must be a direct child of runtime')
    homes = {'codex': _absolute(codex_home), 'claude': _absolute(claude_home)}
    commands = {'codex': codex_cli, 'claude': claude_cli}
    result = {'provider_managed_deleted': [], 'already_missing': [], 'pending': [], 'cleanup_confirmed': False}
    selected, plans, seen_targets = {}, {}, set()
    def pending(target, reason):
        result['pending'].append({'adapter': target.get('adapter'), 'session_id': target.get('session_id'), 'reason': reason})
    if not isinstance(targets, (list, tuple)) or any(not isinstance(raw, dict) for raw in targets):
        raise ValueError('targets must be frozen metadata objects')
    for raw in targets:
        target = dict(raw)
        try:
            _uuid(target.get('session_id'))
            adapter = target.get('adapter')
            if not isinstance(adapter, str) or adapter not in homes:
                raise _Pending('session_adapter_unconfirmed')
            key = (adapter, target['session_id'])
            if key in seen_targets:
                selected.pop(key, None)
                raise _Pending('duplicate_session_target')
            seen_targets.add(key)
            if target.get('shared_room_ids') != []:
                raise _Pending('session_shared_or_unconfirmed')
            if not isinstance(target.get('owned_cwds'), list) or not target['owned_cwds']:
                raise _Pending('session_cwd_unconfirmed')
            target['owned_cwds'] = [_room_cwd(runtime, workspace, room_id, value) for value in target['owned_cwds']]
            if any(type(target.get(key)) not in (int, float) or not math.isfinite(target[key])
                   for key in ('created_after', 'created_before')) or target['created_after'] > target['created_before']:
                raise _Pending('session_time_unconfirmed')
            selected[key] = target
        except _Pending as error:
            pending(target, str(error))
    for key, target in selected.items():
        try:
            adapter, sid = key
            if adapter == 'codex':
                plans[key] = _codex_plan(homes[adapter], target)
            else:
                projects = []
                remaining_project_state = False
                for cwd in target['owned_cwds']:
                    project = _claude_project(homes[adapter], cwd)
                    path = project / (sid + '.jsonl')
                    try:
                        _check_metadata(target, _metadata(path, claude=True))
                    except FileNotFoundError:
                        try:
                            descriptor = _open_absolute(project)
                        except FileNotFoundError:
                            continue
                        try:
                            remaining_project_state = remaining_project_state or bool(os.listdir(descriptor))
                        finally:
                            os.close(descriptor)
                        continue
                    projects.append((cwd, project))
                if not projects and remaining_project_state:
                    raise _Pending('session_metadata_missing_with_project_state')
                plans[key] = {'missing': not projects, 'projects': projects}
        except (OSError, sqlite3.Error, ValueError, TypeError, UnicodeError, _Pending) as error:
            pending(target, str(error) if isinstance(error, _Pending) else 'session_metadata_unconfirmed')
    handled_projects = set()
    for key, plan in plans.items():
        target, adapter = selected[key], key[0]
        try:
            if plan['missing']:
                result['already_missing'].append({'adapter': adapter, 'session_id': key[1]})
                continue
            binary = commands[adapter]
            if not isinstance(binary, (str, Path)) or not Path(binary).is_absolute() or run_command is None:
                raise _Pending('official_delete_unavailable')
            if adapter == 'codex':
                fresh = _codex_plan(homes[adapter], target)
                if any(('codex', child) not in plans or plans[('codex', child)]['missing']
                       for child in fresh['children']):
                    raise _Pending('session_descendants_unconfirmed')
                if fresh['missing']:
                    result['already_missing'].append({'adapter': adapter, 'session_id': key[1]})
                    continue
                argv = [str(binary), 'delete', '--force', key[1]]
                operations = [(argv, {'CODEX_HOME': str(homes[adapter])})]
            else:
                operations = []
                for cwd, project in plan['projects']:
                    if str(project) in handled_projects:
                        continue
                    descriptor = _open_absolute(project)
                    try:
                        for name in os.listdir(descriptor):
                            if name == 'sessions-index.json':
                                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                                with os.fdopen(fd, 'r', encoding='utf-8') as source:
                                    info = os.fstat(source.fileno())
                                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                            or info.st_size > 2 * 1024 * 1024):
                                        raise _Pending('session_project_index_unconfirmed')
                                    encoded = source.read(2 * 1024 * 1024 + 1)
                                if len(encoded.encode('utf-8')) > 2 * 1024 * 1024:
                                    raise _Pending('session_project_index_unconfirmed')
                                index = json.loads(encoded)
                                entries = index.get('entries') if isinstance(index, dict) else None
                                if (not isinstance(entries, list) or any(
                                        not isinstance(entry, dict)
                                        or not isinstance(entry.get('sessionId'), str)
                                        or ('claude', entry.get('sessionId')) not in plans
                                        or plans[('claude', entry.get('sessionId'))]['missing'] for entry in entries)):
                                    raise _Pending('session_project_shared_or_unregistered')
                                continue
                            if name == 'memory':
                                # Claude Code 2.1.3xx creates an empty auto-memory
                                # folder in every project; the official purge
                                # removes it with the project. Notes inside it
                                # are not ours to grant.
                                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                                if not stat.S_ISDIR(info.st_mode):
                                    raise _Pending('session_project_contents_unconfirmed')
                                memory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                                try:
                                    if os.listdir(memory):
                                        raise _Pending('session_project_contents_unconfirmed')
                                finally:
                                    os.close(memory)
                                continue
                            if not name.endswith('.jsonl'):
                                if re.fullmatch(r'[0-9a-f-]{36}', name):
                                    child = ('claude', name)
                                    if child not in plans or plans[child]['missing']:
                                        raise _Pending('session_project_shared_or_unregistered')
                                    _audit_claude_companion(project, name, selected[child])
                                else:
                                    # Official purge removes the whole project;
                                    # unrecognized contents are not ours to grant.
                                    raise _Pending('session_project_contents_unconfirmed')
                                continue
                            child = ('claude', name[:-6])
                            if child not in plans or plans[child]['missing']:
                                raise _Pending('session_project_shared_or_unregistered')
                            _check_metadata(selected[child], _metadata(project / name, claude=True))
                    finally:
                        os.close(descriptor)
                    operations.append(([str(binary), 'purge', '--yes', cwd], {'CLAUDE_CONFIG_DIR': str(homes[adapter])}))
            for argv, environment in operations:
                outcome = run_command(argv, environment)
                if (not isinstance(outcome, dict) or type(outcome.get('exit_code')) is not int
                        or outcome['exit_code'] != 0 or outcome.get('cleanup_confirmed') is not True):
                    raise _Pending('official_delete_unconfirmed')
                if adapter == 'claude':
                    project = _claude_project(homes[adapter], argv[-1])
                    try:
                        descriptor = _open_absolute(project)
                    except FileNotFoundError:
                        descriptor = None
                    if descriptor is not None:
                        try:
                            if os.listdir(descriptor):
                                raise _Pending('official_delete_unconfirmed')
                        finally:
                            os.close(descriptor)
                    handled_projects.add(str(project))
            if adapter == 'codex' and not _codex_plan(homes[adapter], target)['missing']:
                raise _Pending('official_delete_unconfirmed')
            result['provider_managed_deleted'].append({'adapter': adapter, 'session_id': key[1]})
        except (OSError, ValueError, TypeError, _Pending) as error:
            pending(target, str(error) if isinstance(error, _Pending) else 'official_delete_unconfirmed')
    result['cleanup_confirmed'] = not result['pending']
    return result
