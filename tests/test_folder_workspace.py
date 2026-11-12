from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.folder_workspace import FolderWorkspace, validate_folder_path
from local_agent_coordinator.project_catalog import ProjectCatalog
from local_agent_coordinator.project_workspace import ProjectWorkspaceError


class FolderWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.catalog = ProjectCatalog(self.base / "catalog")
        self.registration = self.catalog.register_folder("示例项目", str(self.source))
        self.root = self.base / "private"
        self.room_id = str(uuid4())
        self.workspace = FolderWorkspace(self.root, self.room_id, create=True)

    def assertCode(self, code, operation):
        with self.assertRaises(ProjectWorkspaceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def seed(self):
        return self.workspace.seed_folder(self.registration)

    def inspect(self, kind="list", path="", query=None, offset=0, limit=200, revision=None):
        return self.workspace.inspect({"kind": kind, "path": path, "query": query,
                                       "offset": offset, "limit": limit}, revision=revision)

    def proposal(self, path, content, revision=None):
        entries = self.inspect(revision=revision)["entries"]
        previous = next((item["sha256"] for item in entries if item["path"] == path), None)
        return {"path": path, "previous_sha256": previous, "content": content}

    def test_directory_over_twelve_files_unicode_large_text_and_binary_remains_source_read_only(self):
        for number in range(20):
            (self.source / f"file{number}.txt").write_text(f"item {number}\n", encoding="utf-8")
        nested = self.source / "中文 目录"
        nested.mkdir()
        original = "一行文本\n" * 4000
        (nested / "正文.txt").write_text(original, encoding="utf-8")
        (self.source / "asset.bin").write_bytes(b"\0\x01\xff")
        snapshot = self.seed()
        self.assertEqual(snapshot["total_files"], 22)
        self.assertEqual(snapshot["files"], [])
        self.assertNotIn(original, str(snapshot))
        names = {entry["path"]: entry for entry in self.inspect()["entries"]}
        self.assertFalse(names["asset.bin"]["text"])
        self.assertEqual(self.workspace.files_for_paths(["中文 目录/正文.txt"])[0]["content"], original)
        self.assertEqual(validate_folder_path("中文 目录/正文.txt"), "中文 目录/正文.txt")
        self.assertCode("project_binary_file", lambda: self.inspect("read", "asset.bin"))
        result = self.workspace.apply("codex", "change", snapshot["revision"],
                                      [self.proposal("中文 目录/正文.txt", "改稿\n")])
        self.assertEqual(result["files"][0]["content"], "改稿\n")
        self.assertEqual((nested / "正文.txt").read_text(encoding="utf-8"), original)
        self.assertEqual(self.workspace.files_for_paths(["中文 目录/正文.txt"], snapshot["revision"])[0]["content"], original)

    def test_nested_ignore_fixed_exclusions_secrets_and_symlinks_report_reasons(self):
        (self.source / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
        (self.source / "omit.log").write_text("omit")
        (self.source / "keep.log").write_text("keep")
        nested = self.source / "nested"
        nested.mkdir()
        (nested / ".gitignore").write_text("*.tmp\n!keep.tmp\n")
        (nested / "omit.tmp").write_text("omit")
        (nested / "keep.tmp").write_text("keep")
        for directory in (".git", "node_modules", "runtime", "build"):
            (self.source / directory).mkdir()
            (self.source / directory / "do-not-read.txt").write_text("ignored")
        (self.source / ".env").write_text("PRIVATE_VALUE")
        (self.source / "plain.txt").write_text("token = sk-" + "a" * 30)
        (self.source / "link.txt").symlink_to(self.base / "outside.txt")
        (self.base / "outside.txt").write_text("OUTSIDE")
        snapshot = self.seed()
        paths = {item["path"] for item in self.inspect()["entries"]}
        self.assertEqual(paths, {".gitignore", "keep.log", "nested/.gitignore", "nested/keep.tmp"})
        self.assertEqual(snapshot["excluded"], {"excluded_directory": 4, "gitignore": 2,
                                                 "sensitive_content": 1, "sensitive_path": 1, "symlink": 1})
        self.assertNotIn("PRIVATE_VALUE", str(snapshot))
        self.assertNotIn("OUTSIDE", str(snapshot))

    def test_seed_is_idempotent_without_recapturing_source(self):
        (self.source / "main.txt").write_text("original")
        first = self.seed()
        (self.source / "main.txt").write_text("later")
        with patch.object(self.workspace, "_capture_tree", side_effect=AssertionError("no recapture")):
            again = self.seed()
        self.assertTrue(again.pop("replayed"))
        self.assertEqual(again, first)
        reopened = FolderWorkspace(self.root, self.room_id)
        self.assertEqual(reopened.files_for_paths(["main.txt"])[0]["content"], "original")

    def test_capture_cancel_and_resource_limits_leave_no_published_prefix(self):
        (self.source / "a.txt").write_text("a")
        (self.source / "b.txt").write_text("b")
        self.assertCode("project_capture_cancelled", lambda: self.workspace.seed_folder(
            self.registration, cancelled=lambda: True))
        self.assertEqual(self.workspace.snapshot()["revision"], 0)
        with patch("local_agent_coordinator.folder_workspace.MAX_SOURCE_FILES", 1):
            self.assertCode("project_capture_limit", self.seed)
        self.assertEqual(self.workspace.snapshot()["total_files"], 0)
        self.assertEqual(list((self.root / self.room_id).glob(".capture-*")), [])
        self.assertEqual(list((self.root / self.room_id / "blobs").iterdir()), [])
        self.assertEqual(self.seed()["total_files"], 2)

    def test_capture_detects_already_copied_file_mutation(self):
        (self.source / "a.txt").write_text("original")
        (self.source / "z.txt").write_text("last")
        capture = self.workspace._capture_file
        calls = 0

        def mutate(*args):
            nonlocal calls
            result = capture(*args)
            calls += 1
            if calls == 2:
                (self.source / "a.txt").write_text("modified")
            return result

        with patch.object(self.workspace, "_capture_file", side_effect=mutate):
            self.assertCode("project_source_changed", self.seed)
        self.assertEqual(self.workspace.snapshot()["revision"], 0)
        self.assertEqual(list((self.root / self.room_id).glob(".capture-*")), [])

    def test_capture_does_not_hold_workspace_lock_and_cancel_is_observed(self):
        (self.source / "a.txt").write_text("content")
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        capture = self.workspace._capture_file

        def pause(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return capture(*args)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.workspace, "_capture_file", side_effect=pause):
            pending = pool.submit(self.workspace.seed_folder, self.registration, cancelled=cancelled.is_set)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.workspace.snapshot()["revision"], 0)
            finally:
                cancelled.set()
                release.set()
            self.assertCode("project_capture_cancelled", pending.result)

    def test_cancellation_during_blob_promotion_removes_only_unpublished_capture_bytes(self):
        (self.source / "a.txt").write_text("one")
        (self.source / "b.txt").write_text("two")
        cancelled, real_link = threading.Event(), os.link

        def link_then_cancel(source, target, **kwargs):
            result = real_link(source, target, **kwargs)
            if source == target:
                cancelled.set()
            return result

        with patch("local_agent_coordinator.folder_workspace.os.link", side_effect=link_then_cancel):
            self.assertCode("project_capture_cancelled", lambda: self.workspace.seed_folder(
                self.registration, cancelled=cancelled.is_set))
        self.assertEqual(self.workspace.snapshot()["revision"], 0)
        self.assertEqual(list((self.root / self.room_id / "blobs").iterdir()), [])
        self.assertEqual(list((self.root / self.room_id).glob(".capture-*")), [])

    def test_list_read_and_literal_search_are_paged_and_revision_bound(self):
        (self.source / "a.txt").write_text("needle first\nsecond\nneedle third\n")
        (self.source / "b.txt").write_text("needle fourth\n")
        seed = self.seed()
        page = self.inspect(limit=1)
        self.assertEqual(len(page["entries"]), 1)
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual(self.inspect(offset=1, limit=1)["entries"][0]["path"], "b.txt")
        read = self.inspect("read", "a.txt", offset=1, limit=1)
        self.assertEqual(read["content"], "second\n")
        self.assertEqual(read["next_offset"], 2)
        found = self.inspect("search", query="needle", limit=2)
        self.assertEqual([(item["path"], item["line"]) for item in found["matches"]], [("a.txt", 0), ("a.txt", 2)])
        self.assertEqual(found["next_offset"], 2)
        self.assertEqual(self.inspect("search", query="needle", offset=2)["matches"][0]["path"], "b.txt")
        self.workspace.apply("codex", "edit", seed["revision"], [self.proposal("a.txt", "changed")])
        self.assertEqual(self.inspect("read", "a.txt", revision=seed["revision"])["content"], "needle first\nsecond\nneedle third\n")

    def test_long_line_read_is_explicitly_truncated_and_bounded(self):
        (self.source / "large.txt").write_text("中" * 20_000 + "\nnext\n", encoding="utf-8")
        self.seed()
        result = self.inspect("read", "large.txt")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"].encode("utf-8")), 16 * 1024)
        self.assertEqual(result["next_offset"], 0)
        self.assertEqual(self.inspect("read", "large.txt", offset=1)["content"], "next\n")

    # The per-file bound is far larger now; a small one keeps this boundary behaviour tested without huge files.
    @patch("local_agent_coordinator.folder_workspace.MAX_FILE_BYTES", 1024 * 1024)
    def test_megabyte_line_search_fails_explicitly_and_content_corruption_is_refused(self):
        (self.source / "large.txt").write_text("x" * (1024 * 1024 + 10), encoding="utf-8")
        self.seed()
        self.assertCode("project_search_limit_exceeded", lambda: self.inspect("search", query="x"))
        read = self.inspect("read", "large.txt")
        self.assertEqual(len(read["content"]), 16 * 1024)
        self.assertTrue(read["truncated"])
        entry = self.inspect()["entries"][0]
        blob = self.root / self.room_id / "blobs" / entry["sha256"]
        with blob.open("r+b") as stream:
            stream.write(b"y")
        self.assertCode("project_storage_corrupt", lambda: self.inspect("read", "large.txt"))

    def test_total_source_bytes_timeout_and_read_scan_caps_are_explicit(self):
        (self.source / "a.txt").write_text("a" * 20)
        with patch("local_agent_coordinator.folder_workspace.MAX_SOURCE_BYTES", 10):
            self.assertCode("project_capture_limit", self.seed)
        with patch("local_agent_coordinator.folder_workspace.CAPTURE_SECONDS", 0):
            self.assertCode("project_capture_limit", self.seed)
        self.seed()
        with patch("local_agent_coordinator.folder_workspace.MAX_SEARCH_BYTES", 10):
            self.assertCode("project_read_limit_exceeded", lambda: self.inspect("read", "a.txt"))
            self.assertCode("project_search_limit_exceeded", lambda: self.inspect("search", query="a"))

    def test_parallel_disjoint_commits_and_owner_pinning(self):
        (self.source / "a.txt").write_text("a")
        (self.source / "b.txt").write_text("b")
        seed = self.seed()
        proposals = [self.proposal("a.txt", "A"), self.proposal("b.txt", "B")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.workspace.apply, provider, f"proposal-{number}", seed["revision"], [proposal])
                       for number, (provider, proposal) in enumerate(zip(("codex", "claude"), proposals))]
            results = [future.result() for future in futures]
        self.assertEqual({result["revision"] for result in results}, {2, 3})
        self.assertEqual({item["path"]: item["owner"] for item in self.workspace.snapshot()["files"]},
                         {"a.txt": "codex", "b.txt": "claude"})
        revision = self.workspace.snapshot()["revision"]
        self.assertCode("project_owner_conflict", lambda: self.workspace.apply(
            "claude", "peer-write", revision, [self.proposal("a.txt", "not allowed")]))

    def test_aba_and_stale_sha_refused_and_duplicate_proposal_is_exactly_idempotent(self):
        (self.source / "a.txt").write_text("a")
        seed = self.seed()
        first_files = [self.proposal("a.txt", "b")]
        first = self.workspace.apply("codex", "one", seed["revision"], first_files)
        second = self.workspace.apply("codex", "two", first["revision"], [self.proposal("a.txt", "a")])
        self.assertCode("project_file_conflict", lambda: self.workspace.apply(
            "codex", "stale", seed["revision"], [self.proposal("a.txt", "c", seed["revision"])]))
        replay = self.workspace.apply("codex", "one", seed["revision"], first_files)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["revision"], first["revision"])
        self.assertEqual(self.workspace.snapshot()["revision"], second["revision"])
        self.assertCode("project_proposal_conflict", lambda: self.workspace.apply(
            "codex", "one", seed["revision"], [self.proposal("a.txt", "other", seed["revision"])]))

    # The per-file bound is far larger now; a small one keeps this boundary behaviour tested without huge files.
    @patch("local_agent_coordinator.folder_workspace.MAX_FILE_BYTES", 1024 * 1024)
    def test_whole_batch_rejection_for_secret_size_path_and_file_directory_collision(self):
        self.seed()
        for bad, code in (({"path": ".env", "previous_sha256": None, "content": "value"}, "project_sensitive_content"),
                          ({"path": "secret.txt", "previous_sha256": None, "content": "sk-" + "x" * 30}, "project_sensitive_content"),
                          ({"path": "../escape.txt", "previous_sha256": None, "content": "bad"}, "invalid_project_files"),
                          ({"path": "big.txt", "previous_sha256": None, "content": "x" * (1024 * 1024 + 1)}, "project_limit_exceeded")):
            self.assertCode(code, lambda: self.workspace.apply("codex", "bad", 1,
                [{"path": "good.txt", "previous_sha256": None, "content": "good"}, bad]))
            self.assertEqual(self.workspace.snapshot()["revision"], 1)
        self.assertCode("project_file_conflict", lambda: self.workspace.apply("codex", "collision", 1,
            [{"path": "new", "previous_sha256": None, "content": "parent"},
             {"path": "new/child.txt", "previous_sha256": None, "content": "child"}]))
        self.assertEqual(self.workspace.snapshot()["files"], [])

    def test_native_binary_deletion_and_mode_import_replays_exactly(self):
        # Catches binary bytes, deletions or chmod lost on import, or a replay accepting different bytes.
        (self.source / "logo.png").write_bytes(b"\x89PNG\x00old")
        (self.source / "old.py").write_text("old\n")
        (self.source / "run.sh").write_text("echo hi\n")
        seed = self.seed()
        before = {item["path"]: item["sha256"] for item in self.inspect()["entries"]}
        files = [{"path": "logo.png", "previous_sha256": before["logo.png"], "content_base64": "iVBORwBuZXc="},
                 {"path": "art/new.png", "previous_sha256": None, "content_base64": "AAEC"},
                 {"path": "old.py", "previous_sha256": before["old.py"], "deleted": True},
                 {"path": "run.sh", "previous_sha256": before["run.sh"], "content": "echo hi\n", "executable": True}]
        result = self.workspace.apply("codex", "native-one", seed["revision"], files, native=True)
        self.assertEqual(result["deleted_files"], [{"path": "old.py", "sha256": before["old.py"], "owner": "codex"}])
        self.assertEqual({item["path"]: (item["content"], item.get("text")) for item in result["files"]},
                         {"logo.png": (None, False), "art/new.png": (None, False), "run.sh": ("echo hi\n", None)})
        self.assertEqual(self.workspace.raw_files_for_paths(["logo.png", "art/new.png"]),
                         {"logo.png": b"\x89PNG\x00new", "art/new.png": b"\x00\x01\x02"})
        exported = self.workspace.materialize_snapshot(self.base / "export")
        self.assertEqual({item["path"]: item["executable"] for item in exported["files"]},
                         {"art/new.png": None, "logo.png": False, "run.sh": True})
        self.assertFalse((self.base / "export/old.py").exists())
        self.assertTrue(self.workspace.apply("codex", "native-one", seed["revision"], files, native=True)["replayed"])
        changed = [dict(files[0], content_base64="iVBORwBvdGhlcg==")] + files[1:]
        self.assertCode("project_proposal_conflict", lambda: self.workspace.apply(
            "codex", "native-one", seed["revision"], changed, native=True))
        # Re-creating a deleted captured file clears its deletion record.
        again = self.workspace.apply("codex", "native-two", result["revision"],
                                     [{"path": "old.py", "previous_sha256": None, "content": "new\n"}], native=True)
        self.assertNotIn("deleted_files", again)

    def test_secret_looking_text_is_refused_from_a_reply_but_imported_from_native_work(self):
        # Catches the secret filter rejecting code a native partner already wrote (apiKey: config.apiKey).
        seed = self.seed()
        text = "const client = make({ apiKey: config.apiKey });\n"
        files = [{"path": "client.js", "previous_sha256": None, "content": text}]
        self.assertCode("project_sensitive_content", lambda: self.workspace.apply("codex", "reply", seed["revision"], files))
        imported = self.workspace.apply("codex", "native", seed["revision"], files, native=True)
        self.assertEqual(imported["files"][0]["content"], text)
        # A credential path is never stored, native or not.
        self.assertCode("project_sensitive_content", lambda: self.workspace.apply("codex", "env", imported["revision"],
            [{"path": ".env", "previous_sha256": None, "content": "A=1\n"}], native=True))

    def test_withdrawing_a_deletion_restores_the_file_and_its_record(self):
        # Catches a withdrawn task's deletion staying in the shared copy or leaving a stale deletion record.
        (self.source / "old.py").write_text("old\n")
        seed = self.seed()
        previous = self.inspect()["entries"][0]["sha256"]
        removed = self.workspace.apply("codex", "remove", seed["revision"],
                                       [{"path": "old.py", "previous_sha256": previous, "deleted": True}], native=True)
        self.assertEqual([item["path"] for item in removed["deleted_files"]], ["old.py"])
        restored = self.workspace.restore_paths("withdraw-remove", ["old.py"], ["remove"])
        self.assertEqual(restored["paths"], ["old.py"])
        snapshot = self.workspace.snapshot()
        self.assertNotIn("deleted_files", snapshot)
        self.assertEqual(self.workspace.files_for_paths(["old.py"])[0]["content"], "old\n")

    def test_large_existing_file_small_edit_can_commit_and_cumulative_changed_cap_is_atomic(self):
        content = "x" * (300 * 1024)
        (self.source / "large.txt").write_text(content)
        baseline = self.seed()
        modified = "y" + content[1:]
        changed = self.workspace.apply("codex", "large-edit", baseline["revision"],
                                       [self.proposal("large.txt", modified)])
        self.assertEqual(changed["files"][0]["content"], modified)
        with patch("local_agent_coordinator.folder_workspace.MAX_CHANGED_TOTAL_BYTES", len(content) + 3):
            self.assertCode("project_limit_exceeded", lambda: self.workspace.apply("codex", "over-total", changed["revision"],
                [{"path": "new.txt", "previous_sha256": None, "content": "four"}]))
        self.assertEqual(self.workspace.snapshot(), {key: value for key, value in changed.items() if key != "replayed"})

    def test_export_manifest_root_name_is_readable_in_baseline_but_cannot_be_modified_or_created(self):
        (self.source / ".beanest-export.json").write_text("baseline manifest")
        seed = self.seed()
        self.assertEqual(self.inspect("read", ".beanest-export.json")["content"], "baseline manifest")
        for path in (".beanest-export.json", ".BEANEST-EXPORT.JSON", ".Beanest-Export.Json/child.txt"):
            with self.subTest(path=path):
                self.assertCode("invalid_project_files", lambda: self.workspace.apply("codex", "reserved", seed["revision"],
                    [{"path": "good.txt", "previous_sha256": None, "content": "good"},
                     self.proposal(path, "changed")]))
                self.assertEqual(self.workspace.snapshot(), seed)
        nested = self.workspace.apply("codex", "nested", seed["revision"],
            [{"path": "docs/.beanest-export.json", "previous_sha256": None, "content": "nested ordinary file"}])
        self.assertEqual(nested["files"][0]["path"], "docs/.beanest-export.json")

    def test_casefolded_baseline_file_directory_and_batch_byte_cap_conflicts_are_atomic(self):
        (self.source / "existing.txt").write_text("file")
        (self.source / "Directory").mkdir()
        (self.source / "Directory" / "child.txt").write_text("child")
        seed = self.seed()
        for path in ("existing.txt/child.txt", "EXISTING.txt/child.txt", "directory"):
            self.assertCode("project_file_conflict", lambda: self.workspace.apply("codex", "collision", seed["revision"],
                [{"path": path, "previous_sha256": None, "content": "new"}]))
        with patch("local_agent_coordinator.folder_workspace.MAX_CHANGE_BYTES", 5):
            self.assertCode("project_limit_exceeded", lambda: self.workspace.apply("codex", "batch", seed["revision"],
                [{"path": "a.txt", "previous_sha256": None, "content": "aaa"},
                 {"path": "b.txt", "previous_sha256": None, "content": "bbb"}]))
        self.assertEqual(self.workspace.snapshot()["revision"], seed["revision"])

    def test_private_source_hardlink_is_copied_to_an_independent_blob(self):
        original = self.source / "a.txt"
        original.write_text("content")
        os.link(original, self.source / "b.txt")
        self.seed()
        entry = self.inspect()["entries"][0]
        blob = self.root / self.room_id / "blobs" / entry["sha256"]
        self.assertNotEqual(blob.stat().st_ino, original.stat().st_ino)
        self.assertEqual(blob.stat().st_nlink, 1)
        self.assertEqual(blob.stat().st_mode & 0o777, 0o600)

    def test_publish_failure_never_exposes_a_partial_index(self):
        self.seed()
        before = self.workspace.snapshot()
        with patch("local_agent_coordinator.folder_workspace.os.replace", side_effect=OSError("disk unavailable")):
            self.assertCode("project_commit_unconfirmed", lambda: self.workspace.apply("codex", "publish", 1,
                [{"path": "new.txt", "previous_sha256": None, "content": "new"}]))
        self.assertEqual(self.workspace.snapshot(), before)

    def test_reading_after_source_removal_uses_only_private_snapshot(self):
        (self.source / "a.txt").write_text("private copy")
        self.seed()
        (self.source / "a.txt").unlink()
        self.source.rmdir()
        self.assertEqual(self.inspect("read", "a.txt")["content"], "private copy")

    def test_source_comparison_is_exact_revision_and_reads_only_captured_baseline(self):
        path = self.source / "config.txt"
        path.write_text("keep=yes\nport=1234\n", encoding="utf-8")
        seed = self.seed()
        first = self.workspace.apply("codex", "first", seed["revision"],
            [self.proposal("config.txt", "keep=yes\nport=5678\n")])
        self.workspace.apply("codex", "second", first["revision"],
            [self.proposal("config.txt", "keep=yes\nport=9000\n")])
        path.unlink()
        self.source.rmdir()
        with patch("local_agent_coordinator.folder_workspace._open_absolute", side_effect=AssertionError("no source reads")):
            compared = self.workspace.source_comparison("config.txt", revision=first["revision"])
        self.assertTrue(compared["available"])
        self.assertEqual(compared["scope"], "captured_source")
        self.assertEqual(compared["base_revision"], 1)
        self.assertEqual(compared["revision"], first["revision"])
        self.assertEqual(compared["before_sha256"], hashlib.sha256(b"keep=yes\nport=1234\n").hexdigest())
        self.assertEqual(compared["after_sha256"], first["files"][0]["sha256"])
        self.assertIn(" keep=yes\n", compared["diff"])
        self.assertIn("-port=1234\n+port=5678\n", compared["diff"])
        self.assertNotIn("9000", compared["diff"])
        self.assertFalse(compared["truncated"])

    def test_source_comparison_new_file_unchanged_and_no_baseline(self):
        self.assertEqual(self.workspace.source_comparison("new.txt")["reason"], "project_not_seeded")
        (self.source / "old.txt").write_text("same\n")
        seed = self.seed()
        unchanged = self.workspace.source_comparison("old.txt")
        self.assertTrue(unchanged["available"])
        self.assertEqual(unchanged["diff"], "")
        self.assertEqual(unchanged["before_sha256"], unchanged["after_sha256"])
        changed = self.workspace.apply("codex", "new", seed["revision"],
            [{"path": "new.txt", "previous_sha256": None, "content": "created"}])
        compared = self.workspace.source_comparison("new.txt", revision=changed["revision"])
        self.assertTrue(compared["available"])
        self.assertIsNone(compared["before_sha256"])
        self.assertIn("--- /dev/null\n", compared["diff"])
        self.assertIn("+created\n\\ No newline at end of file\n", compared["diff"])
        self.assertEqual(self.workspace.source_comparison("missing.txt")["reason"], "project_file_unavailable")
        self.assertEqual(self.workspace.source_comparison("old.txt", revision=0)["reason"], "project_not_seeded")

    # The per-file bound is far larger now; a small one keeps this boundary behaviour tested without huge files.
    @patch("local_agent_coordinator.folder_workspace.MAX_FILE_BYTES", 1024 * 1024)
    def test_source_comparison_utf8_diff_is_bounded_and_missing_large_binary_corrupt_are_explicit(self):
        (self.source / "text.txt").write_text("原" * 10_000 + "\n", encoding="utf-8")
        (self.source / "large.txt").write_text("x" * (1024 * 1024 + 1))
        (self.source / "binary.bin").write_bytes(b"\x00\xff")
        seed = self.seed()
        self.workspace.apply("codex", "different", seed["revision"],
            [self.proposal("text.txt", "新" * 10_000 + "\n")])
        compared = self.workspace.source_comparison("text.txt")
        self.assertTrue(compared["available"])
        self.assertTrue(compared["truncated"])
        self.assertLessEqual(len(compared["diff"].encode("utf-8")), 16 * 1024)
        self.assertEqual(self.workspace.source_comparison("large.txt")["reason"], "project_limit_exceeded")
        self.assertEqual(self.workspace.source_comparison("binary.bin")["reason"], "project_binary_file")
        baseline_entry = next(item for item in self.inspect()["entries"] if item["path"] == "text.txt")
        blob = self.root / self.room_id / "blobs" / baseline_entry["sha256"]
        with blob.open("r+b") as stream:
            stream.write(b"bad")
        corrupt = self.workspace.source_comparison("text.txt")
        self.assertFalse(corrupt["available"])
        self.assertEqual(corrupt["reason"], "project_storage_corrupt")
        self.assertEqual(corrupt["diff"], "")

    def test_source_comparison_missing_baseline_index_is_explicitly_unavailable(self):
        self.seed()
        self.workspace.apply("codex", "new", 1,
            [{"path": "new.txt", "previous_sha256": None, "content": "new"}])
        room = self.root / self.room_id
        manifest = json.loads((room / "manifest.json").read_text())
        (room / "revisions" / (manifest["commits"][1]["snapshot_id"] + ".json")).unlink()
        compared = self.workspace.source_comparison("new.txt", revision=2)
        self.assertFalse(compared["available"])
        self.assertEqual(compared["reason"], "project_baseline_unavailable")
        self.assertEqual(compared["diff"], "")


if __name__ == "__main__":
    unittest.main()
