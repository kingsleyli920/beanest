"""Several local CLI installations: use the one that runs and confirms login."""
import asyncio
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator import adapters


def fake_cli(path, *, version, login, code=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n"
                    f"if [ \"$1\" = \"--version\" ]; then echo '{version}'; exit 0; fi\n"
                    f"printf '%s\\n' '{login}'\nexit {code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class CLISelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.saved = {agent: dict(value) for agent, value in adapters._SELECTIONS.items()}
        for value in adapters._SELECTIONS.values():
            value.update(path=None, skipped=[], checked=False)

    def tearDown(self):
        for agent, value in self.saved.items():
            adapters._SELECTIONS[agent].clear()
            adapters._SELECTIONS[agent].update(value)

    def select(self, agent, on_path, fallbacks):
        environment = {**os.environ, "PATH": f"{on_path.parent}:/usr/bin:/bin"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(adapters, "_install_locations", lambda name: tuple(fallbacks) if name == agent else ()):
            result = asyncio.run(adapters.select_cli(agent, self.root))
            return result, getattr(adapters, "find_" + agent)(), adapters.cli_installations(agent)

    def test_stale_codex_first_on_path_that_rejects_the_config_is_skipped(self):
        # Catches: an old nvm Codex first on PATH could not read a newer
        # ~/.codex/config.toml, so login looked unconfirmed and recovery stopped.
        stale = fake_cli(self.root / "nvm/bin/codex", version="codex-cli 0.144.4",
                         login="Error loading configuration: config.toml:187:1: invalid type: map, expected a boolean")
        bundled = fake_cli(self.root / "ChatGPT.app/codex", version="codex-cli 0.163.0", login="Logged in using ChatGPT")
        result, found, installed = self.select("codex", stale, [bundled])
        self.assertEqual(installed, [str(stale), str(bundled)])
        self.assertTrue(result["confirmed"])
        self.assertEqual((result["path"], found), (str(bundled), str(bundled)))
        self.assertEqual(result["skipped"], [{"path": str(stale), "reason": "config_unreadable", "version": "0.144.4"}])

    def test_outdated_claude_first_on_path_is_skipped_for_a_signed_in_copy(self):
        # Catches: an old Claude that rejects --restricted was used for every
        # check because it came first on PATH.
        old = fake_cli(self.root / "nvm/bin/claude", version="2.1.246 (Claude Code)",
                       login="error: unknown option '--restricted'")
        current = fake_cli(self.root / "brew/claude", version="2.1.317 (Claude Code)",
                           login='{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}')
        result, found, _ = self.select("claude", old, [current])
        self.assertEqual((result["confirmed"], found), (True, str(current)))
        self.assertEqual(result["skipped"], [{"path": str(old), "reason": "cli_outdated", "version": "2.1.246"}])

    def test_without_any_confirmed_copy_the_first_installation_stays_in_use_and_each_reason_is_kept(self):
        # Catches: failing closed to "CLI missing" when every copy only lacks login.
        first = fake_cli(self.root / "a/bin/codex", version="codex-cli 0.163.0", login="Not logged in")
        second = fake_cli(self.root / "b/codex", version="codex-cli 0.150.0", login="garbage", code=2)
        result, found, _ = self.select("codex", first, [second])
        self.assertFalse(result["confirmed"])
        self.assertIsNone(result["path"])
        self.assertEqual(found, str(first))
        self.assertEqual([(item["path"], item["reason"]) for item in result["skipped"]],
                         [(str(first), "login_unconfirmed"), (str(second), "login_unconfirmed")])

    def test_one_installation_needs_no_extra_probe_and_duplicates_by_symlink_count_once(self):
        # Catches: probing (spawning CLIs) on every telemetry read for the
        # common single-install case, and counting a symlinked copy twice.
        only = fake_cli(self.root / "kimi-code/bin/kimi", version="kimi 0.34.0", login="")
        alias = self.root / "bin/kimi"
        alias.parent.mkdir()
        alias.symlink_to(only)
        environment = {**os.environ, "PATH": f"{alias.parent}:/usr/bin:/bin"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(adapters, "_install_locations", lambda name: (only,) if name == "kimi" else ()), \
                patch.object(adapters, "select_cli", side_effect=AssertionError("no probe for one copy")):
            self.assertEqual(adapters.cli_installations("kimi"), [str(alias)])
            asyncio.run(adapters._ensure_cli("kimi", self.root))
            self.assertEqual(adapters.find_kimi(), str(alias))


class ProcessTableLocaleTests(unittest.TestCase):
    def test_process_table_is_parsed_in_the_c_locale_under_a_chinese_terminal(self):
        # Catches: LANG=zh_CN made ps print "三 10月/14 15:45:38 2026", so every
        # process started on day 10+ was dropped and cleanup confirmed nothing.
        from local_agent_coordinator import native_adapters
        with patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}):
            table = native_adapters._processes()
        self.assertIn(os.getpid(), table)
        self.assertGreater(len(table), 0.9 * len(subprocess_lines()))


def subprocess_lines():
    import subprocess
    return [line for line in subprocess.run(["ps", "-axo", "pid="], capture_output=True, text=True,
                                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout.splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
