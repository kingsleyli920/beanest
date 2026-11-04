"""One task-local byte correction through real storage and public HTTP."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_team_runs as runs
from tests import test_team_runs_http as http
from tests import test_team_service as fixtures
from tests.test_project_edits import edit


class CapacityScenario:
    def __init__(self, test, *, repeat=False, hold_peer=False):
        self.test, self.repeat, self.hold_peer = test, repeat, hold_peer
        self.calls, self.observations = 0, []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.main = "VALUE = 1\n#" + "x" * (63894 - 12) + "\n"
        proposals = [fixtures.task("main", "main.py"), fixtures.task("checks", "checks.py")]
        owners = {"main": "claude", "checks": "codex"}
        self.first = fixtures.TeamScenario(test, proposals, owners,
            {"main": self.main, "checks": "EXPECTED = 1\n"})
        self.second = fixtures.TeamScenario(test, proposals, owners,
            {"main": self.main.replace("1", "2", 1), "checks": "EXPECTED = 2\n"}, work=self.work)

    async def __call__(self, agent, context):
        if context["job"]["run_number"] == 1:
            if context["job"]["job_phase"] == "work":
                identifier = context["task"]["id"]
                path = context["task"]["write_paths"][0]
                return fixtures.reply(context, commands=[{"type": "complete", "task_id": identifier,
                    "result": "Delivered exact owned file."}],
                    files=[fixtures.changed_file(context, path, self.first.contents[identifier])])
            return await self.first(agent, context)
        return await self.second(agent, context)

    async def work(self, agent, context, attempt):
        if agent == "codex" and self.hold_peer:
            self.entered.set()
            await self.release.wait()
        path = context["task"]["write_paths"][0]
        source = next(file for file in context["project"]["files"] if file["path"] == path)
        replacement = "2"
        if agent == "claude":
            self.calls += 1
            if self.hold_peer:
                await self.entered.wait()
            if self.calls == 1 or self.repeat:
                replacement += " " * 1714
            else:
                self.observations.append(copy.deepcopy(context["capacity_observation"]))
                self.release.set()
        return {**fixtures.reply(context, commands=[{"type": "complete", "task_id": context["task"]["id"],
            "result": "Changed the owned value; await independent review."}]), "edits": [edit(source, "1", replacement)]}


class CapacityCorrectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runs.TeamRunTests.asyncSetUp
    asyncTearDown = runs.TeamRunTests.asyncTearDown
    settle = runs.TeamRunTests.settle
    next_run = runs.TeamRunTests.next_run

    async def create(self, **options):
        scenario = CapacityScenario(self, **options)
        self.provider.handler = scenario
        created = await self.service.create_room("Initial capacity fixture", "all", "initial", collaboration=True, mode="python_artifacts")
        await self.settle()
        source = await self.service.read_room(created["room"]["id"])
        self.assertEqual(source["team"]["phase"], "ready_for_user")
        self.assertEqual(sum(len(file["content"].encode()) for file in source["project"]["files"]), 63907)
        return source, scenario

    async def test_85_byte_overage_gets_one_exact_observation_without_cancelling_running_peer(self):
        source, scenario = await self.create(hold_peer=True)
        recorded = []
        original_finish = self.service._finish
        def finish(job, result):
            original_finish(job, result)
            if any(task.get("capacity_attempts") for task in self.service.store.snapshot(job["room_id"])["team"]["tasks"]):
                recorded.append((copy.deepcopy(job), copy.deepcopy(result)))
        with patch.object(self.service, "_finish", side_effect=finish):
            await self.next_run(source, 2)
            await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["team"]["phase"], "ready_for_user")
        self.assertEqual(scenario.calls, 2)
        observation = scenario.observations[0]
        self.assertEqual({key: observation[key] for key in ("current_bytes", "proposed_bytes", "byte_limit", "excess_bytes")},
                         {"current_bytes": 63907, "proposed_bytes": 65621, "byte_limit": 65536, "excess_bytes": 85})
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "capacity_correction_queued"]), 1)
        self.assertFalse(any(event["kind"].startswith("format_correction") for event in final["events"]))
        self.assertEqual(len(scenario.second.reviews), 2)
        failed = next(job for job in final["jobs"] if job["id"] == observation["job_id"])
        self.assertEqual((failed["status"], failed["error"]), ("failed", "project_limit_exceeded"))
        task = next(task for task in final["team"]["tasks"] if task["id"] == "main")
        self.assertEqual((task["capacity_attempts"], task["result_version"]), (1, 1))
        with self.service.store._connection() as db:
            rejected = json.loads(db.execute("SELECT capacity_rejection_json FROM shared_tasks WHERE run_id=? AND id='main'",
                (final["current_run"]["id"],)).fetchone()[0])
        self.assertIn(" " * 1714, rejected["proposal"]["files"][0]["content"])
        before = self.service._project(source["room"]["id"]).snapshot()
        count = len(final["jobs"])
        for job, result in recorded:
            original_finish(job, result)
        repeated = await self.service.read_room(source["room"]["id"])
        self.assertEqual(len(repeated["jobs"]), count)
        self.assertEqual(self.service._project(source["room"]["id"]).snapshot(), before)

    async def test_second_overage_stops_without_resetting_attempt_or_requeueing(self):
        source, scenario = await self.create(repeat=True)
        await self.next_run(source, 2)
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["team"]["stop_reason"], "project_limit_exceeded")
        self.assertEqual(scenario.calls, 2)
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "capacity_correction_queued"]), 1)
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in final["jobs"]))

    async def test_historical_default_budget_does_not_block_capacity_correction(self):
        source, scenario = await self.create()
        self.service.store.team_max_calls = 6
        await self.next_run(source, 2)
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(scenario.calls, 2)
        self.assertEqual(sum(event["kind"] == "capacity_correction_queued" for event in final["events"]), 1)
        self.assertEqual(final["team"]["phase"], "ready_for_user")
        self.assertIsNone(final["budget"]["limit"])

    async def test_cancelled_capacity_call_cannot_publish_or_restart(self):
        source, scenario = await self.create()
        started = asyncio.Event()
        original = scenario.work
        async def hold(agent, context, attempt):
            if agent == "claude" and context.get("capacity_observation"):
                started.set()
                await asyncio.Event().wait()
            return await original(agent, context, attempt)
        scenario.second.work = hold
        await self.next_run(source, 2)
        await asyncio.wait_for(started.wait(), 3)
        cancelled = await self.service.cancel_room(source["room"]["id"])
        calls = len(self.provider.calls)
        self.service._wake(source["room"]["id"])
        await self.settle()
        self.assertEqual(len(self.provider.calls), calls)
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in cancelled["jobs"]))

    async def test_secret_overage_is_never_capacity_corrected(self):
        source, scenario = await self.create()
        async def secret(agent, context, attempt):
            if agent == "claude":
                original = next(file for file in context["project"]["files"] if file["path"] == "main.py")
                replacement = "1\n# " + "sk" + "-" + "abcdefghijklmnop" + " " * 1800
                return {**fixtures.reply(context, commands=[{"type": "complete", "task_id": "main", "result": "Rejected fixture"}]),
                        "edits": [edit(original, "1", replacement)]}
            await asyncio.Event().wait()
        scenario.second.work = secret
        await self.next_run(source, 2)
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["team"]["stop_reason"], "artifact_sensitive_content")
        self.assertFalse(any(event["kind"] == "capacity_correction_queued" for event in final["events"]))
        self.assertEqual(final["project"]["files"], source["project"]["files"])

    async def test_current_capacity_includes_peers_already_committed_disjoint_file(self):
        source, scenario = await self.create()
        original = scenario.work
        scenario.second.contents["checks"] = "EXPECTED = 2" + " " * 10 + "\n"
        async def disjoint(agent, context, attempt):
            if agent == "codex":
                result = await original(agent, context, attempt)
                result["edits"][0]["new_text"] = "2" + " " * 10
                return result
            while (scenario.calls == 0 and self.service._project(source["room"]["id"]).snapshot()["revision"]
                   == context["proposal_base_revision"]):
                await asyncio.sleep(0)
            return await original(agent, context, attempt)
        scenario.second.work = disjoint
        await self.next_run(source, 2)
        await self.settle()
        observation = scenario.observations[0]
        self.assertEqual((observation["current_bytes"], observation["proposed_bytes"], observation["excess_bytes"]),
                         (63917, 65631, 95))
        self.assertEqual((await self.service.read_room(source["room"]["id"]))["team"]["phase"], "ready_for_user")

    async def test_limit_rejected_before_workspace_cas_cannot_hide_same_file_aba(self):
        source, scenario = await self.create()
        original = scenario.work
        async def stale(agent, context, attempt):
            if agent == "codex":
                await asyncio.Event().wait()
            project = self.service._project(source["room"]["id"])
            old = next(file for file in project.snapshot()["files"] if file["path"] == "main.py")
            changed = project.apply("claude", str(uuid4()), project.snapshot()["revision"],
                [{"path": "main.py", "previous_sha256": old["sha256"], "content": old["content"].replace("1", "8", 1)}])
            changed_file = next(file for file in changed["files"] if file["path"] == "main.py")
            project.apply("claude", str(uuid4()), changed["revision"],
                [{"path": "main.py", "previous_sha256": changed_file["sha256"], "content": old["content"]}])
            return await original(agent, context, attempt)
        scenario.second.work = stale
        await self.next_run(source, 2)
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(scenario.calls, 1)
        self.assertFalse(any(event["kind"] == "capacity_correction_queued" for event in final["events"]))
        self.assertEqual(final["project"]["files"], source["project"]["files"])


class CapacityCorrectionHTTPTests(unittest.TestCase):
    setUp = http.TeamRunHTTPTests.setUp
    tearDown = http.TeamRunHTTPTests.tearDown
    request = http.TeamRunHTTPTests.request
    ready = http.TeamRunHTTPTests.ready

    def test_public_iteration_automatically_corrects_85_byte_overage_and_reviews_new_result(self):
        scenario = CapacityScenario(self)
        self.provider.handler = scenario
        code, source = self.request("POST", "/api/rooms", {"goal": "Initial size fixture", "target": "all",
            "request_id": "http-size", "collaboration": True, "mode": "python_artifacts"})
        self.assertEqual(code, 200)
        source = self.ready(source["room"]["id"])
        run = source["current_run"]
        body = {
            "text": "Change owned values with bounded capacity correction", "target": "all", "request_id": "size-run2",
            "run_id": run["id"], "revision": run["revision"], "digest": run["digest"], "retry": False}
        path = "/api/rooms/" + source["room"]["id"] + "/runs"
        code, _ = self.request("POST", path, body)
        self.assertEqual(code, 200)
        final = self.ready(source["room"]["id"])
        self.assertEqual(scenario.calls, 2)
        self.assertEqual(final["current_run"]["number"], 2)
        self.assertEqual(scenario.observations[0]["excess_bytes"], 85)
        self.assertEqual(len(scenario.second.reviews), 2)
        self.assertEqual(sum(len(file["content"].encode()) for file in final["project"]["files"]), 63907)
        calls = len(self.provider.calls)
        code, repeated = self.request("POST", path, body)
        self.assertEqual(code, 200)
        self.assertEqual(repeated["current_run"]["id"], final["current_run"]["id"])
        self.assertEqual(len(self.provider.calls), calls)
