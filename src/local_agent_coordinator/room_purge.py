"""Delete only a stopped room's coordinator-owned files.

The service owns cancellation, dependency snapshots, its durable operation journal,
and the database transaction. This helper never follows paths from room metadata.
"""
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import stat
from uuid import UUID


ROOM_BUCKETS = (
    "project-artifacts", "folder-artifacts", "native-workspaces", "native-exports",
    "native-receipts", "native-transfers", "native-bootstrap", "native-handoffs",
)
ARTIFACT_BUCKETS = ("project-artifacts", "folder-artifacts")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RoomPurgeError(Exception):
    def __init__(self, code, path):
        self.code, self.path = code, str(path)
        super().__init__(code)


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise RoomPurgeError("invalid_purge_id", "")
    return value


def _path(value):
    try:
        path = Path(value)
    except TypeError:
        raise RoomPurgeError("invalid_purge_root", "") from None
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise RoomPurgeError("invalid_purge_root", path)
    return path


def _open_absolute(path):
    """Pin the root through no-follow directory descriptors, including ancestors."""
    descriptor = os.open(path.anchor, _DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child(parent, name):
    try:
        return os.open(name, _DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        return None


def _erase(parent, name, device):
    """Use relative syscalls so a nested symlink cannot redirect traversal."""
    try:
        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(entry.st_mode):
        os.unlink(name, dir_fd=parent)
        return True
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino) or current.st_dev != device:
            raise RoomPurgeError("purge_path_changed", name)
        for child in os.listdir(descriptor):
            _erase(descriptor, child, device)
        latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (latest.st_dev, latest.st_ino) != (current.st_dev, current.st_ino):
            raise RoomPurgeError("purge_path_changed", name)
        os.rmdir(name, dir_fd=parent)
        return True
    finally:
        os.close(descriptor)


def purge_room_files(runtime_root, workspace_root, room_id, *, execution_ids=()):
    """Idempotently remove one room's files; raise on incomplete/unsafe cleanup.

    ``workspace_root`` must be a direct child of ``runtime_root``. Execution IDs
    come from this room's database records; no provider or metadata path is used.
    Call only after all room writers and owned processes have stopped.
    """
    room_id = _uuid(room_id)
    if not isinstance(execution_ids, (list, tuple, set, frozenset)):
        raise RoomPurgeError("invalid_purge_id", "")
    execution_ids = tuple(dict.fromkeys(_uuid(value) for value in execution_ids))
    runtime_root, workspace_root = _path(runtime_root), _path(workspace_root)
    if workspace_root.parent != runtime_root or workspace_root.name in (*ROOM_BUCKETS, "python-runner", "projects", "partners"):
        raise RoomPurgeError("invalid_purge_root", workspace_root)
    removed = []
    try:
        with ExitStack() as resources:
            root = _open_absolute(runtime_root)
            resources.callback(os.close, root)
            device = os.fstat(root).st_dev
            # Open every bucket and acquire both artifact locks before deleting
            # anything. A busy lock must not produce a partially removed room.
            buckets = {}
            for bucket in (*ROOM_BUCKETS, workspace_root.name):
                descriptor = _open_child(root, bucket)
                if descriptor is not None:
                    resources.callback(os.close, descriptor)
                    buckets[bucket] = descriptor
            locks = []
            for bucket in ARTIFACT_BUCKETS:
                if bucket not in buckets:
                    continue
                name = f".{room_id}.lock"
                try:
                    descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=buckets[bucket])
                except FileNotFoundError:
                    continue
                resources.callback(os.close, descriptor)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise RoomPurgeError("purge_unsafe_lock", runtime_root / bucket / name)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RoomPurgeError("purge_workspace_busy", runtime_root / bucket / name) from None
                locks.append((bucket, name, os.fstat(descriptor)))
            runner = _open_child(root, "python-runner")
            attempts = None
            if runner is not None:
                resources.callback(os.close, runner)
                attempts = _open_child(runner, "attempts")
                if attempts is not None:
                    resources.callback(os.close, attempts)
            for bucket, descriptor in buckets.items():
                if _erase(descriptor, room_id, device):
                    removed.append(f"{bucket}/{room_id}")
            for bucket, name, original in locks:
                latest = os.stat(name, dir_fd=buckets[bucket], follow_symlinks=False)
                if (original.st_dev, original.st_ino) != (latest.st_dev, latest.st_ino):
                    raise RoomPurgeError("purge_path_changed", runtime_root / bucket / name)
                os.unlink(name, dir_fd=buckets[bucket])
                removed.append(f"{bucket}/{name}")
            if attempts is not None:
                for execution_id in execution_ids:
                    name = execution_id + ".json"
                    if _erase(attempts, name, device):
                        removed.append(f"python-runner/attempts/{name}")
    except RoomPurgeError:
        raise
    except OSError as error:
        raise RoomPurgeError("purge_files_incomplete", error.filename or runtime_root) from None
    return {"room_id": room_id, "removed": removed, "cleanup_confirmed": True}
