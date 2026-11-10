"""Rooms that the app was quit or killed under carry on without the user."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import room_service
from local_agent_coordinator.room_service import RoomService
from test_folder_service import response
from test_preflight_failures import ALL_PARTNERS, FolderFaults, ServiceCase, folder_handler, registry


class RestartCase(ServiceCase):
    def make_service(self):
        self.provider = FolderFaults()
        self.provider.handler = folder_handler
        return RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", registry=registry(self.provider))

    async def host_continue(self, room_id):
        """The host's automatic continue, without its 15-second delay."""
        snapshot = self.service._snapshot(room_id)
        with patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0):
            # The first sighting records the stop; the next one, after the delay, acts.
            self.service._schedule_auto_continue(room_id, snapshot)
            self.service._schedule_auto_continue(room_id, snapshot)
        state = self.service._auto_continues.get(room_id)
        if state and state["task"]:
            await state["task"]
        for _ in range(50):
            await self.settle()
            snapshot = await self.service.read_room(room_id)
            if snapshot["objective"]["status"] != "running" or snapshot["team"]["phase"] not in {"working", "planning", "reviewing"}:
                break
            await asyncio.sleep(0.05)
        return snapshot

    async def create(self, **options):
        created = await self.service.create_room("Update the project.", "claude", str(uuid4()),
            mode="project_folder", project_id=self.registration["project"]["id"], participants=ALL_PARTNERS,
            collaboration=True, execution_policy={"mode": "autonomous", "quota_failover": True}, **options)
        return created["room"]["id"]

    def hold_work(self):
        started = asyncio.Event()
        self.provider.held = {agent: (started, lambda error: setattr(error, "cleanup_confirmed", True)) for agent in ALL_PARTNERS}
        return started


class KilledServiceTests(RestartCase):
    async def test_a_reply_only_room_resumes_after_the_service_is_killed_mid_call(self):
        # Catches: a SIGKILL (quit timeout, crash, power loss) marking reply-only partners'
        # identities uncertain, which leaves the room with no way to continue at all.
        started = self.hold_work()
        room_id = await self.create()
        await asyncio.wait_for(started.wait(), 10)
        # The database exactly as a killed process leaves it: a call still working.
        database = self.root / "rooms.sqlite3"
        snapshot = sqlite3.connect(":memory:")
        with sqlite3.connect(database) as live:
            live.backup(snapshot)
        self.provider.held = {}
        await self.service.shutdown()
        for suffix in ("-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        with sqlite3.connect(database) as restored:
            snapshot.backup(restored)
        snapshot.close()

        self.service = self.make_service()
        after = await self.service.read_room(room_id)
        self.assertEqual([member["identity_uncertain"] for member in after["members"]], [False] * len(ALL_PARTNERS))
        # Partners cut off mid-call move to a fresh session that a leftover process cannot touch.
        cut_off = {job["provider"] for job in after["jobs"] if job["error"] == "service_restarted"}
        self.assertTrue(cut_off)
        sessions = {member["id"]: member["session_id"] for member in after["members"]}
        self.assertEqual({provider for provider, session in sessions.items() if session is None}, cut_off)
        self.assertTrue(after["continuation"]["can_send"], after["continuation"])
        final = await self.host_continue(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual([task["status"] for task in final["team"]["tasks"]], ["accepted", "accepted"])


class QuitTwiceTests(RestartCase):
    async def restart(self):
        await self.service.shutdown()
        self.service = self.make_service()

    async def test_quitting_again_before_any_task_finishes_still_resumes(self):
        # Catches: the second quit's stop matching the first one's fingerprint, so the host
        # treats it as "same stop, no progress" and leaves the room waiting for the user.
        room_id = await self.create()
        for attempt in (1, 2):
            started = self.hold_work()
            if attempt == 2:
                snapshot = self.service._snapshot(room_id)
                with patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0):
                    self.service._schedule_auto_continue(room_id, snapshot)
                    self.service._schedule_auto_continue(room_id, snapshot)
                await self.service._auto_continues[room_id]["task"]
            await asyncio.wait_for(started.wait(), 10)
            await self.restart()
            # The page says the host will carry on, not that nothing reruns.
            self.assertEqual((await self.service.read_room(room_id))["stop_kind"], "auto")
        self.assertFalse(self.service.store.auto_continue_state(room_id)[1])
        final = await self.host_continue(room_id)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])


class ReadOnlyFilesTests(RestartCase):
    async def test_a_read_only_reply_carrying_an_edit_is_corrected_not_stopped(self):
        # Catches: a plan-mode partner attaching its proposal as an edit stopping the whole
        # room with plan_read_only, though nothing was written and it can resend as text.
        sent = []
        def handler(agent, context):
            if context["job"]["job_phase"] == "work" and agent == "claude" and not sent:
                sent.append(context["job"]["id"])
                value = response(context, commands=[{"type": "complete", "task_id": context["task"]["id"],
                                                      "result": "Proposed: set VALUE to 2."}])
                value["edits"] = [{"path": "main.py", "base_sha256": "0" * 64, "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]
                return value
            return folder_handler(agent, context)
        self.provider.handler = handler
        room_id = await self.create(work_mode="plan")
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertEqual(sent and len(sent), 1)
        self.assertNotIn(final["team"]["stop_reason"], {"plan_read_only", "permission_read_only"})
        self.assertNotIn("plan_read_only", [event.get("code") for event in final["events"] if event["kind"] == "objective_stopped"])
        self.assertIn("team_files_forbidden", [event.get("code") for event in final["events"]
                                               if event["kind"] == "format_correction_queued"])
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        self.assertTrue(all(task["status"] == "accepted" for task in final["team"]["tasks"]), final["team"])


class ReadOnlyDeliveryTests(RestartCase):
    async def delivered_plan_room(self):
        room_id = await self.create(work_mode="plan")
        await self.settle()
        return room_id, await self.service.read_room(room_id)

    async def test_a_delivered_plan_room_is_finished_not_left_running(self):
        # Catches: a delivered read-only discussion staying "running", so the page offered
        # 暂停推进, listed it in progress, and every restart marked it interrupted.
        room_id, delivered = await self.delivered_plan_room()
        self.assertEqual(delivered["team"]["phase"], "ready_for_user")
        self.assertEqual(delivered["objective"]["status"], "complete")
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        await self.service.shutdown()
        self.service = self.make_service()
        after = await self.service.read_room(room_id)
        self.assertEqual(after["objective"]["status"], "complete")
        self.assertNotIn("service_restarted", [event.get("code") for event in after["events"]])

    async def test_a_follow_up_cut_off_by_quitting_is_still_answered(self):
        # Catches: a question sent after delivery vanishing when the app quits before the reply.
        room_id, delivered = await self.delivered_plan_room()
        before = len([event for event in delivered["events"] if event["kind"] == "message" and event["sender"] != "user"])
        started = asyncio.Event()
        answer = self.provider.__class__.__call__
        async def stuck(provider, agent, prompt, cwd, session_id=None, timeout=120):
            started.set()
            await asyncio.Event().wait()
        with patch.object(self.provider.__class__, "__call__", stuck):
            await self.service.send_message(room_id, "What about naming?", "all", str(uuid4()), intent="continue",
                                            binding=delivered["continuation"]["binding"])
            await asyncio.wait_for(started.wait(), 10)
            await self.service.shutdown()
        self.assertIs(self.provider.__class__.__call__, answer)
        self.service = self.make_service()
        final = await self.host_continue(room_id)
        follow_up = max(event["seq"] for event in final["events"] if event["kind"] == "message" and event["sender"] == "user")
        answers = [event for event in final["events"] if event["kind"] == "message" and event["sender"] != "user" and event["seq"] > follow_up]
        self.assertTrue(any(job["error"] in {"service_shutdown", "cancel_requested", "service_restarted"} or job["status"] in {"cancelled", "interrupted"}
                            for job in final["jobs"]), "the follow-up call was really cut off")
        self.assertTrue(answers)


class AnsweredInDiscussionTests(RestartCase):
    def answer_without_tasks(self, agent, context):
        if context["job"]["job_phase"] in {"plan", "coordinate"}:
            return response(context, message="The answer is 40.", commands=[])
        return folder_handler(agent, context)

    async def test_a_read_only_question_answered_in_discussion_is_delivered(self):
        # Catches: a plan-mode question answered while planning (no task needed) stopping with
        # team_plan_missing and waiting for the user to press continue.
        self.provider.handler = self.answer_without_tasks
        room_id = await self.create(work_mode="plan")
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertNotIn("team_plan_missing", [event.get("code") for event in final["events"]])
        self.assertEqual((final["team"]["phase"], final["objective"]["status"]), ("ready_for_user", "complete"))

    async def test_a_build_room_without_a_plan_still_stops(self):
        # Catches: widening the shortcut to build work, where no task list means nothing was done.
        self.provider.handler = self.answer_without_tasks
        room_id = await self.create()
        await self.settle()
        final = await self.service.read_room(room_id)
        self.assertEqual(final["team"]["stop_reason"], "team_plan_missing")
