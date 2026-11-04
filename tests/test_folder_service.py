"""Real folder/service/store boundary with an explicitly fake provider transport."""
import asyncio
import base64
from collections import Counter
import hashlib
import inspect
import io
import http.client
import json
import os
import shutil
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import zipfile

from local_agent_coordinator.folder_workspace import FolderWorkspace
from local_agent_coordinator.project_workspace import ProjectWorkspaceError
from local_agent_coordinator.room_service import RoomError, RoomService
from local_agent_coordinator.room_server import create_room_server


def response(context, *, request=None, edits=None, commands=(), state="done", message="Checked the relevant files."):
    team = "team" in context
    return {"message": message, "state": "working" if request is not None else state,
            "commands" if team else "actions": list(commands),
            "base_revision": context["proposal_base_revision"], "files": [],
            "edits": edits, "project_request": request}


def request(kind, path="", *, query=None):
    return {"kind": kind, "path": path, "query": query, "offset": 0, "limit": 100}


def observations(context):
    def resolve(value):
        if isinstance(value, dict) and set(value) == {"text_ref", "sha256", "characters"}:
            digest = value["sha256"]
            assert value["text_ref"] == f"full_texts.{digest}.text"
            content = context["full_texts"][digest]["text"]
            assert hashlib.sha256(content.encode()).hexdigest() == digest
            assert len(content) == value["characters"]
            return content
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value
    # Providers receive these exact full values in the same prompt. The fake
    # client follows the reference contract instead of assuming inline text.
    return [resolve(entry["observation"]) for entry in context["project_observations"]["observations"]]


def read_result(context, path):
    return next((item["result"] for item in reversed(observations(context))
                 if item["request"]["kind"] == "read" and item["request"]["path"] == path), None)


class FolderProvider:
    def __init__(self):
        self.handler = None
        self.calls = []
        self.errors = []
        self.active = Counter()
        self.maximum = Counter()
        self.identity_override = None

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        self.calls.append({"agent": agent, "context": context, "cwd": cwd, "prompt": prompt,
                           "session_id": session_id})
        self.active[agent] += 1
        self.maximum[agent] = max(self.maximum[agent], self.active[agent])
        try:
            await asyncio.sleep(0)
            value = self.handler(agent, context)
            if inspect.isawaitable(value):
                value = await value
            return {"session_id": self.identity_override or session_id or str(uuid4()),
                    "status": "completed", "usage": None, "events": [], "text": json.dumps(value)}
        except AssertionError as error:
            self.errors.append(str(error))
            raise
        finally:
            self.active[agent] -= 1


class FolderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "selected-project"
        (self.source / "src").mkdir(parents=True)
        self.contents = {f"filler-{index}.txt": f"PRIVATE-UNREQUESTED-CONTENT-{index}\n" for index in range(20)}
        self.contents.update({"src/main.py": "VALUE = 1\n", "src/说明.txt": "状态：旧值\n"})
        for path, content in self.contents.items():
            (self.source / path).write_text(content)
        self.provider = FolderProvider()
        self.loop_errors = []
        self.previous_exception_handler = asyncio.get_running_loop().get_exception_handler()
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: self.loop_errors.append(context))
        self.now = 1000.0
        self.native = AsyncMock(side_effect=AssertionError("Native inference must not start"))
        self.native_patch = patch("local_agent_coordinator.adapters.invoke", self.native)
        self.native_patch.start()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   invoke=self.provider, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.service.shutdown()
        asyncio.get_running_loop().set_exception_handler(self.previous_exception_handler)
        self.native_patch.stop()
        self.native.assert_not_called()
        self.temporary.cleanup()

    async def settle(self):
        async def wait():
            while True:
                workers = [task for task in self.service._workers.values() if not task.done()]
                if not workers:
                    return
                results = await asyncio.gather(*workers, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
        await asyncio.wait_for(wait(), 5)
        self.assertEqual(self.provider.errors, [])

    async def create(self, *, collaboration=False, participants=None):
        registration = await self.service.register_project("Selected project", str(self.source), selection_kind="folder")
        return await self.service.create_room("Update the project", "claude", str(uuid4()),
            mode="project_folder", project_id=registration["project"]["id"],
            participants=participants or (["claude", "codex"] if collaboration else ["claude"]),
            collaboration=collaboration)

    def assert_source_unchanged(self):
        self.assertEqual({path.relative_to(self.source).as_posix(): path.read_text()
                          for path in self.source.rglob("*") if path.is_file()}, self.contents)

    def assert_private_context(self, call):
        self.assertNotIn(str(self.source), call["prompt"])
        self.assertNotIn("PRIVATE-UNREQUESTED-CONTENT-", call["prompt"])
        self.assertNotIn("files", call["context"]["project"])

    async def test_single_member_lists_searches_reads_then_exact_edits_without_source_write(self):
        seen = []
        async def handler(agent, context):
            self.assertEqual(agent, "claude")
            seen.append(context)
            count = len(observations(context))
            if count == 0:
                self.assertNotIn("VALUE = 1", json.dumps(context))
                return response(context, request=request("list"))
            if count == 1:
                self.assertGreater(observations(context)[0]["result"]["total"], 12)
                return response(context, request=request("search", "src", query="VALUE"))
            if count == 2:
                self.assertIn("src/main.py", json.dumps(observations(context)[-1]["result"]))
                return response(context, request=request("read", "src/main.py"))
            snapshot = self.service.store.snapshot(context["room_id"] if "room_id" in context else room_id)
            self.assertEqual(snapshot["jobs"][-1]["status"], "working")
            result = read_result(context, "src/main.py")
            return response(context, edits=[{"path": "src/main.py", "base_sha256": result["sha256"],
                                             "old_text": "VALUE = 1", "new_text": "VALUE = 2"}])
        self.provider.handler = handler
        created = await self.create()
        room_id = created["room"]["id"]
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        self.assertEqual(snapshot["room"]["status"], "completed")
        self.assertEqual(snapshot["budget"]["used"], 4)
        self.assertEqual([job["kind"] for job in snapshot["jobs"]], ["task"] * 4)
        project = self.service._project(room_id).snapshot()
        self.assertEqual([(file["path"], file["content"]) for file in project["files"]],
                         [("src/main.py", "VALUE = 2\n")])
        for call in self.provider.calls:
            self.assert_private_context(call)
        self.assert_source_unchanged()

    async def test_two_members_discover_claim_modify_baselines_and_review_exact_versions(self):
        paths = {"claude": "src/main.py", "codex": "src/说明.txt"}
        replacements = {"src/main.py": ("VALUE = 1", "VALUE = 9"), "src/说明.txt": ("旧值", "新值")}
        reviewed = []
        async def handler(agent, context):
            phase, board = context["job"]["job_phase"], context["team"]
            if phase in {"plan", "coordinate"}:
                if not observations(context):
                    return response(context, request=request("list", "src"))
                commands = []
                visible = {task["id"]: task for task in board["tasks"]}
                if agent == board["plan_author"] and not visible:
                    for owner, path in paths.items():
                        commands.append({"type": "propose", "id": owner, "title": "Update " + path,
                            "description": "Edit the discovered baseline file", "acceptance": "Peer reads the exact changed version",
                            "dependencies": [], "write_paths": [path], "capability": "text"})
                    visible = {owner: {"owner": None} for owner in paths}
                if agent in visible and visible[agent]["owner"] is None:
                    commands.append({"type": "claim", "task_id": agent})
                if visible:
                    commands.append({"type": "plan_ready"})
                return response(context, commands=commands)
            task = context["task"]
            path = task["write_paths"][0]
            result = read_result(context, path)
            if result is None:
                return response(context, request=request("read", path))
            if phase == "work":
                self.assertEqual(task["owner"], agent)
                old, new = replacements[path]
                return response(context, edits=[{"path": path, "base_sha256": result["sha256"],
                    "old_text": old, "new_text": new}], commands=[{"type": "complete", "task_id": task["id"],
                    "result": "Edited " + path}])
            self.assertEqual(phase, "review")
            self.assertNotEqual(task["owner"], agent)
            self.assertEqual(result["revision"], task["result_revision"])
            self.assertEqual(context["inspected_revision"], task["result_revision"])
            delivered = next(file for file in task["result_files"] if file["path"] == path)
            self.assertEqual(result["sha256"], delivered["sha256"])
            self.assertIn(replacements[path][1], result["content"])
            reviewed.append((agent, path, task["result_version"], task["result_revision"]))
            return response(context, commands=[{"type": "review", "task_id": task["id"],
                "verdict": "accept", "reason": "Read and checked this exact delivered revision and SHA"}])
        self.provider.handler = handler
        created = await self.create(collaboration=True)
        room_id = created["room"]["id"]
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        self.assertEqual(snapshot["team"]["phase"], "ready_for_user")
        self.assertEqual({task["status"] for task in snapshot["team"]["tasks"]}, {"accepted"})
        self.assertEqual({task["owner"] for task in snapshot["team"]["tasks"]}, {"claude", "codex"})
        self.assertEqual(len(reviewed), 2)
        self.assertEqual({item[2] for item in reviewed}, {1})
        self.assertEqual(len({item[3] for item in reviewed}), 2,
                         "Both peers must review their own distinct immutable delivery revisions")
        self.assertEqual(self.provider.maximum, {"claude": 1, "codex": 1})
        project = self.service._project(room_id).snapshot()
        self.assertEqual({file["path"] for file in project["files"]}, set(paths.values()))
        exported = await self.service.export_room(room_id, project["revision"])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content_base64"]))) as archive:
            self.assertEqual(set(archive.namelist()), {*paths.values(), ".beanest-export.json"})
            self.assertEqual(archive.read("src/说明.txt").decode(), "状态：新值\n")
        for call in self.provider.calls:
            self.assert_private_context(call)
        self.assert_source_unchanged()

    async def run_new_output_help_scenario(self, *, partial=False, ordinary=False, missing_complete=False):
        paths = ["trial-results/first.md", "trial-results/second.md"]
        work_calls = 0
        help_calls = 0
        captured_bindings = []
        original_finish = self.service.store.finish_job

        def record_finish(job_id, **kwargs):
            if kwargs.get("message") == "Ask for the missing evidence":
                captured_bindings.append(kwargs.get("artifact_files"))
            return original_finish(job_id, **kwargs)

        async def handler(agent, context):
            nonlocal work_calls, help_calls
            phase, board = context["job"]["job_phase"], context["team"]
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands = [{"type": "propose", "id": "report", "title": "Investigate",
                        "description": "Create two new reports after asking for evidence",
                        "acceptance": "Both reports exist and are independently reviewed",
                        "dependencies": [], "write_paths": paths, "capability": "text"},
                        {"type": "claim", "task_id": "report"}, {"type": "plan_ready"}]
                elif board["tasks"]:
                    commands = [{"type": "plan_ready"}]
                return response(context, commands=commands)
            if phase == "help":
                help_calls += 1
                self.assertEqual(agent, "codex")
                self.assertEqual(context["task"]["help_text"], "Please supply the missing evidence")
                return response(context, message="Independent evidence supplied")
            if phase == "work":
                work_calls += 1
                if missing_complete:
                    # The host gives a side-effect-free missing result one
                    # bounded correction. This provider repeats the omission.
                    return response(context, commands=[{"type": "complete", "task_id": "report",
                        "result": "Incorrectly claiming missing reports"}], message="Missing output")
                if work_calls == 1:
                    commands = [] if ordinary else ([{"type": "complete", "task_id": "report",
                        "result": "Incorrectly claiming missing reports"}] if missing_complete else
                        [{"type": "request_help", "task_id": "report", "to": "codex",
                          "capability": "text", "text": "Please supply the missing evidence"}])
                    value = response(context, commands=commands, message="Ask for the missing evidence")
                    if partial:
                        value["files"] = [{"path": paths[0], "previous_sha256": None, "content": "First evidence\n"}]
                    return value
                self.assertEqual(context["task"]["help_result"], "Independent evidence supplied")
                value = response(context, commands=[{"type": "complete", "task_id": "report",
                    "result": "Both reports incorporate the independent evidence"}])
                value["files"] = [{"path": path, "previous_sha256": None, "content": content}
                    for path, content in zip(paths, ["First evidence\n", "Independent evidence supplied\n"])
                    if not partial or path != paths[0]]
                return value
            self.assertEqual(phase, "review")
            return response(context, commands=[{"type": "review", "task_id": "report", "verdict": "accept",
                "reason": "Checked the exact result files provided by host inspection"}])

        self.provider.handler = handler
        with patch.object(self.service.store, "finish_job", side_effect=record_finish):
            created = await self.create(collaboration=True)
            await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assert_source_unchanged()
        if not missing_complete:
            self.assertNotIn("project_file_unavailable", [job["error"] for job in snapshot["jobs"]])
        if ordinary:
            self.assertEqual(snapshot["team"]["tasks"][0]["blocked_reason"], "team_result_missing")
            work = [job for job in snapshot["jobs"] if job["kind"] == "team_work"]
            self.assertEqual(work[0]["status"], "completed")
            self.assertEqual(captured_bindings, [[]])
        elif missing_complete:
            self.assertNotEqual(snapshot["team"]["phase"], "ready_for_user")
            self.assertEqual(help_calls, 0)
            self.assertEqual(work_calls, 2)
            self.assertEqual(len([job for job in snapshot["jobs"] if job["error"] == "team_result_missing"]), 2)
            self.assertEqual(self.service._project(created["room"]["id"]).snapshot()["files"], [])
        else:
            self.assertEqual(snapshot["team"]["phase"], "ready_for_user")
            self.assertEqual((work_calls, help_calls), (2, 1))
            files = self.service._project(created["room"]["id"]).snapshot()["files"]
            expected = [{"path": paths[0], "sha256": hashlib.sha256(b"First evidence\n").hexdigest()}] if partial else []
            self.assertEqual(captured_bindings, [expected])
            self.assertEqual(snapshot["team"]["tasks"][0]["result_files"],
                             [{"path": file["path"], "sha256": file["sha256"]} for file in files])

    async def test_new_output_work_message_does_not_read_missing_output(self):
        await self.run_new_output_help_scenario(ordinary=True)

    async def test_new_output_help_reaches_peer_and_resumes_owner(self):
        await self.run_new_output_help_scenario()

    async def test_partial_output_help_preserves_existing_hash_and_resumes(self):
        await self.run_new_output_help_scenario(partial=True)

    async def test_complete_without_declared_new_outputs_still_fails(self):
        await self.run_new_output_help_scenario(missing_complete=True)

    async def run_required_review_scenario(self, *, content="NEW_RESULT = 7\n", poison_receipts=False):
        path = "new/新成果.txt"
        review_calls = []
        room_id = None
        premature_message = "UNSEEN_ACCEPT_MUST_NOT_BE_PUBLISHED"
        async def handler(agent, context):
            phase, board = context["job"]["job_phase"], context["team"]
            if phase in {"plan", "coordinate"}:
                commands = []
                if not board["tasks"] and agent == board["plan_author"]:
                    commands = [{"type": "propose", "id": "newfile", "title": "Add new result",
                        "description": "Create the declared new file", "acceptance": "Peer reads exact delivered bytes",
                        "dependencies": [], "write_paths": [path], "capability": "text"},
                        {"type": "claim", "task_id": "newfile"}, {"type": "plan_ready"}]
                elif board["tasks"]:
                    commands = [{"type": "plan_ready"}]
                return response(context, commands=commands)
            task = context["task"]
            if phase == "work":
                value = response(context, commands=[{"type": "complete", "task_id": "newfile",
                    "result": "Created the requested new file"}])
                value["files"] = [{"path": path, "previous_sha256": None, "content": content}]
                return value
            self.assertEqual(phase, "review")
            review_calls.append(context)
            if len(review_calls) == 1:
                if poison_receipts:
                    # Model text cannot create these rows. These deliberately
                    # mismatched host receipts exercise the real scope/version
                    # gate against durable observations from unrelated work.
                    correct = self.service._project(room_id).inspect(request("read", path),
                                                                   revision=task["result_revision"])
                    snapshot = self.service.store.snapshot(room_id)
                    bindings = [(agent, "review", task["result_revision"] - 1),
                                (agent, "plan", task["result_revision"]),
                                (task["owner"], "review", task["result_revision"])]
                    with self.service.store._connection(write=True) as db:
                        for job, (provider, observed_phase, revision) in zip(snapshot["jobs"], bindings):
                            observation = {"request": request("read", path),
                                           "result": {**correct, "revision": revision}}
                            db.execute("INSERT INTO project_inspections(room_id,provider,job_id,run_id,task_id,phase,"
                                "observation_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                                (room_id, provider, job["id"], context["job"]["run_id"], task["id"], observed_phase,
                                 json.dumps(observation, ensure_ascii=False), self.now))
                return response(context, commands=[{"type": "review", "task_id": task["id"], "verdict": "accept",
                    "reason": "Premature unobserved acceptance"}], message=premature_message)
            self.assertEqual(len(review_calls), 2, "One exact host read must suffice for this one-file review")
            required = [item for item in observations(context) if item.get("purpose") == "required_review_inspection"]
            self.assertEqual(len(required), 1)
            result = required[0]["result"]
            self.assertEqual(result["path"], path)
            self.assertEqual(result["content"], content)
            self.assertEqual(result["revision"], task["result_revision"])
            self.assertEqual(result["sha256"], task["result_files"][0]["sha256"])
            self.assertEqual(result["offset"], 0)
            snapshot = self.service.store.snapshot(room_id)
            pending_task = snapshot["team"]["tasks"][0]
            self.assertEqual(pending_task["status"], "review")
            self.assertIsNone(pending_task["review_reason"])
            self.assertFalse(any(premature_message in event.get("text", "") for event in snapshot["events"]))
            return response(context, commands=[{"type": "review", "task_id": task["id"], "verdict": "accept",
                "reason": "Checked exact host-observed result bytes"}], message="Read and checked the delivered file.")
        self.provider.handler = handler
        created = await self.create(collaboration=True)
        room_id = created["room"]["id"]
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        self.assertEqual(snapshot["team"]["phase"], "ready_for_user")
        self.assertEqual(snapshot["team"]["tasks"][0]["status"], "accepted")
        self.assertEqual(len(review_calls), 2)
        self.assertEqual(snapshot["budget"]["used"], len(self.provider.calls))
        self.assertEqual([job["kind"] for job in snapshot["jobs"]][-2:], ["team_review", "team_review"])
        self.assertFalse(any(premature_message in event.get("text", "") for event in snapshot["events"]))
        project = self.service._project(room_id).snapshot()
        self.assertEqual([(file["path"], file["content"]) for file in project["files"]], [(path, content)])
        self.assert_source_unchanged()
        return snapshot

    async def test_premature_accept_gets_host_read_without_publishing_verdict_then_new_file_is_accepted(self):
        await self.run_required_review_scenario()

    async def test_stale_revision_other_phase_and_peer_read_never_substitute_for_reviewer_read(self):
        await self.run_required_review_scenario(poison_receipts=True)

    async def test_empty_new_file_requires_real_read_before_acceptance(self):
        await self.run_required_review_scenario(content="")

    async def run_single_review_comparison(self, before, after, *, remove_source=False):
        path = "src/main.py"
        self.contents[path] = before
        (self.source / path).write_text(before)
        comparison_results = []
        async def handler(agent, context):
            kind = context["job"]["kind"]
            if kind == "task":
                previous = read_result(context, path)
                if previous is None:
                    return response(context, request=request("read", path))
                value = response(context, edits=[{"path": path, "base_sha256": previous["sha256"],
                    "old_text": before, "new_text": after}])
                value["actions"] = [{"type": "request", "to": "codex", "kind": "review",
                                     "text": "Read and compare this exact modified file"}]
                return value
            if kind == "review":
                result = read_result(context, path)
                if result is None:
                    if remove_source:
                        shutil.rmtree(self.source)
                    return response(context, request=request("read", path))
                self.assertEqual(result["content"], after)
                comparison = result["source_comparison"]
                self.assertTrue(comparison["available"])
                self.assertEqual(comparison["base_revision"], 1)
                self.assertEqual(comparison["revision"], context["inspected_revision"])
                self.assertEqual(comparison["revision"], result["revision"])
                self.assertEqual(comparison["before_sha256"], hashlib.sha256(before.encode()).hexdigest())
                self.assertEqual(comparison["after_sha256"], result["sha256"])
                self.assertEqual(comparison["after_sha256"], hashlib.sha256(after.encode()).hexdigest())
                comparison_results.append(observations(context)[-1])
                return response(context, message="Compared the actual captured baseline and delivered bytes.")
            self.assertEqual(kind, "feedback")
            return response(context, message="Reviewed result is ready.")
        self.provider.handler = handler
        created = await self.create(participants=["claude", "codex"])
        room_id = created["room"]["id"]
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        self.assertEqual(snapshot["room"]["status"], "completed")
        self.assertEqual(len(comparison_results), 1)
        self.assertEqual(len(self.provider.calls), 5)
        if remove_source:
            self.assertFalse(self.source.exists(), "Reading captured data must not recreate the deleted source")
        else:
            self.assert_source_unchanged()
        return room_id, comparison_results[0]

    async def test_review_read_contains_exact_baseline_diff_even_after_source_is_deleted(self):
        room_id, observation = await self.run_single_review_comparison(
            "keep=yes\nVALUE = 1\n", "keep=yes\nVALUE = 7\n", remove_source=True)
        comparison = observation["result"]["source_comparison"]
        self.assertEqual(comparison["scope"], "captured_source")
        self.assertIn(" keep=yes\n", comparison["diff"])
        self.assertIn("-VALUE = 1\n+VALUE = 7\n", comparison["diff"])
        self.assertFalse(comparison["truncated"])
        self.assertNotIn(str(self.source), json.dumps(observation))

    async def test_json_escape_growth_truncates_only_comparison_diff_and_preserves_read(self):
        before, after = '"' * 16000 + "\n", "\\" * 16000 + "\n"
        room_id, observation = await self.run_single_review_comparison(before, after)
        result = observation["result"]
        comparison = result["source_comparison"]
        raw_comparison = self.service._project(room_id).source_comparison("src/main.py", revision=result["revision"])
        self.assertEqual(result["content"], after)
        self.assertFalse(result["truncated"])
        self.assertTrue(comparison["truncated"])
        self.assertLess(len(comparison["diff"]), len(raw_comparison["diff"]))
        self.assertEqual({key: value for key, value in comparison.items() if key not in {"diff", "truncated"}},
                         {key: value for key, value in raw_comparison.items() if key not in {"diff", "truncated"}})
        self.assertLessEqual(len(json.dumps(observation, ensure_ascii=False).encode()), 48 * 1024 - 1024)
        with self.service.store._connection() as db:
            stored = db.execute("SELECT observation_json FROM project_inspections WHERE room_id=? AND phase='review'",
                                (room_id,)).fetchone()[0]
        self.assertLessEqual(len(stored.encode("utf-8")), 48 * 1024 - 1024)
        self.assertEqual(json.loads(stored)["result"]["content"], after)

    async def test_capture_failure_dispatches_zero_fake_or_native_calls(self):
        self.provider.handler = lambda agent, context: response(context)
        with patch.object(FolderWorkspace, "seed_folder", side_effect=ProjectWorkspaceError("project_source_changed")):
            with self.assertRaises(RoomError) as failure:
                await self.create()
        self.assertEqual(failure.exception.code, "project_source_changed")
        self.assertEqual(self.provider.calls, [])
        room = self.service.store.list_rooms()["rooms"][0]
        self.assertEqual(room["status"], "failed")
        self.assert_source_unchanged()

    async def test_capture_cancellation_waits_for_owned_capture_and_dispatches_nothing(self):
        entered, exited = threading.Event(), threading.Event()
        def capture(workspace, metadata, *, cancelled):
            entered.set()
            try:
                while not cancelled():
                    exited.wait(0.005)
                raise ProjectWorkspaceError("project_capture_cancelled")
            finally:
                exited.set()
        with patch.object(FolderWorkspace, "seed_folder", capture):
            creating = asyncio.create_task(self.create())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            creating.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(creating, 2)
        self.assertTrue(exited.is_set())
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.service._captures, {})
        self.assertEqual(self.loop_errors, [], "Capture cancellation leaked an uncollected asyncio exception")
        self.assert_source_unchanged()

    async def test_cancel_running_inspection_waits_for_fake_cleanup_and_never_reads(self):
        entered = asyncio.Event()
        async def handler(agent, context):
            entered.set()
            await asyncio.Event().wait()
            return response(context, request=request("read", "src/main.py"))
        self.provider.handler = handler
        with patch.object(FolderWorkspace, "inspect", side_effect=AssertionError("Cancelled request read files")) as inspect_call:
            created = await self.create()
            await asyncio.wait_for(entered.wait(), 2)
            await self.service.cancel_room(created["room"]["id"])
            await self.settle()
        inspect_call.assert_not_called()
        self.assertEqual(self.provider.active["claude"], 0)
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(snapshot["room"]["status"], "cancelled")
        self.assertTrue(snapshot["cancellation_confirmed"])
        self.assert_source_unchanged()

    async def test_malformed_inspection_is_rejected_before_any_workspace_read(self):
        self.provider.handler = lambda agent, context: response(context, request={**request("read", "src/main.py"), "limit": True})
        with patch.object(FolderWorkspace, "inspect", side_effect=AssertionError("Malformed request reached workspace")) as inspect_call:
            created = await self.create()
            await self.settle()
        inspect_call.assert_not_called()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotEqual(snapshot["room"]["status"], "completed")
        self.assert_source_unchanged()

    async def test_escaping_inspection_never_opens_source_or_blob_and_returns_bounded_error(self):
        opened_source = []
        real_open = os.open
        source_directory_inodes = {self.source.stat().st_ino, (self.source / "src").stat().st_ino}
        def guarded_open(path, flags, *args, **kwargs):
            if (isinstance(path, (str, bytes, os.PathLike)) and os.path.isabs(path)
                    and Path(path).is_relative_to(self.source)):
                opened_source.append(str(path))
                raise AssertionError("Inspection reopened source")
            directory = kwargs.get("dir_fd")
            if directory is not None and os.fstat(directory).st_ino in source_directory_inodes:
                opened_source.append(str(path))
                raise AssertionError("Inspection read source directory descriptor")
            return real_open(path, flags, *args, **kwargs)
        gate = asyncio.Event()
        async def handler(agent, context):
            if not observations(context):
                await gate.wait()
                return response(context, request=request("read", "../../outside.txt"))
            result = observations(context)[0]["result"]
            self.assertIn(result["error"], {"invalid_project_files", "invalid_project_request"})
            return response(context)
        self.provider.handler = handler
        created = await self.create()
        with patch("os.open", guarded_open), patch.object(FolderWorkspace, "_verified_blob",
                side_effect=AssertionError("Traversal reached content blob")) as blob_read:
            gate.set()
            await self.settle()
        self.assertEqual(opened_source, [])
        blob_read.assert_not_called()
        self.assertEqual(len(self.provider.calls), 2)
        for call in self.provider.calls:
            self.assert_private_context(call)
        self.assert_source_unchanged()

    async def test_identity_changes_after_inspection_cannot_write(self):
        def handler(agent, context):
            if not observations(context):
                return response(context, request=request("read", "src/main.py"))
            self.provider.identity_override = str(uuid4())
            result = read_result(context, "src/main.py")
            return response(context, edits=[{"path": "src/main.py", "base_sha256": result["sha256"],
                "old_text": "VALUE = 1", "new_text": "VALUE = 3"}])
        self.provider.handler = handler
        created = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(snapshot["jobs"][-1]["error"], "session_identity_changed")
        self.assertEqual(self.service._project(created["room"]["id"]).snapshot()["files"], [])
        self.assert_source_unchanged()

    async def test_sixteen_kib_emoji_read_resumes_with_actual_utf8_observation(self):
        emoji = "😀" * 4096
        self.contents["src/emoji.txt"] = emoji
        (self.source / "src/emoji.txt").write_text(emoji)
        def handler(agent, context):
            if not observations(context):
                return response(context, request=request("read", "src/emoji.txt"))
            result = read_result(context, "src/emoji.txt")
            self.assertEqual(result["content"], emoji)
            self.assertLessEqual(len(json.dumps(context["project_observations"], ensure_ascii=False).encode()), 48 * 1024)
            return response(context)
        self.provider.handler = handler
        created = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(snapshot["room"]["status"], "completed")
        self.assertEqual([job["status"] for job in snapshot["jobs"]], ["completed", "completed"])
        self.assertTrue(any(event["kind"] == "job_created" and event.get("job_id") == snapshot["jobs"][1]["id"]
                            for event in snapshot["events"]))
        self.assert_source_unchanged()

    async def test_later_source_changes_are_never_read_into_captured_task(self):
        gate = asyncio.Event()
        async def handler(agent, context):
            if not observations(context):
                await gate.wait()
                return response(context, request=request("read", "src/main.py"))
            result = read_result(context, "src/main.py")
            self.assertEqual(result["content"], "VALUE = 1\n")
            return response(context)
        self.provider.handler = handler
        created = await self.create()
        self.contents["src/main.py"] = "SOURCE CHANGED AFTER CAPTURE\n"
        (self.source / "src/main.py").write_text(self.contents["src/main.py"])
        gate.set()
        await self.settle()
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.service._project(created["room"]["id"]).snapshot()["files"], [])
        self.assert_source_unchanged()

    async def test_team_prompt_reports_unbounded_claimed_and_queued_budget(self):
        budgets, reached = [], asyncio.Event()
        original_prompt = self.service._team_prompt

        def checked_prompt(job):
            snapshot = self.service.store.snapshot(job["room_id"])
            prompt = original_prompt(job)
            budget = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])["runtime_budget"]
            queued = sum(item["status"] == "queued" for item in snapshot["jobs"])
            working = sum(item["status"] == "working" for item in snapshot["jobs"])
            self.assertEqual(budget["source"], "coordinator_snapshot")
            self.assertEqual(budget["scope"], "current_run")
            self.assertEqual(budget["used"], snapshot["budget"]["used"])
            self.assertEqual((budget["queued"], budget["working"]), (queued, working))
            self.assertGreaterEqual(working, 1)
            for field in ("limit", "remaining", "available_after_queued", "seconds_remaining"):
                self.assertIsNone(budget[field])
            self.assertFalse(budget["conserve"])
            budgets.append(budget)
            return prompt

        async def handler(agent, context):
            if len(budgets) >= 35:
                reached.set()
                await asyncio.Event().wait()
            return response(context, request=request("list", "src"))

        self.provider.handler = handler
        with patch.object(self.service, "_team_prompt", side_effect=checked_prompt):
            created = await self.create(collaboration=True)
            await asyncio.wait_for(reached.wait(), 5)
            running = await self.service.read_room(created["room"]["id"])
            self.assertEqual(running["room"]["status"], "running")
            await self.service.cancel_room(created["room"]["id"])
            await self.settle()
        self.assertGreaterEqual(len(budgets), 35)
        self.assertEqual([budget["used"] for budget in budgets], list(range(1, len(budgets) + 1)))
        self.assertTrue(any(budget["queued"] > 0 for budget in budgets))
        self.assert_source_unchanged()

    async def test_team_prompt_has_no_default_time_conservation_after_old_deadline(self):
        budgets, reached = [], asyncio.Event()

        async def handler(agent, context):
            budgets.append(context["runtime_budget"])
            if len(budgets) == 1:
                self.now += 7201
            if len(budgets) >= 3:
                reached.set()
                await asyncio.Event().wait()
            return response(context, request=request("list", "src"))

        self.provider.handler = handler
        created = await self.create(collaboration=True)
        await asyncio.wait_for(reached.wait(), 5)
        for budget in budgets:
            self.assertIsNone(budget["seconds_remaining"])
            self.assertFalse(budget["conserve"])
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(snapshot["room"]["status"], "running")
        await self.service.cancel_room(created["room"]["id"])
        await self.settle()
        self.assert_source_unchanged()

    async def test_repeated_inspection_can_pass_old_default_call_budget(self):
        reached = asyncio.Event()

        async def handler(agent, context):
            if len(self.provider.calls) >= 10:
                reached.set()
                await asyncio.Event().wait()
            return response(context, request=request("list", "src"))

        self.provider.handler = handler
        created = await self.create()
        await asyncio.wait_for(reached.wait(), 5)
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(snapshot["budget"]["used"], 10)
        self.assertIsNone(snapshot["budget"]["limit"])
        self.assertEqual(snapshot["room"]["status"], "running")
        await self.service.cancel_room(created["room"]["id"])
        await self.settle()
        self.assertEqual(self.service._project(created["room"]["id"]).snapshot()["files"], [])
        self.assert_source_unchanged()

    async def test_historical_deadline_does_not_prevent_current_inspection(self):
        def handler(agent, context):
            if read_result(context, "src/main.py") is None:
                self.now += 901
                return response(context, request=request("read", "src/main.py"))
            self.assertEqual(read_result(context, "src/main.py")["content"], "VALUE = 1\n")
            return response(context)

        self.provider.handler = handler
        created = await self.create()
        with self.service.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET deadline=? WHERE id=?", (1000.0 + 900, created["room"]["id"]))
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(snapshot["room"]["status"], "completed")
        self.assertFalse(any(job["error"] == "deadline_exceeded" for job in snapshot["jobs"]))
        self.assert_source_unchanged()

    async def test_cancel_before_inspection_result_prevents_late_read(self):
        entered = asyncio.Event()

        async def handler(agent, context):
            entered.set()
            await asyncio.Event().wait()
            return response(context, request=request("read", "src/main.py"))

        self.provider.handler = handler
        with patch.object(FolderWorkspace, "inspect", side_effect=AssertionError("Cancelled result read files")) as inspect_call:
            created = await self.create()
            await asyncio.wait_for(entered.wait(), 5)
            await self.service.cancel_room(created["room"]["id"])
            await self.settle()
        inspect_call.assert_not_called()
        self.assertEqual(len(self.provider.calls), 1)
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(snapshot["room"]["status"], "cancelled")
        self.assert_source_unchanged()


class FolderHTTPContractTests(unittest.TestCase):
    """Real loopback router and real RoomService; no fake HTTP handlers."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        for number in range(20):
            (self.source / f"file-{number}.txt").write_text(f"unchanged {number}\n")
        self.provider = FolderProvider()
        self.provider.handler = lambda agent, context: response(context)
        self.native = AsyncMock(side_effect=AssertionError("HTTP fixture must not start native inference"))
        self.native_patch = patch("local_agent_coordinator.adapters.invoke", self.native)
        self.native_patch.start()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=self.provider)
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.native_patch.stop()
        self.native.assert_not_called()
        self.temporary.cleanup()

    def request(self, method, path, body=None, *, mcp=False):
        data = json.dumps(body).encode() if body is not None else None
        token = self.server.mcp_token if mcp else self.server.token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=data,
                               headers={"X-LAC-Token": token, "X-LAC-Instance": self.server.instance_id,
                                        "Content-Type": "application/json"})
            result = connection.getresponse()
            return result.status, json.loads(result.read())
        finally:
            connection.close()

    def test_folder_registration_contract_and_web_only_scope(self):
        body = {"name": "A whole project", "path": str(self.source), "selection_kind": "folder"}
        status, registered = self.request("POST", "/api/projects", body)
        self.assertEqual(status, 200)
        self.assertEqual(registered["project"]["selection_kind"], "folder")
        self.assertEqual(self.provider.calls, [])
        status, invalid = self.request("POST", "/api/projects", {**body, "files": ["file-0.txt"]})
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"], "invalid_arguments")
        for path, expected in (("/api/projects", 403), ("/api/mcp/projects", 404)):
            status, denied = self.request("POST", path, body, mcp=True)
            self.assertEqual(status, expected)
        status, created = self.request("POST", "/api/rooms", {
            "goal": "Inspect the chosen whole project", "target": "claude", "request_id": str(uuid4()),
            "mode": "project_folder", "project_id": registered["project"]["id"], "participants": ["claude"]})
        self.assertEqual(status, 200)
        room_id = created["room"]["id"]
        async def settle():
            while any(not task.done() for task in self.service._workers.values()):
                await asyncio.gather(*[task for task in self.service._workers.values() if not task.done()])
        self.server.runner.call(settle)
        status, snapshot = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["room"]["mode"], "project_folder")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn(str(self.source), self.provider.calls[0]["prompt"])
        self.assertGreater(self.provider.calls[0]["context"]["project"]["total_files"], 12)


if __name__ == "__main__":
    unittest.main()
