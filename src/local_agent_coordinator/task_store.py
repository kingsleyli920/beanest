"""Private, scoped reservations and observed history for the local MCP bridge.

Each operation owns a short SQLite transaction. The caller must finish the
reservation before awaiting a provider, then record the provider result later.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any, Iterator
from uuid import UUID, uuid4


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TASK_STATUSES = {
    "starting", "running", "idle", "awaiting_input", "failed", "unknown",
    "cancel_requested", "cancelled",
}
_PROVIDER_STATUSES = {
    "starting", "running", "idle", "awaiting_input", "not_loaded", "offline", "dead",
}
_TURN_STATUSES = {"running", "completed", "failed", "interrupted"}
_TOOL_STATUSES = {"pending", "running", "in_progress", "completed", "failed", "cancelled"}
_TERMINAL_STATUSES = {"failed", "unknown", "cancelled"}


class StoreError(RuntimeError):
    """A stable code, without SQL, paths, or caller-supplied diagnostic text."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else "store_error"
        super().__init__(f"Task store operation failed ({self.code}).")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code}


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _task_id(value: object) -> str:
    if not isinstance(value, str):
        raise StoreError("task_not_found")
    try:
        return str(UUID(value))
    except ValueError:
        raise StoreError("task_not_found") from None


def _positive_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


class TaskStore:
    def __init__(self, path: Path, scope: str, max_tasks: int = 4,
                 max_turns: int = 3, max_total_turns: int = 8):
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
            raise StoreError("invalid_scope")
        if any(type(value) is not int or value < 1
               for value in (max_tasks, max_turns, max_total_turns)):
            raise StoreError("invalid_budget")
        self.path = Path(path).absolute()
        self.scope = scope
        self.max_tasks = max_tasks
        self.max_turns = max_turns
        self.max_total_turns = max_total_turns
        self._prepare_path()
        with self._connection(write=True) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    session_id TEXT,
                    parent_task_id TEXT REFERENCES tasks(id),
                    status TEXT NOT NULL,
                    provider_status TEXT,
                    provider_session TEXT,
                    provider_seq INTEGER,
                    operation TEXT,
                    turns INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    identity_uncertain INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    deadline REAL NOT NULL,
                    error TEXT,
                    UNIQUE(scope, request_key)
                )
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, kind in (("provider_session", "TEXT"), ("provider_seq", "INTEGER"),
                               ("identity_uncertain", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
            connection.execute("CREATE INDEX IF NOT EXISTS tasks_scope ON tasks(scope)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS task_sessions (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    session_id TEXT NOT NULL,
                    PRIMARY KEY(task_id, session_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS task_items (
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    source_session TEXT NOT NULL,
                    item_kind TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    value_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, source_session, item_kind, item_id)
                )
            """)

    def _prepare_path(self) -> None:
        try:
            if self.path.is_symlink() or self.path.parent.is_symlink():
                raise StoreError("invalid_store_path")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise StoreError("invalid_store_path")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError:
            raise StoreError("storage_error") from None

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except sqlite3.Error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise StoreError("storage_error") from None
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _row(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id=? AND scope=?",
                                 (_task_id(task_id), self.scope)).fetchone()
        if row is None:
            raise StoreError("task_not_found")
        return row

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in (
            "id", "agent", "session_id", "parent_task_id", "status", "provider_status",
            "operation", "turns", "created_at", "deadline", "error",
        )}
        result["cancel_requested"] = bool(row["cancel_requested"])
        result["identity_uncertain"] = bool(row["identity_uncertain"])
        return result

    def _check_total_turns(self, connection: sqlite3.Connection) -> None:
        total = connection.execute("SELECT COALESCE(SUM(turns), 0) FROM tasks WHERE scope=?",
                                   (self.scope,)).fetchone()[0]
        if total >= self.max_total_turns:
            raise StoreError("total_turn_budget_exceeded")

    def reserve(self, agent: str, prompt: str, request_id: str,
                parent_task_id: str | None = None, timeout: float = 180
                ) -> tuple[dict[str, Any], bool]:
        if (not _identifier(agent) or not isinstance(prompt, str) or not prompt.strip()
                or len(prompt) > 128 * 1024 or "\0" in prompt
                or not isinstance(request_id, str) or not request_id.strip()
                or len(request_id) > 512 or not _positive_number(timeout)):
            raise StoreError("invalid_request")
        parent_id = _task_id(parent_task_id) if parent_task_id is not None else None
        request_key = hashlib.sha256(request_id.encode("utf-8", "surrogatepass")).hexdigest()
        parameters = json.dumps({"agent": agent, "prompt": prompt, "parent": parent_id,
                                 "timeout": float(timeout)}, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(parameters.encode()).hexdigest()
        with self._connection(write=True) as connection:
            previous = connection.execute(
                "SELECT * FROM tasks WHERE scope=? AND request_key=?",
                (self.scope, request_key)).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise StoreError("request_conflict")
                return self._public(previous), False
            if parent_id is not None:
                parent = self._row(connection, parent_id)
                if parent["parent_task_id"] is not None:
                    raise StoreError("parent_depth_exceeded")
                if parent["cancel_requested"] or parent["status"] == "cancelled":
                    raise StoreError("task_cancelled")
            count = connection.execute("SELECT COUNT(*) FROM tasks WHERE scope=?",
                                       (self.scope,)).fetchone()[0]
            if count >= self.max_tasks:
                raise StoreError("task_budget_exceeded")
            self._check_total_turns(connection)
            task_id = str(uuid4())
            created_at = time.time()
            connection.execute("""
                INSERT INTO tasks
                    (id, scope, request_key, fingerprint, agent, parent_task_id,
                     status, operation, turns, created_at, deadline)
                VALUES (?, ?, ?, ?, ?, ?, 'starting', 'create', 1, ?, ?)
            """, (task_id, self.scope, request_key, fingerprint, agent, parent_id,
                  created_at, created_at + float(timeout)))
            return self._public(self._row(connection, task_id)), True

    def get(self, task_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            return self._public(self._row(connection, task_id))

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._public(row) for row in connection.execute(
                "SELECT * FROM tasks WHERE scope=? ORDER BY created_at, id", (self.scope,))]

    def begin_reply(self, task_id: str) -> dict[str, Any]:
        with self._connection(write=True) as connection:
            row = self._row(connection, task_id)
            if row["cancel_requested"] or row["status"] == "cancelled":
                raise StoreError("task_cancelled")
            if row["status"] != "idle" or row["operation"] is not None:
                raise StoreError("task_not_idle")
            if row["deadline"] <= time.time():
                raise StoreError("deadline_exceeded")
            if row["turns"] >= self.max_turns:
                raise StoreError("turn_budget_exceeded")
            self._check_total_turns(connection)
            connection.execute("""
                UPDATE tasks SET turns=turns+1, status='running', operation='send' WHERE id=?
            """, (row["id"],))
            return self._public(self._row(connection, row["id"]))

    def finish(self, task_id: str, *, session_id: str | None = None,
               status: str = "running", error: str | None = None,
               identity_uncertain: bool = False) -> dict[str, Any]:
        """Complete a dispatch without clearing uncertainty from a lost resume result."""
        if (not isinstance(status, str) or status not in _TASK_STATUSES
                or (session_id is not None and not _identifier(session_id))
                or type(identity_uncertain) is not bool):
            raise StoreError("invalid_result")
        if error is not None and (not isinstance(error, str) or not _CODE.fullmatch(error)):
            raise StoreError("invalid_error_code")
        with self._connection(write=True) as connection:
            row = self._row(connection, task_id)
            if session_id is not None:
                other = connection.execute("""
                    SELECT 1 FROM task_sessions s JOIN tasks t ON s.task_id=t.id
                    WHERE t.scope=? AND s.session_id=? AND s.task_id!=?
                """, (self.scope, session_id, row["id"])).fetchone()
                if other is not None:
                    raise StoreError("session_conflict")
                connection.execute("INSERT OR IGNORE INTO task_sessions VALUES (?, ?)",
                                   (row["id"], session_id))
            next_status = status
            if row["cancel_requested"] and status != "cancelled":
                next_status = row["status"]
            elif row["status"] in _TERMINAL_STATUSES and status != "cancelled":
                next_status = row["status"]
            next_error = error if error is not None else row["error"]
            if next_status == "cancelled" and next_error == "cancellation_unconfirmed":
                next_error = None
            bound_session = session_id if session_id is not None else row["session_id"]
            session_changed = bound_session != row["session_id"]
            connection.execute("""
                UPDATE tasks SET session_id=?, status=?, operation=NULL,
                    cancel_requested=?, identity_uncertain=?, error=?,
                    provider_status=?, provider_session=?, provider_seq=?
                WHERE id=?
            """, (bound_session, next_status, int(bool(row["cancel_requested"]) or status == "cancelled"),
                  int(bool(row["identity_uncertain"]) or identity_uncertain),
                  next_error, None if session_changed else row["provider_status"],
                  None if session_changed else row["provider_session"],
                  None if session_changed else row["provider_seq"], row["id"]))
            return self._public(self._row(connection, row["id"]))

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        with self._connection(write=True) as connection:
            row = self._row(connection, task_id)
            status = "cancelled" if row["status"] == "cancelled" else "cancel_requested"
            connection.execute("UPDATE tasks SET cancel_requested=1, status=? WHERE id=?",
                               (status, row["id"]))
            return self._public(self._row(connection, row["id"]))

    def observe(self, task_id: str, provider_status: str, *,
                session_id: str | None = None, seq: int | None = None) -> dict[str, Any]:
        if not isinstance(provider_status, str) or provider_status not in _PROVIDER_STATUSES:
            raise StoreError("invalid_provider_status")
        if session_id is not None and not _identifier(session_id):
            raise StoreError("invalid_provider_source")
        if seq is not None and (type(seq) is not int or not 0 <= seq <= 2**63 - 1):
            raise StoreError("invalid_provider_sequence")
        with self._connection(write=True) as connection:
            row = self._row(connection, task_id)
            if session_id is not None and session_id != row["session_id"]:
                return self._public(row)
            source_session = session_id if session_id is not None else row["session_id"]
            same_source = source_session == row["provider_session"]
            if (seq is not None and same_source and row["provider_seq"] is not None
                    and seq <= row["provider_seq"]):
                return self._public(row)
            status = row["status"]
            if (row["operation"] is None and not row["cancel_requested"]
                    and status not in _TERMINAL_STATUSES):
                status = (provider_status if provider_status in {
                    "starting", "running", "idle", "awaiting_input"} else "unknown")
            latest_seq = seq
            if latest_seq is None and same_source:
                latest_seq = row["provider_seq"]
            connection.execute("""
                UPDATE tasks SET provider_status=?, provider_session=?, provider_seq=?, status=? WHERE id=?
            """, (provider_status, source_session, latest_seq, status, row["id"]))
            return self._public(self._row(connection, row["id"]))

    def record_page(self, task_id: str, page: dict[str, Any]) -> None:
        """Retain only the adapter's visible message/turn/tool fields, by native id."""
        if not isinstance(page, dict) or not _identifier(page.get("sessionId")):
            raise StoreError("invalid_page")
        source_session = page["sessionId"]
        items = []
        for category in ("messages", "turns", "tools"):
            values = page.get(category, [])
            if not isinstance(values, list):
                raise StoreError("invalid_page")
            for value in values:
                normalized = self._history_item(category, value)
                if normalized is not None:
                    kind, item = normalized
                    items.append((kind, item))
        with self._connection(write=True) as connection:
            row = self._row(connection, task_id)
            known = connection.execute(
                "SELECT 1 FROM task_sessions WHERE task_id=? AND session_id=?",
                (row["id"], source_session)).fetchone()
            if known is None:
                raise StoreError("session_not_owned")
            for kind, item in items:
                seq = item.get("seq", -1)
                visible = {"sourceSession": source_session, **item}
                connection.execute("""
                    INSERT INTO task_items VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, source_session, item_kind, item_id)
                    DO UPDATE SET seq=excluded.seq, value_json=excluded.value_json
                    WHERE excluded.seq > task_items.seq
                """, (row["id"], source_session, kind, item["id"], seq,
                      json.dumps(visible, separators=(",", ":"))))

    @staticmethod
    def _history_item(category: str, value: object) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(value, dict) or not _identifier(value.get("id")):
            return None
        item: dict[str, Any] = {"id": value["id"]}
        if type(value.get("seq")) is int and 0 <= value["seq"] <= 2**63 - 1:
            item["seq"] = value["seq"]
        if category == "messages":
            role = value.get("role")
            if not isinstance(role, str) or role not in {"user", "assistant"} or not isinstance(value.get("text"), str):
                return None
            item.update(role=role, text=value["text"])
            return "message:" + role, item
        allowed = _TURN_STATUSES if category == "turns" else _TOOL_STATUSES
        if not isinstance(value.get("status"), str) or value["status"] not in allowed:
            return None
        item["status"] = value["status"]
        if category == "tools" and _identifier(value.get("toolName")):
            item["toolName"] = value["toolName"]
        return ("turn" if category == "turns" else "tool"), item

    def history(self, task_id: str) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"messages": [], "turns": [], "tools": []}
        with self._connection() as connection:
            row = self._row(connection, task_id)
            values = connection.execute("""
                SELECT i.item_kind, i.value_json FROM task_items i
                JOIN task_sessions s ON s.task_id=i.task_id AND s.session_id=i.source_session
                WHERE i.task_id=? ORDER BY s.rowid, i.seq, i.item_kind, i.item_id
            """, (row["id"],))
            for value in values:
                category = {"turn": "turns", "tool": "tools"}.get(value["item_kind"], "messages")
                result[category].append(json.loads(value["value_json"]))
        return result
