from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from local_agent_coordinator.project_workspace import ProjectWorkspace, ProjectWorkspaceError
from local_agent_coordinator import project_workspace


def file(path="main.py", content="VALUE = 1\n", previous=None):
    return {"path": path, "content": content, "previous_sha256": previous}


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.temporary = Path(self.directory.name).resolve()
        self.root = self.temporary / "runtime" / "projects"
        self.room = str(uuid4())
        self.workspace = ProjectWorkspace(self.root, self.room)

    def assertCode(self, code, operation):
        with self.assertRaises(ProjectWorkspaceError) as error:
            operation()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(str(error.exception), code)

    def race(self, operations):
        barrier = threading.Barrier(len(operations))

        def invoke(operation):
            barrier.wait(timeout=2)
            try:
                return operation()
            except ProjectWorkspaceError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(invoke, operations))

    def test_reopen_history_and_idempotency_return_the_exact_original_revision(self):
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})
        original = self.workspace.apply("claude", "first", 0, [file()])
        self.assertFalse(original["replayed"])
        first = original["files"][0]
        self.assertEqual(first["sha256"], hashlib.sha256(first["content"].encode()).hexdigest())
        current = self.workspace.apply("claude", "second", 1, [file(content="VALUE = 2\n", previous=first["sha256"])])
        reopened = ProjectWorkspace(self.root, self.room)
        self.assertEqual(reopened.snapshot(), {key: current[key] for key in ("revision", "files")})
        self.assertEqual(reopened.snapshot(1), {key: original[key] for key in ("revision", "files")})
        replayed = reopened.apply("claude", "first", 0, [file()])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["revision"], 1)
        self.assertEqual(replayed["files"], original["files"])
        self.assertEqual(reopened.snapshot()["revision"], 2)
        self.assertCode("project_proposal_conflict", lambda: reopened.apply("codex", "first", 0, [file()]))
        self.assertCode("project_revision_not_found", lambda: reopened.snapshot(3))

    def test_open_existing_never_creates_a_missing_root_or_room(self):
        absent_root = self.temporary / "missing" / "projects"
        self.assertCode("project_unknown_workspace", lambda: ProjectWorkspace(absent_root, str(uuid4()), create=False))
        self.assertFalse((self.temporary / "missing").exists())
        room = str(uuid4())
        before = set(self.root.iterdir())
        self.assertCode("project_unknown_workspace", lambda: ProjectWorkspace(self.root, room, create=False))
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertEqual(ProjectWorkspace(self.root, self.room, create=False).snapshot(), {"revision": 0, "files": []})

    def test_open_existing_refuses_a_room_that_disappears_during_open(self):
        actual_open = project_workspace._open_directory
        saved = self.root / "saved-workspace"

        def disappearing(name, *, parent=None):
            if name == self.room:
                (self.root / self.room).rename(saved)
            return actual_open(name, parent=parent)

        with patch("local_agent_coordinator.project_workspace._open_directory", side_effect=disappearing):
            self.assertCode("project_unknown_workspace", lambda: ProjectWorkspace(self.root, self.room, create=False))
        self.assertFalse((self.root / self.room).exists())
        self.assertTrue((saved / "manifest.json").is_file())
        self.assertFalse(list(self.root.glob(".init-*")))

    def test_two_instances_race_first_ownership_then_enforce_owner_and_cas(self):
        other = ProjectWorkspace(self.root, self.room)
        results = self.race([
            lambda: self.workspace.apply("claude", "claude-first", 0, [file()]),
            lambda: other.apply("codex", "codex-first", 0, [file(content="VALUE = 2\n")]),
        ])
        winners = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(results.count("project_revision_conflict"), 1)
        current = self.workspace.snapshot()
        item = current["files"][0]
        owner = item["owner"]
        peer = "codex" if owner == "claude" else "claude"
        self.assertCode("project_owner_conflict", lambda: other.apply(
            peer, "steal", 1, [file(previous=item["sha256"])]))
        results = self.race([
            lambda: self.workspace.apply(owner, "owner-a", 1, [file(content="A = 1\n", previous=item["sha256"])]),
            lambda: other.apply(owner, "owner-b", 1, [file(content="B = 1\n", previous=item["sha256"])]),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(results.count("project_revision_conflict"), 1)
        self.assertEqual(self.workspace.snapshot()["revision"], 2)
        self.assertEqual(self.workspace.snapshot()["files"][0]["owner"], owner)

    def test_same_proposal_is_atomic_across_instances_and_order_independent(self):
        other = ProjectWorkspace(self.root, self.room)
        changes = [file("a.py"), file("b.py")]
        results = self.race([
            lambda: self.workspace.apply("helper", "one-proposal", 0, changes),
            lambda: other.apply("helper", "one-proposal", 0, list(reversed(changes))),
        ])
        self.assertTrue(all(isinstance(result, dict) for result in results))
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(self.workspace.snapshot()["revision"], 1)
        self.assertEqual(len(list((self.root / self.room / "revisions").iterdir())), 2)

    def test_disjoint_parallel_providers_publish_both_files_without_losing_the_winner(self):
        other = ProjectWorkspace(self.root, self.room)
        results = self.race([
            lambda: self.workspace.apply("claude", "implementation", 0,
                                         [file("worker.py", "VALUE = 3\n")], allow_disjoint=True),
            lambda: other.apply("codex", "independent-tests", 0,
                                [file("test_worker.py", "EXPECTED = 3\n")], allow_disjoint=True),
        ])
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertEqual(sorted(result["revision"] for result in results), [1, 2])
        snapshot = ProjectWorkspace(self.root, self.room, create=False).snapshot()
        self.assertEqual(snapshot["revision"], 2)
        self.assertEqual({item["path"]: (item["owner"], item["content"]) for item in snapshot["files"]},
                         {"worker.py": ("claude", "VALUE = 3\n"),
                          "test_worker.py": ("codex", "EXPECTED = 3\n")})
        self.assertEqual(len(self.workspace.snapshot(1)["files"]), 1)
        self.assertEqual(self.workspace.snapshot(0), {"revision": 0, "files": []})

    def test_default_global_cas_still_rejects_a_stale_independent_path(self):
        self.workspace.apply("claude", "first", 0, [file("worker.py")])
        for options in ({}, {"allow_disjoint": False}):
            self.assertCode("project_revision_conflict", lambda: self.workspace.apply(
                "codex", "legacy-stale", 0, [file("test_worker.py")], **options))
        self.assertEqual(self.workspace.snapshot()["revision"], 1)

    def test_disjoint_merge_updates_unchanged_owned_path_and_preserves_peer_changes(self):
        left = self.workspace.apply("claude", "left", 0, [file("left.py", "LEFT = 1\n")])
        base = self.workspace.apply("codex", "right", 1, [file("right.py", "RIGHT = 1\n")])
        indexed = {item["path"]: item for item in base["files"]}
        self.workspace.apply("claude", "left-update", 2, [
            file("left.py", "LEFT = 2\n", left["files"][0]["sha256"])])
        merged = self.workspace.apply("codex", "right-update", 2, [
            file("right.py", "RIGHT = 2\n", indexed["right.py"]["sha256"]),
            file("test_right.py", "EXPECTED = 2\n")], allow_disjoint=True)
        self.assertEqual(merged["revision"], 4)
        self.assertEqual({item["path"]: item["content"] for item in merged["files"]},
                         {"left.py": "LEFT = 2\n", "right.py": "RIGHT = 2\n",
                          "test_right.py": "EXPECTED = 2\n"})

    def test_disjoint_merge_rejects_stale_same_path_wrong_hash_owner_and_case_collision(self):
        first = self.workspace.apply("claude", "first", 0, [file()])
        original = first["files"][0]
        current = self.workspace.apply("claude", "update", 1, [
            file(content="VALUE = 2\n", previous=original["sha256"])])
        proposals = [
            ("claude", 1, file(content="VALUE = 3\n", previous=original["sha256"]), "project_file_conflict"),
            ("claude", 1, file(previous="0" * 64), "project_file_conflict"),
            ("codex", 2, file(previous=current["files"][0]["sha256"]), "project_owner_conflict"),
            ("codex", 0, file(), "project_file_conflict"),
            ("codex", 0, file("MAIN.py"), "project_file_conflict"),
        ]
        for index, (provider, revision, proposal, code) in enumerate(proposals):
            with self.subTest(index=index):
                self.assertCode(code, lambda: self.workspace.apply(
                    provider, "rejected-" + str(index), revision, [proposal], allow_disjoint=True))
                self.assertEqual(self.workspace.snapshot()["revision"], 2)

    def test_disjoint_proposal_replay_keeps_original_base_flag_and_committed_revision(self):
        original = self.workspace.apply("claude", "opt-in", 0, [file()], allow_disjoint=True)
        self.workspace.apply("codex", "peer", 1, [file("peer.py")])
        replayed = self.workspace.apply("claude", "opt-in", 0, [file()], allow_disjoint=True)
        self.assertEqual(replayed, {**original, "replayed": True})
        self.assertEqual(self.workspace.snapshot()["revision"], 2)
        self.assertCode("project_proposal_conflict", lambda: self.workspace.apply("claude", "opt-in", 0, [file()]))
        self.assertCode("project_proposal_conflict", lambda: self.workspace.apply(
            "claude", "opt-in", 1, [file()], allow_disjoint=True))
        self.assertCode("project_proposal_conflict", lambda: self.workspace.apply(
            "codex", "peer", 1, [file("peer.py")], allow_disjoint=True))
        self.assertTrue(self.workspace.apply("codex", "peer", 1, [file("peer.py")], allow_disjoint=False)["replayed"])
        for invalid in (None, 0, 1, "true"):
            self.assertCode("invalid_project_request", lambda: self.workspace.apply(
                "claude", "bad-flag", 2, [file("extra.py")], allow_disjoint=invalid))

    def test_disjoint_merge_refuses_missing_corrupt_or_linked_original_base(self):
        self.workspace.apply("claude", "peer", 0, [file("peer.py")])
        manifest = json.loads((self.root / self.room / "manifest.json").read_text())
        base_path = self.root / self.room / "revisions" / (manifest["commits"][0]["snapshot_id"] + ".json")
        saved = base_path.read_bytes()
        base_path.unlink()
        self.assertCode("project_storage_unavailable", lambda: self.workspace.apply(
            "codex", "missing-base", 0, [file("test_peer.py")], allow_disjoint=True))
        base_path.write_bytes(b"corrupt")
        base_path.chmod(0o600)
        self.assertCode("project_corrupt", lambda: self.workspace.apply(
            "codex", "corrupt-base", 0, [file("test_peer.py")], allow_disjoint=True))
        base_path.unlink()
        outside = self.temporary / "outside-snapshot.json"
        outside.write_bytes(saved)
        outside.chmod(0o600)
        base_path.symlink_to(outside)
        self.assertCode("project_unsafe_path", lambda: self.workspace.apply(
            "codex", "linked-base", 0, [file("test_peer.py")], allow_disjoint=True))
        self.assertEqual(outside.read_bytes(), saved)
        self.assertEqual(self.workspace.snapshot()["revision"], 1)

    def test_disjoint_replay_requires_the_original_immutable_base(self):
        self.workspace.apply("claude", "original", 0, [file()], allow_disjoint=True)
        manifest = json.loads((self.root / self.room / "manifest.json").read_text())
        base_path = self.root / self.room / "revisions" / (manifest["commits"][0]["snapshot_id"] + ".json")
        base_path.unlink()
        self.assertCode("project_storage_unavailable", lambda: self.workspace.apply(
            "claude", "original", 0, [file()], allow_disjoint=True))
        self.assertEqual(self.workspace.snapshot()["revision"], 1)

    def test_disjoint_merge_rejects_deleted_recreated_changed_or_reowned_paths(self):
        initial = self.workspace.apply("claude", "first", 0, [file()])
        original = initial["files"][0]
        # These histories cannot be authored by apply; simulate storage from an
        # incompatible writer so a matching head cannot hide an intermediate ABA.
        for change in (None, {**original, "owner": "codex"},
                       {**original, "content": "VALUE = 2\n", "sha256": hashlib.sha256(b"VALUE = 2\n").hexdigest()}):
            with self.subTest(change=change):
                room_id = str(uuid4())
                workspace = ProjectWorkspace(self.root, room_id)
                workspace.apply("claude", "first", 0, [file()])
                with workspace._locked() as (room, revisions, _):
                    manifest = workspace._manifest(room)
                    workspace._publish(room, revisions, manifest, [] if change is None else [change],
                                       "incompatible-change", "0" * 64)
                    workspace._publish(room, revisions, workspace._manifest(room), [original],
                                       "incompatible-restore", "1" * 64)
                before = workspace.snapshot()
                self.assertCode("project_file_conflict", lambda: workspace.apply(
                    "claude", "stale", 1, [file(previous=original["sha256"])], allow_disjoint=True))
                self.assertEqual(workspace.snapshot(), before)

    def test_disjoint_merge_enforces_combined_capacity_without_partial_publication(self):
        self.workspace.apply("claude", "first", 0, [file("left.py", "x" * 32769)])
        self.assertCode("project_limit_exceeded", lambda: self.workspace.apply(
            "codex", "too-large", 0, [file("right.py", "x" * 32768)], allow_disjoint=True))
        self.assertEqual(self.workspace.snapshot()["revision"], 1)
        self.assertEqual([item["path"] for item in self.workspace.snapshot()["files"]], ["left.py"])

    def test_concurrent_initialization_creates_one_recognized_empty_workspace(self):
        room = str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            workspaces = list(pool.map(lambda _: ProjectWorkspace(self.root, room), range(2)))
        self.assertEqual([workspace.snapshot() for workspace in workspaces],
                         [{"revision": 0, "files": []}, {"revision": 0, "files": []}])
        self.assertEqual(len(list((self.root / room / "revisions").iterdir())), 1)

    def test_failed_initialization_never_reserves_the_formal_room_directory(self):
        for operation in ("write", "fsync", "replace"):
            with self.subTest(operation=operation):
                room = str(uuid4())
                if operation == "write":
                    target = "local_agent_coordinator.project_workspace.ProjectWorkspace._write_new"
                else:
                    target = "local_agent_coordinator.project_workspace.os." + operation
                with patch(target, side_effect=OSError("temporary failure")):
                    self.assertCode("project_storage_unavailable", lambda: ProjectWorkspace(self.root, room))
                self.assertFalse((self.root / room).exists())
                self.assertFalse(list(self.root.glob(".init-*")))
                reopened = ProjectWorkspace(self.root, room)
                self.assertEqual(reopened.snapshot(), {"revision": 0, "files": []})

    def test_name_collisions_never_clean_up_preexisting_data(self):
        collision = uuid4()
        staging = self.root / (".init-" + str(collision))
        staging.mkdir(mode=0o700)
        sentinel = staging / "keep.txt"
        sentinel.write_text("preexisting data")
        with patch("local_agent_coordinator.project_workspace.uuid4", return_value=collision):
            self.assertCode("project_storage_unavailable", lambda: ProjectWorkspace(self.root, str(uuid4())))
        self.assertEqual(sentinel.read_text(), "preexisting data")
        metadata = json.loads((self.root / self.room / "manifest.json").read_text())
        identifier = metadata["commits"][0]["snapshot_id"]
        original = self.root / self.room / "revisions" / (identifier + ".json")
        saved = original.read_bytes()
        with patch("local_agent_coordinator.project_workspace.uuid4", return_value=UUID(identifier)):
            self.assertCode("project_storage_unavailable", lambda: self.workspace.apply("claude", "collision", 0, [file()]))
        self.assertEqual(original.read_bytes(), saved)
        temporary_id = uuid4()
        temporary = self.root / self.room / (".manifest-" + str(temporary_id))
        temporary.write_text("keep this too")
        temporary.chmod(0o600)
        with patch("local_agent_coordinator.project_workspace.uuid4", side_effect=[uuid4(), temporary_id]):
            self.assertCode("project_storage_unavailable", lambda: self.workspace.apply("claude", "temporary", 0, [file()]))
        self.assertEqual(temporary.read_text(), "keep this too")
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})

    def test_unreferenced_crash_snapshot_never_becomes_a_committed_revision(self):
        orphan = self.root / self.room / "revisions" / (str(uuid4()) + ".json")
        orphan.write_text('{"uncommitted":"not a visible project"}')
        orphan.chmod(0o600)
        reopened = ProjectWorkspace(self.root, self.room)
        self.assertEqual(reopened.snapshot(), {"revision": 0, "files": []})
        self.assertCode("project_revision_not_found", lambda: reopened.snapshot(1))
        self.assertEqual(reopened.apply("helper", "real-commit", 0, [file()])["revision"], 1)
        self.assertEqual(reopened.snapshot()["files"][0]["owner"], "helper")

    def test_lock_really_excludes_a_separate_process(self):
        script = (
            "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR)\n"
            "try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "except BlockingIOError: print('blocked')\n"
            "else: print('unprotected')\n"
            "finally: os.close(fd)\n"
        )
        with self.workspace._locked():
            result = subprocess.run([sys.executable, "-c", script, str(self.root / ("." + self.room + ".lock"))],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "blocked")

    def test_batch_failure_has_no_partial_files_or_owner_claims(self):
        first = self.workspace.apply("claude", "first", 0, [file()])
        before = self.workspace.snapshot()
        self.assertCode("project_owner_conflict", lambda: self.workspace.apply("codex", "mixed", 1, [
            file("another.py"), file(previous=first["files"][0]["sha256"])]))
        self.assertEqual(self.workspace.snapshot(), before)
        self.assertCode("project_file_conflict", lambda: self.workspace.apply(
            "claude", "bad-hash", 1, [file(previous="0" * 64)]))
        self.assertEqual(self.workspace.snapshot(), before)

    def test_publish_failure_rolls_back_and_post_publish_failure_is_explicitly_uncertain(self):
        revision_directory = self.root / self.room / "revisions"
        original_files = set(revision_directory.iterdir())
        with patch("local_agent_coordinator.project_workspace.os.replace", side_effect=OSError("private detail")):
            self.assertCode("project_storage_unavailable", lambda: self.workspace.apply("claude", "before", 0, [file()]))
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})
        self.assertEqual(set(revision_directory.iterdir()), original_files)
        self.assertFalse(list((self.root / self.room).glob(".manifest-*")))
        actual_fsync = os.fsync
        calls = 0

        def fail_last(descriptor):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("private detail")
            return actual_fsync(descriptor)

        changes = [file("a.py"), file("b.py")]
        with patch("local_agent_coordinator.project_workspace.os.fsync", side_effect=fail_last):
            self.assertCode("project_commit_unconfirmed", lambda: self.workspace.apply("claude", "after", 0, changes))
        self.assertEqual(self.workspace.snapshot()["revision"], 1)
        self.assertEqual(len(self.workspace.snapshot()["files"]), 2)
        self.assertTrue(self.workspace.apply("claude", "after", 0, changes)["replayed"])
        actual_replace = os.replace

        def replace_then_fail(*args, **kwargs):
            actual_replace(*args, **kwargs)
            raise OSError("rename result could not be confirmed")

        before = self.workspace.snapshot()
        update = [file("a.py", "VALUE = 3\n", before["files"][0]["sha256"])]
        with patch("local_agent_coordinator.project_workspace.os.replace", side_effect=replace_then_fail):
            self.assertCode("project_commit_unconfirmed", lambda: self.workspace.apply("claude", "rename", 1, update))
        self.assertEqual(self.workspace.snapshot()["revision"], 2)
        self.assertTrue(self.workspace.apply("claude", "rename", 1, update)["replayed"])

    def test_symlinks_hardlinks_and_unknown_existing_directories_are_refused(self):
        outside = self.temporary / "outside"
        outside.mkdir(mode=0o700)
        link = self.temporary / "linked"
        link.symlink_to(outside, target_is_directory=True)
        self.assertCode("project_unsafe_path", lambda: ProjectWorkspace(link, str(uuid4())))
        self.assertEqual(list(outside.iterdir()), [])
        unknown = str(uuid4())
        (self.root / unknown).mkdir(mode=0o700)
        self.assertCode("project_unknown_workspace", lambda: ProjectWorkspace(self.root, unknown))
        self.assertEqual(list((self.root / unknown).iterdir()), [])
        manifest = self.root / self.room / "manifest.json"
        saved = manifest.read_bytes()
        manifest.unlink()
        target = outside / "manifest.json"
        target.write_bytes(saved)
        target.chmod(0o600)
        manifest.symlink_to(target)
        self.assertCode("project_unsafe_path", self.workspace.snapshot)
        manifest.unlink()
        os.link(target, manifest)
        self.assertCode("project_unsafe_path", self.workspace.snapshot)
        self.assertEqual(target.read_bytes(), saved)

    def test_private_permissions_and_revisions_directory_link_escape(self):
        for path in [self.root, self.root / self.room, *(self.root / self.room).rglob("*")]:
            self.assertFalse(os.stat(path).st_mode & 0o077)
        public = self.temporary / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        self.assertCode("project_unsafe_path", lambda: ProjectWorkspace(public, str(uuid4())))
        self.assertEqual(os.stat(public).st_mode & 0o777, 0o755)
        revisions = self.root / self.room / "revisions"
        backup = self.root / self.room / "original-revisions"
        revisions.rename(backup)
        outside = self.temporary / "outside"
        outside.mkdir(mode=0o700)
        revisions.symlink_to(outside, target_is_directory=True)
        self.assertCode("project_unsafe_path", lambda: self.workspace.apply("claude", "escape", 0, [file()]))
        self.assertEqual(list(outside.iterdir()), [])

    def test_corrupted_revision_content_is_not_returned_or_used_for_a_commit(self):
        applied = self.workspace.apply("claude", "first", 0, [file()])
        manifest = json.loads((self.root / self.room / "manifest.json").read_text())
        path = self.root / self.room / "revisions" / (manifest["commits"][1]["snapshot_id"] + ".json")
        value = json.loads(path.read_text())
        value["files"][0]["content"] = "tampered"
        path.write_text(json.dumps(value))
        self.assertCode("project_corrupt", self.workspace.snapshot)
        self.assertCode("project_corrupt", lambda: self.workspace.apply(
            "claude", "second", 1, [file(previous=applied["files"][0]["sha256"])]))
        self.assertEqual(self.workspace.snapshot(0), {"revision": 0, "files": []})

    def test_file_limits_flat_paths_unicode_bounds_and_no_delete_or_transfer(self):
        invalid = [file("../main.py"), file("/main.py"), file(".hidden.py"), file("dir/main.py"),
                   file("main.js"), file("é.py"), file(content="\ud800"), file(content="\0"),
                   file(content=None), file(previous="not-a-hash")]
        for change in invalid:
            self.assertCode("invalid_project_files", lambda change=change: self.workspace.apply("claude", "bad", 0, [change]))
        self.assertCode("invalid_project_files", lambda: self.workspace.apply("claude", "duplicates", 0, [file(), file("Main.py")]))
        self.assertCode("invalid_project_files", lambda: self.workspace.apply("claude", "too-many", 0,
                                                                             [file(f"f{n}.py") for n in range(17)]))
        self.assertCode("project_limit_exceeded", lambda: self.workspace.apply("claude", "bytes", 0, [file(content="x" * 65537)]))
        self.assertCode("invalid_project_request", lambda: self.workspace.apply("service", "bad", 0, [file()]))
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})
        changes = [file(f"f{n}.py", "x" * 4096) for n in range(16)]
        applied = self.workspace.apply("claude", "full", 0, changes)
        self.assertEqual(len(applied["files"]), 16)
        self.assertCode("project_limit_exceeded", lambda: self.workspace.apply("claude", "overflow", 1, [file("extra.py")]))
        self.assertEqual(self.workspace.snapshot()["revision"], 1)

    def python_seed(self):
        source_room = str(uuid4())
        source = ProjectWorkspace(self.root, source_room)
        source.apply("claude", "implementation", 0, [file("worker.py", "VALUE = 1\n")])
        source.apply("codex", "tests", 1, [file("test_worker.py", "import worker\n")])
        return source_room, source.snapshot()

    def test_python_seed_preserves_owners_source_and_input_then_replays_original_revision(self):
        source_room, snapshot = self.python_seed()
        before = copy.deepcopy(snapshot)
        seeded = self.workspace.seed_python(snapshot, source_room_id=source_room,
                                            allowed_owners=("claude", "codex"))
        self.assertEqual(seeded, {"revision": 1, "files": snapshot["files"], "replayed": False})
        self.assertEqual(snapshot, before)
        self.assertEqual(ProjectWorkspace(self.root, source_room, create=False).snapshot(), before)
        item = next(item for item in seeded["files"] if item["path"] == "worker.py")
        self.assertCode("project_owner_conflict", lambda: self.workspace.apply(
            "codex", "steal", 1, [file("worker.py", "VALUE = 2\n", item["sha256"])]))
        current = self.workspace.apply("claude", "edit", 1,
                                       [file("worker.py", "VALUE = 2\n", item["sha256"])])
        reopened = ProjectWorkspace(self.root, self.room, create=False)
        snapshot["files"].reverse()
        replayed = reopened.seed_python(snapshot, source_room_id=source_room,
                                        allowed_owners=["codex", "claude"])
        self.assertEqual(replayed, {**seeded, "replayed": True})
        self.assertEqual(reopened.snapshot(), {key: current[key] for key in ("revision", "files")})
        manifest = json.loads((self.root / self.room / "manifest.json").read_text())
        self.assertEqual(manifest["commits"][1]["proposal_id"], "python-import:" + source_room)

    def test_python_seed_rejects_invalid_source_schema_and_owner_allowlist_before_writing(self):
        source_room, snapshot = self.python_seed()
        invalid_sources = [None, uuid4(), self.room, "not-a-uuid",
                           "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"]
        for source in invalid_sources:
            with self.subTest(source=source):
                self.assertCode("invalid_project_request", lambda: self.workspace.seed_python(
                    snapshot, source_room_id=source, allowed_owners=["claude", "codex"]))
        for owners in ([], "claude", ["claude", "claude"], ["service"], [["claude"]],
                       ["p" + str(n) for n in range(9)]):
            with self.subTest(owners=owners):
                self.assertCode("invalid_project_request", lambda: self.workspace.seed_python(
                    snapshot, source_room_id=source_room, allowed_owners=owners))
        invalid_snapshots = [None, str(self.root), {}, {**snapshot, "path": "/tmp/untrusted"}]
        invalid_snapshots.extend({**snapshot, "revision": value} for value in (True, 0, -1, "1", 1.5))
        for value in invalid_snapshots:
            with self.subTest(snapshot=value):
                self.assertCode("invalid_project_request", lambda: self.workspace.seed_python(
                    value, source_room_id=source_room, allowed_owners=["claude", "codex"]))
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})

    def test_python_seed_rejects_bad_hash_path_unknown_owner_and_duplicate_files(self):
        source_room, snapshot = self.python_seed()
        invalid = [None, {}, {**snapshot["files"][0], "extra": 1}]
        for key, values in {
            "path": ("../x.py", "/x.py", "dir/x.py", ".hidden.py", "x.js", "é.py", None),
            "sha256": (None, "bad", "0" * 64),
            "owner": (None, "unknown", "service", ["claude"]),
            "content": (None, "\0", "\ud800"),
        }.items():
            invalid.extend({**snapshot["files"][0], key: value} for value in values)
        for item in invalid:
            with self.subTest(item=item):
                self.assertCode("invalid_project_files", lambda: self.workspace.seed_python(
                    {"revision": 1, "files": [item]}, source_room_id=source_room,
                    allowed_owners=["claude", "codex"]))
        first = snapshot["files"][0]
        duplicates = [first, {**first, "path": first["path"][:-3].upper() + ".py"}]
        for files in (None, [], duplicates):
            self.assertCode("invalid_project_files", lambda: self.workspace.seed_python(
                {"revision": 1, "files": files}, source_room_id=source_room,
                allowed_owners=["claude", "codex"]))
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})

    def test_python_seed_file_and_byte_limits_do_not_publish_partial_imports(self):
        source_room = str(uuid4())

        def captured(path, content):
            return {"path": path, "content": content, "owner": "claude",
                    "sha256": hashlib.sha256(content.encode()).hexdigest()}

        for files, code in (([captured(f"f{n}.py", "") for n in range(17)], "invalid_project_files"),
                            ([captured("a.py", "x" * 65537)], "project_limit_exceeded"),
                            ([captured("a.py", "x" * 32768), captured("b.py", "x" * 32769)],
                             "project_limit_exceeded")):
            self.assertCode(code, lambda: self.workspace.seed_python(
                {"revision": 1, "files": files}, source_room_id=source_room, allowed_owners=["claude"]))
            self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})
        full = [captured(f"f{n}.py", "x" * 4096) for n in range(16)]
        result = self.workspace.seed_python({"revision": 10, "files": full},
                                            source_room_id=source_room, allowed_owners=["claude"])
        self.assertEqual(len(result["files"]), 16)
        self.assertEqual(result["revision"], 1)

    def test_python_seed_conflicts_on_changed_source_revision_content_or_owner(self):
        source_room, snapshot = self.python_seed()
        self.workspace.seed_python(snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"])
        changed_content = copy.deepcopy(snapshot)
        changed_content["files"][0]["content"] = "VALUE = 99\n"
        changed_content["files"][0]["sha256"] = hashlib.sha256(b"VALUE = 99\n").hexdigest()
        changed_owner = copy.deepcopy(snapshot)
        changed_owner["files"][0]["owner"] = "claude"
        for source, value in ((str(uuid4()), snapshot), (source_room, {**snapshot, "revision": 3}),
                              (source_room, changed_content), (source_room, changed_owner)):
            self.assertCode("project_revision_conflict", lambda: self.workspace.seed_python(
                value, source_room_id=source, allowed_owners=["claude", "codex"]))
        self.assertEqual(self.workspace.snapshot(), {"revision": 1, "files": snapshot["files"]})

    def test_python_seed_refuses_existing_authored_target_and_text_workspace(self):
        source_room, snapshot = self.python_seed()
        before = self.workspace.apply("claude", "already-authored", 0, [file()])
        self.assertCode("project_revision_conflict", lambda: self.workspace.seed_python(
            snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"]))
        self.assertEqual(self.workspace.snapshot()["files"], before["files"])
        text = ProjectWorkspace(self.root, str(uuid4()), kind="text")
        self.assertCode("invalid_project_request", lambda: text.seed_python(
            snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"]))

    def test_python_seed_race_is_atomic_and_same_source_replays_once(self):
        source_room, snapshot = self.python_seed()
        other = ProjectWorkspace(self.root, self.room)
        results = self.race([
            lambda: self.workspace.seed_python(snapshot, source_room_id=source_room,
                                               allowed_owners=["claude", "codex"]),
            lambda: other.seed_python(snapshot, source_room_id=source_room,
                                      allowed_owners=["codex", "claude"]),
        ])
        self.assertTrue(all(isinstance(result, dict) for result in results))
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(self.workspace.snapshot(), {"revision": 1, "files": snapshot["files"]})
        self.assertEqual(len(list((self.root / self.room / "revisions").iterdir())), 2)
        destination = str(uuid4())
        left = ProjectWorkspace(self.root, destination)
        right = ProjectWorkspace(self.root, destination)
        results = self.race([
            lambda: left.seed_python(snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"]),
            lambda: right.seed_python(snapshot, source_room_id=str(uuid4()), allowed_owners=["claude", "codex"]),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(results.count("project_revision_conflict"), 1)
        self.assertEqual(left.snapshot(), {"revision": 1, "files": snapshot["files"]})

    def test_python_seed_replay_detects_corrupt_committed_snapshot(self):
        source_room, snapshot = self.python_seed()
        self.workspace.seed_python(snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"])
        manifest_path = self.root / self.room / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        revision_path = self.root / self.room / "revisions" / (manifest["commits"][1]["snapshot_id"] + ".json")
        value = json.loads(revision_path.read_text())
        value["files"][0]["content"] = "CHANGED = True\n"
        value["files"][0]["sha256"] = hashlib.sha256(b"CHANGED = True\n").hexdigest()
        encoded = json.dumps(value).encode()
        revision_path.write_bytes(encoded)
        manifest["commits"][1]["sha256"] = hashlib.sha256(encoded).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        self.assertCode("project_corrupt", lambda: self.workspace.seed_python(
            snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"]))

    def test_python_seed_publish_failure_is_atomic_and_uncertain_publication_replays(self):
        source_room, snapshot = self.python_seed()
        revisions = self.root / self.room / "revisions"
        before = set(revisions.iterdir())
        with patch("local_agent_coordinator.project_workspace.os.replace", side_effect=OSError("not published")):
            self.assertCode("project_storage_unavailable", lambda: self.workspace.seed_python(
                snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"]))
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": []})
        self.assertEqual(set(revisions.iterdir()), before)
        actual_replace = os.replace

        def replace_then_fail(*args, **kwargs):
            actual_replace(*args, **kwargs)
            raise OSError("publication result unknown")

        with patch("local_agent_coordinator.project_workspace.os.replace", side_effect=replace_then_fail):
            self.assertCode("project_commit_unconfirmed", lambda: self.workspace.seed_python(
                snapshot, source_room_id=source_room, allowed_owners=["claude", "codex"]))
        replayed = self.workspace.seed_python(snapshot, source_room_id=source_room,
                                             allowed_owners=["claude", "codex"])
        self.assertEqual(replayed, {"revision": 1, "files": snapshot["files"], "replayed": True})


class TextProjectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        from local_agent_coordinator.project_catalog import ProjectCatalog
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        (self.source / "src").mkdir(parents=True)
        (self.source / "src" / "score.ts").write_text("export const score = 1;\n")
        (self.source / "README.md").write_text("# Example\n")
        self.catalog = ProjectCatalog(self.base / "catalog")
        self.metadata = self.catalog.register("Example", str(self.source), ["src/score.ts", "README.md"])
        self.capture = self.catalog.capture(self.metadata["id"])
        self.room = str(uuid4())
        self.root = self.base / "workspaces"
        self.workspace = ProjectWorkspace(self.root, self.room, kind="text")

    def assertCode(self, code, operation):
        with self.assertRaises(ProjectWorkspaceError) as error:
            operation()
        self.assertEqual(error.exception.code, code)

    def test_seed_imports_immutable_baseline_and_reopen_keeps_kind_and_source(self):
        self.assertEqual(self.workspace.snapshot(), {"revision": 0, "files": [], "source": None, "changes": []})
        seeded = self.workspace.seed(self.capture, "codex")
        self.assertEqual(seeded["revision"], 1)
        self.assertEqual(seeded["changes"], [])
        self.assertEqual({item["owner"] for item in seeded["files"]}, {"codex"})
        self.assertEqual(seeded["source"]["source_hash"], self.capture["source_hash"])
        replay = self.workspace.seed(self.capture, "codex")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["revision"], 1)
        reopened = ProjectWorkspace(self.root, self.room, create=False, kind="text")
        self.assertEqual(reopened.snapshot(), {key: seeded[key] for key in ("revision", "files", "source", "changes")})
        manifest = json.loads((self.root / self.room / "manifest.json").read_text())
        self.assertEqual(manifest["kind"], "text")
        self.assertCode("project_corrupt", lambda: ProjectWorkspace(self.root, self.room))

    def test_owner_cas_nested_files_diffs_and_original_source_protection(self):
        seeded = self.workspace.seed(self.capture, "codex")
        previous = next(item for item in seeded["files"] if item["path"] == "src/score.ts")
        self.assertCode("project_owner_conflict", lambda: self.workspace.apply("claude", "steal", 1, [
            file("src/score.ts", "export const score = 2;\n", previous["sha256"])]))
        changed = self.workspace.apply("codex", "change", 1, [
            file("src/score.ts", "export const score = 2;\n", previous["sha256"])])
        self.assertEqual(len(changed["changes"]), 1)
        diff = changed["changes"][0]
        self.assertEqual(diff["status"], "modified")
        self.assertIn("-export const score = 1;", diff["diff"])
        self.assertIn("+export const score = 2;", diff["diff"])
        reviewed = self.workspace.apply("claude", "tests", 2, [file("tests/score.test.ts", "// reviewed\n")])
        self.assertEqual([item["status"] for item in reviewed["changes"]], ["modified", "added"])
        self.assertEqual((self.source / "src" / "score.ts").read_text(), "export const score = 1;\n")
        self.assertFalse((self.source / "tests").exists())
        self.assertEqual(self.workspace.snapshot(1)["changes"], [])
        self.assertEqual(self.workspace.snapshot(2)["changes"], changed["changes"])
        replayed = self.workspace.apply("codex", "change", 1, [
            file("src/score.ts", "export const score = 2;\n", previous["sha256"])])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["revision"], 2)
        self.assertEqual(replayed["changes"], changed["changes"])

    def test_disjoint_text_merge_preserves_source_baseline_owner_and_both_changes(self):
        seeded = self.workspace.seed(self.capture, "codex")
        source_file = next(item for item in seeded["files"] if item["path"] == "src/score.ts")
        self.workspace.apply("codex", "source-change", 1, [
            file("src/score.ts", "export const score = 2;\n", source_file["sha256"])])
        merged = self.workspace.apply("claude", "independent-test", 1, [
            file("tests/score.test.ts", "// EXPECTED = 2\n")], allow_disjoint=True)
        self.assertEqual(merged["revision"], 3)
        self.assertEqual(merged["source"], seeded["source"])
        self.assertEqual([(item["path"], item["status"]) for item in merged["changes"]],
                         [("src/score.ts", "modified"), ("tests/score.test.ts", "added")])
        self.assertEqual({item["path"]: item["owner"] for item in merged["files"]},
                         {"README.md": "codex", "src/score.ts": "codex", "tests/score.test.ts": "claude"})
        self.assertEqual((self.source / "src" / "score.ts").read_text(), "export const score = 1;\n")
        self.assertFalse((self.source / "tests").exists())

    def test_source_owner_assignment_changes_only_selected_logical_owners_and_replays(self):
        seeded = self.workspace.seed(self.capture, "claude")
        source_before = {path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        assigned = self.workspace.assign_source_owners({"src/score.ts": "codex"},
                                                       allowed_owners=["claude", "codex"])
        self.assertEqual(assigned["revision"], 2)
        self.assertFalse(assigned["replayed"])
        self.assertEqual(assigned["changes"], [])
        self.assertEqual(assigned["source"], seeded["source"])
        expected = [{**item, "owner": "codex" if item["path"] == "src/score.ts" else "claude"}
                    for item in seeded["files"]]
        self.assertEqual(assigned["files"], expected)
        self.assertEqual(self.workspace.snapshot(1)["files"], seeded["files"])
        self.assertEqual({path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()}, source_before)
        replayed = ProjectWorkspace(self.root, self.room, create=False, kind="text").assign_source_owners(
            {"src/score.ts": "codex"}, allowed_owners=("codex", "claude"))
        self.assertEqual(replayed, {**assigned, "replayed": True})
        source_file = next(item for item in assigned["files"] if item["path"] == "src/score.ts")
        self.assertCode("project_owner_conflict", lambda: self.workspace.apply("claude", "old-owner", 2, [
            file("src/score.ts", "export const score = 3;\n", source_file["sha256"])]))
        current = self.workspace.apply("codex", "new-owner", 2, [
            file("src/score.ts", "export const score = 3;\n", source_file["sha256"])])
        self.assertEqual(current["revision"], 3)
        self.assertEqual(self.workspace.assign_source_owners({"src/score.ts": "codex"},
                         allowed_owners=["claude", "codex"]), {**assigned, "replayed": True})
        self.assertCode("project_proposal_conflict", lambda: self.workspace.assign_source_owners(
            {"README.md": "codex"}, allowed_owners=["claude", "codex"]))

    def test_source_owner_assignment_rejects_unseeded_modified_nontext_and_invalid_scope(self):
        self.assertCode("project_not_seeded", lambda: self.workspace.assign_source_owners(
            {"README.md": "codex"}, allowed_owners=["claude", "codex"]))
        self.workspace.seed(self.capture, "claude")
        for mapping, owners, code in (
            ({}, ["claude"], "invalid_project_request"),
            ({"README.md": "codex"}, ["claude"], "invalid_project_request"),
            ({"README.md": "service"}, ["claude", "codex"], "invalid_project_request"),
            ({"README.md": "codex"}, ["codex", "codex"], "invalid_project_request"),
            ({"README.md": "codex"}, "codex", "invalid_project_request"),
            ({"README.md": "codex"}, [["codex"]], "invalid_project_request"),
            ({"new.txt": "codex"}, ["codex"], "invalid_project_files"),
            ({"../README.md": "codex"}, ["codex"], "invalid_project_files"),
            ({"readme.md": "codex"}, ["codex"], "invalid_project_files"),
        ):
            with self.subTest(mapping=mapping, owners=owners):
                self.assertCode(code, lambda: self.workspace.assign_source_owners(mapping, allowed_owners=owners))
                self.assertEqual(self.workspace.snapshot()["revision"], 1)
        self.workspace.apply("claude", "authored", 1, [file("new.txt", "A draft\n")])
        self.assertCode("project_revision_conflict", lambda: self.workspace.assign_source_owners(
            {"README.md": "codex"}, allowed_owners=["claude", "codex"]))
        python = ProjectWorkspace(self.root, str(uuid4()))
        self.assertCode("invalid_project_request", lambda: python.assign_source_owners(
            {"main.py": "codex"}, allowed_owners=["codex"]))

    def test_source_owner_assignment_is_atomic_across_instances(self):
        self.workspace.seed(self.capture, "claude")
        peer = ProjectWorkspace(self.root, self.room, create=False, kind="text")
        barrier = threading.Barrier(2)

        def assign(workspace):
            barrier.wait(timeout=2)
            return workspace.assign_source_owners({"README.md": "codex"}, allowed_owners=["claude", "codex"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(assign, [self.workspace, peer]))
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(self.workspace.snapshot()["revision"], 2)
        self.assertEqual(len(list((self.root / self.room / "revisions").iterdir())), 3)

    def test_source_owner_assignment_publish_failure_preserves_original_owners(self):
        seeded = self.workspace.seed(self.capture, "claude")
        before = self.workspace.snapshot()
        with patch("local_agent_coordinator.project_workspace.os.replace", side_effect=OSError("not published")):
            self.assertCode("project_storage_unavailable", lambda: self.workspace.assign_source_owners(
                {"README.md": "codex"}, allowed_owners=["claude", "codex"]))
        self.assertEqual(self.workspace.snapshot(), before)
        self.assertEqual(before["files"], seeded["files"])
        assigned = self.workspace.assign_source_owners({"README.md": "codex"}, allowed_owners=["claude", "codex"])
        self.assertEqual(assigned["revision"], 2)

    def test_source_owner_assignment_invalidates_a_stale_previous_owner_proposal(self):
        seeded = self.workspace.seed(self.capture, "claude")
        previous = next(item for item in seeded["files"] if item["path"] == "src/score.ts")
        self.workspace.assign_source_owners({"src/score.ts": "codex"}, allowed_owners=["claude", "codex"])
        self.assertCode("project_file_conflict", lambda: self.workspace.apply("claude", "stale-owner", 1, [
            file("src/score.ts", "export const score = 9;\n", previous["sha256"])], allow_disjoint=True))
        self.assertEqual(self.workspace.snapshot()["revision"], 2)

    def test_seed_conflicts_and_invalid_captures_do_not_reserve_paths(self):
        self.assertCode("project_not_seeded", lambda: self.workspace.apply("codex", "early", 0, [file()]))
        for modify in [lambda value: value.update(source_hash="0" * 64),
                       lambda value: value.update(id=None),
                       lambda value: value["files"][0].update(sha256="0" * 64),
                       lambda value: value["files"][0].update(content="changed"),
                       lambda value: value.update(files=[]),
                       lambda value: value["files"].append(None)]:
            capture = json.loads(json.dumps(self.capture))
            modify(capture)
            self.assertCode("invalid_project_files", lambda: self.workspace.seed(capture, "codex"))
        self.assertEqual(self.workspace.snapshot()["revision"], 0)
        self.workspace.seed(self.capture, "codex")
        self.assertCode("project_revision_conflict", lambda: self.workspace.seed(self.capture, "claude"))
        self.assertCode("invalid_project_request", lambda: ProjectWorkspace(self.root, str(uuid4())).seed(self.capture, "codex"))

    def test_text_mode_rejects_hidden_binary_sensitive_and_oversized_proposals(self):
        self.workspace.seed(self.capture, "codex")
        for path in ["../file.ts", "/file.ts", ".env", ".git/config", "secret.json", "picture.png", "src//x.ts"]:
            self.assertCode("invalid_project_files", lambda: self.workspace.apply("codex", "bad", 1, [file(path)]))
        self.assertCode("project_sensitive_content", lambda: self.workspace.apply(
            "codex", "secret", 1, [file("data.txt", "api_key = 'private-value'")]))
        self.assertCode("project_limit_exceeded", lambda: self.workspace.apply(
            "codex", "large", 1, [file("data.txt", "x" * 16385)]))
        self.assertEqual(self.workspace.snapshot()["revision"], 1)

    def test_snapshot_kind_and_baseline_hash_corruption_are_not_adopted(self):
        self.workspace.seed(self.capture, "codex")
        manifest_path = self.root / self.room / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        commit = manifest["commits"][1]
        snapshot_path = self.root / self.room / "revisions" / (commit["snapshot_id"] + ".json")
        original = snapshot_path.read_bytes()
        value = json.loads(original)
        value["kind"] = "python"
        snapshot_path.write_text(json.dumps(value))
        manifest["commits"][1]["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        self.assertCode("project_corrupt", self.workspace.snapshot)
        self.assertCode("project_corrupt", lambda: self.workspace.apply("codex", "bad", 1, [file("new.py")]))
        value = json.loads(original)
        value["source"]["source_hash"] = "0" * 64
        snapshot_path.write_text(json.dumps(value))
        manifest["commits"][1]["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        self.assertCode("project_corrupt", self.workspace.snapshot)

    def test_old_python_history_schema_and_validation_remain_unchanged(self):
        python_room = str(uuid4())
        workspace = ProjectWorkspace(self.root, python_room)
        self.assertEqual(workspace.snapshot(), {"revision": 0, "files": []})
        workspace.apply("codex", "first", 0, [file()])
        self.assertCode("invalid_project_files", lambda: workspace.apply("codex", "nested", 1, [file("src/x.py")]))
        self.assertCode("project_corrupt", lambda: ProjectWorkspace(self.root, python_room, kind="text"))
        self.assertEqual(ProjectWorkspace(self.root, python_room).snapshot()["revision"], 1)


if __name__ == "__main__":
    unittest.main()
