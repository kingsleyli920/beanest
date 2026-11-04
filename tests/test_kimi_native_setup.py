"""Fictional configuration and owned fake login processes; no real Kimi CLI."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters, kimi_acp, native_adapters
from local_agent_coordinator.native_setup import NativeLogin, device_url


SECRET = "FICTIONAL_SECRET_NEVER_RETURN_THIS"
MODEL = "kimi-code/fictional-0"
DEVICE = "https://www.kimi.com/code/authorize_device?user_code=ABCD-1234"


def configuration(*, default=MODEL, model_extra="", provider_extra=""):
    """The six models are synthetic metadata, not subscription tier evidence."""
    sections = [
        f'default_model = {json.dumps(default)}',
        '[providers."managed:kimi-code"]',
        'type = "kimi"',
        'base_url = "https://api.kimi.com/coding/v1"',
        'oauth = {storage = "file", key = "oauth/kimi-code"}',
        provider_extra,
    ]
    for index in range(6):
        sections.extend([
            f'[models."kimi-code/fictional-{index}"]',
            'provider = "managed:kimi-code"',
            f'model = "fictional-{index}"',
            f'display_name = "Fictional model {index}"',
            'support_efforts = ["low", "high", "max", "high"]',
            'default_effort = "high"',
            model_extra if index == 0 else "",
        ])
    return "\n".join(sections) + "\n"


class ManagedKimiConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.toml"

    def tearDown(self):
        self.temporary.cleanup()

    def read(self, **changes):
        self.path.write_text(configuration(**changes), encoding="utf-8")
        return kimi_acp.managed_configuration(self.path)

    def test_managed_default_projects_six_models_without_claiming_a_tier(self):
        result = self.read()
        self.assertEqual(result["state"], "ready")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["default_model"], MODEL)
        self.assertEqual(len(result["models"]), 6)
        self.assertEqual(result["models"][0], {
            "id": MODEL, "name": "Fictional model 0",
            "efforts": ["low", "high", "max"], "default_effort": "high",
            "is_default": True,
        })
        self.assertEqual(sum(item["is_default"] for item in result["models"]), 1)
        self.assertEqual(set(result), {"state", "reason", "models", "default_model"})
        self.assertNotIn("subscription", json.dumps(result))

    def test_old_api_default_is_not_a_managed_route(self):
        text = configuration(default="moonshot-cn/fictional") + textwrap.dedent(f'''
            [providers."moonshot-cn"]
            type = "kimi"
            base_url = "https://api.moonshot.cn/v1"
            api_key = "{SECRET}"
            [models."moonshot-cn/fictional"]
            provider = "moonshot-cn"
            model = "fictional"
        ''')
        self.path.write_text(text, encoding="utf-8")
        result = kimi_acp.managed_configuration(self.path)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "auth_unconfirmed")
        self.assertIsNone(result["default_model"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_model_credentials_and_endpoint_overrides_exclude_the_selected_alias(self):
        overrides = {
            "api_key": f'api_key = "{SECRET}"',
            "base_url": f'base_url = "https://{SECRET.lower()}.invalid/v1"',
            "oauth": f'oauth = {{storage = "file", key = "{SECRET}"}}',
        }
        for name, extra in overrides.items():
            with self.subTest(override=name):
                result = self.read(model_extra=extra)
                self.assertEqual(result["state"], "unavailable")
                self.assertIsNone(result["default_model"])
                self.assertNotIn(MODEL, [model["id"] for model in result["models"]])
                self.assertNotIn(SECRET.lower(), json.dumps(result).lower())

    def test_provider_api_or_environment_conflicts_are_not_ready_or_exposed(self):
        conflicts = {
            "api_key": f'api_key = "{SECRET}"',
            "api_key_env": f'api_key_env = "{SECRET}"',
            "env": f'env = {{KIMI_API_KEY = "{SECRET}"}}',
        }
        for name, extra in conflicts.items():
            with self.subTest(conflict=name):
                result = self.read(provider_extra=extra)
                self.assertEqual(result["state"], "unavailable")
                self.assertEqual(result["models"], [])
                self.assertNotIn(SECRET, json.dumps(result))


class KimiDeviceURLTests(unittest.TestCase):
    def test_only_exact_official_device_urls_and_single_nonsecret_user_codes(self):
        self.assertEqual(device_url("Opening browser: " + DEVICE), (DEVICE, "ABCD-1234"))
        without_www = DEVICE.replace("www.kimi.com", "kimi.com")
        self.assertEqual(device_url(without_www), (without_www, "ABCD-1234"))
        plain = "https://kimi.com/code/authorize_device"
        self.assertEqual(device_url(plain), (plain, None))
        invalid = [
            "http://kimi.com/code/authorize_device?user_code=ABCD",
            "https://kimi.com.evil.invalid/code/authorize_device?user_code=ABCD",
            "https://evil.invalid@kimi.com/code/authorize_device?user_code=ABCD",
            "https://kimi.com:443/code/authorize_device?user_code=ABCD",
            "https://kimi.com/code/authorize_device/extra?user_code=ABCD",
            "https://kimi.com/code/authorize_device?user_code=ABCD&token=" + SECRET,
            "https://kimi.com/code/authorize_device?user_code=ABCD&user_code=EFGH",
            "https://kimi.com/code/authorize_device?user_code=",
            "https://kimi.com/code/authorize_device?user_code=ABCD#fragment",
            "https://kimi.com/code/authorize_device?user_code=bad%2Fcode",
        ]
        for value in invalid:
            with self.subTest(url=value):
                self.assertIsNone(device_url(value))


class NativeKimiLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.config = self.home / ".kimi-code" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(configuration(), encoding="utf-8")
        self.executable = self.root / "fake-kimi"
        self.executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''
            import os
            from pathlib import Path
            import subprocess
            import sys
            import time

            root = Path(__file__).parent
            assert sys.argv[1:] == ["login"]
            with (root / "starts").open("a") as stream:
                stream.write(str(os.getpid()) + "\\n")
            if (root / "spawn-child").exists():
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"],
                                         start_new_session=True)
                (root / "child-pid").write_text(str(child.pid))
            print("RAW_FICTIONAL_SECRET_DO_NOT_EXPOSE", file=sys.stderr, flush=True)
            print("Opening browser for Kimi device login: https://www.kimi.com/code/authorize_device?user_code=ABCD-1234",
                  file=sys.stderr, flush=True)
            deadline = time.monotonic() + 10
            while not (root / "release").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            sys.exit(0 if (root / "release").exists() else 1)
        '''), encoding="utf-8")
        self.executable.chmod(0o700)
        self.patchers = [
            patch.object(adapters, "find_kimi", return_value=str(self.executable)),
            patch.object(adapters, "_environment", return_value={
                "HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            }),
            patch.object(kimi_acp.Path, "home", return_value=self.home),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.ready = []
        self.owned_births = {}
        self.login = NativeLogin(self.root, on_ready=self.ready.append)

    async def asyncTearDown(self):
        try:
            await self.login.close()
            # Guard only the fake processes whose PIDs this fixture wrote.
            table = await asyncio.to_thread(native_adapters._processes)
            for pid, birth in self.owned_births.items():
                if pid in table and table[pid][2] == birth and "Z" not in table[pid][3]:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        finally:
            for patcher in reversed(self.patchers):
                patcher.stop()
            self.temporary.cleanup()

    async def wait_state(self, state):
        async def wait():
            while True:
                current = self.login.snapshot("kimi")
                if current is not None and current["state"] == state:
                    if state == "waiting":
                        table = await asyncio.to_thread(native_adapters._processes)
                        for marker in (self.root / "starts", self.root / "child-pid"):
                            if marker.exists():
                                for value in marker.read_text().splitlines():
                                    pid = int(value)
                                    if pid in table:
                                        self.owned_births[pid] = table[pid][2]
                    return current
                if current is not None and current["state"] in {"completed", "failed", "cancelled"}:
                    self.fail(f"expected {state}, got {current}")
                await asyncio.sleep(.02)
        return await asyncio.wait_for(wait(), timeout=5)

    async def test_waiting_duplicate_start_exit_zero_and_stale_operation_cancel(self):
        first = self.login.start("kimi")
        waiting = await self.wait_state("waiting")
        self.assertEqual(waiting["verification_uri"], DEVICE)
        self.assertEqual(waiting["user_code"], "ABCD-1234")
        self.assertEqual(self.login.start("kimi")["id"], first["id"])
        self.assertEqual(len((self.root / "starts").read_text().splitlines()), 1)
        self.assertNotIn("RAW_FICTIONAL_SECRET", json.dumps(waiting))
        (self.root / "release").touch()
        completed = await self.wait_state("completed")
        self.assertIsNone(completed["reason"])
        self.assertIsNone(completed["verification_uri"])
        self.assertIsNone(completed["user_code"])
        self.assertEqual(self.ready, ["kimi"])

        (self.root / "release").unlink()
        second = self.login.start("kimi")
        await self.wait_state("waiting")
        self.assertNotEqual(second["id"], first["id"])
        with self.assertRaisesRegex(ValueError, "^login_operation_changed$"):
            await self.login.cancel("kimi", first["id"])
        self.assertEqual(self.login.snapshot("kimi")["id"], second["id"])
        self.assertEqual(self.login.snapshot("kimi")["state"], "waiting")
        cancelled = await self.login.cancel("kimi", second["id"])
        self.assertEqual(cancelled["state"], "cancelled")

    async def test_exit_zero_does_not_confirm_an_api_default(self):
        self.config.write_text(configuration(default="moonshot-cn/fictional"), encoding="utf-8")
        self.login.start("kimi")
        await self.wait_state("waiting")
        (self.root / "release").touch()
        failed = await self.wait_state("failed")
        self.assertEqual(failed["reason"], "login_route_unconfirmed")
        self.assertEqual(self.ready, [])
        self.assertNotIn("RAW_FICTIONAL_SECRET", json.dumps(failed))

    async def test_cancel_terminates_the_owned_login_and_its_detached_child(self):
        (self.root / "spawn-child").touch()
        operation = self.login.start("kimi")
        await self.wait_state("waiting")
        parent_pid = int((self.root / "starts").read_text().strip())
        child_pid = int((self.root / "child-pid").read_text())
        before = await asyncio.to_thread(native_adapters._processes)
        self.assertIn(parent_pid, before)
        self.assertIn(child_pid, before)
        self.assertNotEqual(before[child_pid][1], parent_pid)
        cancelled = await self.login.cancel("kimi", operation["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["reason"], "login_cancelled")
        self.assertIsNone(cancelled["verification_uri"])
        after = await asyncio.to_thread(native_adapters._processes)
        for pid in (parent_pid, child_pid):
            self.assertTrue(pid not in after or "Z" in after[pid][3])
        self.assertEqual(self.ready, [])


if __name__ == "__main__":
    unittest.main()
