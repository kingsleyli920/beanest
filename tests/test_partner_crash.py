"""A reply-only partner's process dies: the room carries on by itself.

Real private trees and SQLite with a synthetic transport; no model runs.
"""
import asyncio
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import room_service
from local_agent_coordinator.room_service import RoomService
from test_folder_service import response
from test_preflight_failures import ALL_PARTNERS, Fault, FolderFaults, ServiceCase, folder_handler, registry


def died(_agent, _permission, *, code="cli_nonzero_exit", events=()):
    """What the CLI adapters return for a process that was killed mid-call."""
    return {"session_id": None, "status": "failed", "failure_code": code, "text": "", "usage": None,
            "events": list(events), "cleanup_confirmed": True}


class SessionAwareFaults(FolderFaults):
    """Lets a fault report the session its call really resumed."""
    def __init__(self):
        super().__init__()
        self.session_of = {}

    async def __call__(self, agent, prompt, cwd, session_id=None, timeout=120):
        self.session_of[agent] = session_id
        return await super().__call__(agent, prompt, cwd, session_id=session_id, timeout=timeout)


class ReplyOnlyCrashes(ServiceCase):
    def make_service(self):
        self.provider = SessionAwareFaults()
        self.provider.handler = folder_handler
        return RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", registry=registry(self.provider))

    async def create(self, participants=ALL_PARTNERS):
        created = await self.service.create_room("Update the project.", participants[0], str(uuid4()),
            mode="project_folder", project_id=self.registration["project"]["id"], participants=participants,
            collaboration=True, execution_policy={"mode": "autonomous", "quota_failover": True})
        return created["room"]["id"]

    async def final(self, room_id):
        await self.settle()
        return await self.service.read_room(room_id)

    async def press_continue_as_host(self, room_id):
        snapshot = self.service._snapshot(room_id)
        code = snapshot["team"]["stop_reason"]
        with patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0):
            self.service._schedule_auto_continue(room_id, snapshot)
            self.service._schedule_auto_continue(room_id, snapshot)
        task = self.service._auto_continues[room_id]["task"]
        self.assertIsNotNone(task, code)
        await task
        return await self.final(room_id)

    async def test_one_dead_call_is_made_again_and_nobody_notices(self):
        # Catches: one killed process stopping the room and interrupting the others.
        for code in ("cli_nonzero_exit", "protocol_turn_incomplete", "native_protocol_invalid", "native_process_or_protocol_failure"):
            with self.subTest(code=code):
                self.provider.faults = [Fault("claude", 1, phase="work", result=lambda a, p: died(a, p, code=code))]
                final = await self.final(await self.create())
                self.assertEqual(final["objective"]["status"], "complete")
                self.assert_healthy(final)
                self.assertFalse(any(job["error"] == "invoke_interrupted" for job in final["jobs"]))
                retries = [event for event in final["events"] if event["kind"] == "partner_call_retry"]
                self.assertEqual([(event["sender"], event["attempt"], event["code"]) for event in retries], [("claude", 1, code)])
                self.assertFalse(any(event["kind"] == "objective_stopped" for event in final["events"]))

    async def test_a_call_that_keeps_dying_stops_cleanly_and_the_host_continues(self):
        # Catches: the reported dead end, where 继续 was unavailable after a crash.
        self.provider.faults = [Fault("claude", 2, phase="work", result=died)]
        confirmed = lambda error: setattr(error, "cleanup_confirmed", True)
        self.provider.held = {agent: (asyncio.Event(), confirmed) for agent in ("codex", "kimi")}
        room_id = await self.create()
        stopped = await self.final(room_id)
        self.provider.held = {}
        self.assertEqual(stopped["team"]["phase"], "stopped")
        self.assertEqual(stopped["team"]["stop_reason"], "cli_nonzero_exit")
        self.assertFalse(next(m for m in stopped["members"] if m["id"] == "claude")["identity_uncertain"])
        self.assertTrue(stopped["continuation"]["can_send"], stopped["continuation"])
        final = await self.press_continue_as_host(room_id)
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertTrue(any(event["kind"] == "objective_work_carried" for event in final["events"]))
        self.assertTrue(any(event["kind"] == "objective_auto_continued" for event in final["events"]))

    async def test_a_dead_first_call_with_no_session_yet_does_not_doubt_identity(self):
        # Catches: a crash before the CLI named a session reading as an identity problem.
        self.provider.faults = [Fault("claude", 2, phase="plan", result=died)]
        room_id = await self.create()
        stopped = await self.final(room_id)
        self.assertNotIn(stopped["team"]["stop_reason"], {"session_identity_unknown", "session_identity_changed"})
        self.assertFalse(any(member["identity_uncertain"] for member in stopped["members"]), stopped["members"])
        self.assertTrue(stopped["continuation"]["can_send"], stopped["continuation"])

    async def test_a_provider_error_with_an_unknown_outcome_can_still_be_continued(self):
        # Catches: phase "unknown" being a dead end when nothing outside a native project is at stake.
        def failed(agent, _permission):
            return {"session_id": self.provider.session_of[agent] or str(uuid4()), "status": "failed", "failure_code": "unknown_provider_error",
                    "text": "", "usage": None, "events": [], "cleanup_confirmed": True}
        self.provider.faults = [Fault("claude", 1, phase="work", result=failed)]
        room_id = await self.create()
        stopped = await self.final(room_id)
        self.assertEqual((stopped["team"]["phase"], stopped["team"]["stop_reason"]), ("unknown", "unknown_provider_error"))
        self.assertTrue(any(task["status"] == "unknown" for task in stopped["team"]["tasks"]), stopped["team"]["tasks"])
        self.assertTrue(stopped["continuation"]["can_send"], stopped["continuation"])
        final = await self.press_continue_as_host(room_id)
        # Catches: carried tasks staying "unknown", so nobody works and the room waits forever.
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertFalse(any(task["status"] == "unknown" for task in final["team"]["tasks"]), final["team"]["tasks"])

    async def test_a_blocked_task_in_a_read_only_room_asks_the_user(self):
        # Catches: a read-only room with only a blocked task left reading “partners are working” forever.
        def blocking(agent, context):
            if context["job"]["job_phase"] == "work" and agent == "claude":
                return response(context, commands=[{"type": "block", "task_id": context["task"]["id"],
                    "cause": "permission_required", "reason": "Needs a command the current permission refuses."}])
            return folder_handler(agent, context)
        self.provider.handler = blocking
        created = await self.service.create_room("Update the project.", "claude", str(uuid4()),
            mode="project_folder", project_id=self.registration["project"]["id"], participants=ALL_PARTNERS,
            collaboration=True, work_mode="plan", execution_policy={"mode": "autonomous", "quota_failover": True})
        final = await self.final(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "needs_user", final["objective"])
        self.assertEqual(final["objective"]["next_step"]["origin"], "task_block")

    async def test_a_dead_call_that_ran_a_tool_is_not_replayed(self):
        # Catches: re-running a call that may really have done something.
        tool = [{"type": "tool_observed", "name": "shell"}]
        self.provider.faults = [Fault("claude", 1, phase="work", result=lambda a, p: died(a, p, events=tool))]
        final = await self.final(await self.create())
        self.assertFalse(any(event["kind"] == "partner_call_retry" for event in final["events"]))
        self.assertNotEqual(final["objective"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
