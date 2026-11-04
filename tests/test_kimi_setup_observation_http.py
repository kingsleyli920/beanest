"""A fresh manager learns official Kimi readiness from its owned quota probe."""
import unittest

from test_kimi_quota_stability_http import StabilityFixture


class KimiSetupObservationHTTPTests(unittest.TestCase):
    def fixture(self):
        fixture = StabilityFixture(budget=None)
        self.addCleanup(fixture.close)
        return fixture

    def setup(self, fixture):
        code, value = fixture.request('GET', '/api/partners')
        self.assertEqual(code, 200)
        return next(item['setup'] for item in value['supported_adapters'] if item['id'] == 'kimi')

    def assert_readonly_probe(self, fixture):
        requests = [item['path'] for item in fixture.records() if item['kind'] == 'request']
        self.assertEqual(requests, ['/api/v1/auth', '/api/v1/oauth/userinfo?provider=managed%3Akimi-code',
                                    '/api/v1/oauth/usage?provider=managed%3Akimi-code',
                                    '/api/v1/oauth/userinfo?provider=managed%3Akimi-code', '/api/v1/auth'])
        self.assertEqual(fixture.active_reads, 0)
        self.assertEqual(fixture.loop_errors, [])
        self.assertEqual(fixture.calls, [])
        self.assertFalse((fixture.root / 'login-starts').exists())

    def test_fresh_unknown_setup_becomes_ready_after_actual_official_quota_probe(self):
        fixture = self.fixture()
        self.assertEqual(self.setup(fixture)['state'], 'unknown')
        self.assertEqual(fixture.records(), [])
        fixture.set_case(used=50)
        account = fixture.refresh()['account']
        self.assertEqual((account['state'], account['quota']['state']), ('ready', 'available'))
        self.assertEqual(account['quota']['windows'][0]['remaining_percent'], 50)
        self.assertEqual(self.setup(fixture)['state'], 'ready')
        self.assert_readonly_probe(fixture)
        count = len(fixture.records())
        self.assertEqual(self.setup(fixture)['state'], 'ready')
        self.assertEqual(len(fixture.records()), count)

    def test_invalid_or_missing_quota_cannot_promote_a_fresh_unknown_setup(self):
        fixture = self.fixture()
        self.assertEqual(self.setup(fixture)['state'], 'unknown')
        fixture.set_case(used=101)
        account = fixture.refresh()['account']
        self.assertEqual(account['quota']['state'], 'unavailable')
        self.assertEqual(account['quota']['windows'], [])
        self.assertNotEqual(account['state'], 'ready')
        self.assertEqual(self.setup(fixture)['state'], 'unknown')
        self.assert_readonly_probe(fixture)


if __name__ == '__main__':
    unittest.main()
