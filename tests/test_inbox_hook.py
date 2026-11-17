"""The Claude Code hook that hands a running call what has reached its inbox.

Run as the real script in a real subprocess, the way the tool starts it.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_agent_coordinator import inbox_hook, job_progress

SCRIPT = Path(inbox_hook.__file__)


class InboxHook(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.progress = job_progress.create_progress_file(Path(self.temp.name) / "p")

    def run_hook(self, environment=None):
        env = {"PATH": os.environ.get("PATH", "")}
        env.update({"BEANEST_PROGRESS_FILE": str(self.progress)} if environment is None else environment)
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)], input='{"tool_name":"Bash"}', capture_output=True,
                              text=True, env=env, timeout=20)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return done.stdout

    def context(self, output):
        value = json.loads(output)
        self.assertEqual(sorted(value), ["hookSpecificOutput"])
        self.assertEqual(value["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        return value["hookSpecificOutput"]["additionalContext"]

    def test_each_entry_is_handed_over_once_in_the_form_the_tool_reads(self):
        # Catches the hook saying nothing, repeating old entries after every tool call, or printing
        # plain text (which this tool does not pass on to the model).
        self.assertEqual(self.run_hook(), "", "no inbox yet: nothing to say")
        job_progress.append_inbox(self.progress, "Message from the user, sent while this call was running", "Stop moving the mouse.")
        job_progress.append_inbox(self.progress, "Request from codex (task B3) about a.py, which your task V8 holds", "Leave me three minutes.")
        first = self.context(self.run_hook())
        self.assertTrue(first.startswith(inbox_hook.HEADING))
        self.assertEqual(first[len(inbox_hook.HEADING):],
                         "## Message from the user, sent while this call was running\nStop moving the mouse.\n\n"
                         "## Request from codex (task B3) about a.py, which your task V8 holds\nLeave me three minutes.")
        self.assertEqual(self.run_hook(), "", "nothing new since the last tool call")
        job_progress.append_inbox(self.progress, "Message from the user, sent while this call was running", "十二图就是十二途。")
        self.assertEqual(self.context(self.run_hook())[len(inbox_hook.HEADING):],
                         "## Message from the user, sent while this call was running\n十二图就是十二途。")
        self.assertEqual(self.run_hook(), "")

    def test_a_long_entry_is_capped_and_still_counted_as_read(self):
        # Catches an oversized entry being pushed to a side file by the tool, or handed over again and again.
        job_progress.append_inbox(self.progress, "From the user", "y" * 3900)
        job_progress.append_inbox(self.progress, "From the user", "z" * 3900)
        job_progress.append_inbox(self.progress, "From the user", "w" * 3900)
        text = self.context(self.run_hook())
        self.assertEqual(len(text), len(inbox_hook.HEADING) + inbox_hook.MAX_CHARACTERS)
        self.assertLess(len(text), 10000)
        self.assertEqual(self.run_hook(), "")

    def test_it_stays_silent_and_harmless_when_anything_is_off(self):
        # Catches a tool call failed or delayed by the hook: no variable, a missing file, a planted link,
        # a broken offset file. Each must end quietly with nothing printed.
        self.assertEqual(self.run_hook({}), "")
        self.assertEqual(self.run_hook({"BEANEST_PROGRESS_FILE": str(Path(self.temp.name) / "gone.progress")}), "")
        target = Path(self.temp.name) / "elsewhere"
        target.write_text("## private\nnot the inbox\n")
        os.symlink(target, job_progress.inbox_path(self.progress))
        self.assertEqual(self.run_hook(), "", "a link in the inbox's place is not followed")
        os.unlink(job_progress.inbox_path(self.progress))
        job_progress.append_inbox(self.progress, "From the user", "hello")
        Path(str(job_progress.inbox_path(self.progress)) + ".seen").write_text("not a number")
        self.assertTrue(self.context(self.run_hook()).endswith("## From the user\nhello"))

    def test_the_offset_file_goes_with_the_call(self):
        # Catches the hook's own bookkeeping being left behind in the runtime folder after the call.
        job_progress.append_inbox(self.progress, "From the user", "hello")
        self.run_hook()
        seen = Path(str(job_progress.inbox_path(self.progress)) + ".seen")
        self.assertEqual((seen.exists(), seen.stat().st_mode & 0o777), (True, 0o600))
        job_progress.remove_progress_file(self.progress)
        self.assertEqual([seen.exists(), job_progress.inbox_path(self.progress).exists(), Path(self.progress).exists()], [False, False, False])
