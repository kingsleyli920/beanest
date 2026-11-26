"""Metadata polling keeps exact bindings without reloading project text."""
import os
import json
from unittest.mock import patch
import unittest
from uuid import uuid4

from local_agent_coordinator.project_workspace import ProjectWorkspaceError, project_digest
import test_folder_workspace as folders
import test_room_paging as paging


class FolderDisplaySnapshotTests(unittest.TestCase):
    setUp = folders.FolderWorkspaceTests.setUp
    seed = folders.FolderWorkspaceTests.seed

    def edit(self, content):
        current = self.workspace.snapshot()
        prior = self.workspace.metadata_for_paths(["report.txt"])
        return self.workspace.apply("codex", str(uuid4()), current["revision"], [{"path": "report.txt",
            "previous_sha256": prior[0]["sha256"] if prior else None, "content": content}])

    def test_exact_digest_unicode_controls_binary_empty_and_deleted_metadata(self):
        empty, digest = self.workspace.display_snapshot()
        self.assertEqual(digest, project_digest(self.workspace.snapshot()))
        self.assertEqual(empty["files"], [])
        for number in range(105):
            (self.source / f"{number}.txt").write_text("baseline")
        self.seed()
        self.edit('中文 😀 "quoted"\\slash\tline\n\r')
        current = self.workspace.snapshot()
        prior = self.workspace.metadata_for_paths(["1.txt"])[0]
        self.workspace.apply("codex", str(uuid4()), current["revision"], [
            {"path": "binary.bin", "previous_sha256": None, "content_base64": "AP8A"},
            {"path": "empty.txt", "previous_sha256": None, "content": ""},
            {"path": "1.txt", "previous_sha256": prior["sha256"], "deleted": True}], native=True)
        full = self.workspace.snapshot()
        metadata, digest = self.workspace.display_snapshot()
        self.assertEqual(digest, project_digest(full))
        expected = [{**{key: value for key, value in file.items() if key != "content"},
                     "content_available": isinstance(file.get("content"), str)} for file in full["files"]]
        self.assertEqual(metadata, {**full, "files": expected})
        self.assertTrue(metadata["root_entries_truncated"])
        with patch.object(self.workspace, "_raw", side_effect=AssertionError("Warm display reloaded text")):
            self.assertEqual(self.workspace.display_snapshot(), (metadata, digest))

    def test_new_revision_and_owner_metadata_recompute_exact_binding(self):
        self.seed()
        self.edit("one")
        _, old_digest = self.workspace.display_snapshot()
        self.edit("two")
        metadata, digest = self.workspace.display_snapshot()
        self.assertNotEqual(old_digest, digest)
        self.assertEqual(digest, project_digest(self.workspace.snapshot()))
        self.assertEqual(metadata["files"][0]["owner"], "codex")
        entry = metadata["files"][0]
        self.workspace.apply("claude", str(uuid4()), metadata["revision"], [{"path": "report.txt",
            "previous_sha256": entry["sha256"], "content": "two"}], ownership_grants={"report.txt": {
                "old_owner": "codex", "new_owner": "claude", "sha256": entry["sha256"]}})
        transferred, transferred_digest = self.workspace.display_snapshot()
        self.assertEqual(transferred["files"][0]["owner"], "claude")
        self.assertNotEqual(transferred_digest, digest)
        self.assertEqual(transferred_digest, project_digest(self.workspace.snapshot()))

    def test_corrupt_index_does_not_reuse_a_previously_valid_display_digest(self):
        self.seed()
        self.edit("exact bytes")
        self.workspace.display_snapshot()
        room = self.root / self.room_id
        manifest = json.loads((room / "manifest.json").read_text())
        index = room / "revisions" / (manifest["commits"][-1]["snapshot_id"] + ".json")
        index.write_text("{}")
        with self.assertRaises(ProjectWorkspaceError):
            self.workspace.display_snapshot()
        with self.assertRaises(ProjectWorkspaceError):
            self.workspace.snapshot()

    def test_blob_corruption_symlink_and_cold_concurrent_change_never_reuse_cached_digest(self):
        self.seed()
        self.edit("valid text")
        self.workspace.display_snapshot()
        entry = self.workspace.snapshot()["files"][0]
        blob = self.root / self.room_id / "blobs" / entry["sha256"]
        blob.write_text("wrong text")  # Same byte count, distinct stat/hash.
        with self.assertRaises(ProjectWorkspaceError):
            self.workspace.display_snapshot()
        with self.assertRaises(ProjectWorkspaceError):
            self.workspace.snapshot()  # Actions keep their independent fresh verification.
        blob.write_text("valid text")
        blob.unlink()
        outside = self.base / "outside"
        outside.write_text("valid text")
        blob.symlink_to(outside)
        with self.assertRaises(ProjectWorkspaceError):
            self.workspace.display_snapshot()
        blob.unlink()
        blob.write_text("valid text")
        os.chmod(blob, 0o600)
        self.workspace._display_digest_cache = None
        compute = self.workspace._display_digest
        def changed_during_digest(*args):
            result = compute(*args)
            blob.write_text("other text")
            return result
        with patch.object(self.workspace, "_display_digest", side_effect=changed_during_digest):
            with self.assertRaises(ProjectWorkspaceError):
                self.workspace.display_snapshot()
        self.assertIsNone(self.workspace._display_digest_cache)


class FolderDisplayHTTPTests(unittest.TestCase):
    setUp = paging.RoomPagingHTTPTests.setUp
    tearDown = paging.RoomPagingHTTPTests.tearDown
    request = paging.RoomPagingHTTPTests.request

    def test_state_poll_does_not_load_large_file_but_full_read_and_binding_remain_exact(self):
        room_id, _ = self.service.store.create_room("Inspect a large report.", "claude", str(uuid4()),
            mode="project_folder", project_id=str(uuid4()), work_mode="build")
        project = self.service._project(room_id, create=True)
        source = self.root / "source"
        source.mkdir()
        registration = self.service.catalog.register_folder("Synthetic source", str(source))
        seeded = project.seed_folder(registration)
        content = "Large report 中文\\n" * (256 * 1024)
        project.apply("claude", "large-report", seeded["revision"], [{"path": "report.txt", "previous_sha256": None, "content": content}])
        full = project.snapshot()
        expected_digest = project_digest(full)
        code, cold, size = self.request("GET", f"/api/rooms/{room_id}/state")
        self.assertEqual(code, 200, cold)
        self.assertEqual(cold["project"]["snapshot_sha256"], expected_digest)
        self.assertTrue(cold["project"]["files"][0]["content_available"])
        self.assertNotIn("content", cold["project"]["files"][0])
        self.assertLess(size, 64 * 1024)
        with patch.object(project, "_raw", side_effect=AssertionError("Polling loaded full content")):
            for _ in range(3):
                code, state, _ = self.request("GET", f"/api/rooms/{room_id}/state")
                self.assertEqual(code, 200, state)
                self.assertEqual(state, cold)
        code, raw, _ = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(code, 200, raw)
        self.assertEqual(raw["project"]["files"][0]["content"], content)
        self.assertEqual(raw["project"]["snapshot_sha256"], expected_digest)
        self.assertEqual(self.calls, [])
