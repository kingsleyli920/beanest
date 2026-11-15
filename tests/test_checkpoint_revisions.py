"""Assessment revision uses the existing native team lifecycle, not a human approval loop."""
import asyncio
import unittest
from uuid import uuid4

from tests import test_native_room_service as fixtures


class CheckpointRevisionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = fixtures.NativeRoomServiceTests.asyncTearDown
    settle = fixtures.NativeRoomServiceTests.settle
    handler = fixtures.NativeRoomServiceTests.handler

    async def create(self):
        return await self.service.create_room("Change the value to three and preserve the notes.", "claude", str(uuid4()),
            mode="native_project", project_id=self.registration["project"]["id"], participants=["claude", "codex"],
            work_mode="build", runtime_settings={"permission": "full_access", "members": {}},
            execution_policy={"mode": "autonomous"})

    def revision_scenario(self):
        original = self.handler
        observed = {"checkpoints": [], "reviews": [], "work": [], "feedback": "Use the existing authorized verification context; distinguish observations from untested claims."}
        def handler(agent, context, cwd):
            phase, stage = context["job"]["job_phase"], context["objective"]["stage"]
            if phase == "work":
                observed["work"].append(context)
                self.assertEqual((cwd / "main.py").read_text(), f"VALUE = {stage}\n")
                (cwd / "main.py").write_text(f"VALUE = {stage + 1}\n")
                return fixtures.native_reply(context, [{"type": "complete", "task_id": "T1", "result": "Completed this exact increment."}],
                    unchanged=[{"path": "notes.md", "reason": "Notes still apply."}])
            if phase == "review":
                self.assertEqual((cwd / "main.py").read_text(), f"VALUE = {stage + 1}\n")
                return fixtures.native_reply(context, [{"type": "review", "task_id": "T1", "verdict": "accept", "reason": "Read this exact result version."}])
            if phase == "checkpoint":
                observed["checkpoints"].append(context)
                feedback = context["objective"]["checkpoint_revision"]
                if stage == 1 and len(observed["checkpoints"]) == 2:
                    self.assertEqual(feedback["reason"], observed["feedback"])
                    self.assertEqual(feedback["proposal_digest"], observed["reviews"][0]["objective"]["checkpoint_proposal"]["proposal_digest"])
                    self.assertEqual(feedback["binding"]["run_id"], context["job"]["run_id"])
                if context["current_user_updates"] and context["current_user_updates"][-1]["text"] == "Use the existing authorized evidence and keep the original scope.":
                    self.assertIsNone(feedback)
                return fixtures.native_reply(context, [{"type": "checkpoint", "decision": "continue" if stage == 1 else "complete",
                    "summary": "The value is still two; the original goal requires three." if stage == 1 else "The requested value is three and notes remain.",
                    "next_goal": ("Finish the original value-three requirement using existing evidence." if feedback else "Complete the remaining increment to three.") if stage == 1 else None,
                    "scope_reference": context["objective"]["original_goal"], "question": None}])
            if phase == "checkpoint_review":
                observed["reviews"].append(context)
                first = len(observed["reviews"]) == 1
                return fixtures.native_reply(context, [{"type": "checkpoint_review", "verdict": "changes_requested" if first else "accept",
                    "reason": observed["feedback"] if first else "The revised assessment uses permitted evidence and retains the original scope.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            return original(agent, context, cwd)
        return handler, observed

    async def test_same_scope_changes_are_revised_by_original_provider_and_freshly_reviewed_before_stage_two(self):
        self.fake.handler, observed = self.revision_scenario()
        created = await self.create()
        room = created["room"]["id"]
        await self.settle()
        final = await self.service.read_room(room)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(final["objective"]["id"], created["objective"]["id"])
        self.assertEqual(final["objective"]["created_at"], created["objective"]["created_at"])
        self.assertEqual(final["room"]["runtime_settings"], created["room"]["runtime_settings"])
        self.assertEqual(len(observed["work"]), 2)
        self.assertEqual([context["self"] for context in observed["checkpoints"][:2]], ["claude", "claude"])
        self.assertEqual(observed["checkpoints"][0]["job"]["run_id"], observed["checkpoints"][1]["job"]["run_id"])
        digests = [context["objective"]["checkpoint_proposal"]["proposal_digest"] for context in observed["reviews"]]
        self.assertNotEqual(digests[0], digests[1])
        self.assertEqual(final["objective"]["stage"], 2)
        revisions = [event for event in final["events"] if event["kind"] == "objective_checkpoint_revision_requested"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["feedback"]["proposal_digest"], digests[0])
        self.assertEqual(revisions[0]["feedback"]["proposal"]["decision"], "continue")
        self.assertFalse(any(event["kind"] == "objective_checkpoint_finished" and event.get("code") == "objective_checkpoint_disagreed" for event in final["events"]))
        self.assertTrue(all(job["status"] == "completed" for job in final["jobs"]))
        self.assertEqual(self.service._project(room).files_for_paths(["main.py"])[0]["content"], "VALUE = 3\n")

    async def test_rejected_whole_goal_completion_is_revised_then_freshly_reviewed_before_continuing(self):
        base, observed = self.revision_scenario()
        def handler(agent, context, cwd):
            result = base(agent, context, cwd)
            if context["job"]["job_phase"] == "checkpoint" and len(observed["checkpoints"]) == 1:
                result["commands"][0].update(decision="complete", next_goal=None,
                    summary="This proposal incorrectly treats the first increment as the complete goal.")
            return result
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual([context["objective"]["checkpoint_proposal"]["decision"] for context in observed["reviews"]],
                         ["complete", "continue", "complete"])
        digests = [context["objective"]["checkpoint_proposal"]["proposal_digest"] for context in observed["reviews"]]
        self.assertNotEqual(digests[0], digests[1])
        self.assertEqual(len(observed["work"]), 2)
        self.assertEqual([event["decision"] for event in final["events"] if event["kind"] == "objective_checkpoint_finished"],
                         ["continue", "complete"])
        self.assertFalse(any(event.get("code") == "objective_checkpoint_disagreed" for event in final["events"]))

    async def test_pause_preserves_feedback_and_resume_queues_the_revision_once(self):
        base, observed = self.revision_scenario()
        ready, release = asyncio.Event(), asyncio.Event()
        async def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "checkpoint_review" and not observed["reviews"]:
                ready.set()
                await release.wait()
            return base(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await asyncio.wait_for(ready.wait(), 3)
        room, objective = created["room"]["id"], created["objective"]["id"]
        await self.service.objective_action(room, "pause", "pause-checkpoint-revision", objective_id=objective)
        release.set()
        await self.settle()
        paused = await self.service.read_room(room)
        self.assertEqual(paused["objective"]["status"], "paused")
        self.assertEqual(paused["objective"]["checkpoint_revision"]["reason"], observed["feedback"])
        self.assertEqual(len(observed["checkpoints"]), 1)
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in paused["jobs"]))
        await self.service.objective_action(room, "resume", "resume-checkpoint-revision", objective_id=objective)
        await self.settle()
        final = await self.service.read_room(room)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(len([context for context in observed["checkpoints"] if context["objective"]["stage"] == 1]), 2)

    async def test_same_proposal_and_evidence_stop_even_if_review_wording_changes(self):
        original, proposals, reviews = self.handler, [], []
        def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "checkpoint":
                proposals.append(context)
                return fixtures.native_reply(context, [{"type": "checkpoint", "decision": "continue",
                    "summary": "The exact same unsupported assessment.", "next_goal": "The exact same next step.",
                    "scope_reference": context["objective"]["original_goal"], "question": None}])
            if phase == "checkpoint_review":
                reviews.append(context)
                return fixtures.native_reply(context, [{"type": "checkpoint_review", "verdict": "changes_requested",
                    "reason": f"Reworded feedback {len(reviews)} with no new evidence.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["stop_reason"], "objective_no_progress")
        # The partners' own disagreement: the host continues it after a pause and asks the user nothing.
        self.assertEqual((final["objective"]["status"], final["stop_kind"]), ("stopped", "auto"))
        self.assertEqual(len(proposals), 3)
        self.assertEqual(len(reviews), 3)
        self.assertEqual(len({context["objective"]["checkpoint_proposal"]["proposal_digest"] for context in reviews}), 1)
        self.assertEqual(final["objective"]["stage"], 1)
        self.assertEqual(len([call for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work"]), 1)
        self.assertFalse(any(event["kind"] == "objective_checkpoint_finished" and event.get("code") == "objective_checkpoint_disagreed" for event in final["events"]))

    async def test_four_distinct_assessment_revisions_continue_on_the_same_delivered_file_evidence(self):
        base, observed = self.revision_scenario()
        def handler(agent, context, cwd):
            result = base(agent, context, cwd)
            phase, stage = context["job"]["job_phase"], context["objective"]["stage"]
            if stage == 1 and phase == "checkpoint":
                result["commands"][0]["summary"] = f"Corrected the concrete evidence attribution for assessment item {len(observed['checkpoints'])}."
            elif stage == 1 and phase == "checkpoint_review" and len(observed["reviews"]) <= 4:
                result["commands"][0]["verdict"] = "changes_requested"
            return result
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        stage_one = [context for context in observed["reviews"] if context["objective"]["stage"] == 1]
        self.assertEqual(len(stage_one), 5)
        self.assertEqual(len({context["objective"]["checkpoint_proposal"]["proposal_digest"] for context in stage_one}), 5)
        self.assertEqual(len({context["proposal_base_revision"] for context in stage_one}), 1)
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "objective_checkpoint_revision_requested"]), 4)
        self.assertEqual(len(observed["work"]), 2)

    async def test_pause_wins_over_third_identical_rejection_and_resume_exposes_the_deferred_stop(self):
        await self.paused_no_progress_case(resume_during_call=False)

    async def test_resume_requested_before_cleanup_cannot_bypass_deferred_no_progress(self):
        await self.paused_no_progress_case(resume_during_call=True)

    async def paused_no_progress_case(self, *, resume_during_call):
        original, reviews = self.handler, []
        ready, release = asyncio.Event(), asyncio.Event()
        async def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "checkpoint":
                return fixtures.native_reply(context, [{"type": "checkpoint", "decision": "continue",
                    "summary": "The same unchanged assessment.", "next_goal": "The same remaining work.",
                    "scope_reference": context["objective"]["original_goal"], "question": None}])
            if phase == "checkpoint_review":
                reviews.append(context)
                if len(reviews) == 3:
                    ready.set()
                    await release.wait()
                return fixtures.native_reply(context, [{"type": "checkpoint_review", "verdict": "changes_requested",
                    "reason": "This exact unchanged proposal still lacks support.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await asyncio.wait_for(ready.wait(), 3)
        room, objective = created["room"]["id"], created["objective"]["id"]
        await self.service.objective_action(room, "pause", "pause-third-rejection", objective_id=objective)
        if resume_during_call:
            # Exercise the host's deferred-resume path without a new direct
            # message. New user text invalidates the old assessment separately.
            await self.service.objective_action(room, "resume", "resume-third-rejection-inflight", objective_id=objective,
                _request_identity=["deferred-resume-test", room, objective])
        release.set()
        await self.settle()
        paused = await self.service.read_room(room)
        self.assertEqual(paused["objective"]["status"], "stopped" if resume_during_call else "paused")
        self.assertEqual(paused["objective"]["stop_reason"], "objective_no_progress")
        self.assertEqual(len(reviews), 3)
        self.assertFalse(paused["objective"]["pause_requested"])
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in paused["jobs"]))
        count = len(self.fake.calls)
        if not resume_during_call:
            await self.service.objective_action(room, "resume", "resume-deferred-stop", objective_id=objective)
            await self.settle()
        final = await self.service.read_room(room)
        self.assertEqual(final["objective"]["status"], "stopped")
        self.assertEqual(final["objective"]["stop_reason"], "objective_no_progress")
        self.assertEqual(len(self.fake.calls), count)

    async def test_a_decision_asked_of_the_user_remains_a_human_gate_without_historical_rewrite(self):
        # Catches: a checkpoint in which the partner itself asks the user a question being
        # continued or revised by the host instead of waiting for the answer.
        original = self.handler
        def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "checkpoint":
                return fixtures.native_reply(context, [{"type": "checkpoint", "decision": "needs_user",
                    "summary": "A concrete human decision remains.", "next_goal": None,
                    "scope_reference": context["objective"]["original_goal"],
                    "question": "Which business requirement should apply?"}])
            if phase == "checkpoint_review":
                return fixtures.native_reply(context, [{"type": "checkpoint_review", "verdict": "accept",
                    "reason": "The user must supply this choice.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            return original(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        room = created["room"]["id"]
        before = await self.service.read_room(room)
        self.assertEqual(before["objective"]["status"], "needs_user")
        self.assertEqual(before["objective"]["stop_reason"], "objective_decision_required")
        self.assertIsNone(before["stop_kind"])
        count = len(self.fake.calls)
        self.service._wake(room)
        await self.settle()
        after = await self.service.read_room(room)
        self.assertEqual(len(self.fake.calls), count)
        self.assertEqual(after["objective"], before["objective"])
        self.assertFalse(any(event["kind"] == "objective_checkpoint_revision_requested" for event in after["events"]))

    async def test_a_checkpoint_that_continues_is_not_held_for_scope_confirmation(self):
        # Catches: the host judging the next stage "possibly outside the original goal" because
        # scope_reference was not a quotation of it, and stopping to ask the user. Whether the work
        # still belongs to the goal is for the partner and its reviewer to judge.
        base, observed = self.revision_scenario()
        def handler(agent, context, cwd):
            phase, stage = context["job"]["job_phase"], context["objective"]["stage"]
            if phase == "checkpoint":
                return fixtures.native_reply(context, [{"type": "checkpoint", "decision": "continue" if stage == 1 else "complete",
                    "summary": "The value is two; three is still required." if stage == 1 else "The requested value is three.",
                    "next_goal": "Raise the value once more." if stage == 1 else None,
                    "scope_reference": "Not a quotation of the goal.", "question": None}])
            if phase == "checkpoint_review":
                return fixtures.native_reply(context, [{"type": "checkpoint_review", "verdict": "accept",
                    "reason": "The remaining step is part of the requested change.",
                    "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
            return base(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual((final["objective"]["status"], final["objective"]["stage"]), ("complete", 2), final["team"])
        self.assertEqual([event.get("code") for event in final["events"] if event["kind"] == "objective_checkpoint_finished"],
                         [None, None])
        self.assertEqual(len(observed["work"]), 2)
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")

    async def test_new_user_update_during_revised_review_invalidates_proposal_and_old_feedback(self):
        base, observed = self.revision_scenario()
        ready, release = asyncio.Event(), asyncio.Event()
        held_job = []
        async def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "checkpoint_review" and len(observed["reviews"]) == 1:
                held_job.append(context["job"]["id"])
                ready.set()
                await release.wait()
            return base(agent, context, cwd)
        self.fake.handler = handler
        created = await self.create()
        await asyncio.wait_for(ready.wait(), 3)
        room = created["room"]["id"]
        await self.service.send_message(room, "Use the existing authorized evidence and keep the original scope.", "codex", "new-checkpoint-direction")
        release.set()
        await self.settle()
        final = await self.service.read_room(room)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        superseded = [event for event in final["events"] if event["kind"] == "objective_checkpoint_superseded"]
        self.assertEqual([event["job_id"] for event in superseded], held_job)
        self.assertEqual(len([event for event in final["events"] if event["kind"] == "objective_checkpoint_revision_requested"]), 1)
        self.assertIsNone(observed["checkpoints"][2]["objective"]["checkpoint_revision"])
        self.assertEqual(final["objective"]["stage"], 2)


    async def test_rejected_misclassified_scope_or_question_is_revised_before_any_human_gate(self):
        for decision in ("complete", "continue", "needs_user"):
            with self.subTest(decision=decision):
                base, observed = self.revision_scenario()
                def handler(agent, context, cwd):
                    result = base(agent, context, cwd)
                    if context["job"]["job_phase"] == "checkpoint" and len(observed["checkpoints"]) == 1:
                        result["commands"][0].update(decision=decision,
                            scope_reference="Incorrectly classified as unrelated scope.",
                            next_goal=None if decision == "needs_user" else "Misclassified remaining original requirement.",
                            question="An unnecessary question." if decision == "needs_user" else None)
                    return result
                self.fake.handler = handler
                created = await self.create()
                await self.settle()
                final = await self.service.read_room(created["room"]["id"])
                self.assertEqual(final["objective"]["status"], "complete", final["team"])
                self.assertEqual(len(observed["work"]), 2)
                self.assertEqual(len(observed["reviews"]), 3)
                revisions = [event for event in final["events"] if event["kind"] == "objective_checkpoint_revision_requested"]
                self.assertEqual(len(revisions), 1)
                self.assertEqual(revisions[0]["feedback"]["proposal"]["decision"], decision)
                self.assertNotEqual(observed["reviews"][0]["objective"]["checkpoint_proposal"]["proposal_digest"],
                                    observed["reviews"][1]["objective"]["checkpoint_proposal"]["proposal_digest"])
                self.assertFalse(any(event.get("code") in {"objective_checkpoint_disagreed", "objective_scope_confirmation", "objective_decision_required"}
                                     for event in final["events"]))
