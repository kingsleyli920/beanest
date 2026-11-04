"""Missing ideal evidence need not block authorized work or become user diagnosis."""
import unittest
from uuid import uuid4

import test_native_room_service as fixtures


class CheckpointInternalResolutionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = fixtures.NativeRoomServiceTests.asyncTearDown
    settle = fixtures.NativeRoomServiceTests.settle
    handler = fixtures.NativeRoomServiceTests.handler

    async def test_peer_rejects_replaceable_missing_evidence_then_internal_work_finishes_current_goal(self):
        original = self.handler
        attempts, works, reviews = [], [], []
        feedback = "Historical logs would help, but current behavior can be checked and corrected within existing authority. Continue internally."

        def handler(agent, context, cwd):
            phase, stage = context["job"]["job_phase"], context["objective"]["stage"]
            if phase == "work":
                works.append(context)
                self.assertEqual((cwd / "main.py").read_text(), f"VALUE = {stage}\n")
                (cwd / "main.py").write_text(f"VALUE = {stage + 1}\n")
                return fixtures.native_reply(context, [{"type": "complete", "task_id": "T1",
                    "result": "Updated the current value; the historical cause remains unconfirmed."}])
            if phase == "review":
                self.assertEqual((cwd / "main.py").read_text(), f"VALUE = {stage + 1}\n")
                return fixtures.native_reply(context, [{"type": "review", "task_id": "T1", "verdict": "accept",
                    "reason": "Read the exact new value and unchanged notes; no historical-cause claim is accepted."}])
            if phase == "checkpoint":
                attempts.append(context)
                first = len(attempts) == 1
                if stage == 1 and not first:
                    self.assertEqual(context["objective"]["checkpoint_revision"]["reason"], feedback)
                return fixtures.native_reply(context, [{"type": "checkpoint",
                    "decision": "needs_user" if first else "continue" if stage == 1 else "complete",
                    "summary": "Historical logs are unavailable." if first else
                        "Current behavior still needs the permitted correction." if stage == 1 else
                        "Current value corrected to three; the historical root cause remains unconfirmed.",
                    "next_goal": "Check and correct the current value within the existing task." if stage == 1 and not first else None,
                    "scope_reference": context["objective"]["scope_reference"],
                    "question": "Find the historical logs before any more development." if first else None}])
            if phase == "checkpoint_review":
                proposal = context["objective"]["checkpoint_proposal"]
                reviews.append(proposal)
                reject = proposal["decision"] == "needs_user"
                return fixtures.native_reply(context, [{"type": "checkpoint_review",
                    "verdict": "changes_requested" if reject else "accept",
                    "reason": feedback if reject else "The current task and the remaining evidence limits are stated accurately.",
                    "proposal_digest": proposal["proposal_digest"]}])
            return original(agent, context, cwd)

        self.fake.handler = handler
        goal = "Correct the current value to three and preserve notes; keep historical-cause uncertainty explicit."
        created = await self.service.create_room(goal, "claude", str(uuid4()), mode="native_project",
            project_id=self.registration["project"]["id"], participants=["claude", "codex"], work_mode="build",
            runtime_settings={"permission": "full_access", "members": {}}, execution_policy={"mode": "autonomous"})
        await self.settle()
        final = await self.service.read_room(created["room"]["id"])
        self.assertEqual(final["objective"]["id"], created["objective"]["id"])
        self.assertEqual(final["objective"]["original_goal"], goal)
        self.assertEqual(final["objective"]["status"], "complete", final["team"])
        self.assertEqual(final["objective"]["stage"], 2)
        self.assertEqual(final["room"]["runtime_settings"], created["room"]["runtime_settings"])
        self.assertEqual([proposal["decision"] for proposal in reviews], ["needs_user", "continue", "complete"])
        self.assertNotEqual(reviews[0]["proposal_digest"], reviews[1]["proposal_digest"])
        self.assertEqual(len(works), 2)
        self.assertEqual([event["decision"] for event in final["events"] if event["kind"] == "objective_checkpoint_finished"],
                         ["continue", "complete"])
        self.assertFalse(any(event.get("code") == "objective_decision_required" for event in final["events"]))
        self.assertEqual(self.service._project(final["room"]["id"]).files_for_paths(["main.py"])[0]["content"], "VALUE = 3\n")
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")
        for call in self.fake.calls:
            if call["context"]["job"]["job_phase"] in {"checkpoint", "checkpoint_review"}:
                self.assertIn("Distinguish ideal diagnostic evidence from a genuine execution blocker", call["prompt"])
                self.assertIn("checkpoint reviewer must request changes", call["prompt"])
                self.assertIn("current authority and available inputs", call["prompt"])


if __name__ == "__main__":
    unittest.main()
