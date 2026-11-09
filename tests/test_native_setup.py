"""Official installers and sign-in. Real child processes running fictional installer scripts."""
import asyncio
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_agent_coordinator import adapters, native_setup
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerManager
from local_agent_coordinator.quota_gate import PROBE_SECONDS


class Installer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.tool = self.root / "installed-tool"
        self.ready = []
        # Nothing has created this directory yet, as on a brand-new machine.
        self.runner = native_setup.NativeInstall(self.root / "not-created-yet", on_ready=self.ready.append)

    def descriptor(self, body):
        script = self.root / "install.sh"
        script.write_text(body)
        def resolve(_self):
            if not self.tool.exists():
                raise RuntimeError("cli_missing")
            return str(self.tool)
        return replace(native_setup.native_adapter("kimi"), install_script=script.as_uri(), install_shell="/bin/sh"), resolve

    async def finish(self, body=None, descriptor=None, resolve=None):
        if body is not None:
            descriptor, resolve = self.descriptor(body)
        with patch.object(native_setup, "native_adapter", lambda _id: descriptor), patch.object(native_setup.NativeAdapter, "resolve", resolve):
            started = self.runner.start("kimi")
            await asyncio.wait_for(self.runner.tasks["kimi"], 20)
        self.assertEqual(self.runner.snapshot("kimi")["id"], started["id"])
        return self.runner.snapshot("kimi")

    async def test_success_on_a_machine_with_no_working_directory_yet(self):
        # Catches the first install on a new machine failing because the partner directory does not exist.
        result = await self.finish(f"touch '{self.tool}'\n")
        self.assertEqual((result["state"], result["reason"]), ("completed", None))
        self.assertEqual(self.ready, ["kimi"])

    async def test_a_download_failure_is_named(self):
        # Catches network trouble being reported as a broken installer.
        descriptor, resolve = self.descriptor("exit 0\n")
        result = await self.finish(descriptor=replace(descriptor, install_script=(self.root / "absent.sh").as_uri()), resolve=resolve)
        self.assertEqual((result["state"], result["reason"]), ("failed", "download_failed"))
        self.assertEqual(self.ready, [])

    async def test_a_failing_installer_is_reported(self):
        # Catches a non-zero installer being treated as success.
        result = await self.finish(f"touch '{self.tool}'\nexit 3\n")
        self.assertEqual((result["state"], result["reason"]), ("failed", "install_failed"))
        self.assertEqual(self.ready, [])

    async def test_success_without_the_tool_is_not_success(self):
        # Catches an installer that exits cleanly but left nothing usable.
        result = await self.finish("exit 0\n")
        self.assertEqual((result["state"], result["reason"]), ("failed", "install_not_found"))

    async def test_loud_output_without_line_breaks_does_not_break_it(self):
        # Catches a progress bar overflowing a line-based reader.
        result = await self.finish(f"head -c 300000 /dev/zero | tr '\\0' x\ntouch '{self.tool}'\n")
        self.assertEqual(result["state"], "completed")

    async def test_timeout_stops_the_installer(self):
        # Catches a hung download leaving the card on installing forever.
        with patch.object(native_setup, "INSTALL_SECONDS", .4):
            result = await self.finish("sleep 30\n")
        self.assertEqual((result["state"], result["reason"]), ("failed", "install_timeout"))

    async def test_cancel_and_duplicate_start(self):
        # Catches two clicks starting two installers, or Cancel leaving one running.
        descriptor, resolve = self.descriptor("sleep 30\n")
        with patch.object(native_setup, "native_adapter", lambda _id: descriptor), patch.object(native_setup.NativeAdapter, "resolve", resolve):
            first = self.runner.start("kimi")
            self.assertEqual(self.runner.start("kimi")["id"], first["id"])
            await asyncio.sleep(.3)
            result = await self.runner.cancel("kimi", first["id"])
        self.assertEqual((result["state"], result["reason"]), ("cancelled", "install_cancelled"))

    def test_only_known_tools_can_be_installed(self):
        # Catches a request naming an arbitrary tool reaching the installer.
        with self.assertRaises(ValueError):
            self.runner.start("somethingelse")


class SignIn(unittest.TestCase):
    def test_only_the_tools_own_sign_in_host_is_offered(self):
        # Catches an arbitrary printed link being shown to the user as the sign-in page.
        claude = native_setup.native_adapter("claude")
        self.assertEqual(native_setup.sign_in_url(claude, "open https://claude.com/cai/oauth/authorize?code=true now."),
                         "https://claude.com/cai/oauth/authorize?code=true")
        for line in ("https://evil.example/claude.com", "http://claude.com/login", "no link here", "https://claude.com.evil.example/x"):
            self.assertIsNone(native_setup.sign_in_url(claude, line), line)

    def test_each_tool_uses_its_own_sign_in_command(self):
        # Catches `claude login`, which does not exist, being run.
        self.assertEqual(native_setup.native_adapter("claude").login_arguments, ("auth", "login", "--claudeai"))
        self.assertEqual(native_setup.native_adapter("codex").login_arguments, ("login",))

    def test_every_paid_claude_plan_counts_as_signed_in(self):
        # Catches colleagues on Team, Enterprise or Ultra being told they are not signed in.
        for plan in ("pro", "max", "ultra", "team", "enterprise"):
            text = '{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "%s"}' % plan
            self.assertTrue(adapters._authentication("claude", 0, text)["subscription_confirmed"], plan)
        free = '{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "free"}'
        self.assertFalse(adapters._authentication("claude", 0, free)["subscription_confirmed"])

    def test_the_standalone_codex_install_is_found(self):
        # Catches a successful official install still reading as not installed.
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", lambda: Path(home)), patch.object(adapters, "CODEX_APP", Path(home) / "absent"), \
                patch.object(adapters.shutil, "which", lambda _name: None):
            binary = Path(home) / ".local/bin/codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(adapters.cli_installations("codex"), [str(binary)])


class LimitProbe(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = 10_000
        self.calls = 0
        self.outcome = {"status": "failed", "failure_code": "subscription_limit_reached"}
        async def invoke(*_args, **_kwargs):
            self.calls += 1
            return self.outcome
        registry = default_registry(invoke)
        registry = type(registry)([replace(registry.get(key), native_account_status=True) for key in registry.ids()])
        self.manager = PartnerManager(Path(temp.name).resolve() / "partners", registry, clock=lambda: self.now)
        self.gate = self.manager.quota_gate

    def block(self, resets_at=None):
        self.gate.blocks["claude"] = {"identity": "x", "reason": "subscription_limit_reached", "source": "claude_cli_limit",
                                      "observed_at": self.now, "resets_at": resets_at}

    async def test_a_limit_with_no_end_is_retried_hourly_and_released_on_success(self):
        # Catches a limit with no reset time blocking the partner until someone clicks a button.
        self.block()
        await self.manager.probe_unknown_limits()
        self.assertEqual(self.calls, 1)
        self.assertIn("claude", self.gate.blocks, "still out of quota")
        await self.manager.probe_unknown_limits()
        self.assertEqual(self.calls, 1, "no second request inside the hour")
        self.now += PROBE_SECONDS
        self.outcome = {"status": "completed"}
        await self.manager.probe_unknown_limits()
        self.assertEqual(self.calls, 2)
        self.assertNotIn("claude", self.gate.blocks)

    async def test_a_known_reset_time_is_never_probed(self):
        # Catches paying for requests when the provider already said when it ends.
        self.block(resets_at=self.now + 500)
        await self.manager.probe_unknown_limits()
        self.assertEqual(self.calls, 0)

    async def test_a_crashed_probe_keeps_the_block(self):
        # Catches an unrelated failure being read as quota having returned.
        self.block()
        self.outcome = {"status": "failed", "failure_code": "cli_nonzero_exit"}
        await self.manager.probe_unknown_limits()
        self.assertIn("claude", self.gate.blocks)


class ToolAdoption(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        async def unused(*_args, **_kwargs):
            raise AssertionError("no model call")
        self.manager = PartnerManager(Path(temp.name).resolve() / "partners", default_registry(unused), clock=lambda: 42)

    def ids(self):
        return [item["id"] for item in self.manager.profiles()]

    def test_a_ready_tool_appears_once(self):
        # Catches a tool set up in the app never becoming a partner, or being added twice.
        self.assertNotIn("kimi", self.ids())
        self.manager.adopt_ready_tool()
        self.assertNotIn("kimi", self.ids(), "not ready yet")
        self.manager._setup_observations["kimi"] = "ready"
        self.manager.adopt_ready_tool()
        self.manager.adopt_ready_tool()
        self.assertEqual(self.ids().count("kimi"), 1)


if __name__ == "__main__":
    unittest.main()
