"""Prompt budgeting preserves authoritative work and only excerpts background."""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from local_agent_coordinator import adapters, room_service
from local_agent_coordinator.room_service import RoomError, RoomService


class TeamContextBudgetTests(unittest.TestCase):
    def setUp(self):
        # Exercise compaction under an explicitly small input budget. The
        # production prompt budget is independent of the response-size cap.
        budget = patch.object(room_service, 'MAX_PROMPT_BYTES', 128 * 1024)
        budget.start()
        self.addCleanup(budget.stop)
        self.deliverable = "交付🙂" * 1000
        self.dependency = "完整依赖结果" * 500
        task = {"id": "tests", "owner": "claude", "dependencies": ["model"],
                "write_paths": ["test_model.py"], "acceptance": "Review this exact version.",
                "result": self.deliverable, "result_revision": 27, "result_version": 2}
        self.snapshot = {
            "room": {"id": "room", "mode": "python_artifacts", "work_mode": "legacy"},
            "team": {"run_id": "current", "phase": "working", "plan_author": "claude",
                     "tasks": [task, {"id": "model", "dependencies": [], "result": self.dependency}]},
            "members": [{"id": "codex", "name": "Codex", "status": "working", "current_task": "Review", "configuration_dispatchable": True}],
            "events": [], "executions": [],
            "budget": {"used": 11, "limit": 32, "deadline": 1600.0},
            "jobs": [{"id": "review", "status": "working"}, {"id": "queued-work", "status": "queued"}],
            "runs": [{"id": "current", "number": 6, "goal": "Current explicit goal", "status": "working",
                      "revision": 27, "digest": "a" * 64, "work_mode": "legacy"}],
        }
        self.snapshot["current_run"] = copy.deepcopy(self.snapshot["runs"][0])
        sizes = [1562, 9222, 4931, 16755, 17987, 13450]
        self.project = {"revision": 27, "files": [
            {"path": f"part_{i}.py", "owner": "claude" if i % 2 else "codex",
             "content": "#" + "x" * (size - 2) + "\n",
             "sha256": hashlib.sha256(("#" + "x" * (size - 2) + "\n").encode()).hexdigest()}
            for i, size in enumerate(sizes)]}
        self.job = {"id": "review", "room_id": "room", "kind": "team_review", "provider": "codex",
                    "parent_id": "work", "text": "Review the bound task", "goal": "Current user-authorized goal",
                    "mode": "python_artifacts", "work_mode": "legacy",
                    "team": {"task_id": "tests", "run_id": "current", "job_phase": "review"}}
        self.service = RoomService.__new__(RoomService)
        self.service.clock = lambda: 1000.0
        self.service.registry = SimpleNamespace(get=lambda _: True)
        self.service.store = SimpleNamespace(snapshot=lambda _: self.snapshot,
            read_run=lambda *args: {"team": {"tasks": []}})
        self.service._project = lambda _: SimpleNamespace(max_bytes=65536, snapshot=lambda **kwargs: self.project)

    def prompt(self):
        return self.service._team_prompt(self.job)

    def context(self, prompt):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        def resolve(value):
            if isinstance(value, dict) and 'text_ref' in value:
                self.assertEqual(set(value), {'text_ref', 'sha256', 'characters'})
                digest = value['sha256']
                self.assertEqual(value['text_ref'], f'full_texts.{digest}.text')
                entry = context['full_texts'][digest]
                text = entry['text']
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
                self.assertEqual(len(text), value['characters'])
                self.assertEqual(len(text.encode()), entry['utf8_bytes'])
                return text
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve(item) for item in value]
            return value
        return resolve(context)

    def assert_authoritative_content(self, context):
        self.assertEqual(context["goal"], self.job["goal"])
        self.assertEqual(context["task"], self.snapshot["team"]["tasks"][0])
        self.assertEqual(context["task"]["result"], self.deliverable)
        self.assertEqual(context["project"], self.project)
        self.assertEqual(next(item for item in context["team"]["tasks"] if item["id"] == "model")["result"], self.dependency)
        self.assertEqual(context["proposal_base_revision"], 27)
        self.assertEqual(context["runtime_budget"], {"source": "coordinator_snapshot", "scope": "current_run",
            "used": 11, "limit": 32, "queued": 1, "working": 1, "remaining": 21,
            "available_after_queued": 20, "seconds_remaining": 600.0, "conserve": False})

    def test_small_prompt_keeps_existing_context_shape(self):
        prompt = self.prompt()
        self.assertLess(len(prompt.encode()), adapters.MAX_TEXT)
        context = self.context(prompt)
        self.assertNotIn("context_compaction", context)
        self.assert_authoritative_content(context)

    def test_large_old_goals_are_explicit_excerpts_with_complete_current_task_and_files(self):
        previous = [{"id": f"old-{i}", "number": i, "goal": "历史说明" * 900,
                     "status": "stopped", "revision": i, "digest": str(i) * 64, "work_mode": "legacy"} for i in range(1, 6)]
        self.snapshot["runs"] = previous + self.snapshot["runs"]
        original = copy.deepcopy((self.snapshot, self.project))
        prompt = self.prompt()
        self.assertLess(len(prompt.encode()), adapters.MAX_TEXT)
        context = self.context(prompt)
        self.assertEqual(context["context_compaction"], {"previous_run_goal_excerpt_characters": 240})
        self.assertEqual(len(context["previous_runs"]), 5)
        for source, excerpt in zip(previous, context["previous_runs"]):
            self.assertEqual(excerpt["goal"], {"text": source["goal"][:240], "truncated": True,
                                             "omitted_characters": len(source["goal"]) - 240})
            self.assertEqual({key: value for key, value in excerpt.items() if key != "goal"},
                             {key: value for key, value in source.items() if key not in {"goal", "work_mode"}})
        self.assert_authoritative_content(context)
        self.assertEqual((self.snapshot, self.project), original)

    def test_remaining_overflow_references_duplicate_result_and_excerpts_old_chat_only(self):
        self.snapshot["events"] = [{"kind": "message", "sender": "claude", "text": "🙂" * 1500} for _ in range(12)]
        original = copy.deepcopy(self.snapshot)
        prompt = self.prompt()
        self.assertLess(len(prompt.encode()), adapters.MAX_TEXT)
        context = self.context(prompt)
        self.assertEqual(context["team"]["tasks"][0]["result"], {"reference": "task.result"})
        self.assertEqual(context["context_compaction"]["recent_message_limit"], 2)
        self.assertEqual(len(context["recent_visible_messages"]), 2)
        self.assertEqual(context["recent_visible_messages"][0]["omitted_characters"], 1260)
        self.assert_authoritative_content(context)
        self.assertEqual(self.snapshot, original)

    def test_indispensable_escaped_file_bytes_still_fail_closed(self):
        content = "\\" * 64000
        self.project["files"] = [{"path": "literal.py", "owner": "claude", "content": content,
                                  "sha256": hashlib.sha256(content.encode()).hexdigest()}]
        original = copy.deepcopy(self.project)
        self.assertLess(len(content.encode()), 65536)
        with self.assertRaises(RoomError) as raised:
            self.prompt()
        self.assertEqual(raised.exception.code, "context_too_large")
        self.assertEqual(self.project, original)
        self.assertEqual(adapters.MAX_TEXT, 128 * 1024)

    def test_large_source_plan_keeps_bound_current_work_and_explicit_handoff_truncation(self):
        current = self.snapshot["current_run"]
        current["work_mode"] = self.snapshot["runs"][0]["work_mode"] = "build"
        self.job["work_mode"] = self.snapshot["room"]["work_mode"] = "build"
        previous = {"id": "source-plan", "number": 5, "goal": "历史方案" * 900, "status": "ready_for_user",
                    "revision": 27, "digest": "b" * 64, "work_mode": "plan"}
        self.snapshot["runs"].insert(0, previous)
        result = "🫘" * 4000
        tasks = [{"id": f"plan-{index}", "title": f"Plan part {index}", "status": "accepted",
                  "result_version": 1, "result_digest": "c" * 64, "reviewer": "codex",
                  "result": result, "result_files": []} for index in range(8)]
        self.service.store.read_run = lambda *_: {"team": {"tasks": tasks}}
        original = copy.deepcopy((self.snapshot, self.project, tasks))
        # The current protocol plus exact source-plan envelope is larger than
        # the old response cap. Give this input fixture its own bounded budget.
        with patch.object(room_service, 'MAX_PROMPT_BYTES', 192 * 1024):
            prompt = self.prompt()
        self.assertLess(len(prompt.encode()), 192 * 1024)
        context = self.context(prompt)
        self.assert_authoritative_content(context)
        source = context["source_plan"]
        self.assertEqual((source["run_id"], source["revision"], source["digest"]), ("source-plan", 27, "b" * 64))
        self.assertTrue(source["truncated"])
        self.assertTrue(source["goal"]["truncated"])
        self.assertEqual(len(source["tasks"]), 8)
        self.assertLessEqual(sum(len(task["result"]["text"].encode()) for task in source["tasks"]), 24 * 1024)
        for task in source["tasks"]:
            self.assertTrue(task["result"]["truncated"])
            self.assertEqual(task["result"]["text"] + result[len(task["result"]["text"]):], result)
            self.assertEqual(task["result_sha256"], hashlib.sha256(result.encode()).hexdigest())
        self.assertEqual(context["previous_deliveries"], [])
        self.assertEqual((self.snapshot, self.project, tasks), original)


if __name__ == "__main__":
    unittest.main()
