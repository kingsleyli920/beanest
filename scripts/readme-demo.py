"""Serve a fictional Beanest room for the README screenshots; no model or CLI ever runs.

    PYTHONPATH=src python scripts/readme-demo.py <empty-runtime-dir> 8795 en   # or zh

Open the printed room at 1280×720 in the light theme and the matching interface
language. Native tools, account checks and model listings are stand-ins, so the
room stays in the same state: two tasks peer-reviewed, one in progress, one in
review and one waiting on a dependency.
"""
import asyncio
import http.client
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator import room_server
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.room_service import RoomService

RUNTIME, PORT, LANG = Path(sys.argv[1]).resolve(), int(sys.argv[2]), sys.argv[3]
EN = LANG == "en"

COPY = {
    "goal": "Add CSV export to Ledger Lite, with tests"
            if EN else "给 Ledger Lite 加上 CSV 导出，补测试和使用说明",
    "project": "Ledger Lite",
    "tasks": [
        ("export-endpoint", "Add a CSV export endpoint for transactions" if EN else "新增交易记录的 CSV 导出接口", "codex", "app/export.py"),
        ("csv-escaping", "Escape commas, quotes and line breaks in CSV fields" if EN else "处理 CSV 字段里的逗号、引号和换行", "claude", "app/csv_format.py"),
        ("export-button", "Add an Export CSV button to the transactions page" if EN else "在交易页加上“导出 CSV”按钮", "kimi", "app/templates/transactions.html"),
        ("export-tests", "Test the export with sample transactions" if EN else "用示例交易数据测试导出", "codex", "tests/test_export.py"),
        ("readme-guide", "Document CSV export in the README" if EN else "在 README 里写清 CSV 导出用法", "claude", "README.md"),
    ],
    "plan": "I'll take the parts I own and review the others." if EN else "我认领自己负责的部分，其余的帮忙互审。",
    "done": "Done and checked against the acceptance criteria." if EN else "已完成，并按验收标准自查。",
    "review": "Checked the exact file and the acceptance criteria." if EN else "核对了交付文件和验收标准。",
}
DEPENDS = {"export-tests": ["export-endpoint"], "readme-guide": ["export-button"]}
# Reviews of these finish; any other review stays in progress for the screenshot.
FINISHED = {"export-endpoint", "csv-escaping"}
HOLD = asyncio.Event()  # never set: in-progress work stays in progress for the screenshot


def project_folder():
    """A small fictional app; nothing here comes from a real project."""
    root = RUNTIME / "sample" / "ledger-lite"
    files = {
        "README.md": "# Ledger Lite\n\nA tiny budgeting app for tracking income and expenses.\n",
        "app/__init__.py": "",
        "app/models.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Transaction:\n    date: str\n    payee: str\n    amount: float\n    category: str\n",
        "app/routes.py": "def list_transactions(store):\n    return store.all()\n",
        "app/templates/transactions.html": "<h1>Transactions</h1>\n<table id=\"transactions\"></table>\n",
        "tests/test_models.py": "from app.models import Transaction\n\n\ndef test_transaction_fields():\n    assert Transaction('2026-01-02', 'Cafe', -4.5, 'Food').payee == 'Cafe'\n",
    }
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    if not (root / ".git").exists():
        env = {"GIT_AUTHOR_NAME": "Sample", "GIT_AUTHOR_EMAIL": "sample@example.invalid",
               "GIT_COMMITTER_NAME": "Sample", "GIT_COMMITTER_EMAIL": "sample@example.invalid", "PATH": "/usr/bin:/bin"}
        for command in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "Initial sample app"]):
            subprocess.run(command, cwd=root, check=True, env=env)
    return root


def native_reply(context, commands=(), *, message):
    from local_agent_coordinator.room_response_schema import command_shape
    _, fields = command_shape(team_phase=context["job"]["job_phase"], can_propose=context["self"] == context["team"]["plan_author"],
                              has_peers=len(context["members"]) > 1, native=True)
    return {"message": message, "state": "done", "commands": [{field: command.get(field) for field in fields} for command in commands],
            "base_revision": context["proposal_base_revision"], "files": [], "edits": [], "unchanged_paths": [], "resolved_conflicts": []}


async def native(agent, prompt, cwd, session_id=None, timeout=None, **options):
    """Stands in for the native CLIs: edits the private copy and answers in the native shape."""
    try:
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        phase, board, cwd = context["job"]["job_phase"], context["team"], Path(cwd)
        held = (phase == "work" and context.get("task", {}).get("id") == "export-button"
                or phase == "review" and context.get("task", {}).get("id") not in FINISHED)
        activity = {"type": "native_tool", "operation": "edit_file" if phase == "work" else "read_file",
                    "state": "running" if held else "completed"}
        options["on_activity"](activity)
        if phase in {"plan", "coordinate"}:
            visible = {item["id"]: item for item in board["tasks"]}
            commands = []
            if not visible and agent == board["plan_author"]:
                commands = [{"type": "propose", "id": key, "title": title, "description": title, "acceptance": title,
                             "dependencies": DEPENDS.get(key, []), "write_paths": [path], "capability": "python"}
                            for key, title, _, path in COPY["tasks"]]
                visible = {key: {"owner": None} for key, *_ in COPY["tasks"]}
            commands += [{"type": "claim", "task_id": key} for key, _, owner, _ in COPY["tasks"]
                         if owner == agent and key in visible and visible[key]["owner"] is None]
            if visible:
                commands.append({"type": "plan_ready"})
            value = native_reply(context, commands, message=COPY["plan"])
        elif phase == "review":
            key = context["task"]["id"]
            if key not in FINISHED:
                await HOLD.wait()
            value = native_reply(context, [{"type": "review", "task_id": key, "verdict": "accept", "reason": COPY["review"]}],
                                 message=COPY["review"])
        elif phase == "work":
            key, path = context["task"]["id"], context["task"]["write_paths"][0]
            if key == "export-button":
                await HOLD.wait()
            target = cwd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {key}\n")
            value = native_reply(context, [{"type": "complete", "task_id": key, "result": COPY["done"]}], message=COPY["done"])
        else:
            value = native_reply(context, message=COPY["done"])
        observation = dict(permission=options["permission"], tool_calls=1, tool_results=1,
                           side_effects_possible=phase == "work", cleanup_confirmed=True, exit_code=0, duration_seconds=0.01)
        return {"session_id": session_id or str(uuid5(NAMESPACE_URL, f"{agent}:{cwd}")), "status": "completed", "usage": None,
                "events": [activity], "cleanup_confirmed": True, "native_execution": observation, "text": json.dumps(value)}
    except Exception:
        import traceback
        traceback.print_exc()
        raise


async def provider(*args, **kwargs):
    raise AssertionError("a native sample room never uses the static provider")


def fictional_accounts(manager, now):
    def account(adapter, source, quota_source, windows):
        return {"state": "ready", "email": None, "plan": None, "auth_method": None, "source": source,
                "scope": "default_cli_login", "checked_at": now, "stale": False, "reason": None, "shared_profile_ids": [],
                "quota": {"state": "available", "windows": windows, "source": quota_source, "scope": "provider_account",
                          "checked_at": now, "stale": False, "reason": None}}
    window = lambda name, left, minutes, hours: {"name": name, "remaining_percent": left, "used_percent": 100 - left,
                                                 "window_minutes": minutes, "resets_at": now + hours * 3600}
    manager._account_cache.update({
        "claude": account("claude", "claude_auth_status", "claude_auth_status",
                          [window("claude:five_hour", 64, 300, 2), window("claude:seven_day", 81, 10080, 70)]),
        "codex": account("codex", "codex_app_server", "codex_app_server_account_rate_limits",
                         [window("codex:primary", 47, 300, 3), window("codex:secondary", 72, 10080, 90)]),
        "kimi": account("kimi", "kimi_managed_oauth", "kimi_managed_oauth", [window("kimi:limit5h", 88, 300, 4)]),
    })


async def no_cli(self, *args, **kwargs):
    """Account and availability checks would run real CLIs; the sample keeps its fixed values."""
    return self.get(args[0]) if args else None


PartnerManager.refresh_account = no_cli
PartnerManager.refresh_availability = lambda self, *args, **kwargs: asyncio.sleep(0)
RoomService.expire_tasks = lambda self: asyncio.sleep(0)


async def listed_options(self, partner_id):
    return await self.runtime_options()


RoomService.refresh_runtime_options = listed_options


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    manager = PartnerManager(RUNTIME / "partners", default_registry(), startup_discovery=False)
    if not any(profile["id"] == "kimi" for profile in manager.profiles()):
        manager.add(id="kimi", name="Kimi Code", adapter="kimi")
    fictional_accounts(manager, time.time())
    service = RoomService(RUNTIME / "rooms.sqlite3", RUNTIME / "workspaces", invoke=provider, partners=manager, native_invoke=native)
    # Fixed model lists stand in for each CLI's own model listing.
    catalog = lambda *models: {"state": "ready", "checked_at": time.time(), "models": [
        {"id": model, "name": name, "efforts": efforts} for model, name, efforts in models]}
    service._runtime_catalogs.update({
        "claude": catalog(("opus", "Opus", ["low", "medium", "high"])),
        "codex": catalog(("gpt-5-codex", "GPT-5 Codex", ["low", "medium", "high"])),
        "kimi": catalog(("kimi-k2", "Kimi K2", []))})
    server = room_server.create_room_server(service, port=PORT)
    room_server.publish_connection(server, RUNTIME)
    folder = project_folder()
    print(server.url, flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        headers = {"X-LAC-Token": server.token, "Content-Type": "application/json"}
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    code, project = request("POST", "/api/projects", {"name": COPY["project"], "path": str(folder), "selection_kind": "folder"})
    print("project", code, project.get("error"), flush=True)
    code, room = request("POST", "/api/rooms", {
        "goal": COPY["goal"], "target": "all", "request_id": "readme-sample-" + LANG, "mode": "native_project",
        "project_id": project["project"]["id"], "work_mode": "build", "participants": ["claude", "codex", "kimi"],
        "collaboration": True, "runtime_settings": {"permission": "full_access", "members": {
            "claude": {"model": "opus", "effort": "high"}, "codex": {"model": "gpt-5-codex", "effort": "medium"},
            "kimi": {"model": "kimi-k2", "effort": None}}},
        "execution_policy": {"mode": "autonomous", "max_seconds": None, "max_calls": None, "quota_failover": True}})
    print("room", code, room.get("error") or room["room"]["id"], flush=True)
    thread.join()


main()
