"""Safe inspection boundaries yield one member to a waiting peer help call."""
import asyncio
import unittest
from unittest.mock import patch

from tests import test_folder_service as folder


class InspectionYieldTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = folder.FolderServiceTests.asyncSetUp
    asyncTearDown = folder.FolderServiceTests.asyncTearDown
    settle = folder.FolderServiceTests.settle
    create = folder.FolderServiceTests.create
    assert_source_unchanged = folder.FolderServiceTests.assert_source_unchanged

    async def scenario(self, *, waiting=True, boundary="normal"):
        started = asyncio.Event()
        help_seen = False
        owner_resumed = False
        original_binding = None
        original_inspection_job = None

        async def handler(agent, context):
            nonlocal help_seen, owner_resumed, original_binding, original_inspection_job
            phase, board = context["job"]["job_phase"], context["team"]
            with self.service.store._connection() as db:
                room_id = db.execute("SELECT room_id FROM jobs WHERE id=?", (context["job"]["id"],)).fetchone()[0]
                self.assertLessEqual(db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE room_id=? AND provider=? AND status IN ('queued','working')",
                    (room_id, agent)).fetchone()[0], 1)
            if phase in {"plan", "coordinate"}:
                visible = {task["id"]: task for task in board["tasks"]}
                commands = []
                if agent == board["plan_author"] and not visible:
                    for owner in ("claude", "codex"):
                        commands.append({"type": "propose", "id": owner, "title": owner + " report",
                            "description": "Inspect then write a report", "acceptance": "Peer reviews report",
                            "dependencies": [], "write_paths": [owner + ".txt"], "capability": "text"})
                    visible = {owner: {"owner": None} for owner in ("claude", "codex")}
                if agent in visible and visible[agent]["owner"] is None:
                    commands.append({"type": "claim", "task_id": agent})
                if visible:
                    commands.append({"type": "plan_ready"})
                return folder.response(context, commands=commands)
            task = context["task"]
            if phase == "help":
                self.assertEqual(agent, "claude")
                self.assertEqual(task["id"], "codex")
                self.assertEqual(folder.observations(context), [], "Owner's inspection cannot leak into help")
                help_seen = True
                return folder.response(context, message="Evidence for the peer")
            if phase == "work":
                if agent == "claude" and not started.is_set():
                    original_binding = (board["run_id"], task["id"])
                    original_inspection_job = context["job"]["id"]
                    started.set()
                    if waiting:
                        while not any(item["status"] == "waiting_peer" for item in
                                      self.service.store.snapshot(room_id)["team"]["tasks"]):
                            await asyncio.sleep(0)
                    if boundary == "budget":
                        with self.service.store._connection(write=True) as db:
                            db.execute("UPDATE rooms SET call_limit=used WHERE id=?", (room_id,))
                    if boundary == "correction":
                        with self.service.store._connection(write=True) as db:
                            db.execute("UPDATE team_jobs SET format_attempt=1 WHERE job_id=?", (context["job"]["id"],))
                    if boundary == "unknown":
                        return {"__unknown__": True}
                    return folder.response(context, request=folder.request("read", "src/main.py"))
                if agent == "codex" and waiting and not task["help_result"]:
                    await started.wait()
                    return folder.response(context, commands=[{"type": "request_help", "task_id": "codex",
                        "to": "claude", "capability": "text", "text": "Need independent evidence"}])
                if agent == "claude":
                    owner_resumed = True
                    if waiting and boundary == "normal":
                        self.assertTrue(help_seen, "Inspection continuation starved waiting peer help")
                    if boundary == "correction":
                        self.assertFalse(help_seen, "Correction calls must preserve their own exact continuation")
                        with self.service.store._connection() as db:
                            self.assertEqual(db.execute("SELECT format_attempt FROM team_jobs WHERE job_id=?",
                                (context["job"]["id"],)).fetchone()[0], 1)
                    self.assertEqual((board["run_id"], task["id"]), original_binding)
                    self.assertEqual(folder.read_result(context, "src/main.py")["content"], "VALUE = 1\n")
                if agent == "codex" and waiting:
                    self.assertEqual(task["help_result"], "Evidence for the peer")
                value = folder.response(context, commands=[{"type": "complete", "task_id": task["id"], "result": "Report written"}])
                value["files"] = [{"path": agent + ".txt", "previous_sha256": None, "content": agent + " evidence\n"}]
                return value
            self.assertEqual(phase, "review")
            return folder.response(context, commands=[{"type": "review", "task_id": task["id"],
                "verdict": "accept", "reason": "Checked exact host-bound files"}])

        self.provider.handler = handler
        if boundary == "unknown":
            original_call = self.provider.__class__.__call__
            async def uncertain(provider, *args, **kwargs):
                result = await original_call(provider, *args, **kwargs)
                if '__unknown__' in result.get('text', ''):
                    result.update(status="unknown", failure_code="process_or_output_failure")
                return result
            with patch.object(self.provider.__class__, "__call__", uncertain):
                created = await self.create(collaboration=True)
                await self.settle()
        else:
            created = await self.create(collaboration=True)
            await self.settle()
        snapshot = self.service.store.snapshot(created["room"]["id"])
        self.assertEqual(self.provider.maximum, {"claude": 1, "codex": 1})
        self.assert_source_unchanged()
        if boundary in {"normal", "correction", "budget"}:
            self.assertEqual(snapshot["team"]["phase"], "ready_for_user")
            self.assertTrue(owner_resumed)
            self.assertEqual(help_seen, waiting)
            receipt = self.service.store.inspection_context(snapshot["room"]["id"], "claude", *original_binding, "work")
            self.assertEqual(receipt["observations"][0]["job_id"], original_inspection_job)
        else:
            self.assertFalse(owner_resumed)
            if boundary == "unknown":
                self.assertFalse(help_seen)
            self.assertNotEqual(snapshot["team"]["phase"], "ready_for_user")

    async def test_waiting_help_runs_before_owner_inspection_continuation(self):
        await self.scenario()

    async def test_no_waiting_help_keeps_normal_inspection_continuation(self):
        await self.scenario(waiting=False)

    async def test_historical_budget_does_not_prevent_restoring_suspended_work(self):
        await self.scenario(boundary="budget")

    async def test_unknown_result_never_yields_or_restores_work(self):
        await self.scenario(boundary="unknown")

    async def test_correction_inspection_preserves_binding_without_yield(self):
        await self.scenario(boundary="correction")
