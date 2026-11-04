"""Completion reports and reviewed optional follow-ups remain distinct."""
import asyncio
import unittest

import test_native_room_service as fixtures


class CompletionFollowupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = fixtures.NativeRoomServiceTests.asyncTearDown
    settle = fixtures.NativeRoomServiceTests.settle
    create = fixtures.NativeRoomServiceTests.create
    handler = fixtures.NativeRoomServiceTests.handler

    async def test_reviewed_followup_is_persisted_separately_and_never_starts_itself(self):
        summary = "The requested value is two and the existing notes are unchanged."
        suggestion = "查看交付的 main.py 和 notes.md，确认结果符合预期。"
        reviews = []
        original = self.handler

        def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "checkpoint":
                return fixtures.native_reply(context, [{"type": "checkpoint", "decision": "complete",
                    "summary": summary, "next_goal": suggestion,
                    "scope_reference": context["objective"]["scope_reference"], "question": None}])
            if phase == "checkpoint_review":
                proposal = context["objective"]["checkpoint_proposal"]
                self.assertEqual((proposal["summary"], proposal["next_goal"]), (summary, suggestion))
                reviews.append(proposal["proposal_digest"])
            return original(agent, context, cwd)

        self.fake.handler = handler
        created = await self.create()
        room_id = created["room"]["id"]
        await self.settle()
        snapshot = await self.service.read_room(room_id)
        objective, run = snapshot["objective"], snapshot["current_run"]
        step = objective["next_step"]
        self.assertEqual(objective["status"], "complete")
        self.assertEqual((step["summary"], step["goal"]), (summary, suggestion))
        self.assertEqual(step["scope"], "new_goal")
        self.assertEqual(step["review_kind"], "independent_review")
        self.assertEqual(step["binding"], {"run_id": run["id"], "revision": run["revision"], "digest": run["digest"]})
        self.assertEqual(reviews, [objective["checkpoint_proposal"]["proposal_digest"]])
        self.assertFalse(step["can_start"])
        self.assertEqual(len(snapshot["runs"]), 1)
        self.assertEqual(sum(call["context"]["job"]["job_phase"] == "work" for call in self.fake.calls), 1)
        assessment_calls = [call for call in self.fake.calls
                            if call["context"]["job"]["job_phase"] in {"checkpoint", "checkpoint_review"}]
        self.assertEqual(len(assessment_calls), 2)
        for call in assessment_calls:
            self.assertIn("suggested next action for the user in next_goal", call["prompt"])
            self.assertIn("not permission or an instruction to start work", call["prompt"])
        calls = len(self.fake.calls)
        await asyncio.sleep(0)
        self.assertEqual(len(self.fake.calls), calls)
        state = await self.service.read_room_state(room_id)
        self.assertEqual(state["objective"]["next_step"]["goal"], suggestion)
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")

    async def test_legacy_null_followup_still_completes_without_inventing_a_suggestion(self):
        created = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        self.assertEqual(snapshot["objective"]["status"], "complete")
        self.assertIsNone(snapshot["objective"]["next_step"]["goal"])
        self.assertEqual(snapshot["objective"]["next_step"]["summary"], "Requested update complete.")
        self.assertEqual(len(snapshot["runs"]), 1)


if __name__ == "__main__":
    unittest.main()
