import hashlib
import json
import unittest

from local_agent_coordinator.message_capacity import MessageCapacityError, checked_message_text


class MessageCapacityTests(unittest.TestCase):
    def assert_code(self, code, function, value):
        with self.assertRaises(MessageCapacityError) as error:
            function(value)
        self.assertEqual(error.exception.code, code)

    def test_message_past_the_old_one_mib_limit_is_accepted_whole(self):
        # Catches a size refusal (the old 1 MiB message_too_large) coming back.
        text = 'a' * (2 * 1024 * 1024 + 1)
        self.assertEqual(checked_message_text(text), text)
        self.assertEqual(len(checked_message_text(text + ' ')), 2097153)

    def test_chinese_and_emoji_messages_of_any_size_are_kept_byte_for_byte(self):
        # Catches a byte-count limit on multi-byte text.
        for character, expected_bytes in (('豆', 6291456), ('🙂', 8388608)):
            with self.subTest(character=character):
                text = character * (2 * 1024 * 1024)
                self.assertEqual(len(checked_message_text(text).encode('utf-8')), expected_bytes)

    def test_only_existing_outer_strip_normalization_is_applied(self):
        text = ' \t豆窝\r\nline e\u0301 🙂\t end\n '
        expected = '豆窝\r\nline e\u0301 🙂\t end'
        self.assertEqual(checked_message_text(text), expected)
        self.assertEqual(checked_message_text(expected), expected)
        self.assertNotEqual(expected, expected.replace('e\u0301', 'é'))
        self.assertGreater(len(checked_message_text('豆' * 4000)), 3000)

    def test_invalid_text_types_empty_nul_and_lone_surrogates(self):
        for value in (None, True, 1, b'text', [], {}, '', ' \t\n', 'a\0b', '\ud800', 'x\udfffy'):
            with self.subTest(value=repr(value)):
                self.assert_code('invalid_text', checked_message_text, value)

    def test_json_surrogate_pair_decodes_normally_but_lone_surrogate_rejects(self):
        self.assertEqual(checked_message_text(json.loads('"\\ud83d\\ude42"')), '🙂')
        self.assert_code('invalid_text', checked_message_text, json.loads('"\\ud83d"'))

    def test_json_escaped_large_message_round_trips_unchanged(self):
        # Catches a request-body limit (the old 8 MiB body_too_large) applied through JSON escaping.
        text = '\x01' * (2 * 1024 * 1024)
        body = json.dumps({'text': text, 'other': text}, ensure_ascii=True).encode('utf-8')
        before = hashlib.sha256(body).hexdigest()
        self.assertEqual(len(body), 25165849)
        self.assertEqual(checked_message_text(json.loads(body)['text']), text)
        self.assertEqual(hashlib.sha256(body).hexdigest(), before)
