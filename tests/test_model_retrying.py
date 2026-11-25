"""A partner whose CLI cannot reach its model says so on the working line.

In the 九烛 room Codex spent 85 minutes retrying a dropped model stream: the
room looked stuck, with nothing on screen, though its CLI reported every
failed attempt.
"""
import json
import unittest

from local_agent_coordinator import native_adapters as native
from local_agent_coordinator import room_projection
from tests import test_partner_resolved_blocks as blocks

THREAD = "01a20f65-7301-7d23-8865-ba4552880eff"


class ModelRetryStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_failed_attempt_is_reported_by_number_only_and_the_call_goes_on(self):
        # Catches a retry going unreported, the provider's message (which can quote a request) leaving
        # the parser, or a retry ending the call.
        seen = []
        stream = native._Stream("codex", True, seen.append)
        await stream.consume(json.dumps({"type": "thread.started", "thread_id": THREAD}).encode())
        await stream.consume(json.dumps({"type": "turn.started"}).encode())
        for attempt in range(1, 4):
            await stream.consume(json.dumps({"type": "error", "message":
                f"Reconnecting... {attempt}/5 (stream disconnected; request sk-SECRET-{attempt})"}).encode())
        retries = [event for event in seen if event["type"] == "native_model_retrying"]
        self.assertEqual(retries, [{"type": "native_model_retrying", "attempt": 1},
                                   {"type": "native_model_retrying", "attempt": 2},
                                   {"type": "native_model_retrying", "attempt": 3}])
        self.assertNotIn("SECRET", json.dumps(seen))
        self.assertIsNone(stream.failure)


class ModelRetryProjectionTests(blocks.PartnerResolvedBlockStoreTests):
    # The block cases run in test_partner_resolved_blocks; here only the cases below.
    locals().update({name: None for name in dir(blocks.PartnerResolvedBlockStoreTests) if name.startswith("test_")})

    def live(self, room, job_id):
        state = room_projection.room_state(self.store.snapshot(room), self.store.job_roles(room), lambda *_: None)
        return next(item for item in state["live"]["jobs"] if item["id"] == job_id)

    def test_the_working_job_shows_its_latest_retry_until_it_does_something_else(self):
        # Catches the retry never reaching the room state, a stale retry kept after the partner got
        # going again, or a finished call recording one.
        room = self.plan(paths=True)
        job = self.store.claim(room, "claude")
        self.assertIsNone(self.live(room, job["id"])["model_retrying"])
        self.store.record_model_retrying(job["id"], 1)
        self.now += 30
        self.store.record_model_retrying(job["id"], 2)
        self.assertEqual(self.live(room, job["id"])["model_retrying"], {"attempt": 2, "created_at": self.now})
        self.store.record_native_activity(job["id"], {"kind": "Bash", "state": "started"})
        self.assertIsNone(self.live(room, job["id"])["model_retrying"], "the partner is working again")
        self.finish(job, [{"type": "complete", "task_id": job["team"]["task_id"], "result": "Concrete result."}])
        before = len(self.events(room, "native_model_retrying"))
        self.store.record_model_retrying(job["id"], 3)
        self.assertEqual(len(self.events(room, "native_model_retrying")), before, "a finished call records nothing")


if __name__ == "__main__":
    unittest.main()
