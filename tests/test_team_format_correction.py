"""One new format-correction call; rejected proposals never commit files."""
import copy
import unittest

from local_agent_coordinator.room_service import RoomError, RoomService
from tests import test_team_runs as runs
from tests import test_team_service as fixtures
from tests import test_room_execution as execution


class FormatCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TeamServiceTests.asyncSetUp
    asyncTearDown = fixtures.TeamServiceTests.asyncTearDown
    settle = execution.RoomExecutionTests.settle
    scenario = runs.TeamRunTests.scenario
    next_run = runs.TeamRunTests.next_run

    async def create(self, mode="python_artifacts"):
        created = await self.service.create_room("Deliver two small modules", "all", "create", mode=mode,
                                                 collaboration=True)
        await self.settle()
        return await self.service.read_room(created["room"]["id"])

    async def test_null_plan_id_gets_one_counted_same_session_correction_without_rejected_files(self):
        valid = self.scenario(1)
        async def handler(agent, context):
            if agent == "claude" and context["job"]["job_phase"] == "plan" and not context["job"]["format_attempt"]:
                invalid = copy.deepcopy(valid.proposals)
                invalid[0]["id"] = None
                return fixtures.reply(context, commands=invalid,
                    files=[fixtures.changed_file(context, "main.py", "REJECTED = True\n")])
            return await valid(agent, context)
        self.provider.handler = handler
        snapshot = await self.create()
        self.assertTrue(snapshot["current_run"]["can_accept"])
        failed = next(job for job in snapshot["jobs"] if job["status"] == "failed")
        self.assertEqual(failed["error"], "invalid_team_task")
        correction = next(call for call in self.provider.calls if call["context"]["job"]["correction_of"] == failed["id"])
        self.assertEqual(correction["context"]["job"]["format_attempt"], 1)
        self.assertIsNotNone(correction["session_id"])
        self.assertEqual(snapshot["budget"]["used"], len(self.provider.calls))
        self.assertEqual(sum(event["kind"] == "format_correction_queued" for event in snapshot["events"]), 1)
        self.assertFalse(any(event["kind"] == "artifact_pending" and event["job_id"] == failed["id"]
                             for event in snapshot["events"]))
        project = self.service._project(snapshot["room"]["id"])
        for revision in range(snapshot["project"]["revision"] + 1):
            self.assertFalse(any("REJECTED" in file["content"] for file in project.snapshot(revision=revision)["files"]))

    async def test_work_correction_retains_task_run_result_version_and_discards_original_files(self):
        valid = self.scenario(1)
        async def handler(agent, context):
            job = context["job"]
            if agent == "claude" and job["job_phase"] == "work" and not job["format_attempt"]:
                return fixtures.reply(context, commands=[{"type":"complete","task_id":None,"result":"Invalid reference"}],
                    files=[fixtures.changed_file(context, "main.py", "REJECTED = True\n")])
            return await valid(agent, context)
        self.provider.handler = handler
        snapshot = await self.create()
        self.assertTrue(snapshot["current_run"]["can_accept"])
        work = [call for call in self.provider.calls if call["agent"] == "claude" and call["context"]["job"]["job_phase"] == "work"]
        self.assertEqual(len(work), 2)
        original, corrected = (call["context"]["job"] for call in work)
        for key in ("task_id", "run_id", "result_version", "revision"):
            self.assertEqual(original[key], corrected[key])
        self.assertEqual(corrected["correction_of"], original["id"])
        self.assertEqual(next(task for task in snapshot["team"]["tasks"] if task["id"] == "main")["result_version"], 1)
        self.assertFalse(any(event["kind"] == "artifact_pending" and event["job_id"] == original["id"]
                             for event in snapshot["events"]))

    async def test_second_invalid_output_stops_without_a_third_attempt(self):
        valid = self.scenario(1)
        async def handler(agent, context):
            if agent == "claude" and context["job"]["job_phase"] == "plan":
                return fixtures.reply(context, commands=[{**valid.proposals[0], "id":None}])
            return await valid(agent, context)
        self.provider.handler = handler
        snapshot = await self.create()
        self.assertEqual(snapshot["team"]["stop_reason"], "invalid_team_task")
        self.assertEqual(len([call for call in self.provider.calls if call["agent"] == "claude"]), 2)
        self.assertEqual(len([job for job in snapshot["jobs"] if job["status"] == "failed"]), 2)
        self.assertEqual(snapshot["project"]["revision"], 0)

    async def test_missing_declared_file_gets_one_complete_new_proposal_then_independent_review(self):
        valid = self.scenario(1)
        valid.proposals[0]["write_paths"].append("extra.py")
        async def handler(agent, context):
            value = await valid(agent, context)
            if context["job"]["job_phase"] == "work" and agent == "claude" and context["job"]["format_attempt"]:
                value["files"].append(fixtures.changed_file(context, "extra.py", "EXTRA = True\n"))
            return value
        # The generic review fixture expects one common content value per task;
        # use our own exact-version assertion for this multi-file result.
        async def reviewed(agent, context):
            if context["job"]["job_phase"] == "review" and context["task"]["id"] == "main":
                self.assertNotEqual(agent, context["task"]["owner"])
                self.assertEqual({item["path"] for item in context["task"]["result_files"]}, {"main.py", "extra.py"})
                return fixtures.reply(context, commands=[{"type":"review", "task_id":"main", "verdict":"accept",
                    "reason":"Read both exact delivered files."}])
            return await handler(agent, context)
        self.provider.handler = reviewed
        snapshot = await self.create()
        self.assertTrue(snapshot["current_run"]["can_accept"])
        failed = [job for job in snapshot["jobs"] if job["error"] == "team_result_missing"]
        self.assertEqual(len(failed), 1)
        calls = [call for call in self.provider.calls if call["agent"] == "claude" and call["context"]["job"]["job_phase"] == "work"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["context"]["job"]["correction_of"], failed[0]["id"])
        self.assertEqual(calls[1]["context"]["task"]["write_paths"], ["main.py", "extra.py"])
        self.assertFalse(any(event["kind"] == "artifact_pending" and event["job_id"] == failed[0]["id"] for event in snapshot["events"]))

    async def test_second_missing_file_stops_and_preserves_rejected_attempts(self):
        valid = self.scenario(1)
        valid.proposals[0]["write_paths"].append("extra.py")
        self.provider.handler = valid
        snapshot = await self.create()
        self.assertEqual(snapshot["team"]["stop_reason"], "team_result_missing")
        self.assertEqual(len([job for job in snapshot["jobs"] if job["error"] == "team_result_missing"]), 2)
        self.assertEqual(len([call for call in self.provider.calls if call["agent"] == "claude" and call["context"]["job"]["job_phase"] == "work"]), 2)
        self.assertFalse(any(file["path"] == "main.py" for file in snapshot["project"]["files"]))

    async def test_observed_tool_never_gets_a_format_correction_or_artifact_commit(self):
        self.scenario(1)
        original = self.service.invoke
        async def observed(*args, **kwargs):
            result = await original(*args, **kwargs)
            result["events"] = [{"type":"tool_observed"}]
            return result
        self.service.invoke = observed
        snapshot = await self.create()
        self.assertEqual(snapshot["team"]["stop_reason"], "unexpected_tool_event")
        self.assertFalse(any(event["kind"] == "format_correction_queued" for event in snapshot["events"]))
        self.assertEqual(snapshot["project"]["revision"], 0)
        self.assertTrue(all(call["context"]["job"]["format_attempt"] == 0 for call in self.provider.calls))

    async def test_obsolete_call_cap_does_not_block_one_bounded_format_correction(self):
        self.service.store.team_max_calls = 2
        valid = self.scenario(1)
        async def handler(agent, context):
            if agent == "claude":
                return fixtures.reply(context, commands=[{**valid.proposals[0], "id":None}])
            return await valid(agent, context)
        self.provider.handler = handler
        snapshot = await self.create()
        self.assertGreater(len(self.provider.calls), 2)
        self.assertEqual(sum(event["kind"] == "format_correction_queued" for event in snapshot["events"]), 1)
        self.assertEqual(snapshot["team"]["stop_reason"], "invalid_team_task")

    async def test_restarted_started_execution_without_receipt_refuses_new_attempt(self):
        self.service._runner_instance = execution.FakeRunner()
        self.scenario(1)
        before = await self.create(mode="python_sandbox")
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE executions SET status='working',result_json=NULL WHERE room_id=?", (before["room"]["id"],))
            db.execute("UPDATE rooms SET status='running' WHERE id=?", (before["room"]["id"],))
        await self.service.shutdown()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   invoke=self.provider, runner=execution.FakeRunner())
        recovered = await self.service.read_room(before["room"]["id"])
        count = len(self.provider.calls)
        self.assertFalse(recovered["current_run"]["can_retry"])
        with self.assertRaises(RoomError) as error:
            await self.next_run(recovered, 2, retry=True)
        self.assertEqual(error.exception.code, "team_cleanup_unconfirmed")
        after = await self.service.read_room(before["room"]["id"])
        self.assertEqual(after["current_run"]["id"], before["current_run"]["id"])
        self.assertIsNone(after["executions"][0]["result"])
        self.assertEqual(len(self.provider.calls), count)
        self.assertEqual(after["jobs"], recovered["jobs"])

    async def test_durable_never_started_reservation_does_not_claim_unknown_resources(self):
        self.service._runner_instance = execution.FakeRunner()
        self.scenario(1)
        before = await self.create(mode="python_sandbox")
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE executions SET status='queued',result_json=NULL WHERE room_id=?", (before["room"]["id"],))
            db.execute("DELETE FROM events WHERE room_id=? AND json_extract(value_json,'$.kind') IN ('execution_started','execution_finished')",
                       (before["room"]["id"],))
        await self.service.shutdown()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=self.provider)
        recovered = await self.service.read_room(before["room"]["id"])
        self.assertFalse(self.service._team_cleanup_unknown(recovered))
        stopped = await self.service.cancel_room(before["room"]["id"])
        self.assertTrue(stopped["cancellation_confirmed"])
        self.assertTrue(stopped["current_run"]["can_retry"])


if __name__ == "__main__":
    unittest.main()
