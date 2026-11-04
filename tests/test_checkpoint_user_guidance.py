"""Needs-user guidance reaches both peers without changing the old protocol."""
import unittest

import test_native_room_service as fixtures


class CheckpointUserGuidanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeRoomServiceTests.asyncSetUp
    asyncTearDown = fixtures.NativeRoomServiceTests.asyncTearDown
    settle = fixtures.NativeRoomServiceTests.settle
    create = fixtures.NativeRoomServiceTests.create
    handler = fixtures.NativeRoomServiceTests.handler

    async def ask(self, question):
        summary = "The local file change is ready; public release has not been performed."
        original = self.handler
        reviewed = []

        def handler(agent, context, cwd):
            phase = context["job"]["job_phase"]
            if phase == "checkpoint":
                return fixtures.native_reply(context, [{"type": "checkpoint", "decision": "needs_user",
                    "summary": summary, "next_goal": None,
                    "scope_reference": context["objective"]["scope_reference"], "question": question}])
            if phase == "checkpoint_review":
                proposal = context["objective"]["checkpoint_proposal"]
                self.assertEqual((proposal["question"], proposal["summary"]), (question, summary))
                reviewed.append(proposal["proposal_digest"])
            return original(agent, context, cwd)

        self.fake.handler = handler
        created = await self.create()
        await self.settle()
        snapshot = await self.service.read_room(created["room"]["id"])
        objective = snapshot["objective"]
        self.assertEqual(objective["status"], "needs_user")
        self.assertEqual(objective["next_step"]["question"], question)
        self.assertEqual(objective["next_step"]["summary"], summary)
        self.assertIsNone(objective["next_step"]["goal"])
        self.assertEqual(objective["next_step"]["review_kind"], "independent_review")
        self.assertEqual(reviewed, [objective["checkpoint_proposal"]["proposal_digest"]])
        self.assertEqual(len(snapshot["runs"]), 1)
        self.assertFalse(any(job["status"] in {"queued", "working"} for job in snapshot["jobs"]))
        self.assertEqual(snapshot["room"]["runtime_settings"], created["room"]["runtime_settings"])
        return snapshot

    async def test_actionable_question_is_reviewed_separately_without_starting_or_authorizing_work(self):
        question = "需要你确认是否允许公开发布成果。请回复“允许发布”或“暂不发布”；目前没有发布。"
        snapshot = await self.ask(question)
        self.assertEqual(snapshot["objective"]["original_goal"], "Update the existing value and preserve notes.")
        calls = [call for call in self.fake.calls
                 if call["context"]["job"]["job_phase"] in {"checkpoint", "checkpoint_review"}]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn("one concrete next step", call["prompt"])
            self.assertIn("Not collected or not covered does not mean absent", call["prompt"])
            self.assertIn("never execute it inside this checkpoint", call["prompt"])
        self.assertEqual((self.source / "main.py").read_text(), "VALUE = 1\n")

    async def test_legacy_question_remains_valid_without_a_new_required_field_or_content_gate(self):
        await self.ask("Please provide the missing input.")


if __name__ == "__main__":
    unittest.main()
