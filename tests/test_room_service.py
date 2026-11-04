import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from local_agent_coordinator.room_service import RoomError, RoomService, _response
from local_agent_coordinator.room_store import RoomStore
from local_agent_coordinator.agent_registry import AgentRegistry, AgentSpec, default_registry


IDENTITIES = {"claude": "11111111-1111-4111-8111-111111111111",
              "codex": "22222222-2222-4222-8222-222222222222"}


def answer(message="Done", *, action=None, state="done", **overrides):
    return {"text": json.dumps({"message": message, "actions": [action] if action else [], "state": state}),
            "status": "completed", "usage": None, "events": [], **overrides}


class FakeInvoke:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.active = {provider: 0 for provider in IDENTITIES}
        self.maximum = dict(self.active)
        self.gate = None
        self.cleaned = 0

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        self.calls.append({"agent": agent, "prompt": prompt, "cwd": cwd,
                           "session_id": session_id, "timeout": timeout})
        self.active[agent] += 1
        self.maximum[agent] = max(self.maximum[agent], self.active[agent])
        try:
            if self.gate:
                await self.gate.wait()
            await asyncio.sleep(0)
            responses = self.responses.get(agent, [])
            result = responses.pop(0) if responses else answer()
            if isinstance(result, BaseException):
                raise result
            return {"session_id": IDENTITIES[agent], **result}
        finally:
            self.active[agent] -= 1
            self.cleaned += 1


class RoomServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_presentation_allows_preamble_but_not_trailing_prose_or_multiple_values(self):
        raw = json.dumps({'message': 'Visible review', 'actions': [], 'state': 'done'})
        for wrapper in ('```json\n{}\n```', '```\n{}\n```', '```JSON\r\n{}\r\n```'):
            self.assertEqual(_response(wrapper.format(raw), 'claude')['message'], 'Visible review')
        for wrapped in ('Before\n' + raw, 'Before\n```json\n' + raw + '\n```'):
            self.assertEqual(_response(wrapped, 'claude')['message'], 'Visible review')
        for invalid in ('Before\n```json\n' + raw + '\n```\nAfter',
                        '```json\n' + raw + '\n```\nAfter', raw + raw):
            with self.assertRaises(RoomError):
                _response(invalid, 'claude')

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.fake = FakeInvoke()
        self.now = 1000.0
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   invoke=self.fake, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.directory.cleanup()

    async def settle(self):
        async def wait():
            while True:
                tasks = [task for task in self.service._workers.values() if not task.done()]
                if not tasks:
                    return
                await asyncio.gather(*tasks)
        await asyncio.wait_for(wait(), 2)

    async def create(self, target="claude"):
        return await self.service.create_room("A shared text goal", target, str(uuid4()))

    async def test_peer_request_answer_and_feedback_is_symmetric_and_bounded(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            # Each room needs dedicated real identities, represented by a fresh fake result.
            identities = {agent: str(uuid4()) for agent in IDENTITIES}
            self.fake.responses[initial] = [
                answer("Please review", action={"type": "request", "to": peer,
                                                "kind": "review", "text": "Review this same goal"},
                       session_id=identities[initial]),
                answer("Accepted peer feedback", session_id=identities[initial]),
            ]
            self.fake.responses[peer] = [answer("Reviewed", session_id=identities[peer])]
            snapshot = await self.create(initial)
            await self.settle()
            snapshot = await self.service.read_room(snapshot["room"]["id"])
            self.assertEqual([job["kind"] for job in snapshot["jobs"]], ["task", "review", "feedback"])
            self.assertEqual(snapshot["budget"]["used"], 3)
            self.assertEqual(snapshot["room"]["status"], "completed")
            messages = [event for event in snapshot["events"] if event["kind"] == "message"]
            self.assertEqual([event["sender"] for event in messages], ["user", initial, peer, initial])
            self.assertEqual(self.fake.calls[-1]["session_id"], identities[initial])
            self.assertIn('"kind": "feedback"', self.fake.calls[-1]["prompt"])
            self.assertEqual(len({event["seq"] for event in snapshot["events"]}), len(snapshot["events"]))
            routed = [event for event in snapshot["events"] if event["kind"] == "job_created"]
            self.assertEqual([event["sender"] for event in routed], ["user", initial, peer])

    async def test_both_start_and_queued_followup_keep_each_member_single_flight(self):
        self.fake.gate = asyncio.Event()
        snapshot = await self.create("both")
        room_id = snapshot["room"]["id"]
        await self.service.send_message(room_id, "Extra detail", "claude", "followup")
        await asyncio.sleep(0)
        self.assertEqual(len(self.fake.calls), 2)
        self.fake.gate.set()
        await self.settle()
        self.assertEqual(self.fake.maximum, {"claude": 1, "codex": 1})
        self.assertEqual(len(self.fake.calls), 3)
        for call in self.fake.calls:
            self.assertEqual(call["cwd"], self.root / "workspaces" / room_id / call["agent"])
            self.assertLessEqual(call["timeout"], 120)

    async def test_task_review_feedback_instructions_are_distinct_and_parent_bound(self):
        self.fake.responses["claude"] = [
            answer("Draft version", action={"type": "request", "to": "codex", "kind": "review", "text": "Review"}),
            answer("Revised result"),
        ]
        self.fake.responses["codex"] = [answer("Review findings")]
        room = await self.create()
        await self.settle()
        calls = self.fake.calls
        self.assertIn("then request ONE independent review", calls[0]["prompt"])
        self.assertIn("user explicitly requests a direct answer/no collaboration", calls[0]["prompt"])
        self.assertIn("Answer the assigned review/help request directly", calls[1]["prompt"])
        self.assertIn("Evaluate the returned peer feedback", calls[2]["prompt"])
        self.assertIn('"text": "Draft version"', calls[1]["prompt"])
        self.assertIn('"text": "Review findings"', calls[2]["prompt"])
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual(len(snapshot["jobs"]), 3)

    async def test_parent_draft_survives_recent_message_eviction_and_marks_truncation(self):
        room_id, _ = self.service.store.create_room("Goal", "claude", "draft-room")
        draft = self.service.store.claim(room_id, "claude")
        self.service.store.finish_job(
            draft["id"], session_id=IDENTITIES["claude"], message="DRAFT-" + "x" * 2500,
            action={"to": "codex", "kind": "review", "text": "Review that exact draft"})
        review = self.service.store.claim(room_id, "codex")
        with self.service.store._connection(write=True) as db:
            for number in range(10):
                self.service.store._event(db, room_id, "message", sender="user", recipient="both",
                                          text=f"Interleaved clarification {number}", job_id=None)
        prompt = self.service._prompt(review)
        context = json.loads(prompt[prompt.index('{"goal":'):])
        self.assertFalse(any("DRAFT-" in event["text"] for event in context["recent_visible_messages"]))
        parent = context["parent_messages"][0]
        self.assertEqual(parent["job_id"], draft["id"])
        self.assertEqual(parent["sender"], "claude")
        self.assertTrue(parent["truncated"])
        self.assertEqual(parent["omitted_characters"], 506)
        self.assertTrue(parent["text"].startswith("DRAFT-"))
        self.assertIn("do not claim full review of omitted text", prompt)

    async def test_create_and_message_retries_do_not_dispatch_twice(self):
        first = await self.service.create_room("Goal", "claude", "stable")
        second = await self.service.create_room("Goal", "claude", "stable")
        self.assertEqual(first["room"]["id"], second["room"]["id"])
        await self.settle()
        for _ in range(2):
            await self.service.send_message(first["room"]["id"], "Detail", "claude", "detail")
        await self.settle()
        self.assertEqual(len(self.fake.calls), 2)

    async def test_changed_identity_is_blocked_and_never_adopted(self):
        snapshot = await self.create()
        await self.settle()
        room_id = snapshot["room"]["id"]
        self.fake.responses["claude"] = [answer("Visible late result", session_id=str(uuid4()))]
        await self.service.send_message(room_id, "Followup", "claude", "second")
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        member = snapshot["members"][0]
        self.assertEqual(member["session_id"], IDENTITIES["claude"])
        self.assertTrue(member["identity_uncertain"])
        self.assertEqual(snapshot["jobs"][-1]["error"], "session_identity_changed")
        with self.assertRaises(RoomError):
            await self.service.send_message(room_id, "Retry", "claude", "third")
        self.assertEqual(len(self.fake.calls), 2)

    async def test_invalid_response_and_action_preserve_visible_text_without_routing(self):
        self.fake.responses["claude"] = [answer(text="Visible non-JSON answer")]
        snapshot = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(snapshot["room"]["id"])
        self.assertEqual(snapshot["jobs"][0]["error"], "invalid_agent_response")
        self.assertTrue(any(event.get("text") == "Visible non-JSON answer" for event in snapshot["events"]))
        self.fake.responses["codex"] = [answer("No spoof", action={"type": "request", "to": "codex",
                                                                   "kind": "help", "text": "Self call"})]
        other = await self.create("codex")
        await self.settle()
        other = await self.service.read_room(other["room"]["id"])
        self.assertEqual(other["jobs"][0]["error"], "invalid_agent_action")
        self.assertEqual(len(other["jobs"]), 1)

    async def test_empty_output_is_failed_and_working_without_action_needs_user(self):
        self.fake.responses["claude"] = [answer(text="")]
        room = await self.create()
        await self.settle()
        self.assertEqual((await self.service.read_room(room["room"]["id"]))["room"]["status"], "failed")
        self.fake.responses["codex"] = [answer("Still thinking", state="working")]
        other = await self.create("codex")
        await self.settle()
        snapshot = await self.service.read_room(other["room"]["id"])
        self.assertEqual(snapshot["room"]["status"], "needs_user")
        self.assertTrue(any(event.get("code") == "working_without_next_step" for event in snapshot["events"]))

    async def test_unchanged_evidence_stops_peer_ping_pong_without_a_total_call_cap(self):
        for agent in IDENTITIES:
            peer = "codex" if agent == "claude" else "claude"
            self.fake.responses[agent] = [answer("Ask peer", action={"type": "request", "to": peer,
                                                                   "kind": "help", "text": "Help"}) for _ in range(8)]
        room = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertLess(len(self.fake.calls), 8)
        self.assertEqual(snapshot["budget"]["used"], len(self.fake.calls))
        self.assertIsNone(snapshot["budget"]["limit"])
        self.assertEqual(snapshot["room"]["status"], "needs_user")
        self.assertTrue(any(event.get("code") == "no_progress" for event in snapshot["events"]))

    async def test_cancel_awaits_own_invocation_cleanup_and_preserves_other_room(self):
        self.fake.gate = asyncio.Event()
        first = await self.create("claude")
        other = await self.create("codex")
        await asyncio.sleep(0)
        snapshot = await self.service.cancel_room(first["room"]["id"])
        self.assertTrue(snapshot["cancellation_confirmed"])
        self.assertEqual(snapshot["room"]["status"], "cancelled")
        self.assertEqual(self.fake.active["claude"], 0)
        self.assertEqual(self.fake.active["codex"], 1)
        self.fake.gate.set()
        await self.settle()
        self.assertEqual((await self.service.read_room(other["room"]["id"]))["room"]["status"], "completed")

    async def test_old_default_timeout_does_not_stop_or_retry_inflight_call(self):
        self.fake.gate = asyncio.Event()
        room = await self.create()
        await asyncio.sleep(0)
        self.now += 901
        await self.service.expire_tasks()
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual(snapshot["room"]["status"], "running")
        self.assertEqual(self.fake.active["claude"], 1)
        self.assertEqual(len(self.fake.calls), 1)
        stopped = await self.service.cancel_room(room["room"]["id"])
        self.assertEqual(stopped["room"]["status"], "cancelled")
        self.assertEqual(self.fake.active["claude"], 0)
        self.assertEqual(len(self.fake.calls), 1)

    async def test_usage_is_latest_provider_snapshot_and_errors_are_safe(self):
        self.fake.responses["claude"] = [answer(usage={"input_tokens": 12, "secret": "private"}),
                                          answer(usage={"input_tokens": 7})]
        room = await self.create()
        room_id = room["room"]["id"]
        await self.settle()
        await self.service.send_message(room_id, "More", "claude", "more")
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        self.assertEqual(snapshot["members"][0]["usage"]["values"], {"input_tokens": 7})
        self.assertEqual(snapshot["members"][0]["usage"]["scope"], "provider_reported_latest_result")
        self.assertNotIn("secret", json.dumps(snapshot))
        self.fake.responses["codex"] = [RuntimeError("Bearer private-provider-detail")]
        other = await self.create("codex")
        await self.settle()
        snapshot = await self.service.read_room(other["room"]["id"])
        self.assertEqual(snapshot["jobs"][0]["error"], "invoke_failed")
        self.assertNotIn("private-provider-detail", json.dumps(snapshot))
        self.assertEqual(snapshot["room"]["status"], "unknown")

    async def test_status_and_restart_cached_reads_do_not_invoke(self):
        self.assertEqual((await self.service.status())["app"], "local-agent-rooms")
        self.assertEqual(self.fake.calls, [])
        room = await self.create()
        await self.settle()
        await self.service.shutdown()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=self.fake)
        before = len(self.fake.calls)
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual(snapshot["room"]["status"], "completed")
        self.assertEqual(len(self.fake.calls), before)

    async def test_adapter_incomplete_failure_is_uncertain_and_not_retried(self):
        self.fake.responses["claude"] = [answer("Visible partial answer", status="failed",
                                                failure_code="protocol_turn_incomplete")]
        room = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertTrue(snapshot["members"][0]["identity_uncertain"])
        self.assertEqual(snapshot["jobs"][0]["status"], "unknown")
        with self.assertRaises(RoomError):
            await self.service.send_message(room["room"]["id"], "Try again", "claude", "again")
        self.assertEqual(len(self.fake.calls), 1)

    async def test_unexpected_adapter_interruption_is_durable_unknown(self):
        self.fake.responses["claude"] = [asyncio.CancelledError()]
        room = await self.create()
        await asyncio.gather(*self.service._workers.values(), return_exceptions=True)
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual(snapshot["jobs"][0]["error"], "invoke_interrupted")
        self.assertEqual(snapshot["room"]["status"], "unknown")
        self.assertTrue(snapshot["members"][0]["identity_uncertain"])

    async def test_real_wait_timeout_cleans_up_without_dense_polling_or_retry(self):
        await self.service.shutdown()
        self.fake.gate = asyncio.Event()
        self.service = RoomService(self.root / "timeout.sqlite3", self.root / "workspaces",
                                   invoke=self.fake, call_timeout=0.01)
        room = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual(snapshot["jobs"][0]["status"], "unknown")
        self.assertEqual(snapshot["jobs"][0]["error"], "deadline_exceeded")
        self.assertEqual(self.fake.active["claude"], 0)
        self.assertEqual(len(self.fake.calls), 1)

    async def test_explicit_followup_can_resolve_a_completed_protocol_error(self):
        self.fake.responses["claude"] = [answer(text="Bad JSON"), answer("Corrected")]
        room = await self.create()
        room_id = room["room"]["id"]
        await self.settle()
        await self.service.send_message(room_id, "Use the requested JSON", "claude", "correction")
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        self.assertEqual(snapshot["room"]["status"], "completed")
        self.assertEqual(snapshot["jobs"][0]["error"], "invalid_agent_response")
        self.assertEqual(snapshot["jobs"][1]["status"], "completed")

    async def test_unconfirmed_cleanup_keeps_instance_lock_until_invocation_finishes(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def stubborn(*args, **kwargs):
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return {"session_id": IDENTITIES["claude"], **answer("Late")}

        async def immediate_wait(tasks, timeout):
            return set(), set(tasks)

        self.service.invoke = stubborn
        room = await self.create()
        await asyncio.wait_for(started.wait(), 1)
        try:
            with patch("local_agent_coordinator.room_service.asyncio.wait", immediate_wait):
                snapshot = await self.service.cancel_room(room["room"]["id"])
                self.assertFalse(snapshot["cancellation_confirmed"])
                self.assertEqual(snapshot["room"]["status"], "unknown")
                with self.assertRaises(RoomError) as error:
                    await self.service.shutdown()
                self.assertEqual(error.exception.code, "cleanup_unconfirmed")
            with self.assertRaises(RoomError) as error:
                RoomStore(self.root / "rooms.sqlite3")
            self.assertEqual(error.exception.code, "runtime_in_use")
        finally:
            release.set()
            await asyncio.gather(*self.service._workers.values())
            if self.service._cleanup_task:
                await self.service._cleanup_task
        replacement = RoomStore(self.root / "rooms.sqlite3")
        try:
            snapshot = replacement.snapshot(room["room"]["id"])
            self.assertFalse(snapshot["cancellation_confirmed"])
            self.assertFalse(any(event.get("text") == "Late" for event in snapshot["events"]))
        finally:
            replacement.close()

    async def test_third_agent_uses_its_identity_usage_and_symmetric_peer_routing(self):
        await self.service.shutdown()
        calls = []

        async def helper(prompt, cwd, session_id=None, timeout=120):
            context = json.loads(prompt.split("CONTEXT DATA:\n", 1)[1])
            calls.append({"prompt": prompt, "context": context, "cwd": cwd, "session_id": session_id})
            action = ({"type": "request", "to": "codex", "kind": "review", "text": "Review my draft"}
                      if context["job"]["kind"] == "task" else None)
            return answer("Helper draft" if action else "Helper revised", action=action,
                          session_id="helper-session:" + cwd.parent.name,
                          usage={"units": 3, "input_tokens": 999, "secret": "private"})

        defaults = default_registry(self.fake)
        registry = AgentRegistry([*(defaults.get(agent_id) for agent_id in defaults.ids()),
                                  AgentSpec("helper", "Local Helper", helper, usage_source="fixture_helper",
                                            usage_fields=("units",), avatar="H")])
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", registry=registry,
                                   clock=lambda: self.now)
        status = await self.service.status()
        self.assertEqual(status["providers"], ["claude", "codex", "helper"])
        self.assertEqual(status["agents"][-1]["avatar"], "H")
        room = await self.create("helper")
        await self.settle()
        snapshot = await self.service.read_room(room["room"]["id"])
        self.assertEqual([job["provider"] for job in snapshot["jobs"]], ["helper", "codex", "helper"])
        self.assertEqual([job["kind"] for job in snapshot["jobs"]], ["task", "review", "feedback"])
        member = next(member for member in snapshot["members"] if member["id"] == "helper")
        self.assertEqual(member["name"], "Local Helper")
        self.assertTrue(member["session_id"].startswith("helper-session:"))
        self.assertEqual(calls[1]["session_id"], member["session_id"])
        self.assertEqual(member["usage"]["source"], "fixture_helper")
        self.assertEqual(member["usage"]["values"], {"units": 3})
        self.assertNotIn("private", json.dumps(snapshot))
        self.assertEqual({member["id"] for member in calls[0]["context"]["members"]},
                         {"claude", "codex", "helper"})
        self.assertIn('"sender": "helper"', self.fake.calls[-1]["prompt"])
        self.assertEqual(snapshot["room"]["status"], "completed")

    async def test_stopped_adapter_is_readable_and_cached_but_never_rerouted(self):
        room = await self.service.create_room("Goal", "claude", "original-create")
        room_id = room["room"]["id"]
        await self.settle()
        await self.service.send_message(room_id, "Original followup", "claude", "original-followup")
        await self.settle()
        await self.service.shutdown()
        calls = len(self.fake.calls)
        codex_only = AgentRegistry([default_registry(self.fake).get("codex")])
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", registry=codex_only)
        self.assertEqual((await self.service.read_room(room_id))["members"][0]["name"], "Claude Code")
        self.assertEqual((await self.service.create_room("Goal", "claude", "original-create"))["room"]["id"], room_id)
        await self.service.send_message(room_id, "Original followup", "claude", "original-followup")
        with self.assertRaises(RoomError) as error:
            await self.service.send_message(room_id, "No substitute", "claude", "new-followup")
        self.assertEqual(error.exception.code, "agent_unavailable")
        self.assertEqual(len(self.fake.calls), calls)

    async def test_registry_and_legacy_invoke_cannot_mix_or_normalize_unsafe_sessions(self):
        with self.assertRaises(RoomError) as error:
            RoomService(self.root / "invalid.sqlite3", self.root / "workspaces",
                        invoke=self.fake, registry=default_registry())
        self.assertEqual(error.exception.code, "invalid_configuration")
        self.assertFalse((self.root / "invalid.sqlite3").exists())
        await self.service.shutdown()

        async def unsafe(*args, **kwargs):
            return answer("Visible result", session_id="anything")

        registry = AgentRegistry([AgentSpec("helper", "Helper", unsafe,
                                             normalize_session_id=lambda _: "../unsafe")])
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", registry=registry,
                                   clock=lambda: self.now)
        room = await self.create("helper")
        await self.settle()
        snapshot = await self.service.read_room(room["room"]["id"])
        member = next(member for member in snapshot["members"] if member["id"] == "helper")
        self.assertIsNone(member["session_id"])
        self.assertTrue(member["identity_uncertain"])
        self.assertEqual(snapshot["jobs"][0]["error"], "session_identity_unknown")

    async def test_new_registry_agent_cannot_be_summoned_into_a_historical_room(self):
        room = await self.create()
        room_id = room["room"]["id"]
        await self.settle()
        await self.service.shutdown()

        async def must_not_run(*args, **kwargs):
            raise AssertionError("A historical room cannot gain an adapter implicitly")

        defaults = default_registry(self.fake)
        registry = AgentRegistry([*(defaults.get(agent_id) for agent_id in defaults.ids()),
                                  AgentSpec("helper", "Local Helper", must_not_run)])
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", registry=registry,
                                   clock=lambda: self.now)
        self.fake.responses["claude"] = [answer("Invalid expansion", action={
            "type": "request", "to": "helper", "kind": "help", "text": "Join us"})]
        await self.service.send_message(room_id, "Continue with original members", "claude", "continue")
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        self.assertEqual([member["id"] for member in snapshot["members"]], ["claude", "codex"])
        self.assertEqual(snapshot["jobs"][-1]["error"], "invalid_agent_action")
        self.assertFalse(any(job["provider"] == "helper" for job in snapshot["jobs"]))


if __name__ == "__main__":
    unittest.main()
