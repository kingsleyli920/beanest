"""Exact owned native administration, using the fake ACP executable only."""
import json
from pathlib import Path
import sys
import unittest

from local_agent_coordinator.kimi_acp import delete_owned_session
from test_kimi_acp import KimiACPTests, FAKE_ACP, SID


class KimiSessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = KimiACPTests.asyncSetUp
    asyncTearDown = KimiACPTests.asyncTearDown
    scenario = KimiACPTests.scenario
    wire_path = KimiACPTests.wire_path
    trace = KimiACPTests.trace

    def prepare(self, case="completed"):
        self.scenario(case)
        branch = '''    elif method == "session/delete":
        if case != "keep_session":
            import shutil
            shutil.rmtree(wire_path(str(Path.cwd())).parents[2])
        response(row["id"], {})
'''
        peer = FAKE_ACP.replace('    elif method == "session/close":', branch + '    elif method == "session/close":')
        self.executable.write_text(f"#!{sys.executable}\n" + peer)
        wire = self.wire_path()
        wire.parent.mkdir(parents=True)
        wire.write_text(json.dumps({"type": "turn.ended", "turnId": 0, "reason": "completed"}) + "\n")
        return {"adapter": "kimi", "session_id": SID, "shared_room_ids": [], "owned_cwds": [str(self.cwd)]}

    async def test_official_exact_delete_then_missing_without_prompt(self):
        target = self.prepare()
        self.assertEqual(await delete_owned_session(target), "provider_managed_deleted")
        self.assertFalse(self.wire_path().parents[2].exists())
        methods = [row["method"] for row in self.trace()]
        self.assertEqual(methods, ["initialize", "authenticate", "session/delete"])
        self.assertEqual(await delete_owned_session(target), "already_missing")

    async def test_shared_unknown_or_not_actually_deleted_remains_pending(self):
        target = self.prepare("keep_session")
        with self.subTest("shared"):
            self.assertEqual(await delete_owned_session({**target, "shared_room_ids": ["another-room"]}), "pending")
            self.assertFalse((self.root / "trace.jsonl").exists())
        with self.subTest("unsafe identity"):
            self.assertEqual(await delete_owned_session({**target, "session_id": "../other"}), "pending")
        with self.subTest("native reply without deletion"):
            self.assertEqual(await delete_owned_session(target), "pending")
            self.assertTrue(self.wire_path().is_file())
