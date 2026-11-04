"""Response presentation must not invent or discard JSON values."""
import json
import unittest

from local_agent_coordinator import adapters


class ResponseObjectTests(unittest.TestCase):
    def parse(self, text):
        return adapters.parse_response_object(text)

    def test_pure_fenced_and_independent_preamble_objects(self):
        value = {"message": "Fictional response", "commands": []}
        encoded = json.dumps(value)
        for text in (encoded, " \n" + encoded + "\n", "```json\n" + encoded + "\n```",
                     "```JSON\r\n" + encoded + "\r\n```", "```\n" + encoded + "\n```",
                     "已接手，下面是当前处理安排。\n\n" + encoded,
                     '已核对 "main"，下面是处理安排。\n\n' + encoded,
                     '已核对 `main`，下面是处理安排。\n\n' + encoded,
                     '已按[检查项]复核，结论为[通过]。\n\n' + encoded,
                     'Review [PASS] and nested [[review label]] references.\n' + encoded,
                     '已引用 "[检查项]" 和 `[review label]`。\n' + encoded,
                     'Earlier [array]\n' + encoded,
                     '审阅[检查项]后，回复如下。\n```json\n' + encoded + '\n```',
                     '已核对 "main"，下面是处理安排。\n\n```json\n' + encoded + '\n```',
                     "Here is the response:\n  " + encoded + " \n",
                     "Current status\nResponse follows.\n\n" + encoded):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), value)

    def test_ambiguous_prefixes_and_incomplete_envelopes_are_rejected(self):
        for text in ('Intro {}', 'Intro\n{}\nTail', '{}\n{}', 'Intro\n{}\n{}',
                     '[]\n{}', 'true\n{}', '12\n{}', 'null\n{}', '"intro"\n{}',
                     'Earlier {object}\n{}', 'Earlier [1,2]\n{}', 'Earlier ["array"]\n{}',
                     'Earlier [[false]]\n{}', 'Earlier []\n{}', '[label]\n{}',
                     'Earlier [1,]\n{}', 'Earlier [null]\n{}', 'Earlier [NaN]\n{}',
                     'Earlier [unclosed\n{}', 'Earlier ]label[\n{}',
                     '```text\nIntro\n```\n{}',
                     'Intro\n```json\n{}\n```\nTail', '```json\n{}\n```\nTail',
                     'Intro\n[]', 'Intro\n{"a":1,"a":2}', 'Intro\n{"a":NaN}',
                     'Intro\n{"a":Infinity}', 'Intro\n{"a":', 'Intro\n{"a":1} trailing'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.parse(text)

    def test_strict_json_transport_parser_still_rejects_presentation(self):
        for text in ('Intro\n{}', '```json\n{}\n```'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    adapters._strict_json(text)


if __name__ == '__main__':
    unittest.main()
