"""Blocks the partners can resolve go back to the partners; only a user decision or permission waits.

Real SQLite task boards and the real native room service; provider transport is fake.
"""
import unittest
from unittest.mock import patch

from local_agent_coordinator import room_service
from tests import test_native_room_service as native
from tests import test_team_store as fixtures

proposal = fixtures.proposal


class PartnerResolvedBlockStoreTests(unittest.TestCase):
    setUp = fixtures.TeamStoreTests.setUp
    tearDown = fixtures.TeamStoreTests.tearDown
    plan = fixtures.TeamStoreTests.plan
    task = fixtures.TeamStoreTests.task
    assertCode = fixtures.TeamStoreTests.assertCode

    def room(self, **options):
        return fixtures.TeamStoreTests.room(self, execution_policy={"mode": "autonomous"}, **options)

    def finish(self, job, commands, **options):
        fixtures.TeamStoreTests.finish(self, job, commands, **options)
        self.store.record_job_cleanup(job["id"], confirmed=True)
        self.store.reconcile_team(job["room_id"])

    def events(self, room, kind):
        return [event for event in self.store.snapshot(room)["events"] if event["kind"] == kind]

    def binding(self, room):
        return {"run_id": self.store.snapshot(room)["current_run"]["id"], "revision": 0, "digest": "fixture"}

    def drain(self, room, *, proposals=()):
        """Answer every queued call the ordinary way until nothing is left to claim."""
        proposals = list(proposals)
        for _ in range(40):
            claimed = False
            for member in ("claude", "codex"):
                job = self.store.claim(room, member)
                if job is None:
                    continue
                claimed = True
                if job["kind"] == "team_coordinate":
                    author = self.store.snapshot(room)["team"]["plan_author"] == member
                    self.finish(job, (proposals if author else []) + [{"type": "plan_ready"}])
                    if author:
                        proposals = []
                elif job["kind"] == "team_review":
                    self.finish(job, [{"type": "review", "task_id": job["team"]["task_id"], "verdict": "accept",
                                       "reason": "Read the exact result."}])
                else:
                    self.assertEqual(job["kind"], "team_work")
                    self.finish(job, [{"type": "complete", "task_id": job["team"]["task_id"],
                                       "result": "Concrete result for " + job["team"]["task_id"] + "."}])
            if not claimed:
                return
        self.fail("the board never became quiet")

    def test_an_external_or_uncategorised_block_is_replanned_by_the_team_and_never_asked_of_the_user(self):
        # Catches: only coordination_required went back to the partners; a block labelled
        # external_dependency, or with no cause at all, froze the task and became a question for the user.
        for cause in ("external_dependency", None):
            with self.subTest(cause=cause):
                room = self.plan(paths=True)
                peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
                self.assertEqual((main["team"]["task_id"], peer["team"]["task_id"]), ("main", "tests"))
                block = {"type": "block", "task_id": "main", "reason": "The staging service this needs is not reachable."}
                if cause is not None:
                    block["cause"] = cause
                self.finish(main, [block])
                self.assertEqual(self.task(room, "main")["block_cause"], cause or "unclassified")
                self.finish(peer, [{"type": "complete", "task_id": "tests", "result": "Concrete checked result."}])
                self.finish(self.store.claim(room, "claude"), [{"type": "review", "task_id": "tests", "verdict": "accept",
                                                                 "reason": "Exact result checked."}])
                snapshot = self.store.snapshot(room)
                self.assertEqual(snapshot["team"]["phase"], "planning")
                self.assertEqual(snapshot["objective"]["status"], "running")
                self.assertEqual([event["task_ids"] for event in self.events(room, "internal_replan_started")], [["main"]])
                self.assertEqual(self.events(room, "objective_decision_required"), [])
                self.drain(room, proposals=[proposal("fix", paths=["fix.py"])])
                final = self.store.snapshot(room)
                self.assertEqual(final["team"]["phase"], "ready_for_user")
                self.assertEqual({task["id"]: task["status"] for task in final["team"]["tasks"]},
                                 {"main": "accepted", "tests": "accepted", "fix": "accepted"})
                self.assertEqual(self.task(room, "main")["dependencies"], ["fix"])
                self.assertIsNone(self.task(room, "main")["block_cause"])
                self.assertEqual(self.events(room, "objective_decision_required"), [])

    def test_a_host_block_is_reopened_while_another_task_keeps_waiting_for_the_users_decision(self):
        # Catches: one task that asks the user froze the whole board, so a task the host itself
        # had blocked on a failed call stayed blocked until the user answered something unrelated;
        # and the question listed that host failure next to the real decision.
        room = self.plan(paths=True)
        peer, main = self.store.claim(room, "codex"), self.store.claim(room, "claude")
        self.finish(main, [{"type": "block", "task_id": "main", "cause": "user_decision", "reason": "Pick A or B."}])
        for _ in range(3):  # three progress-only replies: the host blocks the task as team_result_missing
            self.finish(peer, [])
            peer = self.store.claim(room, "codex") or peer
        self.assertEqual((self.task(room, "tests")["status"], self.task(room, "tests")["blocked_reason"],
                          self.task(room, "tests")["block_cause"]), ("blocked", "team_result_missing", None))
        self.assertTrue(self.store.require_task_decision(room, self.binding(room)))
        asked = self.store.snapshot(room)["objective"]
        self.assertEqual((asked["status"], asked["next_step"]["question"]), ("needs_user", "Deliver main：Pick A or B."))
        self.assertEqual(self.events(room, "objective_decision_required")[-1]["task_ids"], ["main"])

        self.assertTrue(self.store.resume_recoverable_block(room))
        resumed = self.events(room, "task_blocks_resumed")[-1]
        self.assertEqual((resumed["task_ids"], resumed["waiting_for_user_task_ids"]), (["tests"], ["main"]))
        self.assertEqual(self.store.snapshot(room)["objective"]["status"], "running")
        self.assertEqual((self.task(room, "main")["status"], self.task(room, "main")["block_cause"]), ("blocked", "user_decision"))
        self.assertEqual(self.task(room, "tests")["status"], "assigned")
        self.drain(room)
        self.assertEqual(self.task(room, "tests")["status"], "accepted")
        # Nothing else can run: the decision is asked again, alone.
        self.assertTrue(self.store.require_task_decision(room, self.binding(room)))
        again = self.store.snapshot(room)["objective"]
        self.assertEqual((again["status"], again["next_step"]["question"]), ("needs_user", "Deliver main：Pick A or B."))
        # With only the user's own question left, the host has nothing to hand back.
        self.assertFalse(self.store.resume_recoverable_block(room))

    def test_a_host_failure_after_a_technical_block_is_the_hosts_block_not_a_model_question(self):
        # Catches: a technical block turned into peer help kept block_cause 'technical' on the task;
        # when the help call then failed, the host's own block still carried that cause and was
        # treated as a question the model had asked.
        room = self.plan(dependency=True)
        work = self.store.claim(room, "claude")
        self.finish(work, [{"type": "block", "task_id": "main", "cause": "technical",
                            "reason": "The assertion exposes an implementation mismatch."}])
        waiting = self.task(room, "main")
        self.assertEqual((waiting["status"], waiting["help_to"], waiting["block_cause"]), ("waiting_peer", "codex", None))
        help_job = self.store.claim(room, "codex")
        self.assertEqual(help_job["kind"], "team_help")
        self.store.finish_job(help_job["id"], error="cli_nonzero_exit")
        failed = self.task(room, "main")
        self.assertEqual((failed["status"], failed["blocked_reason"], failed["block_cause"]),
                         ("blocked", "cli_nonzero_exit", None))

    def test_a_cause_left_from_an_earlier_round_does_not_survive_the_hosts_own_block(self):
        # Catches: a task carried into a new round keeps the cause of a block the user already
        # answered; the host's block on a failed call must not inherit it and wait for the user again.
        room = self.plan(dependency=True)
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET block_cause='user_decision' WHERE room_id=? AND id='main'", (room,))
        work = self.store.claim(room, "claude")
        self.assertEqual(work["team"]["task_id"], "main")
        self.store.finish_job(work["id"], error="cli_nonzero_exit")
        failed = self.task(room, "main")
        self.assertEqual((failed["status"], failed["blocked_reason"], failed["block_cause"]),
                         ("blocked", "cli_nonzero_exit", None))
        # The dependent task was blocked by the stop itself, never by a model.
        self.assertEqual((self.task(room, "tests")["status"], self.task(room, "tests")["block_cause"]), ("blocked", None))

    def confirmed_plan_with_a_blocked_task(self):
        room = self.room()
        author, peer = self.store.claim(room, "claude"), self.store.claim(room, "codex")
        self.finish(author, [proposal("main"), proposal("extra"), {"type": "plan_ready"}])
        # A task already blocked when the plan is confirmed, the way a host block is recorded.
        with self.store._connection(write=True) as db:
            db.execute("UPDATE shared_tasks SET status='blocked',blocked_reason='team_result_missing' "
                       "WHERE room_id=? AND id='extra'", (room,))
        self.finish(peer, [{"type": "plan_ready"}])
        for _ in range(4):
            job = self.store.claim(room, "codex")
            if job is None or job["kind"] != "team_coordinate":
                break
            self.finish(job, [{"type": "plan_ready"}])
        return room, job

    def test_a_blocked_task_does_not_hold_a_confirmed_plan_in_planning(self):
        # Catches: any blocked task kept a fully confirmed plan in the planning phase, where nothing
        # is dispatched and nothing can reopen the block, so the room sat still for good.
        room, job = self.confirmed_plan_with_a_blocked_task()
        self.assertEqual(self.store.snapshot(room)["team"]["phase"], "working")
        job = job or self.store.claim(room, "claude") or self.store.claim(room, "codex")
        self.assertEqual((job["kind"], job["team"]["task_id"]), ("team_work", "main"))
        self.assertEqual(self.task(room, "extra")["status"], "blocked")

    def test_a_block_met_while_still_planning_is_reopened_by_the_hosts_continue(self):
        # Catches: reopening a host block required the working phase, so a room that had stopped
        # with a blocked task while planning could never be continued although it was shown as
        # continuing by itself.
        room, job = self.confirmed_plan_with_a_blocked_task()
        self.drain(room)
        with self.store._connection(write=True) as db:  # the state such a room was left in
            db.execute("UPDATE teams SET phase='planning' WHERE room_id=?", (room,))
        self.assertTrue(self.store.require_task_decision(room, self.binding(room)))
        self.assertEqual(self.store.snapshot(room)["objective"]["status"], "needs_user")
        self.assertTrue(self.store.resume_recoverable_block(room))
        snapshot = self.store.snapshot(room)
        self.assertEqual((snapshot["objective"]["status"], snapshot["team"]["phase"]), ("running", "working"))
        self.drain(room)
        self.assertEqual({task["id"]: task["status"] for task in self.store.snapshot(room)["team"]["tasks"]},
                         {"main": "accepted", "extra": "accepted"})

    def test_a_task_needing_a_capability_nobody_has_is_refused_when_proposed(self):
        # Catches: an image task was recorded blocked from birth; no partner could ever do it and
        # the plan it belonged to never left planning. Refused with the usual correction, the
        # author plans around it in the same phase.
        room = self.room()
        author, peer = self.store.claim(room, "claude"), self.store.claim(room, "codex")
        self.assertCode("team_capability_unavailable", lambda: self.store.prevalidate_team_result(
            author["id"], [proposal("art", capability="image"), {"type": "plan_ready"}]))
        self.assertEqual(self.store.snapshot(room)["team"]["tasks"], [])
        self.finish(author, [proposal("art"), {"type": "plan_ready"}])
        self.finish(peer, [{"type": "plan_ready"}])
        self.drain(room)
        final = self.store.snapshot(room)
        self.assertEqual([(task["id"], task["capability"], task["status"]) for task in final["team"]["tasks"]],
                         [("art", "text", "accepted")])


class PartnerResolvedBlockNativeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = native.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown
    handler = native.NativeRoomServiceTests.handler
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle

    def replanning_handler(self, first_reply, seen):
        """T1's owner replies with first_reply until the team has added T2; then everything is delivered."""
        original = self.handler

        def handler(agent, context, cwd):
            phase, task, board = context["job"]["job_phase"], context["task"], context["team"]
            ids = {item["id"]: item for item in board["tasks"]}
            if phase in {"plan", "coordinate"} and "T1" in ids:
                commands = []
                if ids["T1"]["status"] == "blocked" and "T2" not in ids and agent == board["plan_author"]:
                    seen["replan_texts"].append(context["job"]["text"])
                    commands.append(native.proposal("T2", capability="python"))
                return native.native_reply(context, commands + [{"type": "plan_ready"}])
            if phase == "work" and task["id"] == "T1":
                seen["t1"].append(context)
                if "T2" not in ids:
                    return native.native_reply(context, [first_reply(len(seen["t1"]))])
                (cwd / "main.py").write_text("VALUE = 2\n")
                return native.native_reply(context, [{"type": "complete", "task_id": "T1", "result": "Updated main after the new step."}],
                    unchanged=[{"path": "notes.md", "reason": "Existing notes remain correct."}])
            if phase == "work" and task["id"] == "T2":
                seen["t2"].append(agent)
                return native.native_reply(context, [{"type": "complete", "task_id": "T2", "result": "Found another way round the blocker."}])
            if phase == "review" and task["id"] == "T2":
                return native.native_reply(context, [{"type": "review", "task_id": "T2", "verdict": "accept", "reason": "Checked the new step."}])
            if phase == "help":
                reply = native.native_reply(context)
                reply["message"] = "A suggestion that does not change the files."
                return reply
            return original(agent, context, cwd)
        return handler

    async def test_an_external_or_uncategorised_block_is_handed_to_the_team_and_the_goal_is_finished(self):
        # Catches: the host turning any block that was not technical or coordination into a question
        # for the user, instead of letting the partners replan around it and finish.
        for cause in ("external_dependency", None):
            with self.subTest(cause=cause):
                seen = {"t1": [], "t2": [], "replan_texts": []}
                self.fake.handler = self.replanning_handler(lambda count: {
                    "type": "block", "task_id": "T1", "cause": cause,
                    "reason": "The staging service this step needs is not reachable."}, seen)
                created = await self.create()
                await self.settle()
                final = await self.service.read_room(created["room"]["id"])
                kinds = [event["kind"] for event in final["events"]]
                self.assertEqual(final["objective"]["status"], "complete", final["team"])
                self.assertEqual(kinds.count("internal_replan_started"), 1)
                self.assertEqual(kinds.count("objective_decision_required"), 0)
                self.assertEqual(len(seen["t1"]), 2)
                self.assertEqual(len(seen["t2"]), 1)
                self.assertIn("The staging service this step needs is not reachable.", seen["replan_texts"][0])
                tasks = {task["id"]: task for task in final["team"]["tasks"]}
                self.assertEqual((tasks["T1"]["status"], tasks["T1"]["dependencies"], tasks["T2"]["status"]),
                                 ("accepted", ["T2"], "accepted"))

    async def test_a_user_decision_or_permission_block_still_waits_for_the_user(self):
        # Catches: loosening the block rules so far that a real question for the user is replanned
        # or reopened by the host instead of waiting for the answer.
        original = self.handler
        for cause in ("user_decision", "permission_required"):
            with self.subTest(cause=cause):
                def handler(agent, context, cwd):
                    if context["job"]["job_phase"] == "work":
                        return native.native_reply(context, [{"type": "block", "task_id": "T1", "cause": cause,
                            "reason": "Should the old export format be kept?"}])
                    return original(agent, context, cwd)
                self.fake.handler = handler
                start = len(self.fake.calls)
                created = await self.create()
                await self.settle()
                final = await self.service.read_room(created["room"]["id"])
                self.assertEqual(final["objective"]["status"], "needs_user")
                self.assertEqual(final["objective"]["stop_reason"], "objective_decision_required")
                self.assertIsNone(final["stop_kind"])
                self.assertEqual(final["objective"]["next_step"]["question"],
                                 "Deliver T1：Should the old export format be kept?")
                self.assertEqual((final["team"]["tasks"][0]["status"], final["team"]["tasks"][0]["block_cause"]), ("blocked", cause))
                self.assertEqual([call["context"]["job"]["job_phase"] for call in self.fake.calls[start:]
                                  if call["context"]["job"]["job_phase"] == "help"], [])
                self.assertEqual([event for event in final["events"]
                                  if event["kind"] in {"internal_replan_requested", "internal_replan_started"}], [])
                # The host's own continue leaves the question alone.
                calls = len(self.fake.calls)
                with patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0):
                    for _ in range(3):
                        await self.service.expire_tasks()
                    await self.settle()
                after = await self.service.read_room(created["room"]["id"])
                self.assertEqual(after["objective"]["status"], "needs_user")
                self.assertEqual(len(self.fake.calls), calls)
                self.assertEqual([event for event in after["events"] if event["kind"] == "task_blocks_resumed"], [])

    async def test_a_technical_block_that_keeps_coming_back_unchanged_is_replanned_not_stopped(self):
        # Catches: the host deciding the partners made no progress after three identical technical
        # blocks, stopping the objective and waiting for the user. The partners are told it is the
        # same result again, and the third time the team replans the task.
        seen = {"t1": [], "t2": [], "replan_texts": []}
        self.fake.handler = self.replanning_handler(lambda count: {
            "type": "block", "task_id": "T1", "cause": "technical",
            "reason": f"Different diagnosis wording {count} without any new file evidence."}, seen)
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        kinds = [event["kind"] for event in final["events"]]
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len(seen["t1"]), 4)
        self.assertEqual(kinds.count("technical_block_continued"), 2)
        self.assertEqual(kinds.count("internal_replan_started"), 1)
        self.assertEqual([event["code"] for event in final["events"] if event["kind"] == "objective_stopped"], [])
        self.assertEqual(kinds.count("objective_decision_required"), 0)
        # First and second call: nothing has repeated yet. Third call: the fact is in its context.
        self.assertNotIn("repeated_results", seen["t1"][0]["objective"])
        self.assertNotIn("repeated_results", seen["t1"][1]["objective"])
        self.assertEqual(seen["t1"][2]["objective"].get("repeated_results"),
                         [{"what": "the task was blocked as technical again with unchanged files and result",
                           "task_id": "T1", "times": 2}])
        prompts = [call["prompt"] for call in self.fake.calls
                   if call["context"]["job"]["job_phase"] == "work" and call["context"]["task"]["id"] == "T1"]
        told = "The same outcome has come back more than once with nothing new to show for it."
        self.assertNotIn(told, prompts[0])
        self.assertIn(told, prompts[2])
        # Every work prompt says which blocks reach the user and which come back to the team.
        self.assertIn("Only two causes go to the user", prompts[0])

    async def test_a_stop_for_repeated_results_is_continued_by_the_host_and_the_next_run_is_told(self):
        # Catches: objective_no_progress left with the user (the host never pressed continue), and a
        # continued run starting with no word of what had kept coming back the same.
        original, reviews, checkpoints = self.handler, [], []

        def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "checkpoint":
                checkpoints.append(context)
                return native.native_reply(context, [{"type": "checkpoint", "decision": "complete",
                    "summary": "The exact same unsupported assessment.", "next_goal": None,
                    "scope_reference": context["objective"]["original_goal"], "question": None}])
            if phase == "checkpoint_review":
                reviews.append(context)
                return native.native_reply(context, [{"type": "checkpoint_review", "verdict": "changes_requested",
                    "reason": "This exact unchanged assessment still lacks support.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        room = created["room"]["id"]
        await self.settle()
        stopped = await self.service.read_room(room)
        self.assertEqual((stopped["objective"]["status"], stopped["objective"]["stop_reason"], stopped["stop_kind"]),
                         ("stopped", "objective_no_progress", "auto"))
        self.assertEqual(len(reviews), 3)
        # The second and third assessment were written knowing the first came back unchanged.
        self.assertNotIn("repeated_results", checkpoints[1]["objective"])
        self.assertEqual(checkpoints[2]["objective"].get("repeated_results"),
                         [{"what": "the reviewer returned the same checkpoint assessment and it came back unchanged",
                           "task_id": None, "times": 2}])
        with patch.object(room_service, "_AUTO_CONTINUE_DELAY_SECONDS", 0):
            for _ in range(20):
                await self.service.expire_tasks()
                state = self.service._auto_continues.get(room)
                if state and state["task"] is not None:
                    await state["task"]
                await self.settle()
                if len(checkpoints) > 3:
                    break
        continued = await self.service.read_room(room)
        self.assertEqual([event["code"] for event in continued["events"] if event["kind"] == "objective_auto_continued"],
                         ["objective_no_progress"])
        self.assertNotEqual(continued["current_run"]["id"], stopped["current_run"]["id"])
        self.assertEqual(checkpoints[3]["objective"]["continuation"].get("repeated_results"),
                         [{"what": "the reviewer returned the same checkpoint assessment and it came back unchanged",
                           "task_id": None, "times": 3}])
        self.assertNotEqual(continued["objective"]["status"], "needs_user")
        self.assertFalse(any(event["kind"] == "objective_decision_required" for event in continued["events"]))


if __name__ == "__main__":
    unittest.main()
