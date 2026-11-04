"""Claude Code limit notices drift between releases; classify the observed ones."""
import json
import unittest

from local_agent_coordinator import adapters

SID = '6216d06d-043b-4c66-ad6a-c85a0de23b71'

# Wording observed in local Claude Code transcripts (2.1.247 onwards); only the
# reset time is varied. Before this change only the first two were recognized.
OBSERVED = [
    "You've hit your weekly limit · resets 4pm (America/Vancouver)",
    "You've hit your 5-hour limit · resets 12:30pm (America/Vancouver)",
    "You've hit your session limit · resets 4pm (America/Vancouver)",
    "You've hit your weekly limit · resets Oct 5 at 4pm (America/Vancouver)",
    "You've hit your 5-hour limit · resets 4pm (UTC)",
    "You've hit your individual spend limit · run /usage-credits to ask your admin for a higher limit · "
    "your session limit resets 4pm (America/Vancouver)",
    "You've hit your individual spend limit · run /usage-credits to ask your admin for a higher limit · "
    "your weekly limit resets Sep 29 at 4pm (America/Vancouver)",
    "You're out of usage credits. Switch to another model, or manage usage credits at "
    "claude.ai/settings/usage?from=cc_cli_limit_message, to continue.",
    "You're out of usage credits. Run /usage-credits to keep using Fable 5 or /model to switch models.",
]


def assistant_notice(text, **extra):
    return {'type': 'assistant', 'isApiErrorMessage': True, 'error': 'rate_limit', 'session_id': SID,
            'message': {'content': [{'type': 'text', 'text': text}]}, **extra}


def parse(*events):
    lines = [json.dumps({'type': 'system', 'subtype': 'init', 'session_id': SID}).encode()]
    lines += [json.dumps(event).encode() for event in events]
    return adapters.parse_events('claude', lines)


class ClaudeLimitWordingTests(unittest.TestCase):
    def test_every_observed_limit_notice_is_a_subscription_limit(self):
        # Catches: session/dated/UTC/spend/credits notices fell through to
        # unknown_provider_error, stopping the objective instead of quota handoff.
        for text in OBSERVED:
            with self.subTest(text=text):
                result = parse(assistant_notice(text),
                               {'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': SID, 'result': text})
                self.assertEqual(result['failure_code'], 'subscription_limit_reached')
                self.assertEqual(result['text'], '')

    def test_server_side_throttling_and_mentions_stay_generic(self):
        # Catches: widening the match into transient throttling or prose.
        for text in ("API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited",
                     "API Error: Overloaded", OBSERVED[2] + " PRIVATE", "Note: " + OBSERVED[0],
                     "You've hit your daily limit · resets 4pm (America/Vancouver)"):
            with self.subTest(text=text):
                result = parse(assistant_notice(text),
                               {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': SID})
                self.assertNotEqual(result.get('failure_code'), 'subscription_limit_reached')

    def test_subagent_refusal_does_not_override_the_main_turn_limit(self):
        # Catches: a Task/Agent subagent's API error (parent_tool_use_id) was
        # counted as unknown_provider_error and beat the main turn's limit.
        child = assistant_notice("API Error: Overloaded", parent_tool_use_id='toolu_child', isSidechain=True)
        main = {'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': SID, 'result': OBSERVED[2]}
        result = parse(child, main)
        self.assertEqual(result['failure_code'], 'subscription_limit_reached')
        self.assertIn({'type': 'warning', 'code': 'sidechain_provider_error'}, result['events'])


if __name__ == '__main__':
    unittest.main()
