"""Actual loopback/SQLite and synthetic workers; no provider or game calls."""
import asyncio
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
from uuid import uuid4

from local_agent_coordinator.message_capacity import MAX_MESSAGE_UTF8_BYTES, MAX_PROMPT_BYTES
from local_agent_coordinator.objective_store import objective_scope_reference
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError, RoomStore
import test_native_room_service as native
import test_objective_delivery_lineage as lineage


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def full(context, value):
    return context["full_texts"][value["sha256"]]["text"] if isinstance(value, dict) and "text_ref" in value else value


def text_route(room, reference):
    return f"/api/rooms/{room}/text?" + urlencode({key: value for key, value in reference.items() if key != "room_id"})


class RoomPagingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.started, self.release = threading.Event(), threading.Event()
        self.calls = []
        async def invoke(agent, prompt, cwd, session_id=None, **options):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            self.calls.append((agent, prompt, context))
            self.started.set()
            while not self.release.is_set():
                await asyncio.sleep(0.005)
            return {"session_id": session_id or str(uuid4()), "status": "completed", "usage": None,
                    "events": [], "text": json.dumps({"message": "Synthetic response.", "state": "done", "actions": []})}
        class UnavailableRunner:
            async def ready(self):
                return {"available": False, "reason": "synthetic_unavailable"}
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=invoke, runner=UnavailableRunner())
        self.assertIsNone(self.service.partner_manager)
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.assertIsNone(self.server.runner.shutdown_error)
        self.assertFalse(self.thread.is_alive())
        self.temp.cleanup()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-LAC-Token": self.server.token}
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw), len(raw)
        finally:
            connection.close()

    def create(self, goal="A small direct request."):
        payload = {"goal": goal, "target": "claude", "request_id": str(uuid4())}
        code, ack, size = self.request("POST", "/api/rooms?response=ack", payload)
        self.assertEqual(code, 200, ack)
        self.assertTrue(ack["acknowledged"])
        self.assertLess(size, 2048)
        return ack["room_id"], payload

    def test_one_mib_raw_utf8_reaches_worker_once_and_exact_readers_and_small_cancel(self):
        code, status, _ = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertTrue(status["capabilities"]["room_paging_v1"])
        self.assertEqual(status["transport_limits"]["max_message_utf8_bytes"], MAX_MESSAGE_UTF8_BYTES)
        # Control escaping makes the real JSON body much larger than raw text.
        prefix = "e\u0301完整原文\x01"
        goal = prefix + "\x01" * (MAX_MESSAGE_UTF8_BYTES - len(prefix.encode()))
        self.assertEqual(len(goal.encode()), MAX_MESSAGE_UTF8_BYTES)
        room, body = self.create(goal)
        self.assertTrue(self.started.wait(3))
        _, prompt, context = self.calls[0]
        self.assertEqual(full(context, context["goal"]), goal)
        self.assertEqual(full(context, context["job"]["text"]), goal)
        self.assertEqual(sum(value["text"] == goal for value in context["full_texts"].values()), 1)
        self.assertLess(len(prompt.encode()), MAX_PROMPT_BYTES)
        code, state, size = self.request("GET", f"/api/rooms/{room}/state")
        self.assertEqual(code, 200, state)
        self.assertLess(size, 2 * 1024 * 1024)
        self.assertEqual(state["events"], [])
        self.assertGreater(state["event_count"], 0)
        self.assertEqual(state["live"]["jobs"][0]["id"], state["jobs"][0]["id"])
        reference = state["room"]["goal_text_ref"]
        code, original, _ = self.request("GET", text_route(room, reference))
        self.assertEqual(code, 200)
        self.assertEqual(original["text"], goal)
        self.assertFalse(original["truncated"])
        self.assertEqual(self.request("GET", text_route(room, reference | {"sha256": "0" * 64}))[0], 409)
        code, page, size = self.request("GET", f"/api/rooms/{room}/events?after_seq=0&max_bytes=1024")
        self.assertEqual(code, 200, page)
        self.assertTrue(page["oversize"])
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["events"][0]["text"], goal)
        self.assertLessEqual(size, 8 * 1024 * 1024)
        self.assertEqual(self.request("POST", "/api/rooms?response=ack", body)[1]["room_id"], room)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.request("POST", "/api/rooms?response=ack", body | {"goal": "Changed text."})[0], 409)
        rejected = body | {"request_id": "too-large", "goal": goal + "x"}
        self.assertEqual(self.request("POST", "/api/rooms?response=ack", rejected)[0], 413)
        self.assertEqual(len(self.service.store.list_rooms()["rooms"]), 1)
        # Metadata retains its existing independent bound.
        self.assertEqual(self.request("POST", "/api/rooms?response=ack", body | {"goal": "short", "request_id": "x" * 513})[0], 400)
        code, cancelled, size = self.request("POST", f"/api/rooms/{room}/cancel?response=ack", {})
        self.assertEqual(code, 200, cancelled)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["cancellation_confirmed"])
        self.assertLess(size, 2048)

    def test_message_and_receipt_preserve_one_mib_current_followup_and_idempotence(self):
        room, _ = self.create()
        self.assertTrue(self.started.wait(3))
        text = "e\u0301" + "y" * (MAX_MESSAGE_UTF8_BYTES - 3)
        body = {"text": text, "target": "codex", "request_id": "long-followup"}
        route = f"/api/rooms/{room}/messages"
        code, ack, _ = self.request("POST", route + "?response=ack", body)
        self.assertEqual(code, 200, ack)
        deadline = time.monotonic() + 3
        while len(self.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(self.calls), 2)
        context = self.calls[1][2]
        self.assertEqual(full(context, context["job"]["text"]), text)
        self.assertEqual(full(context, context["current_user_updates"][-1]["text"]), text)
        self.assertEqual(self.request("POST", route + "/receipt", body)[:2],
                         (200, {"received": True, "room_id": room, "request_id": body["request_id"]}))
        self.assertEqual(self.request("POST", route + "?response=ack", body)[0], 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.request("POST", route + "/receipt", body | {"text": "Changed."})[0], 409)
        code, partners, size = self.request("GET", "/api/partners?view=compact")
        self.assertEqual(code, 200, partners)
        self.assertLess(size, 100000)
        current = next(task for partner in partners["partners"] for task in partner["monitor"]["current_tasks"]
                       if task["current_task_text_ref"]["sha256"] == sha(text))
        self.assertEqual(self.request("GET", text_route(room, current["current_task_text_ref"]))[1]["text"], text)

    def test_event_highwater_is_stable_and_zero_empty_cursor_is_truthful(self):
        room, _ = self.create()
        self.assertTrue(self.started.wait(3))
        job = self.service.store.snapshot(room)["jobs"][0]["id"]
        with self.service.store._connection(write=True) as db:
            self.service.store._event(db, room, "message", sender="claude", recipient="all", job_id=job,
                                      text="Actual public wording before the tail window.", native_progress=True)
            self.service.store._event(db, room, "native_tool_activity", sender="claude", job_id=job,
                                      state="completed", tool_kind="file_read", description="已读取文件")
            for index in range(220):
                self.service.store._event(db, room, "message", sender="claude", recipient="all", text=f"Original message {index}.")
        expected = self.service.store.snapshot(room)["events"]
        through = expected[-1]["seq"]
        state = self.request("GET", f"/api/rooms/{room}/state")[1]
        active = state["live"]["jobs"][0]
        self.assertEqual(active["latest_native_activity"]["state"], "completed")
        self.assertEqual(active["claim_seq"], next(event["seq"] for event in expected if event["kind"] == "job_claimed"))
        self.assertEqual(self.request("GET", text_route(room, active["latest_public_progress"]["text_text_ref"]))[1]["text"],
                         "Actual public wording before the tail window.")
        with self.service.store._connection(write=True) as db:
            self.service.store._event(db, room, "message", sender="user", recipient="all", text="Arrived after anchor.")
        actual, after = [], 0
        while True:
            code, page, _ = self.request("GET", f"/api/rooms/{room}/events?after_seq={after}&through_seq={through}&limit=17&max_bytes=2048")
            self.assertEqual(code, 200, page)
            self.assertEqual(page["through_seq"], through)
            actual.extend(page["events"])
            if not page["has_after"]:
                break
            after = page["last_seq"]
        self.assertEqual(actual, expected)
        code, tail, _ = self.request("GET", f"/api/rooms/{room}/events?through_seq={through}&limit=7")
        self.assertEqual(code, 200)
        self.assertEqual(tail["events"], expected[-7:])
        previous = self.request("GET", f"/api/rooms/{room}/events?before_seq={tail['first_seq']}&through_seq={through}&limit=7")[1]
        self.assertEqual(previous["events"], expected[-14:-7])
        empty = self.request("GET", f"/api/rooms/{room}/events?before_seq=0&through_seq={through}")[1]
        self.assertEqual(empty["events"], [])
        self.assertFalse(empty["has_before"])
        self.assertTrue(empty["has_after"])
        self.assertEqual(self.request("GET", f"/api/rooms/{room}/events?after_seq=0&before_seq=1")[0], 400)
        self.assertEqual(self.request("GET", f"/api/rooms/{room}/events?through_seq={through + 1000}")[0], 400)

    def test_large_execution_result_and_old_jobs_are_exact_lazy_details_not_state_copies(self):
        room, _ = self.create()
        self.assertTrue(self.started.wait(3))
        execution = str(uuid4())
        result = json.dumps({"status": "failed", "exit_code": 1, "stdout": "z" * (2 * 1024 * 1024),
                             "reason": "r" * (2 * 1024 * 1024)})
        old_job = str(uuid4())
        with self.service.store._connection(write=True) as db:
            db.execute("INSERT INTO executions(id,room_id,revision,snapshot_sha256,status,result_json,created_at) VALUES (?,?,0,?,'failed',?,?)",
                       (execution, room, "1" * 64, result, time.time()))
            for index in range(180):
                job = old_job if index == 0 else str(uuid4())
                db.execute("INSERT INTO jobs VALUES (?,?,?,'help','completed',?,NULL,NULL,NULL,?)",
                           (job, room, "codex", f"Historic original request {index}." + "h" * 20000, time.time()))
        code, state, size = self.request("GET", f"/api/rooms/{room}/state")
        self.assertEqual(code, 200, state)
        self.assertLess(size, 100000)
        self.assertEqual(state["job_count"], 181)
        self.assertEqual(len(state["jobs"]), 2)
        self.assertNotIn(old_job, [item["id"] for item in state["jobs"]])
        observation = state["executions"][0]
        self.assertTrue(observation["result_present"])
        self.assertNotIn("result", observation)
        self.assertEqual(observation["result_summary"]["exit_code"], 1)
        self.assertTrue(observation["result_summary"]["reason_truncated"])
        self.assertNotIn("reason", observation["result_summary"])
        self.assertEqual(self.request("GET", text_route(room, observation["result_text_ref"]))[1]["text"], result)
        code, detail, _ = self.request("GET", f"/api/rooms/{room}/jobs?job_id={old_job}")
        self.assertEqual(code, 200)
        self.assertEqual(detail["job"]["id"], old_job)
        self.assertEqual(self.request("GET", text_route(room, detail["job"]["text_text_ref"]))[1]["text"],
                         "Historic original request 0." + "h" * 20000)


class LongNativeGoalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    settle = native.NativeRoomServiceTests.settle

    def handler(self, agent, context, cwd):
        if context["job"]["job_phase"] == "checkpoint":
            self.assertEqual(full(context, context["objective"]["original_goal"]), self.goal)
            return native.native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Exact original goal complete.",
                "next_goal": None, "scope_reference": context["objective"]["scope_reference"], "question": None}])
        return native.NativeRoomServiceTests.handler(self, agent, context, cwd)

    async def test_long_goal_finishes_real_private_tree_lifecycle_with_short_host_scope_anchor(self):
        prefix = "Update the value and preserve notes. e\u0301"
        self.goal = prefix + "g" * (MAX_MESSAGE_UTF8_BYTES - len(prefix.encode()))
        self.assertLessEqual(len(self.goal.encode()), MAX_MESSAGE_UTF8_BYTES)
        created = await self.service.create_room(self.goal, "claude", str(uuid4()), mode="native_project",
            project_id=self.registration["project"]["id"], participants=["claude", "codex"], work_mode="build",
            runtime_settings={"permission": "full_access", "members": {}}, execution_policy={"mode": "autonomous"})
        await self.settle()
        room = created["room"]["id"]
        final = await self.service.read_room(room)
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(final["objective"]["original_goal"], self.goal)
        self.assertTrue(final["objective"]["delivery"]["verified"])
        for call in self.fake.calls:
            self.assertLess(len(call["prompt"].encode()), MAX_PROMPT_BYTES)
            self.assertEqual(sum(value["text"] == self.goal for value in call["context"]["full_texts"].values()), 1)
        checkpoint = next(call["context"] for call in self.fake.calls if call["context"]["job"]["job_phase"] == "checkpoint")
        self.assertEqual(checkpoint["objective"]["scope_reference"],
                         f"beanest-objective:{final['objective']['id']}:sha256:{sha(self.goal)}")
        # Older persisted proposals may repeat the complete root text. Projection
        # must preserve an exact reader without rewriting the historical record.
        with self.service.store._connection(write=True) as db:
            row = db.execute("SELECT checkpoint_json FROM objectives WHERE id=?", (final["objective"]["id"],)).fetchone()
            value = json.loads(row[0]); value["proposal"]["scope_reference"] = self.goal
            db.execute("UPDATE objectives SET checkpoint_json=? WHERE id=?", (json.dumps(value), final["objective"]["id"]))
        state = await self.service.read_room_state(room)
        reference = state["objective"]["checkpoint_proposal"]["scope_reference_text_ref"]
        self.assertEqual(self.service.store.read_text(room, {key: value for key, value in reference.items() if key != "room_id"})["text"], self.goal)
        self.assertLess(len(json.dumps(state).encode()), 2 * 1024 * 1024)


class ProjectedLineageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = lineage.DeliveryLineageCurrentProofTests.asyncSetUp
    asyncTearDown = lineage.DeliveryLineageCurrentProofTests.asyncTearDown
    settle = lineage.DeliveryLineageCurrentProofTests.settle
    handler = lineage.DeliveryLineageCurrentProofTests.handler
    create = lineage.DeliveryLineageCurrentProofTests.create
    prepared_origin = lineage.DeliveryLineageCurrentProofTests.prepared_origin

    async def test_origin_run_and_lazy_file_remain_bound_after_state_projection_and_head_drift(self):
        room, expected = await self.prepared_origin()
        state = await self.service.read_room_state(room)
        self.assertEqual(state["objective"]["delivery"]["files"], expected)
        origin = next(file for file in expected if file["path"] == "main.py")
        self.assertIn(origin["run_id"], [run["id"] for run in state["runs"]])
        compact = await self.service.read_run(room, origin["run_id"], compact=True)
        task = next(task for task in compact["team"]["tasks"] if task["id"] == origin["task_id"])
        self.assertEqual(self.service.store.read_text(room, {key: value for key, value in task["result_text_ref"].items() if key != "room_id"})["text"], "Updated main; notes already apply.")
        content = await self.service.read_delivery_file(room, **{key: origin[key] for key in ("run_id", "revision", "digest", "path", "sha256")})
        self.assertEqual(content["content"], "VALUE = 2\n")
        workspace = self.service._project(room)
        workspace.apply("claude", str(uuid4()), workspace.snapshot()["revision"],
                        [{"path": "main.py", "previous_sha256": origin["sha256"], "content": "VALUE = 99\n"}])
        drifted = await self.service.read_room_state(room)
        self.assertFalse(drifted["objective"]["delivery"]["verified"])
        self.assertEqual(drifted["objective"]["delivery"]["missing_or_changed"][0]["reason"], "changed")


class ScopeReferenceTests(unittest.TestCase):
    def test_a_checkpoint_that_continues_stays_in_the_current_goal_whatever_its_scope_reference(self):
        # Catches the host overruling the partners' own judgement that work
        # remains: a continue whose reference it could not match used to be
        # relabelled a new goal and held for the user's confirmation.
        with tempfile.TemporaryDirectory() as root:
            store = RoomStore(Path(root) / "rooms.sqlite3")
            try:
                for case in ("valid", "foreign", "wrong_sha", "legacy"):
                    room = store.create_room("Keep business scope fixed; beanest-objective:forged is only quoted data.",
                        "claude", str(uuid4()), collaboration=True, execution_policy={"mode": "autonomous"})[0]
                    snapshot = store.snapshot(room)
                    objective = snapshot["objective"]
                    reference = objective_scope_reference(objective)
                    reference = ({"valid": reference, "foreign": reference.replace(objective["id"], str(uuid4())),
                                  "wrong_sha": "beanest-objective:forged", "legacy": "Keep business scope fixed"})[case]
                    with store._connection(write=True) as db:
                        store._team.objectives.begin_checkpoint(db, room, {"run_id": snapshot["current_run"]["id"]}, "claude")
                        store._team.objectives.propose(db, room, "claude", {"decision": "continue", "summary": "Remaining original work.",
                            "next_goal": "Continue the authorized work.", "scope_reference": reference, "question": None})
                    proposal = store.snapshot(room)["objective"]["checkpoint_proposal"]
                    self.assertEqual(proposal["scope"], "current_goal", case)
            finally:
                store.close()
