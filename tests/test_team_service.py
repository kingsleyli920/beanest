"""Team integration over real SQLite/workspaces; only provider transport is fake.

No model CLI, generated-code execution, Docker, or production runtime is used.
"""
import asyncio
from collections import Counter
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.room_service import RoomService


def task(identifier, path, *, dependencies=()):
    return {"type": "propose", "id": identifier, "title": "Deliver " + identifier,
            "description": "Produce the independently owned " + identifier + " deliverable.",
            "acceptance": "The declared file contains its exact expected value and a peer checks it.",
            "dependencies": list(dependencies), "write_paths": [path], "capability": "python"}


def reply(context, *, commands=(), files=(), message="Concrete task result."):
    result = {"message": message, "state": "done", "commands": list(commands)}
    if "project" in context:
        result.update(base_revision=context["proposal_base_revision"], files=list(files))
    return result


def changed_file(context, path, content):
    previous = next((item for item in context["project"]["files"] if item["path"] == path), None)
    return {"path": path, "content": content, "previous_sha256": previous["sha256"] if previous else None}


class FakeTeamProvider:
    def __init__(self):
        self.handler = None
        self.calls = []
        self.active = Counter()
        self.maximum = Counter()
        self.assertion_failures = []

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        call = {"agent": agent, "context": context, "cwd": cwd, "session_id": session_id}
        self.calls.append(call)
        self.active[agent] += 1
        self.maximum[agent] = max(self.maximum[agent], self.active[agent])
        try:
            # Both initial planning calls observe their own real dispatch snapshot.
            await asyncio.sleep(0)
            value = self.handler(agent, context)
            if inspect.isawaitable(value):
                value = await value
            identity = session_id or str(uuid5(NAMESPACE_URL, str(cwd)))
            if "adapter_failure" in value:
                return {"session_id": identity, "status": "failed", "events": [],
                        "failure_code": value["adapter_failure"], "usage": None, "text": ""}
            return {"session_id": identity, "status": "completed", "events": [], "usage": None,
                    "text": json.dumps(value)}
        except AssertionError as error:
            self.assertion_failures.append(str(error))
            raise
        finally:
            self.active[agent] -= 1


class TeamScenario:
    """Respond from task/phase state, rather than a fixed number of round trips."""
    def __init__(self, test, proposals, owners, contents, *, work=None):
        self.test, self.proposals, self.owners, self.contents = test, proposals, owners, contents
        self.work = work
        self.work_calls = Counter()
        self.reviews = []

    async def __call__(self, agent, context):
        phase, board = context["job"]["job_phase"], context["team"]
        if phase in {"plan", "coordinate"}:
            commands = []
            visible = {item["id"]: item for item in board["tasks"]}
            if not visible and agent == board["plan_author"]:
                commands.extend(self.proposals)
                visible = {item["id"]: {"owner": None} for item in self.proposals}
            for identifier, owner in self.owners.items():
                if owner == agent and identifier in visible and visible[identifier]["owner"] is None:
                    commands.append({"type": "claim", "task_id": identifier})
            if visible:
                commands.append({"type": "plan_ready"})
            return reply(context, commands=commands, message="I claim my own concrete parts of the shared list.")
        bound = context["task"]
        identifier = bound["id"]
        if phase == "review":
            self.test.assertNotEqual(bound["owner"], agent)
            self.test.assertEqual(context["project"]["revision"], bound["result_revision"])
            files = {item["path"]: item for item in context["project"]["files"]}
            for delivered in bound["result_files"]:
                self.test.assertEqual(files[delivered["path"]]["sha256"], delivered["sha256"])
                content = files[delivered["path"]]["content"]
                if isinstance(content, dict):
                    # Long values are included once in the same prompt. Check
                    # and follow that contract rather than assuming inline text.
                    self.test.assertEqual(set(content), {"text_ref", "sha256", "characters"})
                    digest = content["sha256"]
                    self.test.assertEqual(content["text_ref"], f"full_texts.{digest}.text")
                    text = context["full_texts"][digest]["text"]
                    self.test.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
                    self.test.assertEqual(len(text), content["characters"])
                    content = text
                self.test.assertEqual(content, self.contents[identifier])
            self.reviews.append((agent, identifier, bound["result_version"]))
            return reply(context, commands=[{"type": "review", "task_id": identifier,
                "verdict": "accept", "reason": "Checked the exact task file content and recorded hash."}])
        if phase == "help":
            return reply(context, message="Use EXPECTED_EDGE = 17 for the empty-input case.")
        self.test.assertEqual(phase, "work")
        self.test.assertEqual(bound["owner"], agent)
        indexed = {item["id"]: item for item in board["tasks"]}
        for dependency in bound["dependencies"]:
            self.test.assertEqual(indexed[dependency]["status"], "accepted")
        self.work_calls[identifier] += 1
        if self.work:
            result = self.work(agent, context, self.work_calls[identifier])
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return result
        return self.complete(context)

    def complete(self, context):
        identifier = context["task"]["id"]
        path = context["task"]["write_paths"][0]
        return reply(context, commands=[{"type": "complete", "task_id": identifier,
                     "result": "Delivered " + path + ": " + self.contents[identifier].strip()}],
                     files=[changed_file(context, path, self.contents[identifier])])


class TeamServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.provider = FakeTeamProvider()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=self.provider)
        self.release_events = []

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        try:
            await self.service.shutdown()
            if self.service._cleanup_task:
                await self.service._cleanup_task
        finally:
            self.temporary.cleanup()

    async def settle(self):
        async def drain():
            while True:
                workers = [worker for worker in self.service._workers.values() if not worker.done()]
                if not workers:
                    return
                await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.wait_for(drain(), 5)
        self.assertIsNone(self.service._background_error)
        self.assertEqual(self.provider.assertion_failures, [])

    async def create(self, *, mode="python_artifacts", project_id=None):
        return await self.service.create_room("Build independently useful parts and integrate their dependencies.",
            "all", "team-fixture", participants=["claude", "codex"], collaboration=True,
            mode=mode, project_id=project_id)

    async def assert_quiet(self, room_id):
        count = len(self.provider.calls)
        jobs = len(self.service.store.snapshot(room_id)["jobs"])
        for _ in range(3):
            await self.service.read_room(room_id)
            await self.service.list_rooms()
            await self.service.status()
            self.service.store.reconcile_team(room_id)
            self.service._wake(room_id)
            await self.settle()
        self.assertEqual(len(self.provider.calls), count)
        self.assertEqual(len(self.service.store.snapshot(room_id)["jobs"]), jobs)

    async def test_five_deliverables_parallel_claims_dependencies_and_exact_peer_reviews(self):
        proposals = [task("schema", "schema.py"), task("examples", "examples.py"),
                     task("edges", "edges.py", dependencies=["schema"]),
                     task("guide", "guide.py", dependencies=["examples"]),
                     task("summary", "summary.py", dependencies=["edges", "guide"])]
        owners = {"schema": "claude", "examples": "codex", "edges": "claude", "guide": "codex", "summary": "codex"}
        contents = {item["id"]: "VALUE = " + str(index) + "\n" for index, item in enumerate(proposals, 11)}
        both_working, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        active = set()

        async def simultaneous(agent, context, attempt):
            if context["task"]["id"] in {"schema", "examples"}:
                active.add((agent, context["task"]["id"]))
                if len(active) == 2:
                    both_working.set()
                await release.wait()

        scenario = TeamScenario(self, proposals, owners, contents, work=simultaneous)
        self.provider.handler = scenario
        created = await self.create()
        room_id = created["room"]["id"]
        await asyncio.wait_for(both_working.wait(), 3)
        during = self.service.store.snapshot(room_id)
        self.assertEqual({item["id"] for item in during["team"]["tasks"] if item["status"] == "working"},
                         {"schema", "examples"})
        self.assertEqual(Counter(item["owner"] for item in during["team"]["tasks"]), {"claude": 2, "codex": 3})
        self.assertNotEqual(during["room"]["status"], "ready_for_user")
        release.set()
        await self.settle()
        saved = await self.service.read_room(room_id)
        self.assertEqual(saved["room"]["status"], "ready_for_user")
        self.assertEqual(saved["collaboration"]["outcome"]["state"], "ready_for_user")
        self.assertEqual(saved["collaboration"]["outcome"]["validation"]["scope"], "static_review")
        self.assertEqual({item["id"]: item["status"] for item in saved["team"]["tasks"]},
                         {identifier: "accepted" for identifier in owners})
        self.assertEqual(len(scenario.reviews), 5)
        self.assertEqual(scenario.work_calls, Counter({identifier: 1 for identifier in owners}))
        self.assertEqual({item["path"]: item["content"] for item in saved["project"]["files"]},
                         {item["write_paths"][0]: contents[item["id"]] for item in proposals})
        self.assertGreater(saved["budget"]["used"], 8)
        self.assertLessEqual(saved["budget"]["used"], 32)
        self.assertEqual(self.provider.maximum, {"claude": 1, "codex": 1})
        self.assertFalse(any(job["kind"] in {"feedback", "review", "task"} for job in saved["jobs"]))
        await self.assert_quiet(room_id)

    async def test_specific_peer_help_resumes_owner_and_is_consumed_in_real_artifact(self):
        consumed = []
        async def help_then_work(agent, context, attempt):
            if context["task"]["id"] == "implementation":
                if attempt == 1:
                    return reply(context, commands=[{"type": "request_help", "task_id": "implementation",
                        "to": "codex", "capability": "text", "text": "What exact empty-input edge value should I encode?"}])
                consumed.append(context["task"]["help_result"])
                self.assertIn("EXPECTED_EDGE = 17", context["task"]["help_result"])

        scenario = TeamScenario(self, [task("implementation", "implementation.py"),
            task("independent_tests", "test_implementation.py", dependencies=["implementation"])],
            {"implementation": "claude", "independent_tests": "codex"},
            {"implementation": "EXPECTED_EDGE = 17\n", "independent_tests": "INDEPENDENT_EXPECTATION = 17\n"}, work=help_then_work)
        self.provider.handler = scenario
        created = await self.create()
        await self.settle()
        saved = await self.service.read_room(created["room"]["id"])
        self.assertEqual(saved["team"]["phase"], "ready_for_user")
        self.assertEqual(scenario.work_calls, {"implementation": 2, "independent_tests": 1})
        self.assertEqual(consumed, ["Use EXPECTED_EDGE = 17 for the empty-input case."])
        helps = [call for call in self.provider.calls if call["context"]["job"]["job_phase"] == "help"]
        self.assertEqual(len(helps), 1)
        self.assertEqual((helps[0]["agent"], helps[0]["context"]["task"]["id"]), ("codex", "implementation"))
        self.assertIn("empty-input edge value", helps[0]["context"]["task"]["help_text"])
        messages = {event["job_id"]: event for event in saved["events"] if event["kind"] == "message"}
        asking = next(call for call in self.provider.calls
                      if call["context"]["job"]["job_phase"] == "work"
                      and call["context"]["task"]["id"] == "implementation")
        self.assertEqual(messages[asking["context"]["job"]["id"]]["recipient"], "codex")
        self.assertEqual(messages[helps[0]["context"]["job"]["id"]]["recipient"], "claude")
        for call in self.provider.calls:
            if call["context"]["job"]["job_phase"] == "review":
                self.assertEqual(messages[call["context"]["job"]["id"]]["recipient"],
                                 call["context"]["task"]["owner"])
        self.assertEqual(len(saved["project"]["files"]), 2)
        await self.assert_quiet(created["room"]["id"])

    async def test_selected_project_paths_get_distinct_initial_owners_without_source_write(self):
        source = self.root / "fictional-source"
        source.mkdir()
        for path, content in {"alpha.txt": "original alpha\n", "beta.txt": "original beta\n"}.items():
            (source / path).write_text(content)
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}
        registered = await self.service.register_project("Fictional source", str(source), ["alpha.txt", "beta.txt"])
        arrived, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        seen = set()
        async def work(agent, context, attempt):
            expected = {"alpha.txt": "claude", "beta.txt": "codex"}
            self.assertEqual({item["path"]: item["owner"] for item in context["project"]["files"]}, expected)
            seen.add(agent)
            if len(seen) == 2:
                arrived.set()
            await release.wait()
        scenario = TeamScenario(self, [task("alpha", "alpha.txt"), task("beta", "beta.txt")],
            {"alpha": "claude", "beta": "codex"}, {"alpha": "delivered alpha\n", "beta": "delivered beta\n"}, work=work)
        self.provider.handler = scenario
        created = await self.create(mode="project_files", project_id=registered["project"]["id"])
        await asyncio.wait_for(arrived.wait(), 3)
        release.set()
        await self.settle()
        saved = await self.service.read_room(created["room"]["id"])
        self.assertEqual(saved["team"]["phase"], "ready_for_user")
        self.assertEqual({item["path"]: (item["owner"], item["content"]) for item in saved["project"]["files"]},
                         {"alpha.txt": ("claude", "delivered alpha\n"), "beta.txt": ("codex", "delivered beta\n")})
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}, before)
        self.assertEqual(saved["collaboration"]["outcome"]["validation"]["scope"], "static_review")

    async def test_unavailable_image_assistance_blocks_without_dispatch_or_fake_artifact(self):
        async def image_request(agent, context, attempt):
            return reply(context, commands=[{"type": "request_help", "task_id": "implementation", "to": "codex",
                "capability": "image", "text": "Render a real diagram image for the deliverable."}])
        scenario = TeamScenario(self, [task("implementation", "implementation.py"),
            task("tests", "test_implementation.py", dependencies=["implementation"])],
            {"implementation": "claude", "tests": "codex"}, {}, work=image_request)
        self.provider.handler = scenario
        created = await self.create()
        await self.settle()
        saved = await self.service.read_room(created["room"]["id"])
        self.assertEqual(saved["room"]["status"], "needs_user")
        bound = next(item for item in saved["team"]["tasks"] if item["id"] == "implementation")
        self.assertEqual((bound["status"], bound["blocked_reason"]), ("blocked", "capability_unavailable"))
        self.assertFalse(any(call["context"]["job"]["job_phase"] == "help" for call in self.provider.calls))
        self.assertEqual(saved["project"]["files"], [])
        await self.assert_quiet(created["room"]["id"])

    async def stopped_peer(self, *, unknown):
        started, late_returned = asyncio.Event(), asyncio.Event()
        async def failing_work(agent, context, attempt):
            if agent == "codex":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    late_returned.set()
                    # A provider finishing while cancellation is in progress may
                    # return a well-formed late result; it must never be saved.
                    return scenario.complete(context)
            await started.wait()
            if unknown:
                return {"adapter_failure": "subscription_limit_reached"}
            result = scenario.complete(context)
            result["files"][0]["path"] = "outside_task.py"
            return result
        scenario = TeamScenario(self, [task("left", "left.py"), task("right", "right.py")],
            {"left": "claude", "right": "codex"}, {"left": "VALUE = 1\n", "right": "LATE_VALUE = 2\n"}, work=failing_work)
        self.provider.handler = scenario
        created = await self.create()
        await self.settle()
        await asyncio.wait_for(late_returned.wait(), 1)
        saved = await self.service.read_room(created["room"]["id"])
        self.assertEqual(saved["team"]["phase"], "unknown" if unknown else "stopped")
        self.assertEqual(saved["project"]["files"], [])
        self.assertFalse(any(job["status"] in {"working", "queued"} for job in saved["jobs"]))
        self.assertFalse(any(call["context"]["job"]["job_phase"] == "review" for call in self.provider.calls))
        self.assertIn(saved["collaboration"]["outcome"]["state"], {"unknown", "stopped"})
        await self.assert_quiet(created["room"]["id"])

    async def test_unknown_provider_result_stops_peer_and_rejects_late_artifact(self):
        await self.stopped_peer(unknown=True)

    async def test_scope_violation_stops_peer_and_rejects_late_artifact(self):
        await self.stopped_peer(unknown=False)


if __name__ == "__main__":
    unittest.main()
