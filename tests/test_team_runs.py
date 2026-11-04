"""Same-chat iterations over real storage/workspaces, with no model processes."""
import asyncio
import copy
from contextlib import closing
import json
import re
import sqlite3
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_service import RoomError
from local_agent_coordinator.room_store import RoomStore
from tests import test_team_service as fixtures
from tests import test_room_execution as execution_fixtures


class TeamRunTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TeamServiceTests.asyncSetUp
    asyncTearDown = fixtures.TeamServiceTests.asyncTearDown
    settle = execution_fixtures.RoomExecutionTests.settle

    def scenario(self, number, *, mode="python_artifacts"):
        paths = ("main.py","checks.py") if mode != "text_artifacts" else ("guide.md","chart.svg")
        contents = {"main":"VALUE = "+str(number)+"\n", "checks":"EXPECTED = "+str(number)+"\n"}
        if mode == "text_artifacts":
            contents = {"main":"# Guide "+str(number)+"\n", "checks":'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="green"/></svg>'}
        proposals = [fixtures.task("main",paths[0]),fixtures.task("checks",paths[1])]
        if mode == "text_artifacts":
            proposals = [{**item,"capability":"text"} for item in proposals]
        scenario = fixtures.TeamScenario(self,proposals,{"main":"claude","checks":"codex"},contents)
        self.provider.handler = scenario
        return scenario

    async def create(self, *, mode="python_artifacts"):
        self.scenario(1,mode=mode)
        created = await self.service.create_room("Initial user goal","all","initial",collaboration=True,mode=mode)
        await self.settle()
        return await self.service.read_room(created["room"]["id"])

    async def next_run(self, snapshot, number, *, retry=False, request_id=None):
        current = snapshot["current_run"]
        return await self.service.start_run(snapshot["room"]["id"],"Explicit user goal "+str(number),"all",
            request_id or "iteration-"+str(number),run_id=current["id"],revision=current["revision"],digest=current["digest"],retry=retry)

    async def test_accept_and_two_iterations_preserve_tasks_native_sessions_and_cumulative_budget(self):
        first = await self.create()
        room_id, run = first["room"]["id"], first["current_run"]
        self.assertTrue(run["can_accept"])
        native = {member["id"]:member["session_id"] for member in first["members"]}
        first_tasks = copy.deepcopy(first["team"]["tasks"])
        calls = len(self.provider.calls)
        accepted = await self.service.accept_run(room_id,"accept-first",run_id=run["id"],revision=run["revision"],digest=run["digest"])
        self.assertIsNotNone(accepted["current_run"]["accepted_at"])
        self.assertTrue(accepted["current_run"]["can_continue"])
        self.assertFalse(accepted["current_run"]["can_accept"])
        self.assertEqual(len(self.provider.calls),calls)
        self.scenario(2)
        await self.next_run(accepted,2)
        await self.settle()
        second = await self.service.read_room(room_id)
        self.assertEqual(second["current_run"]["number"],2)
        self.assertEqual(second["team"]["phase"],"ready_for_user")
        self.assertTrue(all(call["session_id"] == native[call["agent"]] for call in self.provider.calls[calls:]))
        self.assertEqual({member["id"]:member["session_id"] for member in second["members"]},native)
        history = await self.service.read_run(room_id,run["id"])
        self.assertEqual(history["team"]["tasks"],first_tasks)
        self.assertEqual(history["run"]["status"],"accepted")
        self.assertEqual(history["run"]["budget"]["used"],run["budget"]["used"])
        self.assertEqual(second["conversation_budget"]["used"],sum(item["budget"]["used"] for item in second["runs"]))
        self.assertGreater(second["conversation_budget"]["used"],second["budget"]["used"])
        self.assertEqual({task["id"] for task in second["team"]["tasks"]},{"main","checks"})
        replay = await self.service.accept_run(room_id,"accept-first",run_id=run["id"],revision=run["revision"],digest=run["digest"])
        self.assertEqual(replay["current_run"]["id"],second["current_run"]["id"])
        new_plan = next(call for call in self.provider.calls[calls:] if call["context"]["job"]["job_phase"] == "plan")
        self.assertEqual(new_plan["context"]["previous_deliveries"][0]["run_id"],run["id"])

    async def test_idempotent_continue_does_not_restart_work_and_stale_markers_are_rejected(self):
        first = await self.create()
        self.scenario(2)
        await self.next_run(first,2)
        await self.settle()
        calls = len(self.provider.calls)
        repeated = await self.next_run(first,2)
        self.assertEqual(repeated["current_run"]["number"],2)
        self.assertEqual(len(self.provider.calls),calls)
        with self.assertRaises(RoomError) as error:
            await self.next_run(first,3)
        self.assertEqual(error.exception.code,"team_run_conflict")
        with self.assertRaises(RoomError) as error:
            await self.service.accept_run(first["room"]["id"],"stale-accept",run_id=first["current_run"]["id"],
                revision=first["current_run"]["revision"],digest=first["current_run"]["digest"])
        self.assertEqual(error.exception.code,"team_run_conflict")

    async def test_mcp_source_cannot_accept_a_user_delivery(self):
        first = await self.create()
        with self.assertRaises(RoomError) as error:
            await self.service.accept_run(first["room"]["id"],"agent-accept",run_id=first["current_run"]["id"],
                revision=first["current_run"]["revision"],digest=first["current_run"]["digest"],source="mcp")
        self.assertEqual(error.exception.code,"user_acceptance_required")
        self.assertIsNone((await self.service.read_room(first["room"]["id"]))["current_run"]["accepted_at"])

    async def test_three_sandbox_iterations_keep_separate_execution_receipts(self):
        runner = execution_fixtures.FakeRunner()
        self.service._runner_instance = runner
        snapshot = await self.create(mode="python_sandbox")
        for number in (2,3):
            self.scenario(number)
            await self.next_run(snapshot,number)
            await self.settle()
            snapshot = await self.service.read_room(snapshot["room"]["id"])
            self.assertTrue(snapshot["current_run"]["can_continue"])
        self.assertEqual(len(runner.calls),3)
        executions = snapshot["executions"]
        self.assertEqual(len(executions),3)
        self.assertEqual({item["team_run_id"] for item in executions},{item["id"] for item in snapshot["runs"]})
        self.assertTrue(all(item["result"]["cleanup_confirmed"] for item in executions))

    async def test_document_svg_mode_can_preview_and_export_without_execution(self):
        snapshot = await self.create(mode="text_artifacts")
        room_id, revision = snapshot["room"]["id"],snapshot["project"]["revision"]
        preview = await self.service.preview_room(room_id,revision,"chart.svg")
        self.assertEqual(preview["content_type"],"image/svg+xml")
        exported = await self.service.export_room(room_id,revision)
        self.assertEqual(exported["files_count"],2)
        self.assertEqual(snapshot["executions"],[])
        self.assertTrue(snapshot["current_run"]["can_continue"])
        with self.assertRaises(RoomError):
            await self.service.preview_room(room_id,revision-1,"chart.svg")

    async def test_failed_run_needs_explicit_fresh_attempt_and_keeps_prior_evidence(self):
        self.provider.handler = lambda agent,context:{"adapter_failure":"subscription_limit_reached"}
        created = await self.service.create_room("First goal","all","failed",collaboration=True,mode="python_artifacts")
        await self.settle()
        first = await self.service.read_room(created["room"]["id"])
        with self.assertRaises(RoomError) as error:
            await self.next_run(first,2)
        self.assertEqual(error.exception.code,"team_retry_required")
        # The real fresh-session response must have a new identity. Represent
        # that provider boundary here without running a CLI.
        self.scenario(2)
        original = self.service.invoke
        async def fresh(*args,**kwargs):
            value = await original(*args,**kwargs)
            if kwargs.get("session_id") is None:
                value["session_id"] = str(uuid4())
            return value
        self.service.invoke = fresh
        await self.next_run(first,2,retry=True)
        await self.settle()
        second = await self.service.read_room(first["room"]["id"])
        self.assertEqual(second["current_run"]["number"],2)
        self.assertEqual(second["team"]["phase"],"ready_for_user")
        history = await self.service.read_run(first["room"]["id"],first["current_run"]["id"])
        self.assertEqual(history["team"]["stop_reason"],"subscription_limit_reached")


    async def test_legacy_conversation_continues_past_eight_runs_without_resetting_usage(self):
        snapshot = await self.create()
        for number in range(2, 11):
            self.scenario(number)
            await self.next_run(snapshot, number)
            await self.settle()
            snapshot = await self.service.read_room(snapshot["room"]["id"])
        self.assertEqual(len(snapshot["runs"]), 10)
        self.assertTrue(snapshot["current_run"]["can_continue"])
        count = len(self.provider.calls)
        self.assertEqual(snapshot["conversation_budget"]["used"], count)
        self.assertIsNone(snapshot["conversation_budget"]["run_limit"])
        self.assertIsNone(snapshot["conversation_budget"]["max_calls"])

    async def test_unclean_execution_refuses_explicit_retry_and_retains_the_receipt(self):
        runner = execution_fixtures.FakeRunner()
        runner.result.update(status="unknown", exit_code=None, cleanup_confirmed=False)
        self.service._runner_instance = runner
        first = await self.create(mode="python_sandbox")
        count = len(self.provider.calls)
        with self.assertRaises(RoomError) as error:
            await self.next_run(first, 2, retry=True)
        self.assertEqual(error.exception.code, "team_cleanup_unconfirmed")
        current = await self.service.read_room(first["room"]["id"])
        self.assertEqual(current["current_run"]["id"], first["current_run"]["id"])
        self.assertEqual(current["executions"], first["executions"])
        self.assertEqual(len(self.provider.calls), count)

    async def test_fresh_attempt_rejects_a_previous_native_identity(self):
        first = await self.create()
        await self.service.cancel_room(first["room"]["id"])
        stopped = await self.service.read_room(first["room"]["id"])
        self.scenario(2)
        await self.next_run(stopped, 2, retry=True)
        await self.settle()
        second = await self.service.read_room(first["room"]["id"])
        self.assertEqual(second["team"]["phase"], "unknown")
        self.assertIn(second["team"]["stop_reason"], {"session_identity_changed", "session_identity_unknown"})
        self.assertEqual(len(second["runs"]), 2)
        self.assertEqual(second["team"]["tasks"], [])

    async def test_competing_user_goals_create_only_one_next_run(self):
        first = await self.create()
        self.scenario(2)
        results = await asyncio.gather(self.next_run(first, 2, request_id="concurrent-a"),
                                       self.next_run(first, 2, request_id="concurrent-b"),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        errors = [value for value in results if isinstance(value, RoomError)]
        self.assertEqual([error.code for error in errors], ["team_run_conflict"])
        await self.settle()
        second = await self.service.read_room(first["room"]["id"])
        self.assertEqual(len(second["runs"]), 2)
        self.assertEqual(sum(event["kind"] == "run_started" for event in second["events"]), 1)

    async def test_new_round_cannot_transfer_an_existing_file_owner(self):
        first = await self.create()
        scenario = self.scenario(2)
        scenario.owners = {"main":"codex", "checks":"claude"}
        await self.next_run(first, 2)
        await self.settle()
        second = await self.service.read_room(first["room"]["id"])
        self.assertEqual(second["team"]["stop_reason"], "team_scope_conflict")
        self.assertEqual(second["project"]["files"], first["project"]["files"])
        self.assertEqual(second["project"]["revision"], first["project"]["revision"])
        previous = await self.service.read_run(first["room"]["id"], first["current_run"]["id"])
        self.assertEqual(previous["team"]["tasks"], first["team"]["tasks"])

    async def test_existing_single_run_database_migrates_tasks_jobs_sessions_and_execution(self):
        self.service._runner_instance = execution_fixtures.FakeRunner()
        before = await self.create(mode="python_sandbox")
        # Recreate the previous released schema in a separate temporary DB.
        # This exercises migration with populated FK relationships, not an
        # artificial empty table or a changed production database.
        old_path = self.root / "previous-version.sqlite3"
        with closing(sqlite3.connect(self.service.store.path)) as current, closing(sqlite3.connect(old_path)) as old:
            tables = current.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            for name, sql in tables:
                if name in {"team_runs", "native_session_history"}:
                    continue
                sql = re.sub(r'CREATE TABLE "?'+name+r'"?', "CREATE TABLE "+name, sql, count=1)
                columns = [row[1] for row in current.execute("PRAGMA table_info("+name+")")]
                if name in {"shared_tasks", "team_jobs"}:
                    sql = sql.replace("run_id TEXT NOT NULL REFERENCES team_runs(id), ", "")
                    sql = sql.replace("PRIMARY KEY(room_id,run_id,id)", "PRIMARY KEY(room_id,id)")
                    sql = sql.replace("FOREIGN KEY(room_id,run_id,task_id) REFERENCES shared_tasks(room_id,run_id,id)",
                                      "FOREIGN KEY(room_id,task_id) REFERENCES shared_tasks(room_id,id)")
                    sql = sql.replace('FOREIGN KEY(room_id,run_id,task_id) REFERENCES "shared_tasks"(room_id,run_id,id)',
                                      "FOREIGN KEY(room_id,task_id) REFERENCES shared_tasks(room_id,id)")
                    columns.remove("run_id")
                elif name == "executions":
                    sql = sql.replace(" team_run_id TEXT REFERENCES team_runs(id),", "")
                    sql = sql.replace("UNIQUE(room_id,team_run_id,revision)", "UNIQUE(room_id,revision)")
                    columns.remove("team_run_id")
                old.execute(sql)
                selected = ",".join(columns)
                rows = current.execute("SELECT "+selected+" FROM "+name).fetchall()
                old.executemany("INSERT INTO "+name+"("+selected+") VALUES ("+",".join("?" for _ in columns)+")", rows)
            old.commit()
        migrated = RoomStore(old_path)
        try:
            after = migrated.snapshot(before["room"]["id"])
            self.assertEqual([{key:value for key,value in task.items() if key != "run_id"} for task in after["team"]["tasks"]],
                             [{key:value for key,value in task.items() if key != "run_id"} for task in before["team"]["tasks"]])
            self.assertEqual(after["jobs"], before["jobs"])
            self.assertEqual(after["members"], before["members"])
            self.assertEqual(after["budget"], before["budget"])
            self.assertEqual(after["current_run"]["budget"]["used"], before["budget"]["used"])
            self.assertEqual(after["executions"][0]["team_run_id"], after["current_run"]["id"])
            self.assertEqual(after["executions"][0]["result"], before["executions"][0]["result"])
            with migrated._connection() as db:
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM native_session_history").fetchone()[0], 2)
        finally:
            migrated.close()

if __name__ == "__main__":
    unittest.main()
