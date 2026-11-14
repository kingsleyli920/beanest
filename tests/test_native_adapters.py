"""Real subprocess protocol/cleanup tests; no model calls or account reads."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from local_agent_coordinator import native_adapters as native

SESSION = '42619ac2-4bfa-446c-a773-5193109566ae'
FINAL = {'message': 'done', 'action': 'finish'}
SCHEMA = {'type': 'object', 'properties': {'message': {'type': 'string'},
          'action': {'type': 'string'}}, 'required': ['message', 'action'],
          'additionalProperties': False}


def codex_events():
    return [
        {'type': 'thread.started', 'thread_id': SESSION},
        {'type': 'turn.started'},
        {'type': 'item.started', 'item': {'id': 't1', 'type': 'command_execution',
         'command': 'echo SECRET_API_KEY', 'status': 'in_progress'}},
        {'type': 'item.completed', 'item': {'id': 't1', 'type': 'command_execution',
         'aggregated_output': 'PRIVATE_TOOL_OUTPUT', 'exit_code': 0, 'status': 'completed'}},
        {'type': 'item.completed', 'item': {'id': 'a1', 'type': 'agent_message',
         'text': json.dumps(FINAL)}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 11, 'output_tokens': 7}},
    ]


class NativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stub = self.root / 'stub'

    def tearDown(self):
        self.tmp.cleanup()

    def make_stub(self, events, *, agent='codex', before='', after='', auth=True):
        auth_body = ("print('Logged in using ChatGPT'); sys.exit(0)" if agent == 'codex' else
            "print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'max'})); sys.exit(0)")
        if not auth:
            auth_body = "print('unconfirmed'); sys.exit(1)"
        content = f'''#!{sys.executable}
import json, os, sys, time, subprocess
if 'status' in sys.argv:
    {auth_body}
{before}
for event in {events!r}:
    print(json.dumps(event), flush=True)
{after}
'''
        self.stub.write_text(content)
        self.stub.chmod(0o700)
        return patch.object(native.adapters, 'find_' + agent, return_value=str(self.stub))

    async def test_codex_tool_stream_and_structured_final_real_process(self):
        observed = []
        with self.make_stub(codex_events()):
            result = await native.invoke_native('codex', 'hello', self.root,
                permission='full_access', response_schema=SCHEMA, on_activity=observed.append,
                receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        self.assertEqual(result['usage']['input_tokens'], 11)
        self.assertEqual([e['state'] for e in observed], ['started', 'completed'])
        self.assertEqual(observed[-1]['exit_code'], 0)
        self.assertNotIn('SECRET', json.dumps(result['events']))
        self.assertNotIn('PRIVATE_TOOL_OUTPUT', json.dumps(result))
        evidence = result['native_execution']
        self.assertEqual((evidence['tool_calls'], evidence['tool_results']), (1, 1))
        self.assertTrue(evidence['cleanup_confirmed'])
        self.assertTrue(evidence['side_effects_possible'])
        path = Path(evidence['receipt_path'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('PRIVATE_TOOL_OUTPUT', path.read_text())

    async def test_claude_tool_result_actual_not_assistant_claim(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'a',
                'name': 'Bash', 'input': {'command': 'private command'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': 'a', 'content': 'private result', 'is_error': True}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION,
                'structured_output': FINAL, 'usage': {'input_tokens': 3}},
        ]
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root,
                permission='full_access', response_schema=SCHEMA)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['events'][-1]['is_error'])
        self.assertEqual(result['events'][-1]['state'], 'failed')
        self.assertTrue(result['cleanup_confirmed'])
        self.assertNotIn('private command', json.dumps(result))

    async def test_failed_codex_command_can_be_repaired_before_turn_success(self):
        events = codex_events()
        events[3]['item']['exit_code'] = 1
        events[4:4] = [
            {'type': 'item.started', 'item': {'id': 't2', 'type': 'command_execution',
                'command': 'corrected command', 'status': 'in_progress'}},
            {'type': 'item.completed', 'item': {'id': 't2', 'type': 'command_execution',
                'aggregated_output': 'corrected result', 'exit_code': 0, 'status': 'completed'}},
        ]
        observed = []
        with self.make_stub(events):
            result = await native.invoke_native('codex', 'hello', self.root,
                permission='full_access', response_schema=SCHEMA, on_activity=observed.append)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        self.assertEqual([event['state'] for event in observed],
                         ['started', 'failed', 'started', 'completed'])
        self.assertEqual((observed[1]['exit_code'], observed[1]['is_error']), (1, True))
        self.assertEqual((observed[3]['exit_code'], observed[3]['is_error']), (0, False))
        self.assertEqual((result['native_execution']['tool_calls'],
                          result['native_execution']['tool_results']), (2, 2))
        self.assertTrue(result['cleanup_confirmed'])

    async def test_three_identical_failed_tools_with_unchanged_files_stop_and_clean_up(self):
        owned = self.root / 'owned.txt'
        owned.write_text('unchanged')
        for agent in ('codex', 'claude'):
            events = ([codex_events()[0], {'type': 'turn.started'}] if agent == 'codex' else
                      [{'type': 'system', 'subtype': 'init', 'session_id': SESSION}])
            for index in range(3):
                identifier = 'retry-' + str(index)
                if agent == 'codex':
                    events += [
                        {'type': 'item.started', 'item': {'id': identifier,
                            'type': 'command_execution', 'command': 'same failing command',
                            'status': 'in_progress'}},
                        {'type': 'item.completed', 'item': {'id': identifier,
                            'type': 'command_execution', 'status': 'completed',
                            'exit_code': 1, 'aggregated_output': 'same actual failure'}},
                    ]
                else:
                    events += [
                        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                            'id': identifier, 'name': 'Bash',
                            'input': {'command': 'same failing command'}}]}},
                        {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                            'tool_use_id': identifier, 'is_error': True,
                            'content': 'same actual failure'}]}},
                    ]
            fingerprints = []

            def fingerprint():
                value = hashlib.sha256(owned.read_bytes()).hexdigest()
                fingerprints.append(value)
                return value

            with self.subTest(agent=agent), self.make_stub(events, agent=agent, after='time.sleep(60)'):
                result = await asyncio.wait_for(native.invoke_native(agent, 'hello', self.root,
                    permission='full_access', progress_fingerprint=fingerprint), 5)
            self.assertEqual(result['failure_code'], 'native_no_progress')
            self.assertEqual(result['status'], 'failed')
            self.assertEqual((result['native_execution']['tool_calls'],
                              result['native_execution']['tool_results']), (3, 3))
            self.assertEqual(len(fingerprints), 3)
            self.assertEqual(len(set(fingerprints)), 1)
            self.assertTrue(result['native_execution']['side_effects_possible'])
            self.assertTrue(result['cleanup_confirmed'])

    async def test_actual_file_changes_allow_identical_failed_command_retries(self):
        owned, acknowledgement = self.root / 'owned.txt', self.root / 'acknowledgement'
        owned.write_text('version 0')
        # The stub waits for each fingerprint before modifying the real file;
        # otherwise pipe buffering could make every callback see the last one.
        before = f'''
print(json.dumps({codex_events()[0]!r}), flush=True)
print(json.dumps({{'type': 'turn.started'}}), flush=True)
for index in range(3):
    identifier = 'retry-' + str(index)
    print(json.dumps({{'type':'item.started','item':{{'id':identifier,
        'type':'command_execution','command':'same failing command','status':'in_progress'}}}}), flush=True)
    print(json.dumps({{'type':'item.completed','item':{{'id':identifier,
        'type':'command_execution','status':'completed','exit_code':1,
        'aggregated_output':'same actual failure'}}}}), flush=True)
    deadline = time.monotonic() + 3
    while not os.path.exists({str(acknowledgement)!r}) or open({str(acknowledgement)!r}).read() != str(index + 1):
        if time.monotonic() >= deadline:
            sys.exit(9)
        time.sleep(0.01)
    open({str(owned)!r}, 'w').write('version ' + str(index + 1))
'''
        fingerprints = []

        async def fingerprint():
            value = hashlib.sha256(owned.read_bytes()).hexdigest()
            fingerprints.append(value)
            acknowledgement.write_text(str(len(fingerprints)))
            return value

        with self.make_stub(codex_events()[-2:], before=before):
            result = await asyncio.wait_for(native.invoke_native('codex', 'hello', self.root,
                response_schema=SCHEMA, progress_fingerprint=fingerprint), 5)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        self.assertEqual((result['native_execution']['tool_calls'],
                          result['native_execution']['tool_results']), (3, 3))
        self.assertEqual(len(set(fingerprints)), 3)
        self.assertEqual(owned.read_text(), 'version 3')
        self.assertTrue(all(event['is_error'] for event in result['events']
                            if event.get('state') == 'failed'))
        self.assertTrue(result['cleanup_confirmed'])

    async def test_fingerprint_is_only_required_on_actual_failure(self):
        with self.make_stub(codex_events()):
            result = await native.invoke_native('codex', 'hello', self.root,
                progress_fingerprint=lambda: self.fail('successful tool must not query files'))
        self.assertEqual(result['status'], 'completed')
        events = codex_events()
        events[3]['item']['exit_code'] = 1
        with self.make_stub(events, after='time.sleep(60)'):
            result = await asyncio.wait_for(native.invoke_native('codex', 'hello', self.root,
                progress_fingerprint=lambda: 'unknown'), 5)
        self.assertEqual(result['failure_code'], 'native_progress_unconfirmed')
        self.assertNotEqual(result['failure_code'], 'native_no_progress')
        self.assertTrue(result['cleanup_confirmed'])

    def host_fingerprint(self, cwd, write_paths):
        """The host's own fingerprint for one work task, as the room service passes it."""
        from types import SimpleNamespace
        from local_agent_coordinator.room_service import RoomService
        store = SimpleNamespace(snapshot=lambda room_id: {'team': {'tasks': [{'id': 'T1', 'write_paths': write_paths}]}})
        job = {'room_id': 'room', 'team': {'task_id': 'T1', 'job_phase': 'work'}}
        return RoomService._native_progress_fingerprint(SimpleNamespace(store=store), job, cwd)

    async def test_one_failed_command_does_not_end_a_call_whose_task_names_a_folder(self):
        # Catches: a task listing a folder (or a link, or a file not made yet)
        # among its write paths had its call ended as native_progress_unconfirmed
        # by the first command that returned nonzero.
        project = self.root / 'project'
        (project / 'code/scripts').mkdir(parents=True)
        (project / 'code/scripts/a.gd').write_text('a\n')
        (project / 'own.py').write_text('old\n')
        (project / 'link').symlink_to(project / 'own.py')
        fingerprint = self.host_fingerprint(project, ['code/scripts', 'own.py', 'link', 'not-made-yet/out.txt'])
        events = codex_events()
        events[3]['item']['exit_code'] = 1
        with self.make_stub(events):
            result = await asyncio.wait_for(native.invoke_native('codex', 'hello', project,
                progress_fingerprint=fingerprint), 5)
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(result.get('failure_code'))

    async def test_the_host_fingerprint_sees_a_change_inside_a_named_folder(self):
        # Catches the folder tolerance blinding the repeated-failure check:
        # unchanged files must compare equal, an edit inside the folder must not.
        project = self.root / 'project'
        (project / 'code/scripts/deep').mkdir(parents=True)
        (project / 'code/scripts/deep/a.gd').write_text('a\n')
        fingerprint = self.host_fingerprint(project, ['code/scripts'])
        first = await fingerprint()
        self.assertRegex(first, r'^[0-9a-f]{64}$')
        self.assertEqual(await fingerprint(), first)
        (project / 'code/scripts/deep/a.gd').write_text('changed\n')
        changed = await fingerprint()
        self.assertNotEqual(changed, first)
        (project / 'code/scripts/deep/new.gd').write_text('new\n')
        self.assertNotEqual(await fingerprint(), changed)

    async def test_structured_format_result_is_not_an_execution_or_final_source(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                'id': 'format-1', 'name': 'StructuredOutput',
                'input': {'message': 'DO_NOT_STORE_FORMAT_INPUT'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': 'format-1', 'content': 'DO_NOT_STORE_FORMAT_RESULT',
                'is_error': False}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION,
                'structured_output': FINAL},
        ]
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root,
                response_schema=SCHEMA, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        evidence = result['native_execution']
        self.assertEqual((evidence['tool_calls'], evidence['tool_results']), (0, 0))
        self.assertFalse(evidence['side_effects_possible'])
        self.assertTrue(result['cleanup_confirmed'])
        self.assertNotIn('DO_NOT_STORE', Path(evidence['receipt_path']).read_text())
        self.assertFalse(any(event['type'] == 'native_tool' for event in result['events']))

    async def test_unmatched_format_result_remains_invalid(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': 'unmatched', 'content': 'not an established format tool'}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION,
                'structured_output': FINAL},
        ]
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root, response_schema=SCHEMA)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'native_protocol_invalid')
        self.assertTrue(result['cleanup_confirmed'])

    async def test_official_synthetic_user_context_is_neither_execution_nor_final_output(self):
        def context(content, **extra):
            return {'type': 'user', 'message': {'role': 'user', 'content': content},
                    'session_id': SESSION, 'parent_tool_use_id': None, 'uuid': str(uuid.uuid4()),
                    'timestamp': '2026-11-01T15:21:19.838Z', 'isSynthetic': True, **extra}
        contexts = [context('DO_NOT_STORE_SYNTHETIC_SUMMARY', isReplay=False),
                    context([{'type': 'text', 'text': 'DO_NOT_STORE_SYNTHETIC_CONTEXT'}]),
                    context([{'type': 'text', 'text': 'DO_NOT_STORE_SYNTHETIC_REPLAY'}], isReplay=True)]
        # Reproduce the previous tool-result-only branch without claiming that
        # the real rejected stdout frame's unrecorded body was recovered.
        old = native._Stream('claude', True, None)
        old.identity(SESSION)
        with patch.object(native, '_claude_synthetic_user_text', return_value=False):
            await old.consume(json.dumps(contexts[1]).encode())
        self.assertEqual(old.failure, 'native_protocol_unknown')
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'system', 'subtype': 'compact_boundary', 'session_id': SESSION,
             'uuid': str(uuid.uuid4()), 'compact_metadata': {'trigger': 'auto', 'pre_tokens': 1000}},
            *contexts,
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'real-read',
                'name': 'Read', 'input': {'file_path': 'synthetic.txt'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'real-read',
                'content': 'ACTUAL_TOOL_RESULT'}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL},
        ]
        observed = []
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root, response_schema=SCHEMA,
                on_activity=observed.append, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        self.assertEqual((result['native_execution']['tool_calls'], result['native_execution']['tool_results']), (1, 1))
        self.assertEqual([event['state'] for event in observed], ['started', 'completed'])
        self.assertTrue(result['cleanup_confirmed'])
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE', receipt)
        self.assertNotIn('DO_NOT_STORE', json.dumps(result))
        self.assertIn('ACTUAL_TOOL_RESULT', receipt)

    async def test_synthetic_user_context_does_not_relax_unknown_blocks_or_tool_identity(self):
        base = {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'context'}]},
                'session_id': SESSION, 'parent_tool_use_id': None, 'uuid': str(uuid.uuid4()), 'isSynthetic': True}
        cases = [
            {**base, 'isSynthetic': False}, {**base, 'isSynthetic': 1}, {**base, 'isReplay': 'false'},
            {**base, 'parent_tool_use_id': 'unknown-parent'}, {**base, 'uuid': 'invalid'},
            {**base, 'timestamp': []}, {**base, 'future_authority': True},
            {**base, 'message': {'role': 'assistant', 'content': base['message']['content']}},
            {**base, 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'context', 'authority': True}]}},
            {**base, 'message': {'role': 'user', 'content': [{'type': 'image', 'source': {}}]}},
            {**base, 'message': {'role': 'user', 'content': []}},
            {**base, 'message': {'role': 'user', 'content': [
                {'type': 'text', 'text': 'context'}, {'type': 'tool_result', 'tool_use_id': 'real-tool', 'content': 'mixed'}]}},
            {**base, 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'unknown-tool', 'content': 'forged'}]}},
        ]
        for event in cases:
            with self.subTest(fields=sorted(event), message=sorted(event['message'])):
                stream = native._Stream('claude', True, None)
                stream.identity(SESSION)
                await stream.consume(json.dumps({'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': 'real-tool', 'name': 'Read', 'input': {'file_path': 'synthetic'}}]}}).encode())
                await stream.consume(json.dumps(event).encode())
                self.assertIsNotNone(stream.failure)
                self.assertEqual((stream.calls, stream.results), (1, 0))
        for known in (None, str(uuid.uuid4())):
            stream = native._Stream('claude', True, None)
            if known:
                stream.identity(known)
            await stream.consume(json.dumps(base).encode())
            self.assertEqual(stream.failure, 'native_session_identity_mismatch')
            self.assertEqual((stream.calls, stream.results), (0, 0))

    async def test_rejected_user_diagnostic_is_bounded_schema_only(self):
        event = {'type': 'user', 'isSynthetic': True, 'isReplay': 'DO_NOT_STORE_REPLAY',
                 'DO_NOT_STORE_KEY': 'DO_NOT_STORE_AUTH', 'message': {'role': 'user',
                    'DO_NOT_STORE_MESSAGE_KEY': 'DO_NOT_STORE_MESSAGE_VALUE', 'content': [
                        {'type': 'text', 'text': 'DO_NOT_STORE_BODY', 'DO_NOT_STORE_BLOCK_KEY': 'DO_NOT_STORE_VALUE'},
                        {'type': ['DO_NOT_STORE_TYPE'], 'content': 'DO_NOT_STORE_CONTENT'},
                        *[{'type': 'future_secret_type', 'text': 'DO_NOT_STORE_MORE'} for _ in range(20)]]}}
        diagnostic = native._protocol_diagnostic(json.dumps(event).encode())
        self.assertEqual(diagnostic['event_fields'], ['isReplay', 'isSynthetic', 'message', 'type'])
        self.assertEqual(diagnostic['unknown_field_count'], 1)
        self.assertEqual(diagnostic['message_fields'], ['content', 'role'])
        self.assertEqual(diagnostic['content_type'], 'list')
        self.assertEqual(diagnostic['block_count'], 22)
        self.assertEqual(len(diagnostic['blocks']), 8)
        self.assertEqual(diagnostic['blocks'][0]['fields'], ['text', 'type'])
        self.assertTrue(diagnostic['isSynthetic'])
        self.assertNotIn('isReplay', diagnostic)
        self.assertNotIn('DO_NOT_STORE', json.dumps(diagnostic))
        self.assertNotIn('future_secret_type', json.dumps(diagnostic))
        with self.make_stub([{'type': 'system', 'subtype': 'init', 'session_id': SESSION}, event],
                            agent='claude', after='time.sleep(60)'):
            result = await asyncio.wait_for(native.invoke_native('claude', 'hello', self.root,
                receipt_dir=self.root / 'private'), 5)
        self.assertEqual(result['failure_code'], 'native_protocol_unknown')
        self.assertTrue(result['cleanup_confirmed'])
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE', receipt)
        self.assertEqual(json.loads(receipt.splitlines()[-1])['data'], diagnostic)

    async def test_claude_long_tool_heartbeat_and_known_notices_do_not_end_the_turn(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'system', 'subtype': 'informational', 'level': 'notice',
                'content': 'DO_NOT_STORE_BANNER', 'session_id': SESSION},
            {'type': 'system', 'subtype': 'thinking_tokens',
                'estimated_tokens': 10, 'estimated_tokens_delta': 10, 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                'id': 'bash-1', 'name': 'Bash', 'input': {'command': 'synthetic long command'}}]}},
            {'type': 'tool_progress', 'tool_use_id': 'bash-1', 'tool_name': 'Bash',
                'parent_tool_use_id': None, 'elapsed_time_seconds': 30,
                'heartbeat': True, 'session_id': SESSION},
            {'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'max_retries': 3,
                'retry_delay_ms': 1000, 'error_status': 500, 'error_category': 'server_error',
                'session_id': SESSION},
            {'type': 'system', 'subtype': 'task_updated', 'task_id': 'synthetic-task',
                'patch': {'status': 'completed'}, 'session_id': SESSION},
            {'type': 'system', 'subtype': 'files_persisted', 'files': [], 'failed': [],
                'processed_at': '2026-11-01T00:00:00Z', 'session_id': SESSION},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': 'bash-1', 'content': 'actual synthetic tool output', 'is_error': False}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL},
        ]
        observed = []
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root,
                response_schema=SCHEMA, on_activity=observed.append, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        self.assertEqual([event['state'] for event in observed], ['started', 'running', 'completed'])
        self.assertEqual((observed[1]['elapsed_time_seconds'], observed[1]['heartbeat']), (30, True))
        self.assertEqual((result['native_execution']['tool_calls'],
                          result['native_execution']['tool_results']), (1, 1))
        self.assertTrue(result['cleanup_confirmed'])
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE_BANNER', receipt)
        self.assertNotIn('estimated_tokens', receipt)
        progress = [json.loads(line)['data'] for line in receipt.splitlines()
                    if json.loads(line)['data']['type'] == 'tool_progress']
        self.assertEqual(progress, [{'type': 'tool_progress', 'tool_use_id': 'bash-1',
            'tool_name': 'Bash', 'elapsed_time_seconds': 30, 'heartbeat': True}])

    async def test_progress_receipt_keeps_only_safe_diagnostics_when_stream_rejects_it(self):
        event = {'type': 'tool_progress', 'tool_use_id': 'unmatched-tool', 'tool_name': 'Bash',
                 'elapsed_time_seconds': 30, 'heartbeat': True,
                 'session_id': SESSION, 'prompt': 'DO_NOT_STORE_PROMPT',
                 'thinking': 'DO_NOT_STORE_THINKING', 'auth': {'token': 'DO_NOT_STORE_AUTH'}}
        receipt = native._receipt_event('claude', json.dumps(event).encode())
        self.assertEqual(set(receipt), {'type', 'tool_use_id', 'tool_name', 'elapsed_time_seconds', 'heartbeat'})
        self.assertNotIn('DO_NOT_STORE', json.dumps(receipt))
        stream = native._Stream('claude', False, None)
        await stream.consume(json.dumps(event).encode())
        self.assertEqual(stream.failure, 'native_protocol_invalid')
        event.update(tool_name={'thinking': 'DO_NOT_STORE'}, elapsed_time_seconds='DO_NOT_STORE', heartbeat=1)
        receipt = native._receipt_event('claude', json.dumps(event).encode())
        self.assertEqual(receipt['invalid_fields'], ['tool_name', 'elapsed_time_seconds', 'heartbeat'])
        self.assertNotIn('DO_NOT_STORE', json.dumps(receipt))

    async def test_claude_foreground_heartbeat_suffix_binds_only_to_the_real_tool(self):
        # Installed Claude Code 2.1.316 emits foreground heartbeat IDs as
        # `${originalToolUseID}-heartbeat-${counter}`, not the original ID.
        identifier = 'toolu_synthetic'
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                'id': identifier, 'name': 'Bash', 'input': {'command': 'synthetic long command'}}]}},
        ]
        for index in range(2):
            events.append({'type': 'tool_progress', 'tool_use_id': f'{identifier}-heartbeat-{index}',
                'tool_name': 'Bash', 'elapsed_time_seconds': (index + 1) * 30,
                'heartbeat': True, 'session_id': SESSION})
        events += [
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': identifier, 'content': 'synthetic command really returned', 'is_error': False}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL},
        ]
        observed = []
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root,
                response_schema=SCHEMA, on_activity=observed.append, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([event['state'] for event in observed], ['started', 'running', 'running', 'completed'])
        self.assertEqual((result['native_execution']['tool_calls'],
                          result['native_execution']['tool_results']), (1, 1))
        self.assertTrue(result['cleanup_confirmed'])
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertIn('toolu_synthetic-heartbeat-0', receipt)
        self.assertIn('toolu_synthetic-heartbeat-1', receipt)

    async def test_heartbeat_alias_does_not_authorize_unknown_ids_names_or_tool_results(self):
        for identifier, heartbeat, name in [
            ('unknown-heartbeat-0', True, 'Bash'),
            ('a-heartbeat-0', False, 'Bash'),
            ('a-heartbeat--1', True, 'Bash'),
            ('a-heartbeat-0', True, 'Read'),
        ]:
            with self.subTest(identifier=identifier, heartbeat=heartbeat, name=name):
                stream = native._Stream('claude', False, None)
                await stream.consume(json.dumps({'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {'command': 'synthetic'}}]}}).encode())
                await stream.consume(json.dumps({'type': 'tool_progress', 'tool_use_id': identifier,
                    'tool_name': name, 'elapsed_time_seconds': 30, 'heartbeat': heartbeat}).encode())
                self.assertEqual(stream.failure, 'native_protocol_invalid')
                self.assertEqual((stream.calls, stream.results), (1, 0))
        stream = native._Stream('claude', False, None)
        await stream.consume(json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {'command': 'synthetic'}}]}}).encode())
        await stream.consume(json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'a-heartbeat-0', 'is_error': False, 'content': 'forged'}]}}).encode())
        self.assertEqual(stream.failure, 'native_protocol_invalid')
        self.assertEqual((stream.calls, stream.results), (1, 0))

    async def test_installed_ui_invalidation_before_init_is_metadata_not_execution(self):
        notice = {'type': 'system', 'subtype': 'ui_invalidate', 'event': 'ui.render',
                  'uuid': str(uuid.uuid4()), 'session_id': SESSION, 'prompts_screened': True,
                  'instances': [{'surface': 'DO_NOT_STORE_SURFACE', 'component': 'DO_NOT_STORE_COMPONENT',
                                 'instance_id': 'DO_NOT_STORE_INSTANCE'}]}
        events = [notice,
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'a',
                'name': 'Bash', 'input': {'command': 'synthetic command'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': 'a', 'content': 'synthetic result', 'is_error': False}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL}]
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root,
                response_schema=SCHEMA, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['native_execution']['tool_calls'],
                          result['native_execution']['tool_results']), (1, 1))
        self.assertTrue(result['cleanup_confirmed'])
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE', receipt)
        self.assertNotIn('prompts_screened', receipt)

    async def test_ui_invalidation_is_narrow_and_keeps_session_identity_checks(self):
        notice = {'type': 'system', 'subtype': 'ui_invalidate', 'event': 'ui.render',
                  'uuid': str(uuid.uuid4()), 'session_id': SESSION}
        for changes in ({'event': 'Bash'}, {'prompts_screened': False},
                        {'instances': [{'surface': 'surface'}]}, {'output': 'not metadata'},
                        {'uuid': None}):
            with self.subTest(fields=sorted(changes)):
                stream = native._Stream('claude', False, None)
                await stream.consume(json.dumps({**notice, **changes}).encode())
                self.assertEqual(stream.failure, 'native_protocol_invalid')
                self.assertEqual((stream.calls, stream.results), (0, 0))
        stream = native._Stream('claude', False, None)
        stream.identity(SESSION)
        await stream.consume(json.dumps({**notice, 'session_id': str(uuid.uuid4())}).encode())
        self.assertEqual(stream.failure, 'native_session_identity_mismatch')
        # A later, unrelated notice does not clear an identity failure.
        await stream.consume(json.dumps({'type': 'system', 'subtype': 'ui_log', 'text': 'unknown'}).encode())
        self.assertEqual(stream.failure, 'native_session_identity_mismatch')

    async def test_future_system_notice_mid_turn_keeps_the_completed_result(self):
        # Catches: Claude Code 2.1.319's {"type":"system","subtype":"notification"}
        # arriving after StructuredOutput was rejected as native_protocol_unknown,
        # discarding a finished review and stopping the room.
        notice = {'type': 'system', 'subtype': 'notification', 'session_id': SESSION,
                  'uuid': str(uuid.uuid4()), 'text': 'DO_NOT_STORE_NOTICE_BODY', 'key': 'DO_NOT_STORE_KEY'}
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'a',
                'name': 'Bash', 'input': {'command': 'synthetic command'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': 'a', 'content': 'synthetic result', 'is_error': False}]}},
            notice,
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL}]
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root,
                response_schema=SCHEMA, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        self.assertEqual((result['native_execution']['tool_calls'],
                          result['native_execution']['tool_results']), (1, 1))
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE', receipt)
        self.assertIn({'type': 'native_protocol_notice', 'event_type': 'system', 'event_subtype': 'notification'},
                      [json.loads(line)['data'] for line in receipt.splitlines()])

    async def test_future_system_notice_is_narrow(self):
        # Catches: tolerating notices so broadly that a forged identity, an
        # unsafe label, a malformed KNOWN subtype or a new top-level type passes.
        stream = native._Stream('claude', False, None)
        stream.identity(SESSION)
        await stream.consume(json.dumps({'type': 'system', 'subtype': 'notification',
                                         'session_id': str(uuid.uuid4())}).encode())
        self.assertEqual(stream.failure, 'native_session_identity_mismatch')
        for event, failure in [
                ({'type': 'system', 'subtype': 'bad label', 'session_id': SESSION}, 'native_protocol_unknown'),
                ({'type': 'system', 'session_id': SESSION}, 'native_protocol_unknown'),
                ({'type': 'system', 'subtype': 'ui_invalidate', 'event': 'Bash', 'uuid': str(uuid.uuid4()),
                  'session_id': SESSION}, 'native_protocol_invalid'),
                ({'type': 'future_metadata', 'subtype': 'notification'}, 'native_protocol_unknown')]:
            with self.subTest(event=event):
                stream = native._Stream('claude', False, None)
                stream.identity(SESSION)
                await stream.consume(json.dumps(event).encode())
                self.assertEqual(stream.failure, failure)
                self.assertEqual((stream.calls, stream.results), (0, 0))

    async def test_background_membership_is_metadata_and_does_not_invent_tool_results(self):
        task = {'task_id': 'background-1', 'run_id': 'run-1', 'task_type': 'local_bash',
                'description': 'DO_NOT_STORE_TASK_DESCRIPTION', 'ambient': False, 'awaited': True}
        def membership(tasks):
            return {'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': tasks,
                    'uuid': str(uuid.uuid4()), 'session_id': SESSION}
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'bash-1',
                'name': 'Bash', 'input': {'command': 'synthetic background command', 'run_in_background': True}}]}},
            membership([task]), membership([]),
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'bash-1',
                'content': 'actual synthetic error result', 'is_error': True}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL},
        ]
        observed = []
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'hello', self.root,
                response_schema=SCHEMA, on_activity=observed.append, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([event['state'] for event in observed], ['started', 'failed'])
        self.assertEqual((result['native_execution']['tool_calls'],
                          result['native_execution']['tool_results']), (1, 1))
        self.assertIsNone(result['usage'])
        self.assertTrue(result['cleanup_confirmed'])
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertNotIn('DO_NOT_STORE', receipt)
        self.assertNotIn('background_tasks_changed', receipt)

    async def test_background_membership_shape_and_session_remain_strict(self):
        task = {'task_id': 'background-1', 'task_type': 'local_bash', 'description': 'synthetic'}
        event = {'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [task],
                 'uuid': str(uuid.uuid4()), 'session_id': SESSION}
        for changes in ({'tasks': 'not an array'}, {'tasks': [{}]},
                        {'tasks': [{**task, 'awaited': 'true'}]},
                        {'tasks': [{**task, 'description': {'output': 'not text'}}]},
                        {'tasks': [{**task, 'tool_result': 'not metadata'}]},
                        {'usage': {'input_tokens': 0}}, {'uuid': None}):
            with self.subTest(fields=sorted(changes)):
                stream = native._Stream('claude', False, None)
                await stream.consume(json.dumps({**event, **changes}).encode())
                self.assertEqual(stream.failure, 'native_protocol_invalid')
                self.assertEqual((stream.calls, stream.results), (0, 0))
        stream = native._Stream('claude', False, None)
        stream.identity(SESSION)
        await stream.consume(json.dumps({**event, 'session_id': str(uuid.uuid4())}).encode())
        self.assertEqual(stream.failure, 'native_session_identity_mismatch')

    async def test_claude_progress_must_match_an_established_tool(self):
        for identifier, name, elapsed in [('unknown', 'Bash', 30), ('a', 'Read', 30), ('a', 'Bash', -1)]:
            with self.subTest(identifier=identifier, name=name, elapsed=elapsed):
                stream = native._Stream('claude', False, None)
                await stream.consume(json.dumps({'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {'command': 'synthetic'}}]}}).encode())
                await stream.consume(json.dumps({'type': 'tool_progress', 'tool_use_id': identifier,
                    'tool_name': name, 'elapsed_time_seconds': elapsed}).encode())
                self.assertEqual(stream.failure, 'native_protocol_invalid')
                self.assertEqual((stream.calls, stream.results), (1, 0))

    async def test_private_receipt_excludes_thinking_chat_and_stderr_but_keeps_tools(self):
        codex = codex_events()
        codex.insert(2, {'type': 'item.completed', 'item': {'id': 'r1', 'type': 'reasoning',
            'text': 'DO_NOT_STORE_CODEX_THINKING'}})
        codex.insert(3, {'type': 'item.completed', 'item': {'id': 'chat', 'type': 'agent_message',
            'phase': 'commentary', 'text': 'DO_NOT_STORE_CODEX_CHAT'}})
        claude = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION,
             'apiKeySource': 'DO_NOT_STORE_INIT_DETAILS'},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'thinking', 'thinking': 'DO_NOT_STORE_CLAUDE_THINKING'},
                {'type': 'redacted_thinking', 'data': 'DO_NOT_STORE_REDACTED_THINKING'},
                {'type': 'text', 'text': 'DO_NOT_STORE_CLAUDE_CHAT'},
                {'type': 'tool_use', 'id': 'a', 'name': 'Bash',
                 'input': {'command': 'echo RETAIN_TOOL_COMMAND'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': 'a', 'content': 'RETAIN_ACTUAL_TOOL_OUTPUT', 'is_error': False}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION,
                'structured_output': FINAL, 'result': 'DO_NOT_STORE_FINAL_CHAT'},
        ]
        for agent, events in [('codex', codex), ('claude', claude)]:
            with self.subTest(agent=agent), self.make_stub(events, agent=agent,
                    before="print('DO_NOT_STORE_RAW_STDERR',file=sys.stderr,flush=True)"):
                result = await native.invoke_native(agent, 'DO_NOT_STORE_PROMPT', self.root,
                    response_schema=SCHEMA, receipt_dir=self.root / ('private-' + agent))
            self.assertEqual(result['status'], 'completed')
            receipt = Path(result['native_execution']['receipt_path']).read_text()
            self.assertNotIn('DO_NOT_STORE', receipt)
            self.assertIn(SESSION, receipt)
            if agent == 'codex':
                self.assertIn('echo SECRET_API_KEY', receipt)
                self.assertIn('PRIVATE_TOOL_OUTPUT', receipt)
                self.assertIn('"exit_code": 0', receipt)
            else:
                self.assertIn('RETAIN_TOOL_COMMAND', receipt)
                self.assertIn('RETAIN_ACTUAL_TOOL_OUTPUT', receipt)
                self.assertIn('"is_error": false', receipt)

    async def test_assistant_passed_text_is_not_execution_evidence(self):
        events = [codex_events()[0], {'type': 'item.completed', 'item': {'id': 'a',
            'type': 'agent_message', 'text': 'Tests all passed'}}, codex_events()[-1]]
        with self.make_stub(events):
            result = await native.invoke_native('codex', 'hello', self.root)
        self.assertEqual(result['native_execution']['tool_results'], 0)
        self.assertFalse(result['native_execution']['side_effects_possible'])

    async def test_api_credentials_not_inherited(self):
        check = "assert all(k not in os.environ for k in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL','AWS_ACCESS_KEY_ID')); assert os.environ.get('HOME')"
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'forbidden', 'ANTHROPIC_API_KEY': 'forbidden',
                'ANTHROPIC_BASE_URL': 'https://bad.invalid', 'AWS_ACCESS_KEY_ID': 'forbidden'}):
            with self.make_stub(codex_events(), before=check):
                result = await native.invoke_native('codex', 'hello', self.root)
        self.assertEqual(result['status'], 'completed')

    async def test_claude_command_timers_are_process_local_and_settings_cannot_lower_them(self):
        expected = {'BASH_DEFAULT_TIMEOUT_MS': '2147483647', 'BASH_MAX_TIMEOUT_MS': '2147483647',
                    'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'}
        events = [{'type': 'system', 'subtype': 'init', 'session_id': SESSION},
                  {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL}]
        before = (f"expected={expected!r}; "
                  "assert {key:os.environ.get(key) for key in expected} == expected; "
                  "settings=json.loads(sys.argv[sys.argv.index('--settings')+1]); "
                  "assert settings == {'apiKeyHelper':'','env':expected}")
        inherited = {'BASH_DEFAULT_TIMEOUT_MS': '1', 'BASH_MAX_TIMEOUT_MS': '2', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '0'}
        with patch.dict(os.environ, inherited):
            with self.make_stub(events, agent='claude', before=before):
                result = await native.invoke_native('claude', 'hello', self.root,
                    permission='full_access', response_schema=SCHEMA)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['native_execution']['auth_method'], 'claude.ai')
            self.assertEqual(result['native_execution']['permission'], 'full_access')
            self.assertEqual({key: os.environ[key] for key in expected}, inherited)
            self.assertTrue(set(expected).isdisjoint(native.adapters._environment()))
            with self.make_stub(codex_events(), before=
                    "assert 'BASH_DEFAULT_TIMEOUT_MS' not in os.environ; "
                    "assert 'BASH_MAX_TIMEOUT_MS' not in os.environ; assert '--settings' not in sys.argv"):
                codex = await native.invoke_native('codex', 'hello', self.root)
            self.assertEqual(codex['status'], 'completed')

    async def test_claude_vendor_timer_override_does_not_override_host_deadline_cleanup(self):
        events = [{'type': 'system', 'subtype': 'init', 'session_id': SESSION},
                  {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'long-bash',
                    'name': 'Bash', 'input': {'command': 'synthetic long command'}}]}}]
        before = "assert os.environ['BASH_DEFAULT_TIMEOUT_MS'] == '2147483647'"
        with self.make_stub(events, agent='claude', before=before, after='time.sleep(60)'):
            result = await native.invoke_native('claude', 'hello', self.root, timeout=0.5,
                permission='full_access')
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['failure_code'], 'deadline_exceeded')
        self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(result['native_execution']['tool_calls'], 1)
        self.assertTrue(result['native_execution']['side_effects_possible'])

    async def test_unconfirmed_auth_never_infers(self):
        marker = self.root / 'inference'
        with self.make_stub(codex_events(), auth=False, before=f"open({str(marker)!r},'w').write('bad')"):
            with self.assertRaisesRegex(RuntimeError, 'inference refused'):
                await native.invoke_native('codex', 'hello', self.root)
        self.assertFalse(marker.exists())

    async def test_unknown_tool_fails_even_with_valid_final(self):
        events = codex_events()
        events.insert(2, {'type': 'item.completed', 'item': {'id': 'x', 'type': 'future_tool'}})
        with self.make_stub(events):
            result = await native.invoke_native('codex', 'hello', self.root)
        self.assertEqual(result['failure_code'], 'native_tool_unknown')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['native_execution']['side_effects_possible'])

    async def test_unknown_protocol_fails(self):
        with self.make_stub(codex_events() + [{'type': 'mystery'}]):
            result = await native.invoke_native('codex', 'hello', self.root)
        self.assertEqual(result['failure_code'], 'native_protocol_unknown')

    async def test_rejected_protocol_receipt_records_labels_without_private_values(self):
        for event in [
            {'type': 'future_metadata', 'subtype': 'future_startup_notice',
             'output': ['DO_NOT_STORE_AUTH'], 'prompt': 'DO_NOT_STORE_PROMPT',
             'thinking': 'DO_NOT_STORE_THINKING'},
            # A future system subtype is a tolerated notice; an unsafe label is not.
            {'type': 'system', 'subtype': 'future subtype', 'content': 'DO_NOT_STORE_BODY'},
        ]:
            with self.subTest(event_type=event['type']), self.make_stub([event], agent='claude', after='time.sleep(60)'):
                result = await asyncio.wait_for(native.invoke_native('claude', 'hello', self.root,
                    receipt_dir=self.root / 'private'), 5)
            self.assertEqual(result['failure_code'], 'native_protocol_unknown')
            self.assertTrue(result['cleanup_confirmed'])
            text = Path(result['native_execution']['receipt_path']).read_text()
            self.assertNotIn('DO_NOT_STORE', text)
            self.assertEqual([json.loads(line)['data'] for line in text.splitlines()],
                [{'type': 'native_protocol_diagnostic', 'event_type': event['type'],
                  **({'event_subtype': event['subtype']} if ' ' not in event['subtype'] else {})}])
        self.assertEqual(native._protocol_diagnostic(json.dumps({'type': {'auth': 'secret'},
            'subtype': 'DO NOT STORE CONTENT'}).encode()), {'type': 'native_protocol_diagnostic'})

    async def test_nonzero_exit_fails_despite_final(self):
        with self.make_stub(codex_events(), after='sys.exit(7)'):
            result = await native.invoke_native('codex', 'hello', self.root)
        self.assertEqual(result['failure_code'], 'cli_nonzero_exit')
        self.assertEqual(result['native_execution']['exit_code'], 7)

    async def test_session_mismatch_fails_preserves_actual_identity(self):
        with self.make_stub(codex_events()):
            result = await native.invoke_native('codex', 'hello', self.root, str(uuid.uuid4()))
        self.assertEqual(result['failure_code'], 'native_session_identity_mismatch')
        self.assertEqual(result['session_id'], SESSION)

    async def test_malformed_structured_final_fails(self):
        events = codex_events()
        events[-2]['item']['text'] = 'not json'
        with self.make_stub(events):
            result = await native.invoke_native('codex', 'hello', self.root, response_schema=SCHEMA)
        self.assertEqual(result['failure_code'], 'protocol_turn_incomplete')

    async def test_timeout_cleans_group_child(self):
        marker = self.root / 'child'
        before = (f"child=subprocess.Popen([{sys.executable!r},'-c','import time; time.sleep(60)']); "
                  f"open({str(marker)!r},'w').write(str(child.pid))")
        with self.make_stub(codex_events()[:3], before=before, after='time.sleep(60)'):
            result = await native.invoke_native('codex', 'hello', self.root, timeout=0.5)
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(result['cleanup_confirmed'])
        table = native._processes()
        pid = int(marker.read_text())
        self.assertTrue(pid not in table or 'Z' in table[pid][3])
        self.assertTrue(result['native_execution']['side_effects_possible'])

    async def test_cancel_cleans_observed_detached_child(self):
        marker = self.root / 'child'
        before = (f"child=subprocess.Popen([{sys.executable!r},'-c','import time; time.sleep(60)'],start_new_session=True); "
                  f"open({str(marker)!r},'w').write(str(child.pid))")
        with self.make_stub(codex_events()[:3], before=before, after='time.sleep(60)'):
            task = asyncio.create_task(native.invoke_native('codex', 'hello', self.root))
            for _ in range(60):
                if marker.exists():
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(marker.exists())
            await asyncio.sleep(0.3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await task
            self.assertTrue(cancelled.exception.native_execution['cleanup_confirmed'])
            self.assertTrue(cancelled.exception.native_execution['side_effects_possible'])
        table = native._processes()
        pid = int(marker.read_text())
        self.assertTrue(pid not in table or 'Z' in table[pid][3])

    async def test_cancel_during_auth_reports_never_started_only_after_status_process_cleanup(self):
        marker, inference = self.root / 'auth.pid', self.root / 'inference.started'
        self.stub.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
if 'status' in sys.argv:
    Path({str(marker)!r}).write_text(str(os.getpid()))
    time.sleep(60)
else:
    Path({str(inference)!r}).write_text('unexpected inference')
''')
        self.stub.chmod(0o700)
        with patch.object(native.adapters, 'find_codex', return_value=str(self.stub)):
            task = asyncio.create_task(native.invoke_native('codex', 'hello', self.root))
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(marker.exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await task
        evidence = cancelled.exception.native_execution
        self.assertTrue(evidence['never_started'])
        self.assertTrue(evidence['cleanup_confirmed'])
        self.assertFalse(evidence['side_effects_possible'])
        self.assertEqual((evidence['tool_calls'], evidence['tool_results']), (0, 0))
        self.assertIsNone(evidence['receipt_path'])
        self.assertFalse(inference.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    async def test_auth_cleanup_failure_never_reports_its_live_status_process_clean(self):
        marker = self.root / 'auth.pid'
        self.stub.write_text(f'''#!{sys.executable}
import os, time
from pathlib import Path
Path({str(marker)!r}).write_text(str(os.getpid()))
time.sleep(60)
''')
        self.stub.chmod(0o700)
        processes = []
        terminate = native.adapters._terminate
        async def unconfirmed(process):
            processes.append(process)
            raise RuntimeError('Synthetic cleanup failure')
        try:
            with patch.object(native.adapters, 'find_codex', return_value=str(self.stub)), \
                    patch.object(native.adapters, '_terminate', side_effect=unconfirmed):
                task = asyncio.create_task(native.invoke_native('codex', 'hello', self.root))
                for _ in range(100):
                    if marker.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(marker.exists())
                task.cancel()
                with self.assertRaises(RuntimeError) as failed:
                    await task
            evidence = failed.exception.native_execution
            self.assertTrue(evidence['never_started'])
            self.assertFalse(evidence['cleanup_confirmed'])
            os.kill(int(marker.read_text()), 0)
        finally:
            for process in processes:
                await terminate(process)

    async def test_auth_rejection_keeps_no_session_proof_without_claiming_inference(self):
        with self.make_stub([], auth=False):
            with self.assertRaises(RuntimeError) as failed:
                await native.invoke_native('codex', 'hello', self.root)
        evidence = failed.exception.native_execution
        self.assertTrue(evidence['never_started'])
        self.assertTrue(evidence['cleanup_confirmed'])
        self.assertIsNone(evidence['receipt_path'])

    async def test_auth_deadline_preserves_cancelled_status_child_cleanup_proof(self):
        marker = self.root / 'auth.pid'
        self.stub.write_text(f'''#!{sys.executable}
import os, time
from pathlib import Path
Path({str(marker)!r}).write_text(str(os.getpid()))
time.sleep(60)
''')
        self.stub.chmod(0o700)
        processes = []
        terminate = native.adapters._terminate
        async def tracked(process):
            processes.append(process)
            await terminate(process)
        with patch.object(native.adapters, 'find_codex', return_value=str(self.stub)), \
                patch.object(native.adapters, '_terminate', side_effect=tracked):
            with self.assertRaises(asyncio.TimeoutError) as failed:
                await native.invoke_native('codex', 'hello', self.root, timeout=0.15)
        self.assertTrue(failed.exception.native_execution['never_started'])
        self.assertTrue(failed.exception.native_execution['cleanup_confirmed'])
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(processes[0].pid, 0)

    async def test_claude_quota_after_tool_preserves_side_effect_marker(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'a',
                'name': 'Write', 'input': {'file_path': 'hidden'}}]}},
            {'type': 'assistant', 'isApiErrorMessage': True, 'error': 'rate_limit',
                'message': {'content': [{'type': 'text', 'text': "You've hit your weekly limit · resets 3pm (America/Vancouver)"}]}},
            {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                'session_id': SESSION},
        ]
        with self.make_stub(events, agent='claude', after='sys.exit(1)'):
            result = await native.invoke_native('claude', 'hello', self.root)
        self.assertEqual(result['failure_code'], 'subscription_limit_reached')
        self.assertTrue(result['native_execution']['side_effects_possible'])
        self.assertEqual(result['text'], '')

    async def test_structured_provider_limit_after_completed_tool_keeps_cleanup_receipt(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'a',
                'name': 'Write', 'input': {'file_path': 'hidden'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'a',
                'content': 'actual tool result', 'is_error': False}]}},
            {'type': 'assistant', 'session_id': SESSION, 'isApiErrorMessage': True,
                'error': 'rate_limit', 'apiError': 'usage_limit_reached',
                'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'PRIVATE RESET TEXT'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': SESSION},
        ]
        with self.make_stub(events, agent='claude', after='sys.exit(1)'):
            result = await native.invoke_native('claude', 'hello', self.root,
                receipt_dir=self.root / 'private')
        self.assertEqual(result['failure_code'], 'subscription_limit_reached')
        evidence = result['native_execution']
        self.assertEqual((evidence['tool_calls'], evidence['tool_results']), (1, 1))
        self.assertTrue(evidence['side_effects_possible'])
        self.assertTrue(evidence['cleanup_confirmed'])
        self.assertEqual(evidence['exit_code'], 1)
        receipt = Path(evidence['receipt_path']).read_text()
        self.assertIn('usage_limit_reached', receipt)
        self.assertIn('actual tool result', receipt)
        self.assertNotIn('PRIVATE RESET TEXT', receipt)

    async def test_wrong_session_limit_does_not_retire_provider(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'session_id': str(uuid.uuid4()), 'isApiErrorMessage': True,
                'error': 'rate_limit', 'apiError': 'usage_limit_reached',
                'message': {'content': [{'type': 'text', 'text': 'quota'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': SESSION},
        ]
        with self.make_stub(events, agent='claude', after='sys.exit(1)'):
            result = await native.invoke_native('claude', 'hello', self.root)
        self.assertEqual(result['failure_code'], 'native_session_identity_mismatch')

    async def test_namespaced_claude_mcp_tool_preserves_paired_receipt(self):
        name = 'mcp__local_fixture__read_value'
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'mcp-call', 'name': name, 'input': {'key': 'example'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'mcp-call', 'content': 'example-value'}]}},
            {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL}]
        with self.make_stub(events, agent='claude'):
            result = await native.invoke_native('claude', 'fixture', self.root,
                permission='full_access', response_schema=SCHEMA, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['native_execution']['tool_calls'], result['native_execution']['tool_results']), (1, 1))
        self.assertIn(name, Path(result['native_execution']['receipt_path']).read_text())
        for invalid in ('', None, {}, 'mcp__server__bad name', 'Bad\x00Name', 'x' * 257):
            self.assertFalse(native._claude_tool_name_valid(invalid))

    async def test_new_native_claude_tool_name_is_not_an_application_allowlist(self):
        init = {'type': 'system', 'subtype': 'init', 'session_id': SESSION}
        call = {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'new-tool', 'name': 'EndConversation', 'input': {}}]}}
        result = {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'new-tool', 'content': 'done'}]}}
        final = {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL}
        with self.make_stub([init, call, result, final], agent='claude'):
            complete = await native.invoke_native('claude', 'fixture', self.root,
                permission='plan', response_schema=SCHEMA)
        self.assertEqual(complete['status'], 'completed')
        self.assertEqual((complete['native_execution']['tool_calls'], complete['native_execution']['tool_results']), (1, 1))
        with self.make_stub([init, call, final], agent='claude'):
            missing = await native.invoke_native('claude', 'fixture', self.root,
                permission='plan', response_schema=SCHEMA)
        self.assertEqual(missing['failure_code'], 'native_tool_result_missing')
        wrong = {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'unobserved', 'content': 'done'}]}}
        with self.make_stub([init, call, wrong, final], agent='claude'):
            mismatch = await native.invoke_native('claude', 'fixture', self.root,
                permission='plan', response_schema=SCHEMA)
        self.assertNotEqual(mismatch['status'], 'completed')

    async def test_claude_repeated_tool_identity_cannot_change_its_name(self):
        for first, second in (('Read', 'Read'), ('Read', 'EndConversation'),
                              ('Read', 'StructuredOutput'), ('StructuredOutput', 'Read')):
            with self.subTest(first=first, second=second):
                events = [{'type': 'system', 'subtype': 'init', 'session_id': SESSION}]
                for name in (first, second):
                    events.append({'type': 'assistant', 'message': {'content': [
                        {'type': 'tool_use', 'id': 'same-id', 'name': name, 'input': {}}]}})
                events.extend([
                    {'type': 'user', 'message': {'content': [
                        {'type': 'tool_result', 'tool_use_id': 'same-id', 'content': 'done'}]}},
                    {'type': 'result', 'subtype': 'success', 'session_id': SESSION, 'structured_output': FINAL}])
                with self.make_stub(events, agent='claude'):
                    result = await native.invoke_native('claude', 'fixture', self.root,
                        permission='full_access', response_schema=SCHEMA)
                if first == second:
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(result['native_execution']['tool_calls'], 1)
                else:
                    self.assertEqual(result['failure_code'], 'native_protocol_invalid')

    async def test_exact_legacy_config_warning_keeps_receipt_and_real_final(self):
        notice = ('Codex is ignoring 1 unrecognized configuration setting. '
                  'Check for typos or deprecated settings.\n'
                  '  user (/Users/example/.codex/config.toml): `features.rmcp_client` is ignored.')
        events = codex_events()
        events.insert(1, {'type': 'item.completed', 'item': {'type': 'error', 'message': notice}})
        with self.make_stub(events):
            result = await native.invoke_native('codex', 'fixture', self.root,
                permission='plan', response_schema=SCHEMA, receipt_dir=self.root / 'private')
        self.assertEqual(result['status'], 'completed')
        self.assertIn({'type': 'warning', 'code': 'ignored_legacy_config_notice'}, result['events'])
        receipt = Path(result['native_execution']['receipt_path']).read_text()
        self.assertIn('ignored_legacy_config_notice', receipt)
        self.assertNotIn('/Users/example', receipt)
        no_final = [event for event in events if event.get('item', {}).get('type') != 'agent_message']
        with self.make_stub(no_final):
            absent = await native.invoke_native('codex', 'fixture', self.root,
                permission='plan', response_schema=SCHEMA)
        self.assertEqual(absent['status'], 'failed')
        self.assertEqual(absent['failure_code'], 'protocol_turn_incomplete')

    async def test_unknown_tool_alone_cannot_be_replayed_as_effect_free(self):
        events = [codex_events()[0], {'type': 'item.started',
            'item': {'id': 'x', 'type': 'new_unknown_tool'}}]
        with self.make_stub(events, after='time.sleep(60)'):
            result = await asyncio.wait_for(native.invoke_native('codex', 'hello', self.root), 5)
        self.assertEqual(result['failure_code'], 'native_tool_unknown')
        self.assertTrue(result['native_execution']['side_effects_possible'])

    async def test_duplicate_terminal_is_not_success(self):
        with self.make_stub(codex_events() + [codex_events()[-1]]):
            result = await native.invoke_native('codex', 'hello', self.root)
        self.assertEqual(result['failure_code'], 'protocol_turn_incomplete')

    async def test_cleanup_waits_for_a_slow_child_to_exit(self):
        # Catches a child that needs a moment to die after SIGKILL (an MCP server on a busy machine) being
        # reported as alive, which used to stop the whole room.
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'pass', start_new_session=True)
        await process.wait()
        seen = []
        def table():
            seen.append(1)
            return {999_999: (1, 999_999, 'Mon Jan  1 00:00:00 2026', 'S')} if len(seen) <= 4 else {}
        with patch.object(native, '_processes', side_effect=table), patch.object(native, '_CLEANUP_SETTLE_SECONDS', 5):
            self.assertTrue(await native._cleanup(process, {999_999: 'Mon Jan  1 00:00:00 2026'}))
        with patch.object(native, '_processes', return_value={999_999: (1, 999_999, 'Mon Jan  1 00:00:00 2026', 'S')}), \
             patch.object(native, '_CLEANUP_SETTLE_SECONDS', 0.3):
            self.assertFalse(await native._cleanup(process, {999_999: 'Mon Jan  1 00:00:00 2026'}))

    async def test_cleanup_inspection_failure_is_not_confirmed(self):
        with self.make_stub(codex_events()):
            with patch.object(native, '_processes', side_effect=RuntimeError('inspection unavailable')):
                result = await native.invoke_native('codex', 'hello', self.root)
        self.assertFalse(result['cleanup_confirmed'])
        self.assertEqual(result['failure_code'], 'native_cleanup_unconfirmed')


    async def test_real_subprocess_public_speech_callback_precedes_final_return_for_both_providers(self):
        speech = 'I will handle the implementation; please check the edge cases.'
        for agent in ('codex', 'claude'):
            with self.subTest(agent=agent):
                if agent == 'codex':
                    events = codex_events()
                    events.insert(2, {'type': 'item.completed', 'item': {'id': 'public-speech',
                        'type': 'agent_message', 'text': speech}})
                else:
                    events = [
                        {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
                        {'type': 'assistant', 'session_id': SESSION, 'uuid': str(uuid.uuid4()),
                         'parent_tool_use_id': None, 'message': {'role': 'assistant',
                         'content': [{'type': 'text', 'text': speech}]}},
                        {'type': 'result', 'subtype': 'success', 'session_id': SESSION,
                         'structured_output': FINAL},
                    ]
                observed = []
                def callback(event):
                    if event['type'] == 'native_progress':
                        self.assertFalse(operation.done())
                    observed.append(event)
                with self.make_stub(events, agent=agent):
                    operation = asyncio.create_task(native.invoke_native(agent, 'hello', self.root,
                        response_schema=SCHEMA, on_activity=callback))
                    result = await operation
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(json.loads(result['text']), FINAL)
                self.assertEqual([event['text'] for event in observed if event['type'] == 'native_progress'], [speech])


class CommandTests(unittest.TestCase):
    def test_permissions_are_real_flags_and_full_not_default(self):
        with patch.object(native.adapters, 'find_codex', return_value='/codex'), patch.object(native.adapters, 'find_claude', return_value='/claude'):
            for permission, sandbox in [('read_only','read-only'),('workspace_write','workspace-write'),('full_access','danger-full-access')]:
                command = native.native_command('codex', permission=permission)
                self.assertEqual(command[command.index('-s') + 1], sandbox)
                self.assertNotIn('--ephemeral', command)
            self.assertIn('read-only', native.native_command('codex'))
            full = native.native_command('claude', permission='full_access')
            self.assertIn('bypassPermissions', full)
            self.assertNotIn('--restricted', full)
            self.assertNotIn('--tools', full)
            workspace = native.native_command('claude', permission='workspace_write')
            self.assertIn('--restricted', workspace)
            self.assertIn('acceptEdits', workspace)
            self.assertNotIn('Bash', workspace[workspace.index('--tools') + 1])

    def test_native_modes_preserve_user_rules_tools_and_integrations(self):
        with patch.object(native.adapters, 'find_codex', return_value='/codex'), patch.object(native.adapters, 'find_claude', return_value='/claude'):
            for mode in ('plan', 'full_access'):
                codex = native.native_command('codex', permission=mode)
                self.assertFalse(set(codex) & {'--ignore-user-config', '--ignore-rules', '--disable'})
                self.assertNotIn('mcp_servers={}', codex)
                self.assertIn('forced_login_method="chatgpt"', codex)
                self.assertIn('model_provider="openai"', codex)
                self.assertEqual(codex[codex.index('-s') + 1],
                                 'read-only' if mode == 'plan' else 'danger-full-access')
                claude = native.native_command('claude', permission=mode)
                self.assertFalse(set(claude) & {'--safe-mode', '--restricted', '--tools',
                    '--strict-mcp-config', '--mcp-config', '--disable-slash-commands', '--no-chrome'})
                self.assertEqual(claude[claude.index('--permission-mode') + 1],
                                 'plan' if mode == 'plan' else 'bypassPermissions')
                self.assertEqual(json.loads(claude[claude.index('--settings') + 1])['apiKeyHelper'], '')
                # Catches: a background command or monitor waking the model in a
                # later turn, ending one call with several result events.
                self.assertEqual(json.loads(claude[claude.index('--settings') + 1])['env']['CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'], '1')
                self.assertIn('--disallowedTools=Monitor,CronCreate,CronDelete,CronList,ScheduleWakeup', claude)
            for mode in ('read_only', 'workspace_write'):
                codex = native.native_command('codex', permission=mode)
                self.assertIn('--ignore-rules', codex)
                self.assertIn('--ignore-user-config', codex)
                self.assertIn('mcp_servers={}', codex)
                claude = native.native_command('claude', permission=mode)
                self.assertIn('--restricted', claude)
                self.assertIn('--safe-mode', claude)
                self.assertNotIn('Bash', claude[claude.index('--tools') + 1])

    def test_model_effort_and_explicit_session(self):
        with patch.object(native.adapters, 'find_codex', return_value='/codex'):
            command = native.native_command('codex', SESSION, runtime_settings={'model':'test-model','effort':'high'})
            self.assertIn('resume', command)
            self.assertIn(SESSION, command)
            self.assertIn('test-model', command)
            self.assertIn('model_reasoning_effort="high"', command)
            for value in ('last', '', '--last'):
                with self.assertRaises(ValueError):
                    native.native_command('codex', value)




class ActivitySummaryTests(unittest.IsolatedAsyncioTestCase):
    def test_summary_uses_only_finite_labels_and_never_argument_content(self):
        for tool, expected in (("Read", "read_file"), ("Write", "write_file"),
                               ("Edit", "edit_file"), ("Grep", "search_files"),
                               ("WebFetch", "fetch_web"), ("Task", "delegate"),
                               ("image_generation", "generate_image"), ("mcp_tool_call", "use_tool")):
            with self.subTest(tool=tool):
                value = native._activity_summary(tool, {
                    "file_path": "/Users/PRIVATE_PERSON/secret-token.txt",
                    "url": "https://private.example/?key=PRIVATE_KEY",
                    "description": "All tests passed", "operation": "tests_passed",
                })
                self.assertEqual(value, {"operation": expected})

    def test_only_known_simple_leading_executables_are_labelled(self):
        cases = {
            "rg PRIVATE_KEY /Users/private": "rg",
            "git status --short": "git",
            "/private/hidden/.venv/bin/python3 script.py --token PRIVATE_KEY": "python3",
            "/bin/zsh -lc 'godot --headless --path /private/game'": "godot",
            "python -m unittest tests.private_suite": "python",
            "pytest -q": "pytest",
            "env TOKEN=PRIVATE_KEY python3 script.py": None,
            "rg secret . && git status": None,
            "python3 $(cat /private/token)": None,
            "python3 `cat /private/token`": None,
            "echo PRIVATE_KEY": None,
            "unknown /private/python3": None,
            "python3\ngit status": None,
            "'unterminated": None,
        }
        for command, executable in cases.items():
            with self.subTest(command=command):
                expected = {"operation": "run_command"}
                if executable:
                    expected["executable"] = executable
                self.assertEqual(native._activity_summary("Bash", {"command": command}), expected)
        self.assertEqual(native._activity_summary("Bash", {"command": ["python3"]}),
                         {"operation": "run_command"})
        self.assertEqual(native._activity_summary("Bash", {"command": "python3 " + "x" * 8192}),
                         {"operation": "run_command"})

    async def test_codex_summary_stays_bound_to_started_tool_without_output_claims(self):
        observed = []
        stream = native._Stream('codex', True, observed.append)
        events = [
            {'type': 'item.started', 'item': {'id': 'cmd', 'type': 'command_execution',
                'command': 'python3 /private/secret.py --key PRIVATE_KEY'}},
            {'type': 'item.completed', 'item': {'id': 'cmd', 'type': 'command_execution',
                'command': 'git status', 'aggregated_output': 'ALL TESTS PASSED PRIVATE_OUTPUT',
                'exit_code': 0, 'status': 'completed'}},
        ]
        for event in events:
            await stream.consume(json.dumps(event))
        self.assertIsNone(stream.failure)
        self.assertEqual([event['executable'] for event in observed], ['python3', 'python3'])
        self.assertEqual([event['operation'] for event in observed], ['run_command', 'run_command'])
        self.assertEqual(observed[-1]['exit_code'], 0)
        for private in ('PRIVATE_KEY', 'PRIVATE_OUTPUT', '/private', 'secret.py', 'PASSED'):
            self.assertNotIn(private, json.dumps(observed))

    async def test_claude_heartbeat_and_result_reuse_only_safe_started_summary(self):
        observed = []
        stream = native._Stream('claude', True, observed.append)
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'cmd',
                'name': 'Bash', 'input': {'command': '/bin/zsh -lc "godot --headless --path /private/game"'}}]}},
            {'type': 'tool_progress', 'tool_use_id': 'cmd-heartbeat-1', 'tool_name': 'Bash',
                'elapsed_time_seconds': 30, 'heartbeat': True, 'session_id': SESSION},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'cmd',
                'content': 'PRIVATE_OUTPUT All tests passed', 'is_error': False}]}},
        ]
        for event in events:
            await stream.consume(json.dumps(event))
        self.assertIsNone(stream.failure)
        self.assertEqual([event['state'] for event in observed], ['started', 'running', 'completed'])
        self.assertTrue(all(event['operation'] == 'run_command' and event['executable'] == 'godot'
                            for event in observed))
        self.assertNotIn('/private', json.dumps(observed))
        self.assertNotIn('passed', json.dumps(observed))
        self.assertNotIn('PRIVATE_OUTPUT', json.dumps(observed))


class PublicProgressTests(unittest.IsolatedAsyncioTestCase):
    async def consume(self, agent, events, *, expected_session=None):
        observed = []
        stream = native._Stream(agent, True, observed.append, expected_session=expected_session)
        for event in events:
            await stream.consume(json.dumps(event))
        return stream, observed

    def codex_message(self, identifier='speech', text='I will implement the parser; please check the fixture.'):
        return {'type': 'item.completed', 'item': {'id': identifier, 'type': 'agent_message',
                'phase': 'commentary', 'text': text}}

    def claude_message(self, text='I found the missing edge case; can you check the caller?', **fields):
        return {'type': 'assistant', 'uuid': str(uuid.uuid4()), 'session_id': SESSION,
                'parent_tool_use_id': None, 'message': {'role': 'assistant',
                'content': [{'type': 'text', 'text': text}]}, **fields}

    async def test_codex_complete_commentary_is_original_and_deduplicated_without_tool_counts(self):
        speech = self.codex_message()
        stream, observed = await self.consume('codex', [
            {'type': 'thread.started', 'thread_id': SESSION},
            {**speech, 'type': 'item.started'}, {**speech, 'type': 'item.updated'}, speech, speech,
            self.codex_message('final', json.dumps(FINAL)),
            {'type': 'item.completed', 'item': {'id': 'reason', 'type': 'reasoning', 'text': 'PRIVATE_REASONING'}},
        ], expected_session=SESSION)
        self.assertEqual(observed, [{'type': 'native_progress', 'item_id': 'speech',
            'session_id': SESSION, 'text': speech['item']['text']}])
        self.assertEqual((stream.calls, stream.results), (0, 0))
        self.assertIsNone(stream.failure)
        self.assertNotIn('PRIVATE_REASONING', json.dumps(observed))

    async def test_codex_exec_agent_message_without_phase_is_public_but_reasoning_is_not(self):
        speech = self.codex_message()
        del speech['item']['phase']
        final = self.codex_message('final', json.dumps(FINAL))
        del final['item']['phase']
        _, observed = await self.consume('codex', [
            {'type': 'thread.started', 'thread_id': SESSION}, speech,
            {'type': 'item.completed', 'item': {'id': 'reason', 'type': 'reasoning', 'text': 'PRIVATE_REASONING'}},
            final,
        ])
        self.assertEqual([event['text'] for event in observed], [speech['item']['text']])

    async def test_codex_ambiguous_wrong_session_parent_or_noncommentary_never_publishes(self):
        base = self.codex_message()
        candidates = [base, {**base, 'item': {**base['item'], 'phase': 'final_answer'}},
                      {**base, 'item': {**base['item'], 'phase': 'analysis'}},
                      {**base, 'parent_thread_id': 'private-subagent'},
                      {**base, 'thread_id': str(uuid.uuid4())}]
        for candidate in candidates:
            _, observed = await self.consume('codex', [candidate])
            self.assertEqual(observed, [])
        for candidate in candidates[1:]:
            _, observed = await self.consume('codex', [{'type': 'thread.started', 'thread_id': SESSION}, candidate])
            self.assertEqual(observed, [])
        _, observed = await self.consume('codex', [{'type': 'thread.started', 'thread_id': SESSION}, base],
                                         expected_session=str(uuid.uuid4()))
        self.assertEqual(observed, [])

    async def test_claude_only_main_public_text_uuid_blocks_publish_once(self):
        speech = self.claude_message()
        speech['message']['content'] += [{'type': 'thinking', 'thinking': 'PRIVATE_THINKING'},
                                       {'type': 'redacted_thinking', 'data': 'PRIVATE_REDACTED'}]
        stream, observed = await self.consume('claude', [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION}, speech, speech])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['text'], speech['message']['content'][0]['text'])
        self.assertEqual(observed[0]['item_id'], speech['uuid'] + ':0')
        self.assertIsNone(stream.failure)
        self.assertEqual((stream.calls, stream.results), (0, 0))
        self.assertNotIn('PRIVATE', json.dumps(observed))

    async def test_claude_unconfirmed_sidechain_replay_error_and_json_are_not_public(self):
        base = self.claude_message()
        candidates = [
            {key: value for key, value in base.items() if key != 'parent_tool_use_id'},
            {**base, 'parent_tool_use_id': 'private-task'},
            {**base, 'session_id': str(uuid.uuid4())},
            {**base, 'uuid': 'unconfirmed'}, {**base, 'isReplay': True},
            {**base, 'isSidechain': True}, {**base, 'isSynthetic': True},
            {**base, 'isApiErrorMessage': True},
            self.claude_message(json.dumps(FINAL)),
            self.claude_message('```json\n' + json.dumps(FINAL) + '\n```'),
        ]
        for candidate in candidates:
            with self.subTest(fields=tuple(candidate)):
                _, observed = await self.consume('claude', [
                    {'type': 'system', 'subtype': 'init', 'session_id': SESSION}, candidate])
                self.assertEqual(observed, [])

    async def test_display_cache_never_limits_messages_and_long_text_is_explicitly_truncated(self):
        messages = [self.codex_message(str(index)) for index in range(native._PUBLIC_PROGRESS_CACHE_SIZE + 1)]
        long_text = '公开发言' * native._MAX_PUBLIC_PROGRESS_BYTES
        stream, observed = await self.consume('codex', [{'type': 'thread.started', 'thread_id': SESSION},
            self.codex_message('too-long', long_text),
            self.codex_message('../private-path', 'private identifier'), *messages])
        self.assertEqual(len(observed), len(messages) + 1)
        self.assertEqual(len(stream.public_progress_ids), native._PUBLIC_PROGRESS_CACHE_SIZE)
        self.assertTrue(observed[0]['truncated'])
        self.assertEqual(observed[0]['original_characters'], len(long_text))
        self.assertTrue(long_text.startswith(observed[0]['text']))
        self.assertLessEqual(len(observed[0]['text'].encode('utf-8')), native._MAX_PUBLIC_PROGRESS_BYTES)
        self.assertNotIn('truncated', observed[-1])
        self.assertIsNone(stream.failure)
        self.assertEqual((stream.calls, stream.results), (0, 0))

    async def test_public_json_arrays_and_code_examples_are_preserved_as_data(self):
        examples = ['{"example": "keep this"}', '[1, 2, 3]',
                    '```json\n{"commands": ["an example"]}\n```',
                    '```python\nprint("hello")\n```', '{unfinished public snippet',
                    '{"message":"an example", "state":{}, "commands":[]}']
        stream, observed = await self.consume('codex', [{'type': 'thread.started', 'thread_id': SESSION},
            *[self.codex_message(str(index), text) for index, text in enumerate(examples)]])
        self.assertEqual([event['text'] for event in observed], examples)
        self.assertIsNone(stream.failure)
        envelope = {'message': 'done', 'state': 'done', 'commands': [], 'base_revision': 1,
                    'files': [], 'edits': [], 'unchanged_paths': [], 'resolved_conflicts': []}
        for candidate in (json.dumps(envelope), '```json\n' + json.dumps(envelope) + '\n```'):
            self.assertTrue(native._structured_final_text(candidate))

    async def test_public_commentary_does_not_replace_canonical_final_or_protocol_failure(self):
        events = codex_events()
        events.insert(2, self.codex_message())
        stream, observed = await self.consume('codex', events, expected_session=SESSION)
        result = stream.result(0, SESSION)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(result['text']), FINAL)
        self.assertEqual(len([event for event in observed if event['type'] == 'native_progress']), 1)
        self.assertEqual((stream.calls, stream.results), (1, 1))
        await stream.consume(json.dumps({'type': 'unknown_protocol'}))
        failed = stream.result(0, SESSION)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['failure_code'], 'native_protocol_unknown')
