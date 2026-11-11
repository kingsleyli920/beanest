"""When an account counts as down to its reserve, from the quota it reported."""
import tempfile
import unittest
from pathlib import Path

from local_agent_coordinator.quota_gate import QuotaGate


def account(used, *, checked=1000.0, resets=5000.0, email="person@example.test", source="codex_app_server_account_rate_limits",
            name="codex:primary"):
    return {"state": "ready", "email": email, "auth_method": "chatgpt", "stale": True, "checked_at": checked,
            "quota": {"state": "available", "source": source, "checked_at": checked, "stale": True,
                      "windows": [{"name": name, "used_percent": used, "resets_at": resets}]}}


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.now = 1100.0
        self.gate = QuotaGate(Path(self.temporary.name), lambda: self.now)

    def tearDown(self):
        self.temporary.cleanup()

    def test_threshold(self):
        # Catches the boundary drifting: 15% left is reserve, 16% is not.
        self.assertEqual(self.gate.reserve_left("codex", account(85)), 15)
        self.assertEqual(self.gate.reserve_left("codex", account(97.6)), 2)
        self.assertIsNone(self.gate.reserve_left("codex", account(84)))

    def test_a_reading_older_than_half_an_hour_says_nothing(self):
        # Catches a low reading from before the reset keeping an account idle all day.
        self.now = 1000.0 + 1801
        self.assertIsNone(self.gate.reserve_left("codex", account(95)))
        self.now = 1000.0 + 1799
        self.assertEqual(self.gate.reserve_left("codex", account(95)), 5)

    def test_a_window_already_reset_says_nothing(self):
        # Catches a window that has reset still counting as low.
        self.assertIsNone(self.gate.reserve_left("codex", account(95, resets=1050.0)))

    def test_unknown_login_or_other_meter_says_nothing(self):
        # Catches guesses: no confirmed login, a meter from a different source, or a model-only window.
        self.assertIsNone(self.gate.reserve_left("codex", account(95, email="")))
        self.assertIsNone(self.gate.reserve_left("codex", account(95, source="something_else")))
        self.assertIsNone(self.gate.reserve_left("codex", account(95, name="codex:model")))
        self.assertIsNone(self.gate.reserve_left("codex", None))


if __name__ == "__main__":
    unittest.main()
