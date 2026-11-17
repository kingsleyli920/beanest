"""What reaches a partner while its call is running is handed to that call.

Fictional project, real SQLite and the real job loop; only the native transport
is synthetic (see test_task_pool).
"""
import asyncio
from pathlib import Path

from local_agent_coordinator import job_progress, room_prompts
from test_native_room_service import native_reply
import test_task_pool as pool

plan = pool.plan
ASK = ("The ending needs nine new keys in shared.py, which your task holds: add ENDING_TITLE and ENDING_BODY there; "
       "I have not touched that file.")


class CallInbox(pool.PoolService):
    # The pool's own cases run in test_task_pool; here only the cases below.
    locals().update({name: None for name in dir(pool.PoolService) if name.startswith("test_")})

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.service.store.parallel_limits = lambda: {"claude": 3, "codex": 1}
        self.release = asyncio.Event()
        self.ask_once = True

    async def until_released(self):
        return self.release.is_set()

    def hold_all(self):
        for task in self.plan:
            self.holds[task["id"]] = self.until_released

    async def running_work(self, count):
        """The work calls now running, as {(provider the host launched, task id): inbox path}."""
        for _ in range(500):
            calls = {(call["context"]["self"], call["context"]["task"]["id"]): job_progress.inbox_path(call["progress_file"])
                     for call in self.fake.calls if call["context"]["job"]["job_phase"] == "work"}
            if len(calls) >= count:
                return calls
            await asyncio.sleep(0.01)
        self.fail(f"fewer than {count} work calls started")

    @staticmethod
    def inbox(path):
        return path.read_text() if path.exists() else ""

    async def test_a_message_to_everyone_reaches_every_running_call_and_a_clone_too(self):
        # Catches the real gap: a partner hours into a call (or its clone, which no message ever reached)
        # only heard the user after the call ended.
        self.plan = [plan("T1"), plan("T2"), plan("T3")]
        self.hold_all()
        await self.create(["claude", "codex"])
        inboxes = await self.running_work(3)
        self.assertEqual([self.inbox(path) for path in inboxes.values()], ["", "", ""])
        await self.service.send_message(self.room_id, "Stop moving the mouse in the drill.", "all", "to-all", source="user")
        for path in inboxes.values():
            self.assertEqual(self.inbox(path), "## Message from the user, sent while this call was running\n"
                                               "Stop moving the mouse in the drill.\n\n")
        await self.service.send_message(self.room_id, "Codex only: keep the old seed.", "codex", "to-codex", source="user")
        got = {key: self.inbox(path).count("Codex only: keep the old seed.") for key, path in inboxes.items()}
        self.assertEqual(sorted(got.items()), [(("claude", "T1"), 0), (("claude", "T3"), 0), (("codex", "T2"), 1)])
        state = await self.state()
        delivered = [(event["recipient"], event["inbox_kind"]) for event in state["events"] if event["kind"] == "call_inbox_delivered"]
        self.assertEqual(sorted(delivered), [("claude", "user_message"), ("claude~x2", "user_message"),
                                              ("codex", "user_message"), ("codex", "user_message")])
        self.release.set()
        await self.settle()
        self.assertEqual([path.exists() for path in inboxes.values()], [False, False, False], "the inbox goes with the call")

    async def test_a_request_about_a_held_file_goes_to_the_call_that_holds_it_once(self):
        # Catches the stall seen in a real room: a partner asked for nine lines in a file another task held,
        # the holder's other session could only advise, and the session doing the task never heard of it.
        self.plan = [plan("T1", paths=["shared.py", "T1.py"]), plan("T2")]
        self.holds["T1"] = self.until_released
        base = self.fake.handler

        async def handler(agent, context, cwd):
            if context["job"]["job_phase"] == "work" and context["task"]["id"] == "T2" and self.ask_once:
                self.ask_once = False
                return native_reply(context, [{"type": "request_help", "task_id": "T2", "to": "claude",
                                               "capability": "text", "text": ASK}])
            if context["job"]["job_phase"] == "help":
                return native_reply(context)
            return await base(agent, context, cwd)
        self.fake.handler = handler
        await self.create(["claude", "codex"])
        inboxes = await self.running_work(2)
        holder = next(path for (member, task), path in inboxes.items() if task == "T1")
        for _ in range(500):
            if self.inbox(holder):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.inbox(holder), "## Request from codex (task T2) about shared.py, which your task T1 holds\n" + ASK + "\n\n")
        # The partner asked is the one busy on T1, so the help call itself waits behind that work:
        # without the inbox nothing would move until T1's call ended.
        self.assertEqual((await self.statuses()).get("T2"), "waiting_peer")
        # Looking again (as every finished call does) does not hand the same request over twice.
        self.service._carry_requests_to_holders(self.room_id)
        self.service._carry_requests_to_holders(self.room_id)
        self.assertEqual(self.inbox(holder).count(ASK), 1)
        state = await self.state()
        self.assertEqual([(event["sender"], event["recipient"], event["inbox_kind"], event["task_id"]) for event in state["events"]
                          if event["kind"] == "call_inbox_delivered"], [("codex", "claude", "held_file_request", "T1")])
        self.release.set()
        await self.settle()

    def test_the_work_prompt_tells_the_partner_where_its_inbox_is(self):
        # Catches the file being written with nobody told to read it.
        self.assertIn('"$BEANEST_PROGRESS_FILE.inbox"', room_prompts.NATIVE_INBOX_INSTRUCTION)
        self.assertIn("Read it when you start, after every command that ran for more than a few minutes", room_prompts.NATIVE_INBOX_INSTRUCTION)


class InboxFile(pool.unittest.TestCase):
    def test_entries_are_appended_private_and_capped_and_a_link_is_not_followed(self):
        # Catches an entry replacing the earlier ones, a world-readable file, an unbounded entry,
        # and the host writing through a link a command planted in its place.
        import os, tempfile
        with tempfile.TemporaryDirectory() as temp:
            progress = job_progress.create_progress_file(Path(temp) / "p")
            self.assertTrue(job_progress.append_inbox(progress, "From the user", "one"))
            self.assertTrue(job_progress.append_inbox(progress, "From\x1b[31m codex", "y" * 5000))
            path = job_progress.inbox_path(progress)
            text = path.read_text()
            self.assertTrue(text.startswith("## From the user\none\n\n## From codex\n"))
            self.assertEqual(text.count("y"), 4000)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            other = job_progress.create_progress_file(Path(temp) / "p")
            target = Path(temp) / "elsewhere"
            target.write_text("mine")
            os.symlink(target, job_progress.inbox_path(other))
            self.assertFalse(job_progress.append_inbox(other, "From the user", "two"))
            self.assertEqual(target.read_text(), "mine")
            job_progress.remove_progress_file(progress)
            self.assertFalse(path.exists())
