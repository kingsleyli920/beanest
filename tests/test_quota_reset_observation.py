"""A real refreshed quota observation releases only the applicable account gate."""
import copy
import unittest

import test_quota_gate as fixtures


class QuotaResetObservationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.QuotaGateTests.asyncSetUp
    asyncTearDown = fixtures.QuotaGateTests.asyncTearDown
    observe = fixtures.QuotaGateTests.observe
    claude_account = fixtures.QuotaGateTests.claude_account

    def reading(self, adapter, *, exhausted, reset):
        if adapter == 'codex':
            value = fixtures.account(used=0, resets=reset)
            second = copy.deepcopy(value['quota']['windows'][0])
            second['name'] = 'codex:secondary'
            value['quota']['windows'].append(second)
        elif adapter == 'claude':
            value = self.claude_account()
            value['quota']['windows'] = [row for row in value['quota']['windows']
                                          if row['scope'] == 'provider_account']
        else:
            value = fixtures.kimi_account(five_hour_used=0, month_used=0)
            second = copy.deepcopy(value['quota']['windows'][1])
            second['name'] = 'kimi:monthCode'
            value['quota']['windows'].append(second)
        for row in value['quota']['windows']:
            used = 100 if row['name'] in exhausted else 0
            row.update(used_percent=used, remaining_percent=100 - used, resets_at=reset)
        return value

    async def test_new_provider_window_with_room_releases_each_providers_persisted_block(self):
        self.manager.add(id='kimi', name='Kimi Code', adapter='kimi')
        limiting_pool = {'codex': 'codex:secondary', 'claude': 'claude:seven_day', 'kimi': 'kimi:monthTotal'}
        for adapter, pool in limiting_pool.items():
            with self.subTest(adapter=adapter):
                self.now = 1000
                before = await self.observe(self.reading(adapter, exhausted={pool}, reset=1100), adapter)
                self.assertEqual(before['availability']['state'], 'blocked')
                self.assertFalse(before['selectable'])
                self.now = 1101
                recovered = await self.observe(self.reading(adapter, exhausted=set(), reset=2100), adapter)
                self.assertEqual(recovered['availability']['state'], 'available')
                self.assertTrue(recovered['selectable'])
                self.assertGreater(recovered['availability']['observed_at'], before['availability']['observed_at'])
                self.assertNotIn(adapter, self.manager.quota_gate.blocks)

    async def test_a_reset_other_window_does_not_release_a_still_exhausted_shared_pool(self):
        self.manager.add(id='kimi', name='Kimi Code', adapter='kimi')
        limiting_pool = {'codex': 'codex:secondary', 'claude': 'claude:seven_day', 'kimi': 'kimi:monthTotal'}
        for adapter, pool in limiting_pool.items():
            with self.subTest(adapter=adapter):
                self.now = 1000
                await self.observe(self.reading(adapter, exhausted={pool}, reset=1100), adapter)
                self.now = 1101
                # A fresh report after the old reset still says this shared
                # pool is exhausted, even though every other meter is full.
                current = await self.observe(self.reading(adapter, exhausted={pool}, reset=2100), adapter)
                self.assertEqual(current['availability']['state'], 'blocked')
                self.assertFalse(current['selectable'])
                self.assertEqual(current['availability']['resets_at'], 2100)


if __name__ == '__main__':
    unittest.main()
