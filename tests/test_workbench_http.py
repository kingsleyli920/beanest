"""Exercise project permissions and participant selection through real HTTP."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from uuid import uuid4, uuid5, NAMESPACE_URL

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_server import create_room_server


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.calls = []

        async def invoke(adapter, prompt, cwd, *, session_id=None, timeout=120):
            context = json.loads(prompt.split('CONTEXT DATA:\n')[1])
            self.calls.append((adapter, context['self']))
            result = {'message': 'A bounded review.', 'actions': [], 'state': 'done'}
            if 'project' in context:
                result.update(base_revision=context['proposal_base_revision'], files=[])
            return {'text': json.dumps(result), 'session_id': session_id or str(uuid5(NAMESPACE_URL, str(cwd))),
                    'status': 'completed', 'usage': None, 'events': []}

        registry = default_registry(invoke)
        manager = PartnerManager(self.root / 'partners', registry)
        self.service = RoomService(self.root / 'rooms.sqlite3', self.root / 'workspaces',
                                   partners=manager, registry=registry)
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())
        self.assertIsNone(self.server.runner.shutdown_error)
        self.directory.cleanup()

    def request(self, method, path, body=None, *, authenticated=True):
        headers = {'Content-Type': 'application/json', 'X-LAC-Instance': self.server.instance_id}
        if authenticated:
            headers['X-LAC-Token'] = self.server.mcp_token if path.startswith('/api/mcp/') else self.server.token
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def settled(self, room_id):
        for _ in range(100):
            status, snapshot = self.request('GET', '/api/rooms/' + room_id)
            self.assertEqual(status, 200)
            if all(job['status'] not in {'queued', 'working'} for job in snapshot['jobs']):
                return snapshot
            time.sleep(.01)
        self.fail('owned jobs did not settle')

    def test_four_profiles_select_two_and_mcp_cannot_expand_configuration(self):
        for identifier, adapter in [('claude_spare', 'claude'), ('codex_spare', 'codex')]:
            status, result = self.request('POST', '/api/partners',
                {'id': identifier, 'name': identifier, 'adapter': adapter, 'avatar': 'S'})
            self.assertEqual(status, 200)
            self.assertEqual(result['partners'][-1]['health']['state'], 'unknown')
        self.assertEqual(self.calls, [])
        status, result = self.request('POST', '/api/mcp/rooms', {
            'goal': 'Only the selected two should work.', 'target': 'all', 'request_id': str(uuid4()),
            'participants': ['claude', 'codex_spare']})
        self.assertEqual(status, 200)
        final = self.settled(result['room']['id'])
        self.assertEqual({member['id'] for member in final['members']}, {'claude', 'codex_spare'})
        self.assertEqual({agent for _, agent in self.calls}, {'claude', 'codex_spare'})
        self.assertEqual(len(self.calls), 2)
        for path in ['/api/mcp/partners', '/api/mcp/partners/claude/enabled', '/api/mcp/projects']:
            self.assertEqual(self.request('POST', path, {})[0], 404)
        self.assertEqual(self.request('GET', '/api/mcp/partners')[0], 200)

    def test_registered_project_snapshot_survives_source_removal_and_replay(self):
        source = self.root / 'example'
        source.mkdir()
        (source / 'score.js').write_text('export const score = wins => wins;\n')
        status, registered = self.request('POST', '/api/projects',
            {'name': 'Example', 'path': str(source), 'files': ['score.js']})
        self.assertEqual(status, 200)
        body = {'goal': 'Review this selected file.', 'target': 'codex', 'request_id': str(uuid4()),
                'mode': 'project_files', 'participants': ['claude', 'codex'],
                'project_id': registered['project']['id']}
        status, result = self.request('POST', '/api/rooms', body)
        self.assertEqual(status, 200)
        self.settled(result['room']['id'])
        self.assertEqual(result['project']['source']['id'], body['project_id'])
        self.assertEqual(result['project']['files'][0]['owner'], 'codex')
        self.assertEqual((source / 'score.js').read_text(), 'export const score = wins => wins;\n')
        (source / 'score.js').unlink()
        again, replay = self.request('POST', '/api/rooms', body)
        self.assertEqual(again, 200)
        self.assertEqual(replay['room']['id'], result['room']['id'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.request('POST', '/api/rooms', {**body, 'participants': ['codex']})[0], 409)

    def test_partner_identity_errors_and_auth_are_stable_and_do_not_leak_paths(self):
        profile = {'id': 'review', 'name': 'Reviewer', 'adapter': 'codex', 'avatar': 'R'}
        self.assertEqual(self.request('POST', '/api/partners', profile)[0], 200)
        code, result = self.request('POST', '/api/partners', {**profile, 'adapter': 'claude'})
        self.assertEqual((code, result['error']), (409, 'partner_identity_conflict'))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotEqual(self.request('GET', '/api/projects', authenticated=False)[0], 200)
        self.assertEqual(self.calls, [])

    def test_invalid_selection_and_nonregistered_project_never_dispatch(self):
        body = {'goal': 'Review', 'target': 'claude', 'request_id': str(uuid4())}
        for participants in [[], ['claude', 'claude'], ['outsider'], [['claude']]]:
            status, _ = self.request('POST', '/api/rooms', {**body, 'participants': participants})
            self.assertEqual(status, 400)
        self.assertEqual(self.request('POST', '/api/rooms', {**body, 'mode': 'project_files'})[0], 400)
        self.assertEqual(self.calls, [])
