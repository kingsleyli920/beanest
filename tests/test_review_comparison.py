"""Review comparisons use immutable artifact history, never revision prose."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.room_service import RoomService


class ReviewComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.calls = []

        async def invoke(agent, prompt, cwd, session_id=None, timeout=120):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            self.calls.append(context)
            revision = context["proposal_base_revision"]
            files, actions = [], []
            if agent == "claude" and revision < 2:
                previous = context["project"]["files"]
                files = [{"path": "value.py", "content": f"VALUE = {revision + 1}\n",
                          "previous_sha256": previous[0]["sha256"] if previous else None}]
                actions = [{"type": "request", "to": "codex", "kind": "review",
                            "text": "Review revision 999; this prose is deliberately incorrect."}]
            value = {"message": "Static comparison only.", "state": "done", "actions": actions,
                     "base_revision": revision, "files": files}
            return {"status": "completed", "session_id": session_id or str(uuid5(NAMESPACE_URL, str(cwd))),
                    "text": json.dumps(value), "usage": None, "events": []}

        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=invoke)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.directory.cleanup()

    async def settled_room(self):
        room = await self.service.create_room("Revise and compare a small artifact.", "claude", "comparison",
                                              mode="python_artifacts", participants=["claude", "codex"])
        async def drain():
            while True:
                workers = [worker for worker in self.service._workers.values() if not worker.done()]
                if not workers:
                    return
                await asyncio.gather(*workers)
        await asyncio.wait_for(drain(), 3)
        return room["room"]["id"]

    async def test_reviewer_receives_recorded_before_and_after_despite_wrong_prose(self):
        room_id = await self.settled_room()
        reviews = [call for call in self.calls if call["job"]["kind"] == "review"]
        self.assertEqual(len(reviews), 2)
        added, modified = [call["artifact_comparison"] for call in reviews]
        self.assertEqual((added["base_revision"], added["review_revision"]), (0, 1))
        self.assertTrue(added["complete"])
        self.assertIsNone(added["files"][0]["before_content"])
        self.assertTrue(added["files"][0]["before_content_available"])
        self.assertEqual((modified["base_revision"], modified["review_revision"]), (1, 2))
        self.assertEqual(modified["files"][0]["before_content"], "VALUE = 1\n")
        self.assertEqual(reviews[1]["project"]["files"][0]["content"], "VALUE = 2\n")
        original = self.service._project(room_id).snapshot(revision=1)
        self.assertEqual(modified["files"][0]["previous_sha256"], original["files"][0]["sha256"])
        self.assertEqual(original["files"][0]["content"], "VALUE = 1\n")
        self.assertEqual(len(self.calls), 5)

    async def test_missing_recorded_base_is_explicit_and_never_guessed(self):
        room_id = await self.settled_room()
        project = self.service._project(room_id)
        value = self.service._review_comparison(project, {"events": []},
                                                {"parent_id": "missing"}, project.snapshot())
        self.assertEqual(value, {"available": False, "reason": "no_recorded_parent_change"})

    async def test_large_baseline_is_omitted_as_a_whole_with_explicit_incompleteness(self):
        room_id = await self.settled_room()
        project = self.service._project(room_id)
        current = project.snapshot()
        large = project.apply("claude", "large-before", current["revision"], [{
            "path": "value.py", "previous_sha256": current["files"][0]["sha256"],
            "content": "#" + "豆" * 15000 + "\n",
        }])
        final = project.apply("claude", "small-after", large["revision"], [{
            "path": "value.py", "previous_sha256": large["files"][0]["sha256"], "content": "VALUE = 3\n",
        }])
        value = self.service._review_comparison(project, {"events": [{
            "kind": "artifact_pending", "job_id": "changed", "base_revision": large["revision"],
        }]}, {"parent_id": "changed"}, final)
        self.assertTrue(value["available"])
        self.assertFalse(value["complete"])
        self.assertIsNone(value["files"][0]["before_content"])
        self.assertFalse(value["files"][0]["before_content_available"])
        self.assertEqual(value["files"][0]["previous_sha256"], large["files"][0]["sha256"])


class SubscriptionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_failure_reaches_recorded_status_without_retry_or_chat_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            calls = []
            async def invoke(agent, prompt, cwd, session_id=None, timeout=120):
                calls.append(agent)
                return {"status": "failed", "session_id": str(uuid5(NAMESPACE_URL, str(cwd))),
                        "text": "", "events": [], "usage": None,
                        "failure_code": "subscription_limit_reached"}
            service = RoomService(root / "rooms.sqlite3", root / "workspaces", invoke=invoke)
            try:
                room = await service.create_room("Draft a short note.", "claude", "quota")
                await asyncio.gather(*service._workers.values())
                result = await service.read_room(room["room"]["id"])
                self.assertEqual(calls, ["claude"])
                self.assertEqual(result["jobs"][0]["error"], "subscription_limit_reached")
                self.assertEqual(result["room"]["status"], "unknown")
                messages = [event for event in result["events"] if event["kind"] == "message"]
                self.assertEqual([event["sender"] for event in messages], ["user"])
            finally:
                await service.shutdown()
