import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from local_agent_coordinator import adapters

SID = '6216d06d-043b-4c66-ad6a-c85a0de23b71'
WEEKLY_LIMIT_NOTICE = "You've hit your weekly limit · resets 9am (America/Vancouver)"


def claude_limit_event():
    return {'type': 'assistant', 'isApiErrorMessage': True, 'error': 'rate_limit',
            'message': {'content': [{'type': 'text', 'text': WEEKLY_LIMIT_NOTICE}]}}


class InstallationResolverTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = root / 'home'
        self.path_first = root / 'path-first'
        self.path_second = root / 'path-second'
        for directory in (self.home, self.path_first, self.path_second):
            directory.mkdir()
        self.codex_app = root / 'Codex.app' / 'bin' / 'codex'
        self.claude_home = self.home / '.local' / 'bin' / 'claude'
        self.claude_system = (root / 'homebrew' / 'claude', root / 'usr-local' / 'claude')
        self.enterContext(patch.dict(os.environ, {
            'HOME': str(self.home),
            'PATH': os.pathsep.join((str(self.path_first), str(self.path_second))),
        }))
        self.enterContext(patch.object(adapters, 'CODEX_APP', self.codex_app))
        self.enterContext(patch.object(adapters, 'CLAUDE_SYSTEM_PATHS', self.claude_system))

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # These are installation metadata fixtures, never launched as commands.
        path.write_text('not an executable program\n')
        path.chmod(0o700)
        return str(path)

    def test_path_order_precedes_fixed_installations(self):
        self.executable(self.codex_app)
        self.executable(self.claude_home)
        for name, resolver in (('codex', adapters.find_codex), ('claude', adapters.find_claude)):
            with self.subTest(name=name):
                expected = self.executable(self.path_first / name)
                self.executable(self.path_second / name)
                self.assertEqual(resolver(), expected)

    def test_codex_uses_existing_app_fallback(self):
        self.executable(self.codex_app)
        self.assertEqual(adapters.find_codex(), str(self.codex_app))

    def test_relative_path_result_survives_a_different_working_directory(self):
        for name in ('codex', 'claude'):
            self.executable(self.path_first / name)
        original = Path.cwd()
        try:
            os.chdir(self.path_first)
            discovered_cwd = Path.cwd()
            with patch.dict(os.environ, {'PATH': '.'}):
                paths = (adapters.find_codex(), adapters.find_claude())
            os.chdir(self.path_second)
            for name, path in zip(('codex', 'claude'), paths):
                with self.subTest(name=name):
                    self.assertEqual(path, str(discovered_cwd / name))
                    self.assertTrue(Path(path).is_absolute())
                    self.assertTrue(Path(path).is_file())
        finally:
            os.chdir(original)

    def test_claude_local_installation_precedes_system_paths(self):
        self.executable(self.claude_home)
        for path in self.claude_system:
            self.executable(path)
        self.assertEqual(adapters.find_claude(), str(self.claude_home))

    def test_claude_fixed_system_order_and_fallback(self):
        for path in self.claude_system:
            self.executable(path)
        self.assertEqual(adapters.find_claude(), str(self.claude_system[0]))
        self.claude_system[0].chmod(0o600)
        self.assertEqual(adapters.find_claude(), str(self.claude_system[1]))

    def test_missing_installations_fail_without_path_leak(self):
        for resolver, message in ((adapters.find_codex, 'Codex CLI is unavailable'),
                                  (adapters.find_claude, 'Claude Code CLI is unavailable')):
            with self.subTest(resolver=resolver.__name__):
                with self.assertRaisesRegex(RuntimeError, '^' + message + '$'):
                    resolver()

    def test_directories_and_nonexecutable_files_are_not_installations(self):
        for name in ('codex', 'claude'):
            (self.path_first / name).mkdir()
            self.executable(self.path_second / name)
            (self.path_second / name).chmod(0o600)
        self.executable(self.codex_app)
        self.codex_app.chmod(0o600)
        self.claude_home.mkdir(parents=True)
        for path in self.claude_system:
            self.executable(path)
            path.chmod(0o600)
        for resolver in (adapters.find_codex, adapters.find_claude):
            with self.subTest(resolver=resolver.__name__):
                with self.assertRaises(RuntimeError):
                    resolver()

    def test_normal_installation_symlinks_are_accepted(self):
        for name, resolver in (('codex', adapters.find_codex), ('claude', adapters.find_claude)):
            with self.subTest(name=name):
                target = Path(self.executable(self.home / 'versions' / name))
                link = self.path_first / name
                link.symlink_to(target)
                self.assertEqual(resolver(), str(link))

    def test_inaccessible_candidate_does_not_hide_later_installation(self):
        expected = self.executable(self.claude_system[0])
        is_file = Path.is_file

        def inspect(path):
            if path == self.claude_home:
                raise PermissionError('unreadable installation')
            return is_file(path)

        with patch.object(Path, 'is_file', autospec=True, side_effect=inspect):
            self.assertEqual(adapters.find_claude(), expected)

    def test_resolvers_only_inspect_installation_metadata(self):
        self.executable(self.codex_app)
        self.executable(self.claude_home)
        with patch.object(adapters, '_status', side_effect=AssertionError('CLI execution forbidden')), \
             patch.object(adapters.subprocess, 'run', side_effect=AssertionError('CLI execution forbidden')), \
             patch.object(adapters.asyncio, 'create_subprocess_exec',
                          side_effect=AssertionError('CLI execution forbidden')):
            self.assertEqual(adapters.find_codex(), str(self.codex_app))
            self.assertEqual(adapters.find_claude(), str(self.claude_home))


class ProtocolTests(unittest.TestCase):
    def test_structured_native_quota_metadata_and_untrusted_lookalikes(self):
        base = {'type': 'assistant', 'session_id': SID, 'isApiErrorMessage': True,
            'error': 'rate_limit', 'apiError': 'usage_limit_reached',
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'PRIVATE five hour notice'}]}}
        self.assertEqual(adapters._claude_native_error_code(base, SID), 'subscription_limit_reached')
        for changed in ({'parent_tool_use_id': 'child'}, {'isSidechain': True},
                        {'session_id': str(uuid.uuid4())}, {'isApiErrorMessage': False},
                        {'message': {'role': 'user', 'content': []}}, {'error': 'server_error'}):
            with self.subTest(changed=changed):
                self.assertEqual(adapters._claude_native_error_code({**base, **changed}, SID),
                                 'unknown_provider_error')
        tool = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': json.dumps(base)}]}}
        result = adapters.parse_events('claude', [json.dumps(x).encode() for x in (
            {'type': 'system', 'subtype': 'init', 'session_id': SID}, tool,
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': json.dumps(base)}]}},
            {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': SID})])
        self.assertNotEqual(result.get('failure_code'), 'subscription_limit_reached')

    def test_explicit_result_quota_metadata_with_error_flag(self):
        event = {'type': 'result', 'session_id': SID, 'is_error': True,
                 'apiError': 'usage_limit_reached', 'error': 'rate_limit', 'subtype': 'success'}
        result = adapters.parse_events('claude', [json.dumps(x).encode() for x in (
            {'type': 'system', 'subtype': 'init', 'session_id': SID}, event)])
        self.assertEqual(result['failure_code'], 'subscription_limit_reached')
        event['is_error'] = False
        self.assertNotEqual(adapters._claude_native_error_code(event, SID), 'subscription_limit_reached')

    def test_observed_five_hour_notice_requires_provider_envelope(self):
        event = claude_limit_event()
        event['message']['content'][0]['text'] = "You've hit your 5-hour limit · resets 12:30pm (America/Vancouver)"
        self.assertEqual(adapters._claude_native_error_code(event), 'subscription_limit_reached')
        for change in ({'isApiErrorMessage': False}, {'parent_tool_use_id': 'child'},
                       {'session_id': str(uuid.uuid4())}, {'error': 'server_error'}):
            self.assertNotEqual(adapters._claude_native_error_code({**event, **change}, SID),
                                'subscription_limit_reached')

    def test_observed_native_claude_weekly_limit_is_safe_and_not_a_subscription_balance(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SID},
            {**claude_limit_event(), 'apiError': {'diagnostic': 'PRIVATE'}},
            {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
             'result': 'PRIVATE RESULT DIAGNOSTIC',
             'usage': {'input_tokens': 0, 'output_tokens': 0, 'subscription_balance': 0}},
        ]
        for structured in (False, True):
            with self.subTest(structured=structured):
                result = adapters.parse_events('claude', [json.dumps(event).encode() for event in events], structured=structured)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['failure_code'], 'subscription_limit_reached')
                self.assertEqual(result['text'], '')
                self.assertEqual(result['usage'], {'input_tokens': 0, 'output_tokens': 0})
                self.assertIn({'type': 'provider_error', 'code': 'subscription_limit_reached'}, result['events'])
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertNotIn('weekly limit', json.dumps(result))
                self.assertNotIn('subscription_balance', json.dumps(result))
        no_usage = adapters.parse_events('claude', [json.dumps(claude_limit_event()).encode()], structured=True)
        self.assertIsNone(no_usage['usage'])

    def test_unconfirmed_native_errors_and_quota_lookalikes_remain_generic(self):
        cases = [
            {**claude_limit_event(), 'error': 'server_error'},
            {**claude_limit_event(), 'isApiErrorMessage': 'true'},
            {**claude_limit_event(), 'isApiErrorMessage': False},
        ]
        for text in ('Too many requests PRIVATE', WEEKLY_LIMIT_NOTICE + ' PRIVATE',
                     'Example: ' + WEEKLY_LIMIT_NOTICE, WEEKLY_LIMIT_NOTICE.replace('weekly', 'daily'),
                     'x' * 257):
            value = claude_limit_event()
            value['message']['content'][0]['text'] = text
            cases.append(value)
        malformed = claude_limit_event()
        malformed['message']['content'] = None
        cases.append(malformed)
        for event in cases:
            with self.subTest(event=event):
                lines = [json.dumps(event).encode(), json.dumps({'type': 'result', 'subtype': 'error_during_execution',
                                                               'is_error': True, 'result': 'PRIVATE'}).encode()]
                result = adapters.parse_events('claude', lines, structured=True)
                self.assertEqual(result['status'], 'failed')
                self.assertNotEqual(result.get('failure_code'), 'subscription_limit_reached')
                self.assertEqual(result['text'], '')
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_claude_structured_result_is_the_only_payload_and_native_formatter_is_opt_in(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SID},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'thinking', 'thinking': 'PRIVATE THINKING'},
                {'type': 'text', 'text': 'PRIVATE PROSE before ```json'},
                {'type': 'tool_use', 'name': 'StructuredOutput', 'input': {'ignored': 'PRIVATE TOOL INPUT'}}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'PRIVATE RESULT STRING',
             'structured_output': {'ok': True, 'note': 'shape probe'}, 'usage': {'output_tokens': 7}},
        ]
        lines = [json.dumps(event).encode() for event in events]
        result = adapters.parse_events('claude', lines, structured=True)
        self.assertEqual(json.loads(result['text']), {'ok': True, 'note': 'shape probe'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['session_id'], SID)
        self.assertEqual(result['usage'], {'output_tokens': 7})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertIn({'type': 'structured_output_format'}, result['events'])
        self.assertFalse(any(event['type'] == 'tool_observed' for event in result['events']))
        legacy = adapters.parse_events('claude', lines)
        self.assertTrue(any(event['type'] == 'tool_observed' for event in legacy['events']))
        self.assertIn('PRIVATE PROSE', legacy['text'])

    def test_other_or_malformed_claude_tools_are_not_formatter_exceptions(self):
        for name, value in (('Read', {}), ('mcp__StructuredOutput', {}), ('structuredoutput', {}),
                            ('StructuredOutput', [])):
            events = [
                {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': value}]}},
                {'type': 'result', 'subtype': 'success', 'structured_output': {'ok': True}},
            ]
            result = adapters.parse_events('claude', [json.dumps(event).encode() for event in events], structured=True)
            self.assertTrue(any(event['type'] == 'tool_observed' for event in result['events']))

    def test_claude_missing_refused_multiple_or_invalid_structured_results_never_fallback(self):
        successful = {'type': 'result', 'subtype': 'success', 'structured_output': {'ok': True}}
        cases = [
            [{'type': 'result', 'subtype': 'success', 'result': '{"ok":true}'}],
            [{**successful, 'is_error': True, 'result': '{"ok":true}'}],
            [{**successful, 'structured_output': []}],
            [successful, successful],
        ]
        for events in cases:
            with self.subTest(events=events):
                lines = [json.dumps({'type': 'assistant', 'message': {'content': [
                    {'type': 'text', 'text': '{"ok":true}'}]}}).encode()]
                lines += [json.dumps(event).encode() for event in events]
                result = adapters.parse_events('claude', lines, structured=True)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['text'], '')
                self.assertEqual(result['failure_code'], 'protocol_turn_incomplete')
        duplicate = b'{"type":"result","subtype":"success","structured_output":{"ok":true,"ok":false}}'
        self.assertEqual(adapters.parse_events('claude', [duplicate], structured=True)['status'], 'failed')

    def test_codex_structured_data_is_the_final_whole_message_without_commentary(self):
        events = [
            {'type': 'thread.started', 'thread_id': SID},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'PRIVATE COMMENTARY'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'phase': 'final_answer', 'text': '{"ok":true}'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'phase': 'commentary', 'text': 'PRIVATE EXTRA'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 3}},
        ]
        result = adapters.parse_events('codex', [json.dumps(event).encode() for event in events], structured=True)
        self.assertEqual(result['text'], '{"ok":true}')
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('PRIVATE', json.dumps(result))
        for text in ('prefix {"ok":true}', '```json\n{"ok":true}\n```', '{"ok":true,"ok":false}'):
            changed = events[:2] + [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}, events[-1]]
            invalid = adapters.parse_events('codex', [json.dumps(event).encode() for event in changed], structured=True)
            self.assertEqual(invalid['text'], '')
            self.assertEqual(invalid['status'], 'failed')

    def test_structured_payload_over_limit_is_rejected_without_truncation(self):
        event = {'type': 'result', 'subtype': 'success', 'structured_output': {'text': 'x' * adapters.MAX_TEXT}}
        result = adapters.parse_events('claude', [json.dumps(event).encode()], structured=True)
        self.assertEqual(result['text'], '')
        self.assertEqual(result['status'], 'failed')

    @patch("local_agent_coordinator.adapters.MAX_TEXT", 128 * 1024)
    def test_structured_chinese_payload_uses_actual_utf8_limit_and_rejects_invalid_unicode(self):
        payload = {'text': '豆' * 23000}
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')), adapters.MAX_TEXT)
        self.assertGreater(len(json.dumps(payload, ensure_ascii=True).encode('utf-8')), adapters.MAX_TEXT)
        event = {'type': 'result', 'subtype': 'success', 'structured_output': payload}
        result = adapters.parse_events('claude', [json.dumps(event).encode()], structured=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), payload)
        event['structured_output'] = {'text': '\ud800'}
        invalid = adapters.parse_events('claude', [json.dumps(event).encode()], structured=True)
        self.assertEqual(invalid['status'], 'failed')
        self.assertEqual(invalid['text'], '')
        self.assertEqual(invalid['failure_code'], 'protocol_turn_incomplete')

    def test_codex_whitelist_and_usage(self):
        events = [
            {'type': 'thread.started', 'thread_id': SID},
            {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'PRIVATE'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'hello'}},
            {'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'PRIVATE'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2, 'secret': 'PRIVATE'}},
        ]
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertEqual(result['text'], 'hello')
        self.assertEqual(result['session_id'], SID)
        self.assertEqual(result['usage'], {'input_tokens': 10, 'output_tokens': 2})
        self.assertIn({'type': 'tool_observed', 'item_type': 'command_execution'}, result['events'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_error_item_is_not_a_tool_and_failure_is_separate(self):
        events = [
            {'type': 'item.completed', 'item': {'type': 'error', 'message': 'PRIVATE'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 5}},
        ]
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertIn({'type': 'provider_error', 'code': 'unknown_provider_error'}, result['events'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'unknown_provider_error')

    def test_transport_wording_alone_does_not_allow_recovery(self):
        events = [
            {'type': 'item.completed', 'item': {'type': 'error',
             'message': 'WebSocket failed; falling back to HTTP.'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 5}},
        ]
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertIn({'type': 'provider_error', 'code': 'unknown_provider_error'}, result['events'])
        self.assertEqual(result['status'], 'failed')

    def test_exact_disabled_code_mode_notice_is_warning(self):
        events = [
            {'type': 'item.completed', 'item': {'type': 'error',
             'message': adapters.CODE_MODE_DISABLED_NOTICE}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 5}},
        ]
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertEqual(result['status'], 'completed')
        self.assertIn({'type': 'warning', 'code': 'code_mode_disabled_notice'}, result['events'])
        events.append({'type': 'turn.failed', 'error': {'message': 'PRIVATE'}})
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(result))
        # Even a later completion cannot erase an observed fatal turn event.
        events.append({'type': 'turn.completed', 'usage': {}})
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertEqual(result['status'], 'failed')

    def test_exact_ignored_legacy_config_notice_is_sanitized_warning(self):
        notice = ('Codex is ignoring 1 unrecognized configuration setting. '
                  'Check for typos or deprecated settings.\n'
                  '  user (/Users/example/.codex/config.toml): `features.rmcp_client` is ignored.')
        events = [{'type': 'item.completed', 'item': {'type': 'error', 'message': notice}},
                  {'type': 'turn.completed', 'usage': {}}]
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertEqual(result['status'], 'completed')
        self.assertIn({'type': 'warning', 'code': 'ignored_legacy_config_notice'}, result['events'])
        self.assertNotIn('/Users/', json.dumps(result))
        for changed in (notice + ' extra', notice.replace('rmcp_client', 'other'),
                        notice.replace('user (', 'project ('), notice.replace('ignoring 1', 'ignoring 2'),
                        notice + '\nFatal error'):
            events[0]['item']['message'] = changed
            rejected = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
            self.assertEqual(rejected['failure_code'], 'unknown_provider_error')
        events[0]['item']['message'] = notice
        events.append({'type': 'turn.failed', 'error': {'message': 'unrelated error'}})
        self.assertEqual(adapters.parse_events('codex', [json.dumps(x).encode() for x in events])['status'], 'failed')

    def test_resume_with_a_changed_default_model_is_a_warning_not_a_failure(self):
        # Catches: Codex's "recorded with model X but resuming with Y" notice, emitted when its
        # default model changes between calls, failing a turn that completed normally.
        notice = ('This session was recorded with model `gpt-6-luna` but is resuming with `gpt-6.1-sol`. '
                  'Consider switching back to `gpt-6-luna` as it may affect Codex performance.')
        events = [{'type': 'thread.started', 'thread_id': '00000000-0000-4000-8000-000000000060'},
                  {'type': 'item.completed', 'item': {'type': 'error', 'message': notice}},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ok'}},
                  {'type': 'turn.completed', 'usage': {}}]
        result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
        self.assertEqual((result['status'], result['text']), ('completed', 'ok'))
        self.assertIn({'type': 'warning', 'code': 'model_resume_notice'}, result['events'])
        for changed in (notice + ' extra', notice.replace('Consider', 'You must'), 'Fatal: ' + notice):
            events[1]['item']['message'] = changed
            rejected = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
            self.assertEqual(rejected['failure_code'], 'unknown_provider_error')

    def test_near_match_disabled_code_mode_notice_fails(self):
        for message in (adapters.CODE_MODE_DISABLED_NOTICE + ' extra',
                        adapters.CODE_MODE_DISABLED_NOTICE.lower()):
            events = [
                {'type': 'item.completed', 'item': {'type': 'error', 'message': message}},
                {'type': 'turn.completed', 'usage': {}},
            ]
            result = adapters.parse_events('codex', [json.dumps(x).encode() for x in events])
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['failure_code'], 'unknown_provider_error')

    def test_claude_whitelist_deduplicates_result(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SID, 'email': 'PRIVATE'},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'thinking', 'thinking': 'PRIVATE'}, {'type': 'text', 'text': 'answer'}]}},
            {'type': 'result', 'subtype': 'success', 'result': 'answer', 'usage': {'output_tokens': 3}},
        ]
        result = adapters.parse_events('claude', [json.dumps(x).encode() for x in events])
        self.assertEqual(result['text'], 'answer')
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_auth_is_redacted(self):
        raw = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty',
               'subscriptionType': 'max', 'email': 'PRIVATE', 'orgId': 'PRIVATE'}
        with patch.object(adapters, '_status', side_effect=[(0, 'Logged in using ChatGPT'), (0, json.dumps(raw))]) as status, \
             patch.object(adapters, 'find_codex', return_value='/bin/codex'), \
             patch.object(adapters, 'find_claude', return_value='/bin/claude'):
            result = adapters.doctor()
        self.assertEqual(status.call_args_list[1].args[0],
                         ['/bin/claude', '--safe-mode', '--restricted', 'auth', 'status'])
        self.assertTrue(result['claude']['subscription_confirmed'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_claude_command_uses_resolved_executable(self):
        with patch.object(adapters, 'find_claude', return_value='/fixed/install/claude'):
            command = adapters._command('claude', SID)
        self.assertEqual(command[0], '/fixed/install/claude')
        self.assertEqual(command[1:4], ['--safe-mode', '--restricted', '--tools'])
        self.assertEqual(command[-2:], ['--resume', SID])

    def test_doctor_does_not_execute_unavailable_cli(self):
        with patch.object(adapters, 'find_codex', side_effect=RuntimeError('unavailable')), \
             patch.object(adapters, 'find_claude', side_effect=RuntimeError('unavailable')), \
             patch.object(adapters, '_status') as status:
            result = adapters.doctor()
        status.assert_not_called()
        self.assertFalse(result['ready'])
        self.assertFalse(result['claude']['available'])

    def test_billing_environment_is_not_inherited(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE', 'ANTHROPIC_API_KEY': 'PRIVATE',
                                    'CLAUDE_CODE_USE_BEDROCK': '1', 'AWS_PROFILE': 'PRIVATE'}):
            child = adapters._environment()
        self.assertFalse(any(k in child for k in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'AWS_PROFILE')))

    def test_full_access_environment_keeps_the_toolchain_and_drops_only_account_selectors(self):
        # Catches three defects: a full access call losing ordinary variables
        # the user's build reads (they were all stripped, so builds failed);
        # the fix passing a variable that selects a key, endpoint or login
        # home; and the host's own probes (ps, status, account checks, which
        # call this without ``inherit``) starting to inherit anything more.
        host = {'HOME': '/home/user', 'PATH': '/usr/bin:/bin', 'JAVA_HOME': '/opt/jdk-21',
                'ANDROID_HOME': '/opt/android-sdk', 'GODOT': '/opt/godot', 'GRADLE_OPTS': '-Xmx2g',
                'NODE_AUTH_TOKEN': 'registry-token', 'VITE_API_BASE_URL': 'https://project.invalid',
                'AWS_PROFILE': 'project-deploy', 'HTTPS_PROXY': 'http://127.0.0.1:7890',
                'ANTHROPIC_API_KEY': 'PRIVATE', 'ANTHROPIC_AUTH_TOKEN': 'PRIVATE',
                'ANTHROPIC_BASE_URL': 'https://router.invalid', 'CLAUDE_CODE_USE_BEDROCK': '1',
                'CLAUDE_CODE_USE_VERTEX': '1', 'CLAUDE_CODE_OAUTH_TOKEN': 'PRIVATE',
                'CLAUDE_CONFIG_DIR': '/user/shell/claude', 'CLAUDECODE': '1',
                'OPENAI_API_KEY': 'PRIVATE', 'OPENAI_BASE_URL': 'https://router.invalid',
                'CODEX_API_KEY': 'PRIVATE', 'CODEX_HOME': '/user/shell/codex',
                'KIMI_API_KEY': 'PRIVATE', 'KIMI_BASE_URL': 'https://router.invalid',
                'KIMI_CODE_HOME': '/user/shell/kimi', 'MOONSHOT_API_KEY': 'PRIVATE',
                'BEANEST_PROGRESS_FILE': '/another/call/progress'}
        with patch.dict(os.environ, host, clear=True):
            inherited = adapters._environment(inherit=True)
            minimal = adapters._environment()
            with adapters.account_home('codex', '/beanest/homes/profile'):
                bound = adapters._environment(inherit=True)
        self.assertEqual(inherited, {
            'HOME': '/home/user', 'PATH': '/usr/bin:/bin', 'JAVA_HOME': '/opt/jdk-21',
            'ANDROID_HOME': '/opt/android-sdk', 'GODOT': '/opt/godot', 'GRADLE_OPTS': '-Xmx2g',
            'NODE_AUTH_TOKEN': 'registry-token', 'VITE_API_BASE_URL': 'https://project.invalid',
            'AWS_PROFILE': 'project-deploy', 'HTTPS_PROXY': 'http://127.0.0.1:7890'})
        self.assertEqual(minimal, {'HOME': '/home/user', 'PATH': '/usr/bin:/bin',
                                   'HTTPS_PROXY': 'http://127.0.0.1:7890'})
        # Only the profile this call is bound to names a login home.
        self.assertEqual(bound, {**inherited, 'CODEX_HOME': '/beanest/homes/profile'})

    def test_full_access_environment_drops_only_the_hosts_own_python_path(self):
        # Catches: a host run from a source checkout handing its own src to
        # the user's Python (which would then import the host's code), or the
        # fix touching a PYTHONPATH the user set themselves.
        own = str(Path(adapters.__file__).resolve().parents[1])
        for value, expected in [(own + os.pathsep + '/user/lib', '/user/lib'),
                                ('/user/lib' + os.pathsep + own, '/user/lib'),
                                (own, None),
                                ('/user/a' + os.pathsep + '/user/b', '/user/a' + os.pathsep + '/user/b')]:
            with self.subTest(value=value), patch.dict(os.environ, {'PYTHONPATH': value}):
                self.assertEqual(adapters._environment(inherit=True).get('PYTHONPATH'), expected)
                self.assertNotIn('PYTHONPATH', adapters._environment())

    def test_network_proxy_and_ca_reach_the_child_but_provider_routing_does_not(self):
        # Catches: users behind an HTTPS proxy or corporate CA could not reach
        # the provider from Beanest although their own terminal could.
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:7890', 'no_proxy': 'localhost',
                                    'NODE_EXTRA_CA_CERTS': '/etc/corp-ca.pem', 'SSL_CERT_FILE': '/etc/corp-ca.pem',
                                    'ANTHROPIC_BASE_URL': 'https://router.invalid', 'OPENAI_BASE_URL': 'https://router.invalid'}):
            child = adapters._environment()
        self.assertEqual({key: child.get(key) for key in ('HTTPS_PROXY', 'no_proxy', 'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE')},
                         {'HTTPS_PROXY': 'http://127.0.0.1:7890', 'no_proxy': 'localhost',
                          'NODE_EXTRA_CA_CERTS': '/etc/corp-ca.pem', 'SSL_CERT_FILE': '/etc/corp-ca.pem'})
        self.assertNotIn('ANTHROPIC_BASE_URL', child)
        self.assertNotIn('OPENAI_BASE_URL', child)

    def test_no_implicit_resume(self):
        with self.assertRaises(ValueError):
            adapters._command('codex', '--last')


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_native_quota_survives_nonzero_cli_exit_but_unknown_error_is_generic(self):
        from unittest.mock import AsyncMock
        for structured in (False, True):
            for known in (False, True):
                event = claude_limit_event()
                if not known:
                    event['message']['content'][0]['text'] = 'PRIVATE unknown rate_limit diagnostic'
                lines = [json.dumps(event).encode(), json.dumps({
                    'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                    'session_id': SID, 'result': 'PRIVATE RESULT'}).encode()]
                with self.subTest(structured=structured, known=known), tempfile.TemporaryDirectory() as directory, \
                     patch.object(adapters, '_command', return_value=['unused']), \
                     patch.object(adapters, '_subscription_status', new=AsyncMock(return_value={'subscription_confirmed': True})), \
                     patch.object(adapters, '_run', new=AsyncMock(return_value=(lines, 1))):
                    result = await adapters.invoke('claude', 'hello', Path(directory),
                        **({'response_schema': {'type': 'object'}} if structured else {}))
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['failure_code'], 'subscription_limit_reached' if known else 'cli_nonzero_exit')
                self.assertEqual(result['text'], '')
                self.assertIsNone(result['usage'])
                self.assertNotIn('PRIVATE', json.dumps(result))

    async def test_native_quota_signal_does_not_hide_an_actual_tool_event(self):
        from unittest.mock import AsyncMock
        events = [claude_limit_event(), {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Read', 'input': {'PRIVATE': 'data'}}]}}]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(adapters, '_command', return_value=['unused']), \
             patch.object(adapters, '_subscription_status', new=AsyncMock(return_value={'subscription_confirmed': True})), \
             patch.object(adapters, '_run', new=AsyncMock(return_value=([json.dumps(event).encode() for event in events], 1))):
            result = await adapters.invoke('claude', 'hello', Path(directory), response_schema={'type': 'object'})
        self.assertEqual(result['status'], 'failed')
        self.assertNotEqual(result['failure_code'], 'subscription_limit_reached')
        self.assertTrue(any(event['type'] == 'tool_observed' for event in result['events']))
        self.assertNotIn('PRIVATE', json.dumps(result))

    async def test_structured_claude_keeps_subscription_and_disabled_tools_with_inline_schema(self):
        from unittest.mock import AsyncMock
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
        lines = [json.dumps({'type': 'result', 'subtype': 'success', 'session_id': SID,
                             'structured_output': {'ok': True}}).encode()]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(adapters, 'find_claude', return_value='/trusted/claude'), \
             patch.object(adapters, '_subscription_status', new=AsyncMock(return_value={'subscription_confirmed': True})) as auth, \
             patch.object(adapters, '_run', new=AsyncMock(return_value=(lines, 0))) as run:
            result = await adapters.invoke('claude', 'hello', Path(directory), SID, response_schema=schema)
            self.assertEqual(list(Path(directory).iterdir()), [])
        command = run.call_args.args[0]
        self.assertEqual(json.loads(command[command.index('--json-schema') + 1]), schema)
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertIn('--safe-mode', command)
        self.assertIn('--restricted', command)
        self.assertEqual(command[-2:], ['--resume', SID])
        auth.assert_awaited_once()
        self.assertEqual(result['text'], '{"ok":true}')

    async def test_codex_schema_file_is_private_random_and_cleaned_on_success_failure_timeout_and_cancel(self):
        from unittest.mock import AsyncMock
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        for outcome in ('success', 'failure', 'timeout', 'cancel'):
            seen = []
            async def run(command, prompt, cwd, timeout):
                path = Path(command[command.index('--output-schema') + 1])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(json.loads(path.read_text()), schema)
                self.assertEqual(command[-3:], ['resume', SID, '-'])
                self.assertIn('read-only', command)
                seen.append(path)
                if outcome == 'timeout':
                    raise asyncio.TimeoutError
                if outcome == 'cancel':
                    raise asyncio.CancelledError
                if outcome == 'failure':
                    raise RuntimeError('PRIVATE')
                return [json.dumps(event).encode() for event in (
                    {'type': 'thread.started', 'thread_id': SID},
                    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
                    {'type': 'turn.completed'})], 0
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory, \
                 patch.object(adapters, 'find_codex', return_value='/trusted/codex'), \
                 patch.object(adapters, '_subscription_status', new=AsyncMock(return_value={'subscription_confirmed': True})), \
                 patch.object(adapters, '_run', new=run):
                original = Path(directory) / 'schema.json'
                original.write_text('preserve existing file')
                if outcome == 'cancel':
                    with self.assertRaises(asyncio.CancelledError):
                        await adapters.invoke('codex', 'hello', Path(directory), SID, response_schema=schema)
                else:
                    result = await adapters.invoke('codex', 'hello', Path(directory), SID, response_schema=schema)
                    self.assertEqual(result['status'], {'success': 'completed', 'failure': 'failed', 'timeout': 'timeout'}[outcome])
                self.assertEqual(original.read_text(), 'preserve existing file')
                self.assertEqual(list(Path(directory).iterdir()), [original])
            self.assertEqual(len(seen), 1)
            self.assertFalse(seen[0].exists())

    async def test_invalid_structured_schema_fails_before_authentication_or_processes(self):
        from unittest.mock import AsyncMock
        for schema in (True, [], {'type': 'array'}, {'type': 'object', 'invalid': float('nan')},
                       {'type': 'object', 'description': '\ud800'},
                       {'type': 'object', 'description': 'x' * 65536}):
            with self.subTest(schema=str(schema)[:50]), \
                 patch.object(adapters, '_subscription_status', new=AsyncMock()) as auth, \
                 patch.object(adapters, '_run', new=AsyncMock()) as run:
                with self.assertRaises(ValueError):
                    await adapters.invoke('claude', 'hello', Path('/not/used'), response_schema=schema)
                auth.assert_not_awaited()
                run.assert_not_awaited()

    async def test_structured_success_does_not_allow_other_tool_events(self):
        from unittest.mock import AsyncMock
        events = [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {'PRIVATE': 'SECRET'}}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SID, 'structured_output': {'ok': True}},
        ]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(adapters, 'find_claude', return_value='/trusted/claude'), \
             patch.object(adapters, '_subscription_status', new=AsyncMock(return_value={'subscription_confirmed': True})), \
             patch.object(adapters, '_run', new=AsyncMock(return_value=([json.dumps(x).encode() for x in events], 0))):
            result = await adapters.invoke('claude', 'hello', Path(directory), response_schema={'type': 'object'})
        self.assertEqual(result['failure_code'], 'unexpected_tool_event')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(result))

    async def test_claude_auth_uses_the_same_resolved_executable(self):
        from unittest.mock import AsyncMock
        raw = json.dumps({'loggedIn': True, 'authMethod': 'claude.ai',
                          'apiProvider': 'firstParty', 'subscriptionType': 'max'}).encode()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(adapters, 'find_claude', return_value='/fixed/install/claude'), \
             patch.object(adapters, '_run', new=AsyncMock(return_value=([raw], 0))) as run:
            result = await adapters._subscription_status('claude', Path(directory))
        run.assert_awaited_once_with(
            ['/fixed/install/claude', '--safe-mode', '--restricted', 'auth', 'status'],
            '', Path(directory), 15, include_stderr=True, cleanup_metadata={})
        self.assertTrue(result['subscription_confirmed'])

    async def test_missing_claude_auth_does_not_try_a_different_path(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(adapters, 'find_claude', side_effect=RuntimeError('unavailable')), \
             patch.object(adapters, '_run', new=AsyncMock()) as run:
            result = await adapters._subscription_status('claude', Path(directory))
        run.assert_not_awaited()
        self.assertFalse(result['subscription_confirmed'])

    async def test_invoke_rejects_tool_even_with_successful_exit(self):
        from unittest.mock import AsyncMock
        lines = [json.dumps(event).encode() for event in (
            {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'arguments': 'PRIVATE'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 5}})]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(adapters, '_command', return_value=['unused']), \
             patch.object(adapters, '_subscription_status', new=AsyncMock(return_value={'subscription_confirmed': True})), \
             patch.object(adapters, '_run', new=AsyncMock(return_value=(lines, 0))):
            result = await adapters.invoke('codex', 'hello', Path(directory))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'unexpected_tool_event')
        self.assertNotIn('PRIVATE', json.dumps(result))

    async def test_stdin_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            lines, code = await adapters._run([sys.executable, '-c',
                'import sys,os; print(sys.stdin.read()); print(os.getcwd())'],
                'safe $(literal)', Path(directory), 2)
            self.assertEqual(code, 0)
            self.assertEqual(lines[0], b'safe $(literal)')
            self.assertEqual(Path(lines[1].decode()).resolve(), Path(directory).resolve())

    async def _spawn_and_interrupt(self, cancel):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'pid'
            script = ('import os,time; from pathlib import Path; '
                      f'Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)')
            task = asyncio.create_task(adapters._run([sys.executable, '-c', script], '', Path(directory),
                                                     5 if cancel else 0.3))
            for _ in range(100):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text())
            if cancel:
                task.cancel()
            with self.assertRaises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_timeout_terminates_process(self):
        await self._spawn_and_interrupt(False)

    async def test_cancellation_terminates_process(self):
        await self._spawn_and_interrupt(True)

    async def test_cancel_during_auth_stops_status_child_before_returning(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'auth.pid'
            script = ('import os,time; from pathlib import Path; '
                      f'Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)')
            real_run = adapters._run

            async def status_process(command, prompt, cwd, timeout, **kwargs):
                self.assertTrue(kwargs['include_stderr'])
                return await real_run([sys.executable, '-c', script], prompt, cwd, timeout, **kwargs)

            with patch.object(adapters, '_command', return_value=['unused']), \
                 patch.object(adapters, 'find_codex', return_value='unused'), \
                 patch.object(adapters, '_run', side_effect=status_process):
                task = asyncio.create_task(adapters.invoke('codex', 'hello', Path(directory)))
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(pidfile.exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)

    async def test_auth_status_captures_stderr_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            lines, code = await adapters._run([sys.executable, '-c',
                'import sys; print("Logged in using ChatGPT", file=sys.stderr)'],
                '', Path(directory), 2, include_stderr=True)
            self.assertTrue(adapters._authentication('codex', code, b'\n'.join(lines).decode())
                            ['subscription_confirmed'])

    async def test_repeated_cancellation_waits_for_owned_process_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'stubborn.pid'
            script = ('import os,time,signal; from pathlib import Path; '
                      'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                      f'Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)')
            cleanup_started = asyncio.Event()
            terminate = adapters._terminate

            async def observed_terminate(process):
                cleanup_started.set()
                await terminate(process)

            with patch.object(adapters, '_terminate', side_effect=observed_terminate):
                task = asyncio.create_task(adapters._run([sys.executable, '-c', script],
                                                       '', Path(directory), 10))
                for _ in range(100):
                    if pidfile.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(pidfile.exists())
                task.cancel()
                await asyncio.wait_for(cleanup_started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)


if __name__ == '__main__':
    unittest.main()
