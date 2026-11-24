"""A team edit that grows a project past the old 64 KiB cap commits first time, through real storage and HTTP."""
import asyncio
import unittest
from uuid import uuid4

from tests import test_team_runs as runs
from tests import test_team_runs_http as http
from tests import test_team_service as fixtures
from tests.test_project_edits import edit


class GrowthScenario:
    def __init__(self, test, *, hold_peer=False):
        self.test, self.hold_peer = test, hold_peer
        self.calls = 0
        self.entered = asyncio.Event()
        self.main = "VALUE = 1\n#" + "x" * (63894 - 12) + "\n"
        proposals = [fixtures.task("main", "main.py"), fixtures.task("checks", "checks.py")]
        owners = {"main": "claude", "checks": "codex"}
        self.first = fixtures.TeamScenario(test, proposals, owners,
            {"main": self.main, "checks": "EXPECTED = 1\n"})
        self.second = fixtures.TeamScenario(test, proposals, owners,
            {"main": self.main.replace("1", "2" + " " * 1714, 1), "checks": "EXPECTED = 2\n"}, work=self.work)

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
        path = context["task"]["write_paths"][0]
        source = next(file for file in context["project"]["files"] if file["path"] == path)
        replacement = "2"
        if agent == "claude":
            self.calls += 1
            self.test.assertNotIn("capacity_observation", context)
            if self.hold_peer:
                await self.entered.wait()
            replacement += " " * 1714
        return {**fixtures.reply(context, commands=[{"type": "complete", "task_id": context["task"]["id"],
            "result": "Changed the owned value; await independent review."}]), "edits": [edit(source, "1", replacement)]}


class ProjectGrowthTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runs.TeamRunTests.asyncSetUp
    asyncTearDown = runs.TeamRunTests.asyncTearDown
    settle = runs.TeamRunTests.settle
    next_run = runs.TeamRunTests.next_run

    async def create(self, **options):
        scenario = GrowthScenario(self, **options)
        self.provider.handler = scenario
        created = await self.service.create_room("Initial size fixture", "all", "initial", collaboration=True, mode="python_artifacts")
        await self.settle()
        source = await self.service.read_room(created["room"]["id"])
        self.assertEqual(source["team"]["phase"], "ready_for_user")
        self.assertEqual(sum(len(file["content"].encode()) for file in source["project"]["files"]), 63907)
        return source, scenario

    async def test_edit_past_the_old_64kib_cap_commits_first_time_beside_a_running_peer(self):
        # Catches the old cap refusing the edit (project_limit_exceeded) and spending a second "capacity correction" call.
        source, scenario = await self.create(hold_peer=True)
        await self.next_run(source, 2)
        await self.settle()
        final = await self.service.read_room(source["room"]["id"])
        self.assertEqual(final["team"]["phase"], "ready_for_user")
        self.assertEqual(scenario.calls, 1)
        self.assertEqual(sum(len(file["content"].encode()) for file in final["project"]["files"]), 65621)
        self.assertFalse(any(event["kind"].startswith("capacity_correction") for event in final["events"]))
        self.assertFalse(any(job.get("error") == "project_limit_exceeded" for job in final["jobs"]))
        self.assertEqual(len(scenario.second.reviews), 2)
        task = next(task for task in final["team"]["tasks"] if task["id"] == "main")
        self.assertEqual((task["capacity_attempts"], task["status"]), (0, "accepted"))

    async def test_secret_in_a_large_edit_is_still_refused(self):
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
        self.assertEqual(final["project"]["files"], source["project"]["files"])

    async def test_large_edit_still_cannot_hide_a_same_file_aba(self):
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
        self.assertEqual(final["team"]["stop_reason"], "project_file_conflict")
        self.assertEqual([file["content"] for file in final["project"]["files"]],
                         [file["content"] for file in source["project"]["files"]])


class ProjectGrowthHTTPTests(unittest.TestCase):
    setUp = http.TeamRunHTTPTests.setUp
    tearDown = http.TeamRunHTTPTests.tearDown
    request = http.TeamRunHTTPTests.request
    ready = http.TeamRunHTTPTests.ready

    def test_public_iteration_grows_project_past_the_old_cap_and_reviews_it(self):
        # Catches the public run path still refusing a project that grows past 64 KiB.
        scenario = GrowthScenario(self)
        self.provider.handler = scenario
        code, source = self.request("POST", "/api/rooms", {"goal": "Initial size fixture", "target": "all",
            "request_id": "http-size", "collaboration": True, "mode": "python_artifacts"})
        self.assertEqual(code, 200)
        source = self.ready(source["room"]["id"])
        run = source["current_run"]
        body = {
            "text": "Change owned values", "target": "all", "request_id": "size-run2",
            "run_id": run["id"], "revision": run["revision"], "digest": run["digest"], "retry": False}
        path = "/api/rooms/" + source["room"]["id"] + "/runs"
        code, _ = self.request("POST", path, body)
        self.assertEqual(code, 200)
        final = self.ready(source["room"]["id"])
        self.assertEqual(scenario.calls, 1)
        self.assertEqual(final["current_run"]["number"], 2)
        self.assertEqual(final["team"]["phase"], "ready_for_user")
        self.assertEqual(len(scenario.second.reviews), 2)
        self.assertEqual(sum(len(file["content"].encode()) for file in final["project"]["files"]), 65621)
        calls = len(self.provider.calls)
        code, repeated = self.request("POST", path, body)
        self.assertEqual(code, 200)
        self.assertEqual(repeated["current_run"]["id"], final["current_run"]["id"])
        self.assertEqual(len(self.provider.calls), calls)
