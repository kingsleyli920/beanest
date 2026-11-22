"""A command a partner is waiting on forever is ended; the partner and busy commands are not.

Real processes stand in for a CLI and the commands it runs: a `cat` reading a
named pipe nobody writes to (the 九烛 room's Claude Code waited 27 hours on
/tmp/steam.pipe this way, and never read the user's messages), a busy loop,
and a child the CLI starts at launch the way it starts its MCP servers.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from local_agent_coordinator import native_adapters

# The stand-in CLI: an MCP-like child at once, then after a pause the commands
# named on its command line, waiting for each and printing how each ended.
CLI = r'''
import subprocess, sys, time
server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "fake-mcp-server"])
time.sleep(float(sys.argv[1]))
children = [subprocess.Popen(["/bin/sh", "-c", command]) for command in sys.argv[2:]]
for child in children:
    child.wait()
    print("ended", child.returncode, flush=True)
time.sleep(600)
'''


class StuckToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.pipe = self.directory / "steam.pipe"
        os.mkfifo(self.pipe)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            process.wait()

    def launch(self, *commands, delay=1.5, expected=None):
        process = subprocess.Popen([sys.executable, "-c", CLI, str(delay), *commands], stdout=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            trees = native_adapters._tool_trees(process.pid, native_adapters._process_details())
            if len(trees) == (len(commands) if expected is None else expected):
                return process
            time.sleep(0.2)
        self.fail("the stand-in CLI did not start its commands")

    def alive(self, pid):
        return pid in native_adapters._process_details()

    def test_a_command_waiting_on_a_named_pipe_is_ended_and_the_cli_and_busy_work_are_not(self):
        with mock.patch.object(native_adapters, "_TOOL_START_GRACE_SECONDS", 1), \
                mock.patch.object(native_adapters, "STUCK_TOOL_IDLE_SECONDS", 3), \
                mock.patch.object(native_adapters, "STUCK_TOOL_CPU_SECONDS", 0.5):
            cli = self.launch(f"cat {self.pipe}", "python3 -c 'while True: pass'")
            table = native_adapters._process_details()
            trees = native_adapters._tool_trees(cli.pid, table)
            self.assertEqual(len(trees), 2, "the MCP-like child started at launch is not a command")
            self.assertFalse(any("fake-mcp-server" in table[root][3] for root, _ in trees))
            waiting = next(members for members in trees.values() if any("cat" in table[pid][3] for pid in members))
            busy = next(members for members in trees.values() if members is not waiting)
            watch = native_adapters._StuckTools(cli.pid)
            ended, deadline = [], time.monotonic() + 30
            while not ended and time.monotonic() < deadline:
                ended = watch.check()
                time.sleep(0.5)
            self.assertEqual(len(ended), 1, "only the command that stopped using CPU is ended")
            self.assertFalse(any(self.alive(pid) for pid in waiting))
            self.assertTrue(any(self.alive(pid) for pid in busy), "a busy command keeps running")
            self.assertTrue(self.alive(cli.pid), "the partner's own process is never touched")
            self.assertEqual(cli.stdout.readline().strip().split()[0], "ended", "the CLI sees its command end and carries on")

    def test_the_user_can_end_the_running_commands_and_the_cli_carries_on(self):
        with mock.patch.object(native_adapters, "_TOOL_START_GRACE_SECONDS", 1):
            # A server the CLI starts later (an MCP server it connects on demand) is not a command either.
            cli = self.launch(f"cat {self.pipe}", "exec python3 -c 'import time; time.sleep(600)' late-mcp-server", expected=1)
            time.sleep(0.5)
            self.assertEqual(native_adapters.interrupt_tools(cli.pid), 1)
            self.assertEqual(cli.stdout.readline().strip(), "ended -15")
            self.assertTrue(self.alive(cli.pid))
            running = native_adapters._process_details().values()
            self.assertTrue(any("fake-mcp-server" in row[3] for row in running), "the CLI's own servers keep running")
            self.assertTrue(any("late-mcp-server" in row[3] for row in running), "a server started later keeps running")
            self.assertEqual(native_adapters.interrupt_tools(cli.pid), 0, "nothing left to end")

    def test_cpu_time_formats(self):
        self.assertEqual(native_adapters._cpu_seconds("0:01.50"), 1.5)
        self.assertEqual(native_adapters._cpu_seconds("1:02:03.00"), 3723.0)
        self.assertEqual(native_adapters._cpu_seconds("2-01:00:00"), 2 * 86400 + 3600)
        self.assertIsNone(native_adapters._cpu_seconds("n/a"))


from tests import test_partner_resolved_blocks as blocks  # noqa: E402


class InterruptStoreTests(blocks.PartnerResolvedBlockStoreTests):
    # The block cases run in test_partner_resolved_blocks; here only the cases below.
    locals().update({name: None for name in dir(blocks.PartnerResolvedBlockStoreTests) if name.startswith("test_")})

    def test_only_a_working_call_with_a_recorded_process_can_be_interrupted(self):
        # Catches the button ending the process of another room's call, of a finished call, or of nothing.
        room = self.plan(paths=True)
        job = self.store.claim(room, "claude")
        self.assertIsNone(self.store.native_call_process(room, job["id"]), "no process recorded yet")
        self.store.record_native_process(job["id"], 4242)
        self.assertEqual(self.store.native_call_process(room, job["id"]), 4242)
        self.assertIsNone(self.store.native_call_process(self.plan(paths=True), job["id"]), "another room's call")
        self.store.record_tool_interrupted(job["id"], reason="user", commands=1)
        self.store.record_tool_interrupted(job["id"], reason="idle", commands=1, idle_seconds=1800)
        said = [(event["sender"], event["reason"], event["commands"], event.get("idle_seconds"))
                for event in self.events(room, "native_tool_interrupted")]
        self.assertEqual(said, [("user", "user", 1, None), ("system", "idle", 1, 1800)])
        self.finish(job, [])
        self.assertIsNone(self.store.native_call_process(room, job["id"]), "a finished call has nothing to interrupt")


if __name__ == "__main__":
    unittest.main()
