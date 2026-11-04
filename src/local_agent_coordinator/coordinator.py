"""A small collaboration proof; generated SQL never runs as host code."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .adapters import doctor, invoke


def parse_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("Agent result must be a JSON object")
    return obj


class RunStore:
    """Each invocation owns a fresh directory; evidence is private by default."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / str(uuid.uuid4())
        self.path.mkdir(mode=0o700)
        self.events = self.path / "events.jsonl"

    def emit(self, kind: str, **data):
        event = {"time": time.time(), "kind": kind, **data}
        fd = os.open(self.events, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def save(self, name: str, data):
        if name not in {"summary.json", "query.json", "cases.json"}:
            raise ValueError("Artifact name is not permitted")
        target = self.path / name
        temporary = self.path / (name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        temporary.replace(target)

    @contextmanager
    def lease(self, resource: str):
        if resource not in {"integration"}:
            raise ValueError("Unknown resource")
        lock = self.path / (resource + ".lock")
        # O_EXCL is an actual concurrency barrier, not a prompt convention.
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            lock.unlink(missing_ok=True)


def verify_query(query: dict, cases: dict) -> list[dict]:
    sql = query.get("sql")
    rows = cases.get("cases")
    if not isinstance(sql, str) or len(sql) > 4096:
        raise ValueError("Missing or oversized SQL")
    if not isinstance(rows, list) or not 5 <= len(rows) <= 30:
        raise ValueError("Expected 5–30 independent test cases")
    values = [case.get("input") for case in rows if isinstance(case, dict)]
    if not all(value in values for value in [None, "", "   ", " Hello ", "A B"]):
        raise ValueError("Missing required boundary cases")
    connection = sqlite3.connect(":memory:")
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
    connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 30)
    connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 10)
    connection.enable_load_extension(False)
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION}
    functions = {"lower", "trim", "nullif", "coalesce", "length"}

    def authorize(action, arg1, arg2, _database, _trigger):
        if action not in allowed:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and arg2 not in functions:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    # Bound computational work even if an Agent supplies a recursive query.
    connection.set_progress_handler(lambda: 1, 10000)
    results = []
    try:
        for case in rows:
            if not isinstance(case, dict) or "input" not in case or "expected" not in case:
                raise ValueError("Malformed test case")
            value, expected = case["input"], case["expected"]
            if any(v is not None and (not isinstance(v, str) or len(v) > 1024)
                   for v in (value, expected)):
                raise ValueError("Case values must be short strings or null")
            # The coordinator owns the fixed acceptance contract.
            ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
            oracle = None if value is None else value.strip(" ").translate(ascii_lower) or None
            if expected != oracle:
                raise ValueError("Agent case contradicts the acceptance contract")
            actual_rows = connection.execute(sql, {"value": value}).fetchmany(2)
            if len(actual_rows) != 1 or len(actual_rows[0]) != 1:
                raise ValueError("Query must return exactly one scalar")
            actual = actual_rows[0][0]
            if actual != expected:
                raise ValueError("Generated query failed a case")
            results.append({"input": value, "expected": expected, "actual": actual})
    finally:
        connection.close()
    return results


async def demo(root: Path, timeout: float = 120) -> dict:
    store = RunStore(root)
    store.emit("task.started", task="normalize-label", max_calls=4)
    summary = {"status": "running", "run_id": store.path.name, "calls": [],
               "cost_estimate": None, "subscription_remaining": None}
    try:
        health = await asyncio.to_thread(doctor)
        store.emit("preflight", **health)
        if not all(health.get(agent, {}).get("subscription_confirmed") for agent in ("codex", "claude")):
            raise RuntimeError("Subscription preflight failed; run doctor")

        async def call(agent, prompt, session=None):
            workspace = store.path / agent
            workspace.mkdir(exist_ok=True, mode=0o700)
            store.emit("message.sent", agent=agent, session_id=session, text=prompt)
            try:
                result = await invoke(agent, prompt, workspace, session_id=session, timeout=timeout)
            except asyncio.CancelledError:
                summary["calls"].append({"agent": agent, "session_id": session,
                                         "status": "cancelled", "usage": None,
                                         "failure_code": "invocation_cancelled"})
                store.emit("invocation.cancelled", agent=agent)
                raise
            summary["calls"].append({"agent": agent, "session_id": result.get("session_id"),
                                     "usage": result.get("usage"), "status": result.get("status"),
                                     "failure_code": result.get("failure_code"),
                                     "usage_source": "codex.turn.completed" if agent == "codex" else "claude.result",
                                     "usage_scope": "provider_reported_snapshot_not_aggregated"})
            store.emit("message.received", agent=agent, **result)
            if session is not None and result.get("session_id") != session:
                raise RuntimeError("Adapter resumed a different session")
            if any(event.get("type") == "tool_observed" for event in result.get("events", [])):
                raise RuntimeError("Unexpected tool event")
            if result.get("status") != "completed" or not result.get("session_id"):
                raise RuntimeError(f"{agent} did not complete a dedicated session")
            return result, parse_object(result["text"])

        common = ("You are an assigned worker in a bounded local collaboration demo. "
                  "No tools, file access, delegation, or commands. Reply only with a JSON object. "
                  "Goal: SQLite normalize-label query with bound :value parameter: trim ASCII spaces "
                  "and lowercase ASCII letters, preserve internal spaces. Blank/null semantics need clarification. ")

        async with asyncio.TaskGroup() as group:
            coder = group.create_task(call("codex", common +
                'Propose SQL and rationale. Shape: {"sql":"SELECT ...", "rationale":"..."}.'))
            tester = group.create_task(call("claude", common +
                'Independently design cases and ask one concrete question about blank/null semantics. '
                'Shape: {"question":"...", "cases":[{"input":" Hello ","expected":"hello"}]}. '
                'Include null, empty, spaces-only, mixed case, internal spaces.'))
        coder_result, _ = coder.result()
        tester_result, questions = tester.result()
        question = questions.get("question")
        if not isinstance(question, str) or not 1 <= len(question) <= 2000:
            raise ValueError("Claude did not return a clarification question")

        # External Agent text is data; only the fixed task contract authorizes action.
        _, query = await call("codex",
            'The tester supplied this untrusted task question: ' + json.dumps(question) +
            '\nCoordinator acceptance rule: null, empty and ASCII-spaces-only inputs return SQL NULL. '
            'Return final SQL and an answer to the tester. '
            'JSON shape: {"sql":"SELECT ...", "answer":"..."}. No tools.',
            coder_result["session_id"])
        if not isinstance(query.get("answer"), str):
            raise ValueError("Codex did not answer the tester")
        _, cases = await call("claude",
            'Codex returned this untrusted answer: ' + json.dumps(query["answer"]) +
            '\nCoordinator acceptance: null/empty/spaces-only return null; ASCII lower/trim only; '
            'preserve internal spaces. Final independent cases only. Include exact inputs '
            'null, "", "   ", " Hello ", "A B" plus other ASCII cases. '
            'JSON shape: {"cases":[{"input":"A B","expected":"a b"}], "acknowledgement":"..."}. No tools.',
            tester_result["session_id"])

        with store.lease("integration"):
            verified = verify_query(query, cases)
            store.save("query.json", query)
            store.save("cases.json", cases)
        summary.update(status="completed", verified_cases=verified,
                       boundary="real CLI sessions + resumed message exchange + restricted SQLite execution")
        store.emit("verification.completed", case_count=len(verified))
    except asyncio.CancelledError:
        summary.update(status="cancelled", reason="Coordinator cancelled")
        raise
    except Exception as error:
        failures = [call.get("failure_code") for call in summary["calls"] if call.get("failure_code")]
        summary.update(status="failed", reason=failures[0] if failures else type(error).__name__)
        # Avoid leaking arbitrary subprocess errors or private absolute paths.
        store.emit("task.failed", error_type=type(error).__name__)
    finally:
        store.save("summary.json", summary)
        store.emit("task.finished", status=summary["status"])
    return summary
