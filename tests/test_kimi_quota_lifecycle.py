"""Owned quota reads finish before server cleanup; one deadline covers all stages."""
import asyncio
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters, kimi_usage, native_adapters
from tests import test_kimi_usage as fixture


class KimiQuotaLifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.KimiQuotaProcessTests.setUp
    assert_gone = fixture.KimiQuotaProcessTests.assert_gone

    def program(self, *, startup=0, usage=0):
        path = Path(fixture.KimiQuotaProcessTests.program(self, "hang"))
        code = path.read_text().replace('time.sleep(30)', f'time.sleep({usage!r})')
        code = code.replace("print('http://127.0.0.1:'", f"time.sleep({startup!r})\nprint('http://127.0.0.1:'")
        path.write_text(code)
        return str(path)

    async def test_cold_start_and_near_native_eight_second_fetch_fit_kimi_budget(self):
        # The installed CLI may spend up to 8s in fetch *after* cold startup
        # and OAuth refresh. The former shared 10s deadline cuts this short.
        path = self.program(startup=2.4, usage=7.8)
        with patch.object(adapters, "find_kimi", return_value=path):
            result = await adapters.account_status("kimi", self.root, timeout=kimi_usage.DEFAULT_TIMEOUT)
        self.assertEqual(result["quota"]["state"], "available")
        self.assertEqual(result["quota"]["diagnostics"]["phase"], "complete")
        self.assertGreater(result["quota"]["diagnostics"]["elapsed_seconds"], 10)
        self.assert_gone()

    async def test_timeout_joins_read_before_owned_server_cleanup_and_identifies_phase(self):
        active = False
        seen = []
        original = kimi_usage._get_json
        settle = native_adapters._settle_cleanup
        def get(*args, **kwargs):
            nonlocal active
            active = True
            try:return original(*args, **kwargs)
            finally:active = False
        async def cleanup(*args, **kwargs):
            seen.append(active)
            return await settle(*args, **kwargs)
        with patch.object(adapters, "find_kimi", return_value=self.program(usage=3)), patch.object(kimi_usage, "_get_json", get), patch.object(native_adapters, "_settle_cleanup", cleanup):
            started = time.monotonic()
            result = await kimi_usage.account_status(self.root, timeout=.3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(seen, [False])
        self.assertFalse(active)
        self.assertEqual(result["quota"]["reason"], "account_check_timeout")
        self.assertEqual(result["quota"]["diagnostics"]["phase"], "usage")
        self.assert_gone()

    async def test_cancel_and_repeated_cancel_abort_read_before_cleanup(self):
        with patch.object(adapters, "find_kimi", return_value=self.program(usage=3)):
            task = asyncio.create_task(kimi_usage.account_status(self.root))
            for _ in range(200):
                if (self.root / "usage-started").exists():break
                await asyncio.sleep(.01)
            self.assertTrue((self.root / "usage-started").exists())
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assert_gone()

    async def test_cancel_while_launch_is_returning_still_owns_and_cleans_process(self):
        original = asyncio.create_subprocess_exec
        launched = asyncio.Event()
        owned = []
        async def create(*args, **kwargs):
            process = await original(*args, **kwargs)
            owned.append(process)
            launched.set()
            await asyncio.sleep(.15)
            return process
        with patch.object(adapters, "find_kimi", return_value=self.program(usage=0)), patch.object(asyncio, "create_subprocess_exec", create):
            task = asyncio.create_task(kimi_usage.account_status(self.root))
            await launched.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(all(process.returncode is not None for process in owned))
        for process in owned:
            with self.assertRaises(ProcessLookupError):os.kill(process.pid, 0)

    async def test_each_read_receives_only_remaining_budget_and_other_adapters_keep_limit(self):
        timeouts = []
        original = kimi_usage._get_json
        def get(port, token, path, timeout, **kwargs):
            timeouts.append(timeout)
            return original(port, token, path, timeout, **kwargs)
        with patch.object(adapters, "find_kimi", return_value=self.program(startup=.15, usage=.1)), patch.object(kimi_usage, "_get_json", get):
            result = await kimi_usage.account_status(self.root, timeout=1)
        self.assertEqual(result["quota"]["state"], "available")
        self.assertEqual(len(timeouts), 5)
        self.assertLess(timeouts[0], .9)
        self.assertGreater(timeouts[0], timeouts[-1])
        for adapter in ("claude", "codex"):
            with self.assertRaises(adapters.AccountTelemetryError):
                await adapters.account_status(adapter, self.root, timeout=30)
        self.assert_gone()
