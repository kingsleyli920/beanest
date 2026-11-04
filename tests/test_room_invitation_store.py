"""Invitation membership transactions and independent outbox intent."""
import tempfile
from pathlib import Path
import unittest
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError, RoomStore


class RoomInvitationStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="beanest-invite-store-")
        self.store = RoomStore(Path(self.directory.name) / "rooms.sqlite3",
                               agents={"m" + str(i): "Peer " + str(i) for i in range(10)})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def assertCode(self, code, operation):
        with self.assertRaises(RoomError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_capacity_and_unsupported_legacy_mode_never_partially_change_members(self):
        room, _ = self.store.create_room("Existing chat", "m0", str(uuid4()), participants=["m" + str(i) for i in range(7)])
        before = self.store.snapshot(room)
        self.assertCode("member_limit_reached", lambda: self.store.invite_members(room, ["m7", "m8"], str(uuid4())))
        self.assertEqual(self.store.snapshot(room), before)
        self.assertEqual(self.store.invite_members(room, ["m7"], str(uuid4())), ["m7"])
        joined = self.store.snapshot(room)
        self.assertEqual(self.store.invite_members(room, ["m7"], str(uuid4())), [])
        self.assertEqual(self.store.snapshot(room), joined)
        legacy, _ = self.store.create_room("Legacy files", "m0", str(uuid4()), participants=["m0"], mode="python_artifacts")
        previous = self.store.snapshot(legacy)
        self.assertCode("invitation_mode_unsupported", lambda: self.store.invite_members(legacy, ["m1"], str(uuid4())))
        self.assertEqual(self.store.snapshot(legacy), previous)

    def test_joined_pending_requires_a_real_post_join_claim_and_text_invites_stay_idle(self):
        room, _ = self.store.create_room("Text", "m0", str(uuid4()), participants=["m0"])
        original_jobs = self.store.snapshot(room)["jobs"]
        self.store.invite_members(room, ["m1"], str(uuid4()))
        joined = self.store.snapshot(room)
        self.assertEqual(joined["jobs"], original_jobs)
        self.assertEqual({member["id"]: member["joined_pending"] for member in joined["members"]}, {"m0": False, "m1": True})
        self.store.send_message(room, "Please handle this supplement", "m1", str(uuid4()))
        self.assertTrue(next(member for member in self.store.snapshot(room)["members"] if member["id"] == "m1")["joined_pending"])
        self.assertIsNotNone(self.store.claim(room, "m1"))
        self.assertFalse(next(member for member in self.store.snapshot(room)["members"] if member["id"] == "m1")["joined_pending"])

    def test_stop_does_not_turn_staged_invitation_into_resume_but_archive_still_blocks_it(self):
        room, _ = self.store.create_room("Text", "m0", str(uuid4()), participants=["m0"])
        source = self.store.snapshot(room)["events"][-1]["seq"]
        self.store.note_outbox_stop(room)
        scope = self.store.user_outbox_scope("a" * 64)
        request_id = str(uuid4())
        body = {"request_id": request_id, "participants": ["m1"]}
        item = self.store.stage_outbox(scope, request_id, "invite", room, body,
            self.store.invitation_parameters(room, body["participants"]), "first-instance", source_seq=source)["outbox"]
        self.assertTrue(item["delivery"]["can_send"])
        self.store.note_outbox_stop(room)
        self.assertTrue(self.store.read_outbox(scope, request_id, instance_id="first-instance")["outbox"]["delivery"]["can_send"])
        self.store.request_room_lifecycle(room, "archive", str(uuid4()))
        final = self.store.read_outbox(scope, request_id, instance_id="first-instance")["outbox"]
        self.assertFalse(final["delivery"]["can_send"])
        self.assertEqual(final["delivery"]["reason"], "outbox_intervening_control")
        self.assertCode("room_archived", lambda: self.store.invite_members(room, ["m1"], str(uuid4())))


if __name__ == "__main__":
    unittest.main()
