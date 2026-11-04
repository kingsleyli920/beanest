import concurrent.futures
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_store import RoomError, RoomStore


class RoomStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "rooms.sqlite3"
        self.now = 1000.0
        self.store = RoomStore(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def room(self, target="claude"):
        return self.store.create_room("A shared goal", target, str(uuid4()))[0]

    def assertCode(self, code, operation):
        with self.assertRaises(RoomError) as error:
            operation()
        self.assertEqual(error.exception.code, code)

    def review_chain(self, initial="claude", peer="codex"):
        room_id = self.room(initial)
        author = self.store.claim(room_id, initial)
        self.store.finish_job(author["id"], message="Original draft", artifact_revision=1,
                              action={"to": peer, "kind": "review", "text": "Review the draft"})
        return room_id, author, self.store.claim(room_id, peer)

    def test_create_and_message_idempotency_and_conflict(self):
        room_id, fresh = self.store.create_room("Goal", "both", "create-key")
        self.assertTrue(fresh)
        self.assertEqual(self.store.create_room("Goal", "both", "create-key"), (room_id, False))
        self.assertCode("request_conflict", lambda: self.store.create_room("Other", "both", "create-key"))
        self.assertTrue(self.store.send_message(room_id, "Clarify", "claude", "message-key"))
        self.assertFalse(self.store.send_message(room_id, "Clarify", "claude", "message-key"))
        self.assertEqual(len(self.store.snapshot(room_id)["jobs"]), 3)

    def test_atomic_claim_and_room_global_sequence(self):
        room_id = self.room()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.claim(room_id, "claude"), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.store.snapshot(room_id)["budget"]["used"], 1)
        other = self.room("codex")
        sequences = [event["seq"] for room in (room_id, other)
                     for event in self.store.snapshot(room)["events"]]
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertEqual(sequences, sorted(sequences))

    def test_mcp_origin_is_recorded_without_impersonating_user_or_provider(self):
        room_id, _ = self.store.create_room('External goal', 'claude', 'mcp-start', source='mcp')
        self.store.send_message(room_id, 'External detail', 'codex', 'mcp-detail', source='mcp')
        events = self.store.snapshot(room_id)['events']
        self.assertEqual({event['sender'] for event in events}, {'mcp'})
        self.assertCode('request_conflict', lambda: self.store.create_room(
            'External goal', 'claude', 'mcp-start'))
        self.assertCode('invalid_request', lambda: self.store.create_room(
            'Spoof', 'claude', 'spoof', source='codex'))

    def test_obsolete_queue_budget_does_not_block_new_user_or_peer_work(self):
        self.store.close()
        self.store = RoomStore(self.path, max_calls=2, clock=lambda: self.now)
        room_id = self.room("both")
        self.assertTrue(self.store.send_message(room_id, "More", "claude", "extra"))
        self.assertEqual(len(self.store.snapshot(room_id)["jobs"]), 3)
        job = self.store.claim(room_id, "claude")
        self.store.finish_job(job["id"], message="Visible result", session_id=str(uuid4()),
                              action={"to": "codex", "kind": "help", "text": "Help"})
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(len(snapshot["jobs"]), 4)
        self.assertIsNone(snapshot["budget"]["limit"])
        self.assertFalse(any(event.get("code") == "budget_exhausted" for event in snapshot["events"]))

    def test_direct_request_back_returns_answer_once_and_preserves_parent_artifact_and_reply_chain(self):
        for initial, peer in (("claude", "codex"), ("codex", "claude")):
            for kind in ("review", "help"):
                with self.subTest(initial=initial, kind=kind):
                    room_id, author, reviewer = self.review_chain(initial, peer)
                    action = {"to": initial, "kind": kind, "text": "Inspect my new independent tests"}
                    self.store.finish_job(reviewer["id"], message="Draft findings and new tests", artifact_revision=2,
                                          action=action)
                    # Repeating a completed finish cannot enqueue another copy.
                    self.store.finish_job(reviewer["id"], message="Duplicate", action=action)
                    snapshot = self.store.snapshot(room_id)
                    self.assertEqual(len(snapshot["jobs"]), 3)
                    followup = snapshot["jobs"][-1]
                    self.assertEqual((followup["provider"], followup["kind"], followup["text"]),
                                     (initial, kind, action["text"]))
                    self.assertEqual(followup["parent_id"], reviewer["id"])
                    statuses = {member["id"]: member["status"] for member in snapshot["members"]}
                    self.assertEqual(statuses, {initial: "queued", peer: "waiting_peer"})
                    parent = next(event for event in snapshot["events"]
                                  if event["kind"] == "message" and event.get("job_id") == reviewer["id"])
                    self.assertEqual(parent["text"], "Draft findings and new tests")
                    self.assertEqual(parent["artifact_revision"], 2)
                    claimed = self.store.claim(room_id, initial)
                    self.assertEqual(claimed["reply_to"], peer)
                    self.store.finish_job(claimed["id"], message="Tests reviewed", artifact_revision=2)
                    feedback = self.store.claim(room_id, peer)
                    self.assertEqual(feedback["kind"], "feedback")
                    self.assertEqual(feedback["parent_id"], claimed["id"])
                    self.assertIsNone(feedback["reply_to"])
                    self.store.finish_job(feedback["id"], message="Review received")
                    complete = self.store.snapshot(room_id)
                    self.assertEqual([job["provider"] for job in complete["jobs"]], [initial, peer, initial, peer])
                    self.assertEqual(complete["budget"]["used"], 4)
                    self.assertEqual(complete["room"]["status"], "completed")

    def test_request_to_third_member_keeps_both_feedback_and_new_request(self):
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now,
                               agents={"claude": "Claude", "codex": "Codex", "helper": "Helper"})
        room_id, _author, reviewer = self.review_chain()
        self.store.finish_job(reviewer["id"], message="Findings for original author",
                              action={"to": "helper", "kind": "help", "text": "A distinct new question"})
        children = [job for job in self.store.snapshot(room_id)["jobs"] if job["parent_id"] == reviewer["id"]]
        self.assertEqual([(job["provider"], job["kind"], job["text"]) for job in children],
                         [("claude", "feedback", "Findings for original author"),
                          ("helper", "help", "A distinct new question")])
        original = self.store.claim(room_id, "claude")
        self.assertIsNone(original["reply_to"])
        self.store.finish_job(original["id"], message="Feedback received")
        helper = self.store.claim(room_id, "helper")
        self.assertEqual(helper["reply_to"], "codex")
        self.store.finish_job(helper["id"], message="Answer to distinct question")
        returned = self.store.claim(room_id, "codex")
        self.assertEqual(returned["parent_id"], helper["id"])
        self.assertEqual(returned["kind"], "feedback")
        self.store.finish_job(returned["id"], message="Thanks")
        self.assertEqual(self.store.snapshot(room_id)["room"]["status"], "completed")

    def test_same_target_request_and_feedback_are_not_blocked_by_obsolete_slots(self):
        self.store.close()
        self.store = RoomStore(self.path, max_calls=3, clock=lambda: self.now)
        room_id, _author, reviewer = self.review_chain()
        self.store.finish_job(reviewer["id"], message="Found an issue",
                              action={"to": "claude", "kind": "help", "text": "Fix this specific issue"})
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["budget"]["used"], 2)
        self.assertEqual(len(snapshot["jobs"]), 3)
        self.assertEqual(snapshot["jobs"][-1]["kind"], "help")
        self.assertEqual(snapshot["jobs"][-1]["text"], "Fix this specific issue")
        self.assertFalse(any(event.get("code") == "budget_exhausted" for event in snapshot["events"]))
        followup = self.store.claim(room_id, "claude")
        self.store.finish_job(followup["id"], message="Specific issue addressed")
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["budget"]["used"], 3)
        self.assertEqual(len(snapshot["jobs"]), 4)
        feedback = self.store.claim(room_id, "codex")
        self.assertEqual(feedback["kind"], "feedback")
        self.store.finish_job(feedback["id"], message="Correction received.")
        self.assertEqual(self.store.snapshot(room_id)["room"]["status"], "completed")

    def test_obsolete_two_call_cap_does_not_drop_an_answer_or_review_request(self):
        self.store.close()
        self.store = RoomStore(self.path, max_calls=2, clock=lambda: self.now)
        for action in (None, {"to": "claude", "kind": "review", "text": "Review my new tests"}):
            with self.subTest(action=action):
                room_id, _author, reviewer = self.review_chain()
                self.store.finish_job(reviewer["id"], message="An important answer", action=action)
                snapshot = self.store.snapshot(room_id)
                self.assertEqual(snapshot["budget"]["used"], 2)
                self.assertEqual(len(snapshot["jobs"]), 3)
                self.assertEqual(snapshot["room"]["status"], "running")
                self.assertFalse(any(event.get("code") == "budget_exhausted" for event in snapshot["events"]))
                returned = self.store.claim(room_id, "claude")
                self.assertIsNotNone(returned)
                self.assertEqual(returned["kind"], "review" if action else "feedback")

    def test_return_does_not_override_requester_already_queued_or_working(self):
        self.store.close()
        self.store = RoomStore(self.path, max_calls=3, clock=lambda: self.now)
        for status in ("queued", "working"):
            with self.subTest(status=status):
                room_id, _author, reviewer = self.review_chain()
                self.store.send_message(room_id, "Independent user followup", "claude", str(uuid4()))
                if status == "working":
                    self.assertIsNotNone(self.store.claim(room_id, "claude"))
                self.store.finish_job(reviewer["id"], message="Return to original author",
                                      action={"to": "claude", "kind": "help", "text": "A new question"})
                snapshot = self.store.snapshot(room_id)
                original = next(member for member in snapshot["members"] if member["id"] == "claude")
                self.assertEqual(original["status"], status)
                self.assertEqual(len(snapshot["jobs"]), 4)

    def test_restart_marks_unfinished_work_interrupted_without_replay(self):
        room_id = self.room("both")
        job = self.store.claim(room_id, "claude")
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now)
        self.store.recover()
        snapshot = self.store.snapshot(room_id)
        self.assertEqual(snapshot["room"]["status"], "interrupted")
        self.assertEqual({job["status"] for job in snapshot["jobs"]}, {"interrupted"})
        self.assertIsNone(self.store.claim(room_id, "claude"))
        self.assertTrue(next(member for member in snapshot["members"] if member["id"] == "claude")["identity_uncertain"])
        self.assertCode("room_not_active", lambda: self.store.send_message(room_id, "Retry", "claude", "retry"))
        self.assertEqual(snapshot["budget"]["used"], 1)

    def test_private_files_instance_lock_and_symlink_refusal(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertCode("runtime_in_use", lambda: RoomStore(self.path))
        link = self.path.parent / "link.sqlite3"
        link.symlink_to(self.path)
        self.assertCode("invalid_state_path", lambda: RoomStore(link))
        self.store.close()
        self.store = RoomStore(self.path)

    def test_old_deadline_does_not_prevent_claim_and_invalid_inputs_do_not_reserve(self):
        self.assertCode("invalid_text", lambda: self.store.create_room("\ud800", "claude", "bad"))
        self.assertCode("invalid_target", lambda: self.store.create_room("Goal", "outsider", "bad2"))
        self.assertEqual(self.store.list_rooms(), {"rooms": []})
        room_id = self.room()
        with self.store._connection(write=True) as db:
            db.execute("UPDATE rooms SET deadline=? WHERE id=?", (self.now + 900, room_id))
        self.now += 901
        job = self.store.claim(room_id, "claude")
        self.assertIsNotNone(job)
        self.assertIsNone(job["deadline"])
        self.assertEqual(self.store.snapshot(room_id)["budget"]["used"], 1)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT deadline FROM rooms WHERE id=?", (room_id,)).fetchone()[0], 1900)

    def test_membership_names_and_all_targets_are_fixed_per_room(self):
        old = self.room()
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now,
                               agents={"claude": "Renamed Claude", "codex": "Codex", "helper": "Local Helper"})
        self.store.send_message(old, "For original members", "all", "old-all")
        previous = self.store.snapshot(old)
        self.assertEqual([member["id"] for member in previous["members"]], ["claude", "codex"])
        self.assertEqual(previous["members"][0]["name"], "Claude Code")
        self.assertCode("invalid_target", lambda: self.store.send_message(old, "No new member", "helper", "intrude"))
        new = self.room("all")
        snapshot = self.store.snapshot(new)
        self.assertEqual({job["provider"] for job in snapshot["jobs"]}, {"claude", "codex", "helper"})
        self.assertEqual({member["id"]: member["name"] for member in snapshot["members"]},
                         {"claude": "Renamed Claude", "codex": "Codex", "helper": "Local Helper"})

    def test_legacy_member_name_migration_preserves_unknown_ids(self):
        room_id = self.room()
        # Removing the added column reproduces the actual pre-registry members schema.
        with self.store._connection(write=True) as db:
            db.execute("ALTER TABLE members DROP COLUMN name")
            db.execute("INSERT INTO members(room_id,provider,status) VALUES (?,?,'idle')", (room_id, "retired"))
        self.store.close()
        self.store = RoomStore(self.path, clock=lambda: self.now)
        names = {member["id"]: member["name"] for member in self.store.snapshot(room_id)["members"]}
        self.assertEqual(names, {"claude": "Claude Code", "codex": "Codex", "retired": "retired"})
        self.assertCode("agent_unavailable", lambda: self.store.send_message(room_id, "No rerouting", "retired", "retired-new"))


if __name__ == "__main__":
    unittest.main()
