"""Token counts read from a Kimi session's wire file. Synthetic wire files, shaped like the real records."""
import json
import tempfile
import unittest
from pathlib import Path

from local_agent_coordinator import kimi_acp, workload


def counts(other, output, read=0, creation=0):
    return {"inputOther": other, "output": output, "inputCacheRead": read, "inputCacheCreation": creation}


def ledger(usage, scope="turn", **extra):
    return {"type": "usage.record", "usage": usage, "usageScope": scope, **extra}


def step_end(usage, message_id="chat-1"):
    return {"type": "context.append_loop_event", "agentId": "main",
            "event": {"type": "step.end", "turnId": "0", "step": 1, "messageId": message_id, "usage": usage}}


def appended(usage, message_id="chat-1"):
    return {"type": "agent.message.appended", "kind": "event",
            "message": {"message": {"role": "assistant"}, "meta": {"usage": usage, "messageId": message_id}}}


def model_call(usage, message_id="chat-1", **ledger_extra):
    """One model call as the CLI writes it: the same counts in three places."""
    return [ledger(usage, **ledger_extra), step_end(usage, message_id), appended(usage, message_id)]


class TurnUsage(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "wire.jsonl"

    def write(self, rows, mode="w"):
        with self.path.open(mode, encoding="utf-8") as stream:
            for row in rows:
                stream.write((row if isinstance(row, str) else json.dumps(row)) + "\n")

    def read(self, offset=0):
        return kimi_acp.turn_usage(self.path, kimi_acp.wire_offset(self.path)[:1] + (offset,))

    def test_one_call_repeated_in_three_paths_counts_once(self):
        # Catches a call being tripled because the same counts appear at /usage, /event/usage and /message/meta/usage.
        self.write(model_call(counts(100, 10, 1000, 7)))
        self.assertEqual(self.read(), {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 1000,
                                       "cache_creation_input_tokens": 7})

    def test_every_model_call_of_the_turn_is_added(self):
        # Catches only the last step of a multi-step turn being kept, which would make tool-heavy work look cheap.
        self.write([*model_call(counts(100, 10), "chat-1"), *model_call(counts(200, 20, 500), "chat-2")])
        self.assertEqual(self.read(), {"input_tokens": 300, "output_tokens": 30, "cache_read_input_tokens": 500,
                                       "cache_creation_input_tokens": 0})

    def test_a_compaction_call_scoped_to_the_session_still_counts(self):
        # Catches compaction, which spends real quota, being dropped because it is not scoped to the turn.
        self.write([ledger(counts(9000, 100), scope="session"), *model_call(counts(10, 1))])
        self.assertEqual(self.read()["input_tokens"], 9010)

    def test_older_records_before_the_offset_are_ignored(self):
        # Catches a resumed session being charged again for everything earlier calls already reported.
        self.write(model_call(counts(50000, 5000), "old"))
        offset = self.path.stat().st_size
        self.write(model_call(counts(10, 1), "new"), mode="a")
        self.assertEqual(self.read(offset)["input_tokens"], 10)

    def test_step_end_is_used_when_no_ledger_record_exists(self):
        # Catches a wire without usage.record losing its counts, or message records doubling the step records.
        self.write([step_end(counts(70, 7), "chat-1"), appended(counts(70, 7), "chat-1"),
                    step_end(counts(70, 7), "chat-1")])
        self.assertEqual(self.read()["input_tokens"], 70)

    def test_message_meta_is_the_last_resort_and_repeats_collapse(self):
        # Catches the appended-message path being skipped, or a repeated message being added twice.
        self.write([appended(counts(80, 8), "chat-1"), appended(counts(80, 8), "chat-1"), appended(counts(5, 1), "chat-2")])
        self.assertEqual(self.read()["input_tokens"], 85)

    def test_another_agents_records_are_not_this_calls(self):
        # Catches a sub-agent's records written alongside being charged to the main call.
        self.write([ledger(counts(9999, 9), agentId="helper"), ledger(counts(1, 1), agentId="main")])
        self.assertEqual(self.read()["input_tokens"], 1)

    def test_no_usage_is_none_not_zero(self):
        # Catches a turn with nothing reported looking like a free call instead of an unknown one.
        self.write([{"type": "turn.prompt", "input": []}, {"type": "turn.ended", "turnId": 0, "reason": "completed"}])
        self.assertIsNone(self.read())

    def test_damaged_or_unreadable_input_stays_none(self):
        # Catches a torn line or a wrong-typed counter producing a partial total that looks exact.
        cases = {"broken json": ["{not json"], "torn last line": None,
                 "wrong type": [ledger({**counts(1, 1), "output": "many"})],
                 "negative": [ledger({**counts(1, 1), "inputOther": -1})],
                 "no known field": [ledger({"unrelated": 5})], "not an object": [ledger(7)]}
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.path.unlink(missing_ok=True)
                if rows is None:
                    self.write(model_call(counts(1, 1)))
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(ledger(counts(1, 1))))
                else:
                    self.write([*model_call(counts(1, 1)), *rows])
                self.assertIsNone(self.read())
        self.assertIsNone(kimi_acp.turn_usage(self.path.with_name("missing.jsonl"), (None, 0)))

    def test_a_replaced_or_shortened_wire_says_nothing(self):
        # Catches a file swapped under the session being read as this call's records.
        self.write(model_call(counts(1, 1)))
        info = self.path.stat()
        self.assertIsNone(kimi_acp.turn_usage(self.path, ((info.st_dev, info.st_ino + 1), 0)))
        self.assertIsNone(kimi_acp.turn_usage(self.path, ((info.st_dev, info.st_ino), info.st_size + 1)))

    def test_the_ledger_charges_kimi_by_what_it_really_used(self):
        # Catches the conversion producing fields the ledger cannot price; a short coordination call must cost little.
        self.write(model_call(counts(100, 10, 1000)))
        usage = self.read()
        self.assertEqual(workload.usage_points("kimi", {"values": usage}), 250.0)


if __name__ == "__main__":
    unittest.main()
