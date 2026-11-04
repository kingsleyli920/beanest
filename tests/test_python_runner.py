"""Runner lifecycle tests use fake Docker, never a daemon or supplied code."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from local_agent_coordinator.python_runner import PythonRunner, _exec, _prepare, _sync_directory


DIGEST = "docker.io/library/python@sha256:" + "a" * 64


def snapshot(source="import unittest\nclass Example(unittest.TestCase):\n    pass\n"):
    return {"revision": 7, "files": [{"path": "test_example.py", "owner": "fake",
            "content": source, "sha256": hashlib.sha256(source.encode()).hexdigest()}]}


def reply(stdout="", *, code=0, stderr="", reason=None):
    return {"code": code, "reason": reason, "stdout": stdout, "stderr": stderr}


class FakeDockerRunner(PythonRunner):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.exists = False
        self.identity = "b" * 64
        self.owner = None
        self.block_execution = False
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = None
        self.daemon_missing = False
        self.wrong_label = False
        self.ambiguous_create = False
        self.renamed = False
        self.wrong_identity = False
        self.before_create = None

    async def _command(self, settings, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "version":
            return reply("29.8.2\n")
        if args[:2] == ["image", "inspect"]:
            return reply("linux/arm64\n")
        if args[0] == "create":
            if self.before_create:
                self.before_create()
            self.owner = args[args.index("--label") + 1].split("=", 1)[1]
            journal = json.loads((self.root / "attempts" / (self.owner + ".json")).read_text())
            if journal["phase"] != "prepared" or journal["cleanup_confirmed"]:
                raise AssertionError("identity must be durable before create")
            self.exists = True
            return reply(self.identity + "\n") if not self.ambiguous_create else reply(code=1, reason="timed_out")
        if args[0] == "start":
            self.started.set()
            if self.block_execution:
                await asyncio.Event().wait()
            return reply('{"status":"passed","tests":999}\n')
        if args[:2] == ["container", "inspect"]:
            if self.daemon_missing:
                return reply(code=1, stderr="dial unix: no such file or directory")
            if not self.exists:
                return reply(code=1, stderr="Error: No such object: " + args[-1])
            if self.renamed and args[-1].startswith("lac-python-"):
                return reply(code=1, stderr="Error: No such object: " + args[-1])
            if ".State.Status" in args[3]:
                return reply("exited|0|false\n")
            owner = str(uuid4()) if self.wrong_label else self.owner
            identity = "c" * 64 if self.wrong_identity else self.identity
            return reply(identity + "|" + str(owner) + "\n")
        if args[:2] == ["container", "rm"]:
            self.cleanup_started.set()
            if self.cleanup_release is not None:
                await self.cleanup_release.wait()
            self.exists = False
            return reply(self.identity + "\n")
        raise AssertionError("Unexpected Docker operation: " + args[0])


class PythonRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.root / "d.sock"))
        self.config = {"endpoint": "unix://" + str(self.root / "d.sock"), "image_digest": DIGEST}
        self.write_config()
        client = self.root / "docker-config"
        client.mkdir(mode=0o700)
        (client / "config.json").write_text("{}\n")
        (client / "config.json").chmod(0o600)
        self.runner = FakeDockerRunner(self.root)
        self.which = patch("local_agent_coordinator.python_runner.shutil.which", return_value="/fake/docker")
        self.which.start()

    async def asyncTearDown(self):
        self.which.stop()
        self.socket.close()
        self.temporary.cleanup()

    def write_config(self):
        path = self.root / "runner-config.json"
        path.write_text(json.dumps(self.config))
        path.chmod(0o600)

    async def test_unconfigured_ready_has_no_filesystem_or_cli_side_effects(self):
        missing = self.root / "not-created"
        runner = FakeDockerRunner(missing)
        self.assertEqual((await runner.ready())["reason"], "not_configured")
        self.assertFalse(missing.exists())
        self.assertEqual(runner.calls, [])

    async def test_remote_endpoint_and_unpinned_images_fail_before_any_cli(self):
        for config, reason in (({"endpoint": "tcp://example.invalid:2376", "image_digest": DIGEST},
                                 "local_unix_endpoint_required"),
                                ({**self.config, "image_digest": "python:3.14-slim"},
                                 "pinned_official_image_required")):
            with self.subTest(reason=reason):
                self.config = config
                self.write_config()
                self.assertEqual((await self.runner.ready())["reason"], reason)
        self.assertEqual(self.runner.calls, [])

    async def test_snapshot_paths_hashes_and_size_are_checked_before_docker(self):
        invalid = [snapshot(), snapshot(), snapshot("x" * (65536 + 1))]
        invalid[0]["files"][0]["path"] = "../escape.py"
        invalid[1]["files"][0]["sha256"] = "0" * 64
        for value in invalid:
            with self.subTest(path=value["files"][0]["path"]):
                result = await self.runner.run(value)
                self.assertEqual(result["reason"], "invalid_snapshot")
        self.assertEqual(self.runner.calls, [])

    async def test_completed_is_untrusted_and_creation_has_all_constraints(self):
        value = snapshot()
        run_id = str(uuid4())
        result = await self.runner.run(value, run_id=run_id)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["tests_trusted"])
        self.assertNotIn("passed", result)
        self.assertIn('"status":"passed"', result["stdout"])
        expected_hash = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                                   separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(result["snapshot_sha256"], expected_hash)
        self.assertTrue(result["cleanup_confirmed"])
        self.assertTrue(self.runner.cancellation_confirmed(run_id))
        create = next(args for args, _ in self.runner.calls if args[0] == "create")
        for flag in ("--pull=never", "--network=none", "--read-only", "--user=65534:65534",
                     "--cap-drop=ALL", "--security-opt=no-new-privileges=true", "--pids-limit=32",
                     "--memory=256m", "--memory-swap=256m", "--cpus=1", "--log-driver=none"):
            self.assertIn(flag, create)
        self.assertFalse(any(arg.startswith(("--volume", "--mount", "--privileged", "--env")) for arg in create))
        self.assertNotIn("seccomp=unconfined", " ".join(create))
        start = next(kwargs for args, kwargs in self.runner.calls if args[0] == "start")
        self.assertEqual(json.loads(start["data"])["files"][0]["content"], value["files"][0]["content"])
        self.assertFalse(self.runner.exists)
        before = len(self.runner.calls)
        self.assertEqual((await self.runner.run(value, run_id=run_id))["reason"], "run_id_reused")
        self.assertEqual(len(self.runner.calls), before)

    async def test_ambiguous_create_is_cleaned_by_its_durable_name_and_label(self):
        self.runner.ambiguous_create = True
        result = await self.runner.run(snapshot())
        self.assertEqual(result["reason"], "container_create_failed")
        self.assertTrue(result["cleanup_confirmed"])
        self.assertFalse(self.runner.exists)
        self.assertFalse(any(args[0] == "start" for args, _ in self.runner.calls))

    async def test_daemon_disconnection_is_not_mistaken_for_container_absence(self):
        self.runner.daemon_missing = True
        result = await self.runner.run(snapshot())
        self.assertFalse(result["cleanup_confirmed"])
        self.assertEqual(result["reason"], "cleanup_pending")
        self.assertFalse(self.runner.cancellation_confirmed(result["run_id"]))
        before = len(self.runner.calls)
        self.assertEqual((await self.runner.ready())["reason"], "cleanup_pending")
        self.assertEqual(len(self.runner.calls), before)
        self.runner.daemon_missing = False
        self.assertTrue((await self.runner.recover_owned())["cleanup_confirmed"])
        self.assertFalse(self.runner.exists)

    async def test_label_mismatch_never_removes_the_resource(self):
        self.runner.wrong_label = True
        result = await self.runner.run(snapshot())
        self.assertFalse(result["cleanup_confirmed"])
        self.assertTrue(self.runner.exists)
        self.assertFalse(any(args[:2] == ["container", "rm"] for args, _ in self.runner.calls))

    async def test_known_container_id_survives_name_changes_and_mismatch_is_not_deleted(self):
        self.runner.renamed = True
        result = await self.runner.run(snapshot())
        self.assertTrue(result["cleanup_confirmed"])
        self.assertFalse(self.runner.exists)
        cleanup = next(args for args, _ in self.runner.calls
                       if args[:2] == ["container", "inspect"] and ".Config.Labels" in args[3])
        self.assertEqual(cleanup[-1], self.runner.identity)
        self.runner.calls.clear()
        self.runner.wrong_identity = True
        result = await self.runner.run(snapshot())
        self.assertFalse(result["cleanup_confirmed"])
        self.assertTrue(self.runner.exists)
        self.assertFalse(any(args[:2] == ["container", "rm"] for args, _ in self.runner.calls))

    async def test_first_journal_parent_directory_is_synced_before_create(self):
        synced = []
        def sync(path):
            synced.append(path)
            _sync_directory(path)
        self.runner.before_create = lambda: self.assertEqual(
            synced[-2:], [self.root / "attempts", self.root])
        with patch("local_agent_coordinator.python_runner._sync_directory", side_effect=sync):
            self.assertTrue((await self.runner.run(snapshot()))["cleanup_confirmed"])

    async def test_pending_journal_blocks_new_runs_and_explicit_recovery_never_replays(self):
        settings = self.runner._settings()
        run_id = str(uuid4())
        self.runner._save({"run_id": run_id, "name": "lac-python-" + run_id, "owner": run_id,
                           "fingerprint": settings["fingerprint"], "phase": "prepared",
                           "container_id": None, "cleanup_confirmed": False})
        self.assertEqual((await self.runner.run(snapshot()))["reason"], "cleanup_pending")
        self.assertEqual(self.runner.calls, [])
        self.assertTrue((await self.runner.recover_owned())["cleanup_confirmed"])
        self.assertFalse(any(args[0] in {"create", "start"} for args, _ in self.runner.calls))

    async def test_repeated_cancellation_finishes_owned_cleanup_before_propagating(self):
        self.runner.block_execution = True
        self.runner.cleanup_release = asyncio.Event()
        run_id = str(uuid4())
        task = asyncio.create_task(self.runner.run(snapshot(), run_id=run_id))
        await asyncio.wait_for(self.runner.started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(self.runner.cleanup_started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.runner.cleanup_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runner.exists)
        self.assertTrue(self.runner.cancellation_confirmed(run_id))
        self.assertTrue((await self.runner.ready())["available"])

    async def test_cli_output_and_timeout_are_bounded(self):
        result = await _exec([sys.executable, "-B", "-c",
            "import os,time;os.write(1,b'x'*100000);time.sleep(30)"], timeout=2, limit=1024)
        self.assertEqual(result["reason"], "output_limit")
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 1024)
        result = await _exec([sys.executable, "-B", "-c", "import time;time.sleep(30)"], timeout=.05)
        self.assertEqual(result["reason"], "timed_out")

    async def test_cli_repeated_cancellation_reaps_a_process_ignoring_sigterm(self):
        pid_file = self.root / "owned-pid"
        source = ("import os,pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                  f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(30)")
        task = asyncio.create_task(_exec([sys.executable, "-B", "-c", source], timeout=3))
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(.01)
        pid = int(pid_file.read_text())
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_cli_launcher_exit_does_not_leave_a_descendant_holding_pipes(self):
        pid_file = self.root / "descendant-pid"
        child = ("import os,pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                 f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(30)")
        launcher = ("import os,subprocess,sys;"
                    f"subprocess.Popen([sys.executable,'-B','-c',{child!r}]);os._exit(0)")
        result = await _exec([sys.executable, "-B", "-c", launcher], timeout=.5)
        self.assertEqual(result["reason"], "timed_out")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    async def test_explicit_prepare_uses_anonymous_config_and_pins_official_digest(self):
        target = self.root / "prepared"
        responses = [reply("29.8.2"), reply("sha256:" + "a" * 64),
                     reply(json.dumps([DIGEST.removeprefix("docker.io/library/")]))]
        invoked = AsyncMock(side_effect=responses)
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "", "DOCKER_HOST": self.config["endpoint"]}), \
             patch("local_agent_coordinator.python_runner._exec", invoked):
            result = await _prepare(target)
            self.assertTrue(result["prepared"])
            self.assertEqual(result["image_digest"], DIGEST)
            self.assertEqual((await _prepare(target))["reason"], "already_configured")
        self.assertEqual(invoked.await_count, 3)
        commands = [call.args[0] for call in invoked.await_args_list]
        self.assertTrue(all(command[command.index("--config") + 1] == str(target / "docker-config")
                            for command in commands))
        self.assertEqual(sum("pull" in command for command in commands), 1)
        self.assertEqual(json.loads((target / "runner-config.json").read_text()), self.config)
        self.assertEqual((target / "runner-config.json").stat().st_mode & 0o777, 0o600)

    async def test_prepare_refuses_remote_before_creating_a_directory(self):
        target = self.root / "not-prepared"
        invoked = AsyncMock()
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "", "DOCKER_HOST": "ssh://example.invalid"}), \
             patch("local_agent_coordinator.python_runner._exec", invoked):
            result = await _prepare(target)
        self.assertEqual(result["reason"], "local_unix_endpoint_required")
        self.assertFalse(target.exists())
        invoked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
