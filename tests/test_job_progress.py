"""Display-only progress file: real files, real child processes, synthetic models."""
import asyncio
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from local_agent_coordinator import job_progress, kimi_acp, native_adapters, room_service
import test_native_adapters as adapter_tests
import test_native_room_service as native

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "src/local_agent_coordinator/room_ui"
PROGRESS = "96/182 done, 0 failed"


class ReadProgressLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "one.progress"

    def read_bytes(self, data, **options):
        self.path.write_bytes(data)
        return job_progress.read_progress_line(self.path, **options)

    def test_takes_the_last_non_empty_line(self):
        # Catches showing the oldest line, or an empty result when a command ends the file with blank lines.
        self.assertEqual(self.read_bytes(b"1/9 done\n2/9 done\n\n   \n"), "2/9 done")

    def test_a_line_without_a_final_newline_counts(self):
        # Catches waiting for a newline, which would hide the line a command is still extending.
        self.assertEqual(self.read_bytes(b"1/9 done\n2/9 in flight"), "2/9 in flight")

    def test_an_empty_file_has_no_progress(self):
        # Catches an empty string becoming a blank event.
        self.assertIsNone(self.read_bytes(b""))

    def test_a_long_line_is_capped_at_200_characters(self):
        # Catches an unbounded line reaching the event log and the card; counts characters, not bytes.
        self.assertEqual(self.read_bytes(("进" * 500 + "\n").encode()), "进" * 200)

    def test_reads_only_the_end_of_a_large_file(self):
        # Catches a read of the whole file: the old line sits outside the 4 KiB window, and the last line wins.
        data = b"old line\n" + b"x" * 8192 + b"\n" + b"latest\n"
        self.assertEqual(self.read_bytes(data), "latest")
        self.assertIsNone(self.read_bytes(b"old line\n" + b"\n" * 8192), "the only text is before the window")

    def test_half_a_character_at_the_end_is_replaced(self):
        # Catches a decode error from a command caught mid-write.
        self.assertEqual(self.read_bytes("进度".encode()[:-1]), "进�")

    def test_the_window_opening_inside_a_character_adds_no_replacement_mark(self):
        # Catches the reader's own cut showing as a garbage character at the start of the line.
        data = "进".encode() * 2000
        self.assertGreater(len(data), job_progress.MAX_READ_BYTES)
        self.assertEqual(self.read_bytes(data), "进" * 200)
        self.path.write_bytes(b"a" + data[:job_progress.MAX_READ_BYTES])
        self.assertEqual(job_progress.read_progress_line(self.path), "进" * 200)

    def test_control_characters_and_terminal_sequences_are_removed(self):
        # Catches colour codes showing as "[31m", and bidi or NUL characters reaching the card.
        self.assertEqual(self.read_bytes(b"\x1b[31m3/9\x1b[0m\x00 do\x07ne\tok\n"), "3/9 done ok")
        self.assertEqual(self.read_bytes("a‮b\n".encode()), "ab")

    def test_a_carriage_return_progress_bar_shows_its_last_state(self):
        # Catches a whole redraw history being treated as one line.
        self.assertEqual(self.read_bytes(b"10%\r20%\r30%"), "30%")

    def test_a_line_of_only_control_characters_is_skipped(self):
        # Catches a sanitized empty line hiding the real line before it.
        self.assertEqual(self.read_bytes(b"4/9 done\n\x1b[2K\x07\n"), "4/9 done")

    def test_a_symlink_is_never_followed(self):
        # Catches a command pointing the file at another file and having its contents shown on the card.
        target = self.root / "other.txt"
        target.write_text("PRIVATE TARGET\n")
        self.path.symlink_to(target)
        self.assertIsNone(job_progress.read_progress_line(self.path))

    def test_a_deleted_file_is_tolerated(self):
        # Catches a command removing the file crashing the watcher.
        self.assertIsNone(job_progress.read_progress_line(self.root / "gone.progress"))

    def test_a_fifo_does_not_block_and_a_directory_is_refused(self):
        # Catches a blocking open, which would pin a worker thread for the rest of the job.
        os.mkfifo(self.path)
        started = time.monotonic()
        self.assertIsNone(job_progress.read_progress_line(self.path))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(job_progress.read_progress_line(self.root))

    def test_redaction_runs_before_the_length_cap(self):
        # Catches a cap at 200 cutting a secret in half so the redaction pattern no longer matches it.
        secret = "sk-" + "a" * 30
        line = "x" * 189 + " " + secret + "\n"
        text = self.read_bytes(line.encode(), redact=room_service._visible)
        self.assertEqual(text, "x" * 189 + " [redacted]")
        self.assertNotIn("sk-aaa", text)


class Throttle(unittest.TestCase):
    def test_first_line_is_due_and_empty_is_not(self):
        # Catches a blank or unreadable poll producing an event.
        throttle = job_progress.ProgressThrottle()
        self.assertTrue(throttle.due("a", 1000.0))
        self.assertFalse(throttle.due("", 1000.0))
        self.assertFalse(throttle.due(None, 1000.0))

    def test_the_same_content_is_never_written_twice(self):
        # Catches a repeat event every 30 seconds for an unchanged line.
        throttle = job_progress.ProgressThrottle()
        throttle.mark("a", 1000.0)
        self.assertFalse(throttle.due("a", 5000.0))

    def test_new_content_waits_for_the_thirty_second_window(self):
        # Catches a fast command writing one event per poll.
        throttle = job_progress.ProgressThrottle()
        throttle.mark("a", 1000.0)
        self.assertFalse(throttle.due("b", 1029.9))
        self.assertTrue(throttle.due("b", 1030.0))

    def test_a_clock_stepped_backwards_does_not_silence_the_channel(self):
        # Catches a negative age being treated as still inside the window.
        throttle = job_progress.ProgressThrottle()
        throttle.mark("a", 1000.0)
        self.assertTrue(throttle.due("b", 900.0))


class FileLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_creates_an_empty_private_absolute_file(self):
        # Catches a file or directory other local users can read, or a relative path a child resolves elsewhere.
        path = job_progress.create_progress_file(self.root / "private")
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotEqual(job_progress.create_progress_file(self.root / "private"), path)

    def test_refuses_a_shared_or_symlinked_directory(self):
        # Catches writing the file through a directory someone else can list or redirect.
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        with self.assertRaises(ValueError):
            job_progress.create_progress_file(shared)
        real = self.root / "real"
        real.mkdir(mode=0o700)
        (self.root / "link").symlink_to(real)
        with self.assertRaises(ValueError):
            job_progress.create_progress_file(self.root / "link")

    def test_removal_tolerates_a_missing_file_and_a_replaced_path(self):
        # Catches a command removing or replacing the file breaking job cleanup.
        job_progress.remove_progress_file(self.root / "gone.progress")
        directory = self.root / "directory"
        directory.mkdir()
        job_progress.remove_progress_file(directory)
        self.assertTrue(directory.is_dir())


class ChildEnvironment(unittest.IsolatedAsyncioTestCase):
    """Real child processes running a fictional CLI; nothing here reaches a model."""
    setUp = adapter_tests.NativeTests.setUp
    tearDown = adapter_tests.NativeTests.tearDown
    make_stub = adapter_tests.NativeTests.make_stub

    def recorder(self, *names):
        out = self.root / "seen"
        values = ", ".join(f"os.environ.get({name!r}, 'absent')" for name in names)
        return out, f"open({str(out)!r}, 'w').write(chr(10).join([{values}]))"

    async def test_claude_and_codex_children_receive_the_variable(self):
        # Catches the variable being set for one adapter only; the Claude row also guards the existing background-task switch.
        for agent, extra in (("claude", "1"), ("codex", "absent")):
            with self.subTest(agent=agent):
                out, write = self.recorder("BEANEST_PROGRESS_FILE", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")
                target = self.root / f"{agent}.progress"
                with self.make_stub([], agent=agent, before=write):
                    await asyncio.wait_for(native_adapters.invoke_native(
                        agent, "hello", self.root, permission="full_access", progress_file=target), 10)
                self.assertEqual(out.read_text().split("\n"), [str(target), extra])

    async def test_without_a_file_the_variable_is_absent(self):
        # Catches a stale or empty variable that would send a command's write to the wrong place.
        out, write = self.recorder("BEANEST_PROGRESS_FILE")
        with self.make_stub([], agent="codex", before=write):
            await asyncio.wait_for(native_adapters.invoke_native("codex", "hello", self.root, permission="full_access"), 10)
        self.assertEqual(out.read_text(), "absent")

    async def test_kimi_child_receives_the_variable(self):
        # Catches the channel stopping at the one adapter that starts its process through a separate function.
        out, write = self.recorder("BEANEST_PROGRESS_FILE")
        self.stub.write_text(f"#!{sys.executable}\nimport os\n{write}\n")
        self.stub.chmod(0o700)
        target = self.root / "kimi.progress"
        with patch.object(native_adapters.adapters, "find_kimi", return_value=str(self.stub)):
            process, connection = await kimi_acp.start(self.root, progress_file=target)
            await asyncio.wait_for(process.wait(), 10)
            await connection.close()
        self.assertEqual(out.read_text(), str(target))

    async def test_the_kimi_route_forwards_the_file(self):
        # Catches invoke_native returning early into Kimi and dropping the argument.
        target = self.root / "kimi.progress"
        with patch.object(kimi_acp, "invoke_native", new=AsyncMock(return_value={"status": "completed"})) as forwarded:
            await native_adapters.invoke_native("kimi", "hello", self.root, progress_file=target)
        self.assertEqual(forwarded.await_args.kwargs["progress_file"], target)


class ServiceChannel(unittest.IsolatedAsyncioTestCase):
    create = native.NativeRoomServiceTests.create
    settle = native.NativeRoomServiceTests.settle
    handler = native.NativeRoomServiceTests.handler
    asyncTearDown = native.NativeRoomServiceTests.asyncTearDown

    async def asyncSetUp(self):
        await native.NativeRoomServiceTests.asyncSetUp(self)
        poll = patch.object(room_service, "PROGRESS_POLL_SECONDS", 0.01)
        poll.start()
        self.addCleanup(poll.stop)

    async def eventually(self, predicate):
        for _ in range(500):
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.01)
        self.fail("condition never became true")

    def statuses(self, room_id):
        return [event for event in self.service.store.snapshot(room_id)["events"] if event["kind"] == "native_progress_status"]

    async def held_room(self):
        self.fake.hold = asyncio.Event()
        created = await self.create()
        await asyncio.wait_for(self.fake.started.wait(), 5)
        return created["room"]["id"], self.fake.calls[0]

    def write(self, call, text, mode="a"):
        with open(call["progress_file"], mode, encoding="utf-8") as stream:
            stream.write(text + "\n")

    async def test_every_call_gets_its_own_existing_file_and_it_is_removed_afterwards(self):
        # Catches a shared file between calls, a file created too late, and files left behind after a job.
        seen = {}
        original = self.fake.handler
        def recording(agent, context, cwd):
            call = self.fake.calls[-1]
            seen[Path(call["progress_file"])] = Path(call["progress_file"]).read_bytes()
            return original(agent, context, cwd)
        self.fake.handler = recording
        await self.create()
        await self.settle()
        self.assertGreater(len(seen), 3)
        self.assertEqual({call["agent"] for call in self.fake.calls}, {"claude", "codex"})
        self.assertEqual(len(seen), len(self.fake.calls))
        self.assertEqual(set(seen.values()), {b""})
        self.assertTrue(all(path.parent == self.root / "native-progress" and path.is_absolute() for path in seen))
        self.assertEqual(list((self.root / "native-progress").iterdir()), [])

    async def test_a_progress_line_reaches_the_projection_and_nothing_else(self):
        # Catches the text leaking into chat or a later model prompt, counting as tool activity, or finishing the job's evidence.
        marker = PROGRESS + " channel-only-text"
        room_id, call = await self.held_room()
        self.assertTrue(Path(call["progress_file"]).exists())
        self.write(call, marker)
        statuses = await self.eventually(lambda: self.statuses(room_id))
        self.assertEqual(len(statuses), 1)
        event = statuses[0]
        snapshot = self.service.store.snapshot(room_id)
        job = next(item for item in snapshot["jobs"] if item["provider"] == call["agent"] and item["status"] == "working")
        self.assertEqual((event["job_id"], event["text"], event["sender"], event["run_id"]),
                         (job["id"], marker, call["agent"], snapshot["current_run"]["id"]))
        self.assertIsInstance(event["created_at"], float)
        state = await self.service.read_room_state(room_id)
        live = next(item for item in state["live"]["jobs"] if item["id"] == job["id"])
        self.assertEqual(live["latest_progress_status"]["text"], marker)
        self.assertIsNone(live["latest_native_activity"], "a progress line is not tool activity")
        self.assertIsNone(live["latest_public_progress"], "a progress line is not a public message")
        self.assertFalse([item for item in snapshot["events"] if item["kind"] == "message" and marker in str(item.get("text"))])
        before = len(self.fake.calls)
        self.fake.hold.set()
        await self.settle()
        later = self.fake.calls[before:]
        self.assertGreater(len(later), 2)
        self.assertFalse([item["context"]["job"]["job_phase"] for item in self.fake.calls if marker in item["prompt"]])
        final = await self.service.read_room_state(room_id)
        self.assertEqual(final["objective"]["status"], "complete")
        self.assertEqual(self.statuses(room_id), statuses, "no event after the job ended")

    async def test_the_service_throttles_and_skips_unchanged_text(self):
        # Catches the watcher ignoring the 30 second limit, or re-recording an unchanged line after it.
        base = time.time()
        self.offset = 0.0
        room_id, call = await self.held_room()
        self.service.clock = lambda: base + self.offset
        self.write(call, "first")
        await self.eventually(lambda: self.statuses(room_id))
        self.write(call, "second")
        self.offset = 10.0
        await asyncio.sleep(0.12)
        self.assertEqual([event["text"] for event in self.statuses(room_id)], ["first"])
        self.offset = 30.0
        await self.eventually(lambda: len(self.statuses(room_id)) == 2)
        self.offset = 90.0
        await asyncio.sleep(0.12)
        self.assertEqual([event["text"] for event in self.statuses(room_id)], ["first", "second"])
        self.fake.hold.set()
        await self.settle()

    async def test_a_deleted_or_swapped_file_never_disturbs_the_job(self):
        # Catches a command that removes the file, or swaps in a symlink, breaking the watcher or the run.
        room_id, call = await self.held_room()
        path = Path(call["progress_file"])
        self.write(call, "before removal")
        await self.eventually(lambda: self.statuses(room_id))
        path.unlink()
        await asyncio.sleep(0.05)
        target = self.root / "elsewhere.txt"
        target.write_text("PRIVATE TARGET\n")
        path.symlink_to(target)
        await asyncio.sleep(0.05)
        self.assertEqual([event["text"] for event in self.statuses(room_id)], ["before removal"])
        self.fake.hold.set()
        await self.settle()
        self.assertEqual((await self.service.read_room_state(room_id))["objective"]["status"], "complete")
        self.assertTrue(target.exists(), "cleanup removes the link, not its target")
        self.assertFalse(path.is_symlink())

    async def test_a_failure_creating_the_file_still_runs_the_call(self):
        # Catches a display-only channel taking a real job down with it.
        with patch.object(job_progress, "create_progress_file", side_effect=OSError("no space")):
            result = await self.service._with_progress_channel({"id": "x"}, AsyncMock(return_value={"status": "completed"}))
        self.assertEqual(result, {"status": "completed"})

    async def test_cancellation_keeps_the_transport_receipt_and_removes_the_file(self):
        # Catches the cleanup replacing the CancelledError that carries the transport's cleanup receipt.
        receipt = {"cleanup_confirmed": True}
        seen = {}
        async def cancelled(options):
            seen.update(options)
            error = asyncio.CancelledError()
            error.native_execution = receipt
            raise error
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.service._with_progress_channel({"id": "x"}, cancelled)
        self.assertIs(caught.exception.native_execution, receipt)
        self.assertFalse(Path(seen["progress_file"]).exists())

    async def test_store_refuses_oversized_blank_and_finished_jobs(self):
        # Catches the store trusting its caller's limits, or a late write landing on a finished job.
        room_id, call = await self.held_room()
        job = next(item for item in self.service.store.snapshot(room_id)["jobs"] if item["provider"] == call["agent"] and item["status"] == "working")
        record = self.service.store.record_native_progress_status
        self.assertFalse(record(job["id"], "x" * 201))
        self.assertFalse(record(job["id"], ""))
        self.assertFalse(record(job["id"], None))
        self.assertTrue(record(job["id"], "x" * 200))
        self.assertFalse(record("not-a-job", "text"))
        self.fake.hold.set()
        await self.settle()
        self.assertFalse(record(job["id"], "late"))
        self.assertEqual([event["text"] for event in self.statuses(room_id)], ["x" * 200])

    async def test_instruction_goes_to_work_and_help_only_and_never_names_the_path(self):
        # Catches the path entering a prompt, which would put a per-call value into cached context.
        await self.create()
        await self.settle()
        phases = {call["context"]["job"]["job_phase"]: call for call in self.fake.calls}
        self.assertIn("$BEANEST_PROGRESS_FILE", phases["work"]["prompt"])
        self.assertNotIn("$BEANEST_PROGRESS_FILE", phases["plan"]["prompt"])
        self.assertFalse([call["agent"] for call in self.fake.calls if str(call["progress_file"]) in call["prompt"]])


class ProjectNeutralWording(unittest.TestCase):
    """The channel serves any project and any long command."""
    FORBIDDEN = re.compile(r"开局|演练|测试进度|九烛|游戏|godot|nine.?candles|\bgame|\bdrill|\bsteam\b", re.IGNORECASE)

    def wording(self):
        script = (UI / "app.js").read_text(encoding="utf-8")
        start = script.index("const progressStatusStaleSeconds")
        interface = script[start:script.index("function renderMemberPublicProgress", start)]
        catalog = (UI / "i18n-en-c.js").read_text(encoding="utf-8").splitlines()
        keys = ('"进度：{p0} · {p1}"', '"刚刚"', '"{p0} 分钟前"', '"（可能已过期）"')
        english = [line for line in catalog if line.strip().startswith(keys)]
        self.assertEqual(len(english), 4)
        source = (ROOT / "src/local_agent_coordinator/job_progress.py").read_text(encoding="utf-8")
        return {"prompt": room_service.NATIVE_PROGRESS_INSTRUCTION, "interface": interface,
                "english": "\n".join(english), "module": source}

    def test_prompt_and_new_interface_copy_name_no_project(self):
        # Catches project-specific words returning to the prompt, the card or the English copy; the marker line proves the pattern bites.
        self.assertIsNotNone(self.FORBIDDEN.search("开局演练 96/182"))
        for name, text in self.wording().items():
            with self.subTest(name=name):
                self.assertIsNone(self.FORBIDDEN.search(text), text)

    def test_prompt_explains_the_channel_works_without_changing_the_project(self):
        # Catches the instruction narrowing back to scripts a project already prepared.
        prompt = room_service.NATIVE_PROGRESS_INSTRUCTION
        for phrase in ("any project", "No project change is needed", "pipe or tee", "96/182 done, 0 failed", "exit status", "$BEANEST_PROGRESS_FILE"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)
