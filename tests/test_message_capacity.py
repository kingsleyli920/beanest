import hashlib
import json
import unittest

from local_agent_coordinator.message_capacity import (
    MAX_JSON_BODY_BYTES, MAX_MESSAGE_UTF8_BYTES, MessageCapacityError,
    checked_json_body_size, checked_message_text,
)


class MessageCapacityTests(unittest.TestCase):
    def assert_code(self, code, function, value):
        with self.assertRaises(MessageCapacityError) as error:
            function(value)
        self.assertEqual(error.exception.code, code)

    def test_ascii_exact_raw_limit_and_one_byte_over(self):
        text = 'a' * MAX_MESSAGE_UTF8_BYTES
        self.assertEqual(checked_message_text(text), text)
        self.assert_code('message_too_large', checked_message_text, text + 'a')
        # Normalization must not let an oversized raw message through.
        self.assert_code('message_too_large', checked_message_text, text + ' ')

    def test_chinese_and_emoji_limits_count_bytes_not_characters(self):
        for character in ('豆', '🙂'):
            with self.subTest(character=character):
                unit = len(character.encode('utf-8'))
                text = character * (MAX_MESSAGE_UTF8_BYTES // unit)
                text += 'a' * (MAX_MESSAGE_UTF8_BYTES - len(text.encode('utf-8')))
                self.assertEqual(len(text.encode('utf-8')), MAX_MESSAGE_UTF8_BYTES)
                self.assertEqual(checked_message_text(text), text)
                self.assert_code('message_too_large', checked_message_text, text + 'a')

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

    def test_wire_capacity_uses_actual_bytes_without_parsing_or_rewriting(self):
        for size in (0, 1, MAX_JSON_BODY_BYTES):
            self.assertEqual(checked_json_body_size(size), size)
        self.assert_code('body_too_large', checked_json_body_size, MAX_JSON_BODY_BYTES + 1)
        for size in (-1, True, False, 1.0, '1', None):
            with self.subTest(size=size):
                self.assert_code('invalid_request', checked_json_body_size, size)

    def test_json_escape_expansion_fits_one_maximum_message_and_preserves_body(self):
        # U+0001 is valid under the existing no-NUL contract and has the
        # largest JSON text-byte expansion: one UTF-8 byte becomes six bytes.
        text = '\x01' * MAX_MESSAGE_UTF8_BYTES
        body = json.dumps({'text': text, 'request_id': 'example'}, ensure_ascii=True).encode('utf-8')
        before = hashlib.sha256(body).hexdigest()
        self.assertGreater(len(body), 6 * MAX_MESSAGE_UTF8_BYTES)
        self.assertEqual(checked_json_body_size(len(body)), len(body))
        self.assertEqual(checked_message_text(json.loads(body)['text']), text)
        self.assertEqual(hashlib.sha256(body).hexdigest(), before)
        # An entire JSON package has its own bound, even when every field fits.
        oversized = json.dumps({'text': text, 'other': text}).encode('utf-8')
        self.assert_code('body_too_large', checked_json_body_size, len(oversized))

    def test_json_ascii_escaping_does_not_change_decoded_unicode_budget(self):
        text = '🙂' * (MAX_MESSAGE_UTF8_BYTES // 4)
        bodies = [json.dumps({'text': text}, ensure_ascii=ascii_only).encode('utf-8')
                  for ascii_only in (True, False)]
        self.assertGreater(len(bodies[0]), len(bodies[1]))
        for body in bodies:
            self.assertEqual(checked_json_body_size(len(body)), len(body))
            self.assertEqual(checked_message_text(json.loads(body)['text']), text)
