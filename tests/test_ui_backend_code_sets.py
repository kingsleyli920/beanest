"""Catches the room page's copies of backend code sets drifting from the backend."""
from pathlib import Path
import re
import unittest

from local_agent_coordinator.native_outcome import FORMAT_ERRORS
from local_agent_coordinator.stop_policy import KEEP_SCENE, USER_STOPS, continues_automatically

APP = Path(__file__).resolve().parent.parent / "src/local_agent_coordinator/room_ui/app.js"


def js_set(name, source=None):
    """Read `const name = new Set([...])`; return its string literals and spread names."""
    source = APP.read_text(encoding="utf-8") if source is None else source
    found = re.findall(r"const " + re.escape(name) + r" = new Set\(\[(.*?)\]\);", source, re.S)
    if len(found) != 1:
        raise AssertionError(f"expected exactly one Set named {name}, found {len(found)}")
    literals, spreads = set(), set()
    for item in (part.strip() for part in found[0].split(",")):
        if literal := re.fullmatch(r"'([a-z][a-z0-9_]*)'", item):
            literals.add(literal[1])
        elif spread := re.fullmatch(r"\.\.\.(\w+)", item):
            spreads.add(spread[1])
        else:
            raise AssertionError(f"{name} holds an entry this check cannot read: {item!r}")
    return literals, spreads


class CodeSetMirrorTests(unittest.TestCase):
    def test_reply_corrected_codes_are_exactly_the_backend_format_errors(self):
        # Catches a code added to or dropped from only one side.
        literals, spreads = js_set("replyCorrectedCodes")
        self.assertEqual(spreads, set())
        self.assertEqual(literals, set(FORMAT_ERRORS))

    def test_self_corrected_codes_add_only_the_listed_page_exception(self):
        # Catches a second page-only code slipping in beside the mirrored set.
        literals, spreads = js_set("selfCorrectedCodes")
        self.assertEqual(spreads, {"replyCorrectedCodes"})
        self.assertEqual(literals, {"team_scope_conflict"})

    def test_page_exception_stays_a_state_conflict_the_host_continues(self):
        # Catches the backend reclassifying the code, which would retire the exception.
        self.assertNotIn("team_scope_conflict", FORMAT_ERRORS)
        self.assertTrue(continues_automatically("team_scope_conflict"))

    def test_stop_code_lists_mirror_the_stop_policy(self):
        # Catches the page disagreeing with the host about which stops wait for the user.
        self.assertEqual(js_set("userStopCodes"), (set(USER_STOPS), set()))
        self.assertEqual(js_set("keepSceneCodes"), (set(KEEP_SCENE), set()))

    def test_reader_refuses_an_entry_it_cannot_understand(self):
        # Catches the check going blind if a Set is rewritten in a form it cannot parse.
        with self.assertRaisesRegex(AssertionError, "cannot read"):
            js_set("sample", "const sample = new Set(['a_code', otherCodes]);")
        with self.assertRaisesRegex(AssertionError, "exactly one"):
            js_set("missing", "const other = new Set(['a_code']);")


if __name__ == "__main__":
    unittest.main()
