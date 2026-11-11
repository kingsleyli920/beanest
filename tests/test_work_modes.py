"""Mode authority over real storage/workspaces; provider transport stays fake."""
import copy
from contextlib import closing
import hashlib
import sqlite3
import unittest
from unittest.mock import AsyncMock

from local_agent_coordinator.room_service import RoomError
from local_agent_coordinator.room_store import RoomStore
from tests import test_team_service as team
from tests import test_folder_service as folder


class WorkModeServiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = team.TeamServiceTests.asyncSetUp
    asyncTearDown = team.TeamServiceTests.asyncTearDown
    settle = team.TeamServiceTests.settle

    def plan(self, agent, context):
        self.assertEqual(context["work_mode"], "plan")
        self.assertEqual(context["available_capabilities"], ["text"])
        phase = context["job"]["job_phase"]
        if phase in {"plan", "coordinate"}:
            commands = []
            visible = {item["id"]: item for item in context["team"]["tasks"]}
            if not visible and agent == context["team"]["plan_author"]:
                commands = [{**team.task(key, "unused.md"), "write_paths": [], "capability": "text"}
                            for key in ("main", "checks")]
                visible = {key: {"owner": None} for key in ("main", "checks")}
            key = "main" if agent == "claude" else "checks"
            if key in visible and visible[key]["owner"] is None:
                commands.append({"type": "claim", "task_id": key})
            if visible:
                commands.append({"type": "plan_ready"})
            return team.reply(context, commands=commands)
        bound = context["task"]
        if phase == "review":
            self.assertEqual(bound["result_files"], [])
            self.assertTrue(bound["result"])
            return team.reply(context, commands=[{"type": "review", "task_id": bound["id"],
                "verdict": "accept", "reason": "The discussion identifies concrete implementation and checks."}])
        return team.reply(context, commands=[{"type": "complete", "task_id": bound["id"],
            "result": "Implement a score counter, and independently verify the edge cases."}])

    async def create_plan(self):
        self.provider.handler = self.plan
        created = await self.service.create_room("Discuss a score tool", "all", "plan-first",
            mode="text_artifacts", collaboration=True, work_mode="plan")
        await self.settle()
        return await self.service.read_room(created["room"]["id"])

    async def test_plan_accept_is_read_only_then_explicit_build_writes_in_same_room(self):
        first = await self.create_plan()
        room_id, run = first["room"]["id"], first["current_run"]
        self.assertEqual(run["work_mode"], "plan")
        self.assertEqual(first["project"]["revision"], 0)
        self.assertEqual(first["project"]["files"], [])
        self.assertTrue(run["can_accept"])
        calls = len(self.provider.calls)
        await self.service.accept_run(room_id, "accept-plan", run_id=run["id"], revision=run["revision"], digest=run["digest"])
        await self.settle()
        self.assertEqual(len(self.provider.calls), calls, "accept must not start implementation")
        proposals = [{**team.task("main", "score.json"), "capability": "text"},
                     {**team.task("checks", "guide.md"), "capability": "text"}]
        self.provider.handler = team.TeamScenario(self, proposals, {"main": "claude", "checks": "codex"},
            {"main": '{"win":3}\n', "checks": "# Score\nA win earns three points.\n"})
        options = dict(run_id=run["id"], revision=run["revision"], digest=run["digest"], work_mode="build")
        await self.service.start_run(room_id, "Implement the reviewed plan", "all", "build-next", **options)
        await self.settle()
        built = await self.service.read_room(room_id)
        self.assertEqual(built["current_run"]["work_mode"], "build")
        self.assertEqual(built["room"]["work_mode"], "build")
        self.assertEqual({file["path"] for file in built["project"]["files"]}, {"score.json", "guide.md"})
        self.assertTrue(all(task["status"] == "accepted" for task in built["team"]["tasks"]))
        source_plans = [call["context"]["source_plan"] for call in self.provider.calls[calls:]]
        self.assertTrue(source_plans)
        self.assertTrue(all(plan == source_plans[0] for plan in source_plans))
        self.assertEqual(source_plans[0]["run_id"], run["id"])
        self.assertEqual(source_plans[0]["revision"], run["revision"])
        self.assertEqual(source_plans[0]["digest"], run["digest"])
        self.assertFalse(source_plans[0]["truncated"])
        self.assertEqual({item["id"]: item["result"]["text"] for item in source_plans[0]["tasks"]},
                         {item["id"]: item["result"] for item in first["team"]["tasks"]})
        self.assertTrue(all(item["result_sha256"] == hashlib.sha256(item["result"]["text"].encode()).hexdigest()
                            for item in source_plans[0]["tasks"]))
        history = await self.service.read_run(room_id, run["id"])
        self.assertEqual(history["run"]["work_mode"], "plan")
        self.assertEqual(history["team"]["tasks"], first["team"]["tasks"])
        calls = len(self.provider.calls)
        await self.service.start_run(room_id, "Implement the reviewed plan", "all", "build-next", **options)
        self.assertEqual(len(self.provider.calls), calls)
        with self.assertRaises(RoomError) as caught:
            await self.service.start_run(room_id, "Implement the reviewed plan", "all", "build-next", **{**options, "work_mode": "plan"})
        self.assertEqual(caught.exception.code, "request_conflict")

    async def test_large_source_plan_is_host_bound_and_explicitly_truncated_for_both_peers(self):
        def long_plan(agent, context):
            value = self.plan(agent, context)
            for command in value["commands"]:
                if command["type"] == "complete":
                    command["result"] = "🫘" * 4000
            return value
        self.provider.handler = long_plan
        created = await self.service.create_room("A long plan", "all", "long-plan",
            mode="text_artifacts", collaboration=True, work_mode="plan")
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        run = snapshot["current_run"]
        calls = len(self.provider.calls)
        self.provider.handler = team.TeamScenario(self,
            [{**team.task("main", "guide.md"), "capability": "text"}], {"main": "claude"}, {"main": "# Work\n"})
        await self.service.start_run(created["room"]["id"], "Build", "all", "long-build",
            run_id=run["id"], revision=run["revision"], digest=run["digest"], work_mode="build")
        await self.settle()
        plans = [call["context"]["source_plan"] for call in self.provider.calls[calls:]]
        self.assertEqual({call["agent"] for call in self.provider.calls[calls:]}, {"claude", "codex"})
        self.assertTrue(all(plan == plans[0] for plan in plans))
        self.assertTrue(plans[0]["truncated"])
        self.assertLessEqual(sum(len(item["result"]["text"].encode()) for item in plans[0]["tasks"]), 24 * 1024)
        self.assertTrue(all(item["result"]["omitted_characters"] > 0 for item in plans[0]["tasks"]))
        self.assertTrue(all(item["result_sha256"] == hashlib.sha256(("🫘" * 4000).encode()).hexdigest()
                            for item in plans[0]["tasks"]))
        built = await self.service.read_room(created["room"]["id"])
        self.assertTrue(built["current_run"]["source_plan"]["truncated"])
        self.assertNotIn("tasks", built["current_run"]["source_plan"])

    async def test_omitted_continuation_inherits_plan_without_relabelling_history(self):
        first = await self.create_plan()
        run = first["current_run"]
        await self.service.start_run(first["room"]["id"], "Refine the design", "all", "more-plan",
            run_id=run["id"], revision=run["revision"], digest=run["digest"])
        await self.settle()
        next_snapshot = await self.service.read_room(first["room"]["id"])
        self.assertEqual([run["work_mode"] for run in next_snapshot["runs"]], ["plan", "plan"])
        self.assertEqual(next_snapshot["project"]["files"], [])

    async def test_plan_host_rejects_files_edits_write_paths_and_execution_capabilities(self):
        attempts = [
            {"files": [{"path": "bad.md", "previous_sha256": None, "content": "unauthorized"}]},
            {"edits": [{"path": "bad.md", "base_sha256": "0" * 64, "old_text": "x", "new_text": "y"}]},
            {"commands": [{**team.task("bad", "bad.md"), "capability": "text"}]},
            {"commands": [{**team.task("bad", "bad.md"), "write_paths": []}]},
        ]
        for number, attempt in enumerate(attempts):
            with self.subTest(attempt=attempt):
                self.provider.handler = lambda agent, context, value=attempt: {**team.reply(context), **copy.deepcopy(value)}
                room = await self.service.create_room("Discuss only", "all", f"reject-{number}",
                    mode="text_artifacts", collaboration=True, work_mode="plan")
                await self.settle()
                snapshot = await self.service.read_room(room["room"]["id"])
                # Files or edits are a reply mistake the partner corrects; a write scope is refused outright.
                expected = "team_files_forbidden" if "files" in attempt or "edits" in attempt else "plan_read_only"
                self.assertTrue(any(job["error"] == expected for job in snapshot["jobs"]), [job["error"] for job in snapshot["jobs"]])
                self.assertEqual(snapshot["project"]["revision"], 0)
                self.assertEqual(snapshot["project"]["files"], [])
                self.assertEqual(snapshot["team"]["tasks"], [])
                self.assertEqual(snapshot["executions"], [])

    async def test_legacy_migration_and_old_request_fingerprint_stay_compatible(self):
        path = self.root / "old.sqlite3"
        old = RoomStore(path)
        room_id, _ = old.create_room("Legacy draft", "claude", "old-id", collaboration=True)
        before = old.snapshot(room_id)
        old.close()
        with closing(sqlite3.connect(path)) as db:
            db.execute("ALTER TABLE rooms DROP COLUMN work_mode")
            db.execute("ALTER TABLE team_runs DROP COLUMN work_mode")
            db.commit()
        migrated = RoomStore(path)
        try:
            self.assertEqual(migrated.create_room("Legacy draft", "claude", "old-id", collaboration=True), (room_id, False))
            snapshot = migrated.snapshot(room_id)
            self.assertEqual(snapshot["room"]["work_mode"], "legacy")
            self.assertEqual(snapshot["current_run"]["work_mode"], "legacy")
            for key in ("members", "jobs", "events", "budget"):
                self.assertEqual(snapshot[key], before[key])
            with self.assertRaises(RoomError) as caught:
                migrated.create_room("Legacy draft", "claude", "old-id", collaboration=True, work_mode="plan")
            self.assertEqual(caught.exception.code, "request_conflict")
        finally:
            migrated.close()

    async def test_plan_sandbox_never_prepares_or_reserves_execution_and_build_requires_runner(self):
        self.provider.handler = self.plan
        self.service._execution_ready = AsyncMock(side_effect=AssertionError("A plan must not prepare a runner"))
        created = await self.service.create_room("Plan only", "all", "sandbox-plan",
            mode="python_sandbox", collaboration=True, work_mode="plan")
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.service._execution_ready.assert_not_called()
        self.assertEqual(snapshot["project"]["execution"], "disabled")
        self.assertEqual(snapshot["project"]["execution_reason"], "plan_read_only")
        self.assertEqual(snapshot["executions"], [])
        self.assertTrue(snapshot["current_run"]["can_accept"], "A plan does not require execution to finish")
        self.assertIsNone(self.service.store.reserve_execution(snapshot["room"]["id"], 1, "0" * 64))
        self.service._execution_ready = AsyncMock(return_value={"available": False})
        run = snapshot["current_run"]
        with self.assertRaises(RoomError) as caught:
            await self.service.start_run(snapshot["room"]["id"], "Implement", "all", "sandbox-build",
                run_id=run["id"], revision=run["revision"], digest=run["digest"], work_mode="build")
        self.assertEqual(caught.exception.code, "execution_unavailable")
        self.assertEqual((await self.service.read_room(snapshot["room"]["id"]))["current_run"]["id"], run["id"])

    async def test_fork_inherits_source_run_mode_and_retry_survives_later_mode_change(self):
        def build(number):
            self.provider.handler = team.TeamScenario(self,
                [team.task("main", "main.py"), team.task("checks", "checks.py")],
                {"main": "claude", "checks": "codex"},
                {"main": f"VALUE = {number}\n", "checks": f"EXPECTED = {number}\n"})
        build(1)
        created = await self.service.create_room("Implement", "all", "fork-source",
            mode="python_artifacts", collaboration=True, work_mode="build")
        await self.settle()
        room_id = created["room"]["id"]
        first = await self.service.read_room(room_id)
        self.provider.handler = self.plan
        run = first["current_run"]
        await self.service.start_run(room_id, "Discuss next steps", "all", "fork-source-plan",
            run_id=run["id"], revision=run["revision"], digest=run["digest"], work_mode="plan")
        await self.settle()
        planned = await self.service.read_room(room_id)
        run = planned["current_run"]
        binding = {key: run[key] for key in ("revision", "digest")}
        binding["run_id"] = run["id"]
        forked = await self.service.fork_room(room_id, "Discuss this copy", "all", "fork-plan", **binding)
        await self.settle()
        forked = await self.service.read_room(forked["room"]["id"])
        self.assertEqual(forked["current_run"]["work_mode"], "plan")
        self.assertEqual(forked["project"]["revision"], 1)
        self.assertEqual(forked["executions"], [])
        build(2)
        await self.service.start_run(room_id, "Implement next", "all", "later-build", work_mode="build", **binding)
        await self.settle()
        repeated = await self.service.fork_room(room_id, "Discuss this copy", "all", "fork-plan", **binding)
        self.assertEqual(repeated["room"]["id"], forked["room"]["id"])
        self.assertEqual(repeated["current_run"]["work_mode"], "plan")


class WorkModeFolderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = folder.FolderServiceTests.asyncSetUp
    asyncTearDown = folder.FolderServiceTests.asyncTearDown
    settle = folder.FolderServiceTests.settle

    async def test_plan_can_inspect_registered_project_without_writing_or_executing(self):
        def handler(agent, context):
            self.assertEqual(context["work_mode"], "plan")
            if not folder.read_result(context, "src/main.py"):
                return folder.response(context, request=folder.request("read", "src/main.py"))
            self.assertEqual(folder.read_result(context, "src/main.py")["content"], "VALUE = 1\n")
            return folder.response(context, message="The value can be raised after the design is approved.")
        self.provider.handler = handler
        registration = await self.service.register_project("Readonly plan", str(self.source), selection_kind="folder")
        created = await self.service.create_room("Inspect only", "claude", "inspect-plan", mode="project_folder",
            project_id=registration["project"]["id"], participants=["claude"], work_mode="plan")
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(snapshot["room"]["status"], "completed")
        self.assertEqual(snapshot["project"]["revision"], 1)
        self.assertEqual(snapshot["project"]["files"], [])
        self.assertEqual(snapshot["executions"], [])
        self.assertEqual((self.source / "src/main.py").read_text(), "VALUE = 1\n")

    async def test_one_person_plan_also_rejects_file_and_edit_before_commit(self):
        registration = await self.service.register_project("Readonly plan", str(self.source), selection_kind="folder")
        attempts = [
            {"files": [{"path": "new.md", "previous_sha256": None, "content": "not authorized"}]},
            {"edits": [{"path": "src/main.py", "base_sha256": "0" * 64, "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]},
        ]
        for number, attempt in enumerate(attempts):
            self.provider.handler = lambda agent, context, value=attempt: {**folder.response(context), **value}
            created = await self.service.create_room("Plan only", "claude", f"one-plan-{number}", mode="project_folder",
                project_id=registration["project"]["id"], participants=["claude"], work_mode="plan")
            await self.settle()
            snapshot = await self.service.read_room(created["room"]["id"])
            self.assertTrue(any(job["error"] == "plan_read_only" for job in snapshot["jobs"]))
            self.assertEqual(snapshot["project"]["revision"], 1)
            self.assertEqual(snapshot["project"]["files"], [])
        self.assertEqual((self.source / "src/main.py").read_text(), "VALUE = 1\n")

