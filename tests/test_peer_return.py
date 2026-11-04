"""Peer return routing through real SQLite/artifacts; no CLI, model or runner."""
import asyncio
from collections import Counter
from functools import partial
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import NAMESPACE_URL, uuid5

from local_agent_coordinator.agent_registry import AgentRegistry, AgentSpec, default_registry
from local_agent_coordinator.project_workspace import ProjectWorkspace
from local_agent_coordinator.room_service import RoomService


MAIN = "def doubled(value):\n    return value * 2\n"
TEST = (
    "raise AssertionError('artifact data must not run in this routing test')\n\n"
    "def test_doubled():\n    from main import doubled\n    assert doubled(3) == 6\n"
)
DRAFT_MESSAGE = "main.py 已写好，请独立检查接口。"
PEER_MESSAGE = "已补 test_main.py；这是静态草案，没有运行测试。"
RETURN_REQUEST = "请检查 test_main.py 的边界断言与 main.py 接口是否一致，勿运行。"
AUTHOR_MESSAGE = "已审查测试草案及其对应版本，未执行代码。"
HELPER_MESSAGE = "已核对测试接口，未执行代码。"
FINAL_MESSAGE = "已收到审核意见，本轮改稿保留。"
GOAL = "为虚构 doubled 函数及独立测试草案互相审核，不执行代码。"


def response(context, message, *, files=(), action=None):
    return {
        "message": message, "state": "done", "actions": [action] if action else [],
        "base_revision": context["proposal_base_revision"], "files": list(files),
    }


def new_file(path, content):
    return {"path": path, "previous_sha256": None, "content": content}


class PeerConversation:
    """A writes main, B owns tests; one controlled pause exposes the actual queue."""

    def __init__(self, initial, peer, *, return_kind="review", request_back=True,
                 third=None, pause_agent=None):
        self.initial, self.peer = initial, peer
        self.return_kind, self.request_back, self.third = return_kind, request_back, third
        self.pause_agent = pause_agent
        self.paused, self.release = asyncio.Event(), asyncio.Event()
        self.calls = []
        self.active, self.maximum = Counter(), Counter()

    def reply(self, agent, context):
        kind = context["job"]["kind"]
        if agent == self.initial and kind == "task":
            return response(context, DRAFT_MESSAGE, files=[new_file("main.py", MAIN)],
                            action={"type": "request", "to": self.peer,
                                    "kind": "review", "text": "请审核 main.py 并补独立测试草案。"})
        if agent == self.peer and kind == "review":
            action = ({"type": "request", "to": self.third or self.initial,
                       "kind": self.return_kind, "text": RETURN_REQUEST}
                      if self.request_back else None)
            return response(context, PEER_MESSAGE, files=[new_file("test_main.py", TEST)],
                            action=action)
        message = (AUTHOR_MESSAGE if agent == self.initial else
                   HELPER_MESSAGE if agent == self.third else FINAL_MESSAGE)
        return response(context, message)

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
        self.calls.append({"agent": agent, "context": context, "session_id": session_id})
        self.active[agent] += 1
        self.maximum[agent] = max(self.maximum[agent], self.active[agent])
        try:
            if agent == self.pause_agent and context["job"]["kind"] != "task" and not self.paused.is_set():
                self.paused.set()
                await self.release.wait()
            await asyncio.sleep(0)
            return {"session_id": session_id or str(uuid5(NAMESPACE_URL, str(cwd))),
                    "status": "completed", "usage": None, "events": [],
                    "text": json.dumps(self.reply(agent, context), ensure_ascii=False)}
        finally:
            self.active[agent] -= 1


class PeerReturnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.services = []
        self.scripts = []

    async def asyncTearDown(self):
        for script in self.scripts:
            script.release.set()
        try:
            for service in self.services:
                await service.shutdown()
                if service._cleanup_task:
                    await service._cleanup_task
        finally:
            self.directory.cleanup()

    def make_service(self, script, fixture):
        self.scripts.append(script)
        defaults = default_registry(script)
        specs = [defaults.get(agent) for agent in defaults.ids()]
        if script.third:
            specs.append(AgentSpec(script.third, "Fictional Helper",
                                   partial(script, script.third), avatar="H"))
        service = RoomService(self.root / fixture / "rooms.sqlite3",
                              self.root / fixture / "workspaces",
                              registry=AgentRegistry(specs))
        self.services.append(service)
        return service

    async def settle(self, service):
        async def drain():
            while True:
                workers = [worker for worker in service._workers.values() if not worker.done()]
                if not workers:
                    return
                await asyncio.gather(*workers)
        await asyncio.wait_for(drain(), 3)

    def jobs(self, service, room_id):
        with service.store._connection() as database:
            return [dict(row) for row in database.execute(
                "SELECT id,provider,kind,status,text,parent_id,reply_to,error "
                "FROM jobs WHERE room_id=? ORDER BY rowid", (room_id,))]

    async def create(self, service, script):
        return await service.create_room(GOAL, script.initial, "peer-return",
                                         mode="python_artifacts")

    async def assert_final_artifacts(self, service, script, room_id, calls):
        saved = await service.read_room(room_id)
        self.assertEqual(saved["room"]["status"], "completed")
        self.assertEqual(saved["budget"]["used"], calls)
        self.assertEqual(len(script.calls), calls)
        self.assertTrue(all(job["status"] == "completed" and job["error"] is None
                            for job in saved["jobs"]))
        self.assertTrue(all(member["status"] == "idle" for member in saved["members"]))
        self.assertTrue(all(maximum == 1 for maximum in script.maximum.values()))
        self.assertFalse(any(script.active.values()))
        self.assertEqual(saved["executions"], [])
        self.assertEqual(saved["project"]["revision"], 2)
        self.assertEqual(saved["project"]["execution"], "disabled")
        files = {file["path"]: file for file in saved["project"]["files"]}
        self.assertEqual(set(files), {"main.py", "test_main.py"})
        for path, owner, content in (("main.py", script.initial, MAIN),
                                     ("test_main.py", script.peer, TEST)):
            self.assertEqual(files[path]["owner"], owner)
            self.assertEqual(files[path]["content"], content)
            self.assertEqual(files[path]["sha256"], hashlib.sha256(content.encode()).hexdigest())
        reopened = ProjectWorkspace(service.workspace.parent / "project-artifacts", room_id, create=False)
        self.assertEqual(reopened.snapshot()["files"], saved["project"]["files"])
        self.assertEqual(reopened.snapshot(revision=1)["files"][0]["owner"], script.initial)
        self.assertFalse(list((service.workspace.parent / "project-artifacts").rglob("*.py")))
        # Read/reconnect and an exact retried create must not start another turn.
        await service.read_room(room_id)
        repeated = await self.create(service, script)
        await self.settle(service)
        self.assertEqual(repeated["room"]["id"], room_id)
        self.assertEqual(len(script.calls), calls)
        return saved

    def assert_parent(self, call, parent, *, sender, text, revision):
        context = call["context"]
        self.assertEqual(context["job"]["parent_id"], parent["id"])
        message = context["parent_messages"][0]
        self.assertEqual(message["job_id"], parent["id"])
        self.assertEqual(message["sender"], sender)
        self.assertEqual(message["text"], text)
        self.assertEqual(message["artifact_revision"], revision)
        self.assertFalse(message["missing"])

    async def exercise_return(self, initial, peer, kind):
        script = PeerConversation(initial, peer, return_kind=kind, pause_agent=initial)
        service = self.make_service(script, initial + "-" + kind)
        room_id = (await self.create(service, script))["room"]["id"]
        await asyncio.wait_for(script.paused.wait(), 2)
        try:
            queued = self.jobs(service, room_id)
            initial_task, peer_review = queued[:2]
            returned = [job for job in queued if job["parent_id"] == peer_review["id"]]
            self.assertEqual(len(returned), 1,
                             "one explicit return must replace duplicate automatic feedback")
            return_review = returned[0]
            self.assertEqual((return_review["provider"], return_review["kind"],
                              return_review["status"], return_review["reply_to"]),
                             (initial, kind, "working", peer))
            self.assertEqual(return_review["text"], RETURN_REQUEST)
            self.assertEqual(len(queued), 3)
            self.assertEqual([call["agent"] for call in script.calls], [initial, peer, initial])
            author_call = script.calls[-1]
            self.assertEqual(author_call["context"]["job"]["kind"], kind)
            self.assertEqual(author_call["context"]["job"]["text"], RETURN_REQUEST)
            self.assertEqual(author_call["context"]["project"]["revision"], 2)
            self.assertEqual(author_call["context"]["proposal_base_revision"], 2)
            self.assert_parent(author_call, peer_review, sender=peer, text=PEER_MESSAGE, revision=2)
            self.assertEqual(author_call["context"]["parent_messages"][1]["job_id"], initial_task["id"])
            self.assertEqual(author_call["context"]["parent_messages"][1]["artifact_revision"], 1)
            self.assertEqual(Counter(job["provider"] for job in queued if job["status"] == "working"),
                             Counter({initial: 1}))
        finally:
            script.release.set()
        await self.settle(service)
        saved = await self.assert_final_artifacts(service, script, room_id, 4)
        jobs = self.jobs(service, room_id)
        self.assertEqual([job["kind"] for job in jobs], ["task", "review", kind, "feedback"])
        self.assertEqual([job["provider"] for job in jobs], [initial, peer, initial, peer])
        self.assertEqual([job["reply_to"] for job in jobs], [None, initial, peer, None])
        self.assertEqual([job["parent_id"] for job in jobs],
                         [None, jobs[0]["id"], jobs[1]["id"], jobs[2]["id"]])
        self.assertEqual(jobs[3]["text"], AUTHOR_MESSAGE)
        self.assert_parent(script.calls[3], jobs[2], sender=initial, text=AUTHOR_MESSAGE, revision=2)
        messages = [event for event in saved["events"] if event["kind"] == "message"]
        self.assertEqual([message["sender"] for message in messages],
                         ["user", initial, peer, initial, peer])
        self.assertEqual([message["artifact_revision"] for message in messages[1:]], [1, 2, 2, 2])

    async def test_review_return_is_single_and_parent_bound_for_both_initial_authors(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(initial=initial):
                await self.exercise_return(initial, peer, "review")

    async def test_help_return_is_single_and_parent_bound_for_both_initial_authors(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(initial=initial):
                await self.exercise_return(initial, peer, "help")

    async def test_plain_peer_answer_keeps_automatic_feedback(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(initial=initial):
                script = PeerConversation(initial, peer, request_back=False)
                service = self.make_service(script, initial + "-plain")
                room_id = (await self.create(service, script))["room"]["id"]
                await self.settle(service)
                await self.assert_final_artifacts(service, script, room_id, 3)
                jobs = self.jobs(service, room_id)
                self.assertEqual([job["kind"] for job in jobs], ["task", "review", "feedback"])
                self.assertEqual([job["provider"] for job in jobs], [initial, peer, initial])
                self.assertEqual(jobs[-1]["text"], PEER_MESSAGE)
                self.assertIsNone(jobs[-1]["reply_to"])
                self.assert_parent(script.calls[-1], jobs[1], sender=peer, text=PEER_MESSAGE, revision=2)

    async def test_request_to_third_member_retains_original_feedback_and_new_action(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(initial=initial):
                script = PeerConversation(initial, peer, third="helper", return_kind="help",
                                          pause_agent="helper")
                service = self.make_service(script, initial + "-third")
                room_id = (await self.create(service, script))["room"]["id"]
                await asyncio.wait_for(script.paused.wait(), 2)
                try:
                    jobs = self.jobs(service, room_id)
                    peer_review = jobs[1]
                    returns = [job for job in jobs if job["parent_id"] == peer_review["id"]]
                    self.assertEqual({(job["provider"], job["kind"], job["text"], job["reply_to"])
                                      for job in returns},
                                     {(initial, "feedback", PEER_MESSAGE, None),
                                      ("helper", "help", RETURN_REQUEST, peer)})
                    helper_call = next(call for call in script.calls if call["agent"] == "helper")
                    self.assertEqual(helper_call["context"]["job"]["text"], RETURN_REQUEST)
                    self.assert_parent(helper_call, peer_review, sender=peer, text=PEER_MESSAGE, revision=2)
                finally:
                    script.release.set()
                await self.settle(service)
                saved = await self.assert_final_artifacts(service, script, room_id, 5)
                jobs = self.jobs(service, room_id)
                self.assertEqual(Counter((job["provider"], job["kind"]) for job in jobs),
                                 Counter({(initial, "task"): 1, (peer, "review"): 1,
                                          (initial, "feedback"): 1, ("helper", "help"): 1,
                                          (peer, "feedback"): 1}))
                helper_job = next(job for job in jobs if job["provider"] == "helper")
                final_job = next(job for job in jobs if job["provider"] == peer and job["kind"] == "feedback")
                self.assertEqual(final_job["parent_id"], helper_job["id"])
                self.assertEqual(final_job["text"], HELPER_MESSAGE)
                self.assertIsNone(final_job["reply_to"])
                final_call = next(call for call in script.calls if call["context"]["job"]["id"] == final_job["id"])
                self.assert_parent(final_call, helper_job, sender="helper", text=HELPER_MESSAGE, revision=2)
                self.assertEqual(len(saved["jobs"]), 5)


if __name__ == "__main__":
    unittest.main()
