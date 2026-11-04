"""Actual loopback language assets, with no model, account, or desktop launch."""
from pathlib import Path
import threading
import unittest

from local_agent_coordinator.ui_server import create_server
import test_ui_server as fixture


class LanguageAssetHTTPTests(unittest.TestCase):
    request = fixture.LocalUIServerTests.request

    def setUp(self):
        self.bridge = fixture.FakeBridge()
        self.assets = Path(__file__).parents[1] / 'src/local_agent_coordinator/room_ui'
        self.server = create_server(self.bridge, port=0, asset_dir=self.assets)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def test_each_catalog_is_served_exactly_with_existing_security_and_without_api_access(self):
        for filename in ('i18n-en-a.js', 'i18n-en-b.js', 'i18n-en-c.js', 'i18n.js'):
            with self.subTest(filename=filename):
                status, headers, body = self.request('GET', '/' + filename, token=False)
                self.assertEqual(status, 200)
                self.assertEqual(body, (self.assets / filename).read_bytes())
                self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
        for path in ('/../i18n.js', '/i18n.js/anything', '/i18n.js?locale=arbitrary'):
            self.assertEqual(self.request('GET', path, token=False)[0], 404)
        self.assertEqual(self.bridge.calls, [])
