from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.project_catalog import ProjectCatalog, ProjectCatalogError, source_hash


class ProjectCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "example"
        (self.source / "src").mkdir(parents=True)
        (self.source / "src" / "score.ts").write_text("export const score = 1;\n")
        (self.source / "README.md").write_text("# A small example\n")
        self.root = self.base / "private" / "catalog"
        self.catalog = ProjectCatalog(self.root)

    def assertCode(self, code, operation):
        with self.assertRaises(ProjectCatalogError) as error:
            operation()
        self.assertEqual(error.exception.code, code)
        self.assertEqual(str(error.exception), code)

    def register(self):
        return self.catalog.register("Example", str(self.source), ["src/score.ts", "README.md"])

    def test_folder_registration_only_opens_directory_and_remains_compatible_with_selected_files(self):
        old = self.register()
        with patch.object(ProjectCatalog, "_capture", side_effect=AssertionError("no content reads")), \
             patch("os.listdir", side_effect=AssertionError("no enumeration")), \
             patch("os.scandir", side_effect=AssertionError("no enumeration")):
            folder = self.catalog.register_folder("Whole folder", str(self.source))
        self.assertEqual(folder["files"], [])
        self.assertEqual(folder["selection_kind"], "folder")
        self.assertEqual(ProjectCatalog(self.root).list(), [old, folder])
        self.assertEqual(self.catalog.get(folder["id"]), folder)
        self.assertCode("project_folder_capture_required", lambda: self.catalog.capture(folder["id"]))
        self.assertEqual(len(self.catalog.capture(old["id"])["files"]), 2)
        self.assertNotIn("A small example", (self.root / "catalog.json").read_text())

    def test_folder_registration_rejects_missing_file_and_symlink_directories(self):
        self.assertCode("project_file_unavailable", lambda: self.catalog.register_folder("Missing", str(self.source / "missing")))
        self.assertCode("project_unsafe_path", lambda: self.catalog.register_folder("File", str(self.source / "README.md")))
        linked = self.base / "linked"
        linked.symlink_to(self.source, target_is_directory=True)
        self.assertCode("project_unsafe_path", lambda: self.catalog.register_folder("Linked", str(linked)))

    def test_metadata_is_private_content_free_and_capture_is_fresh_stable_and_read_only(self):
        before = {path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        item = self.register()
        self.assertEqual(item["files"], ["README.md", "src/score.ts"])
        self.assertEqual(self.catalog.list(), [item])
        self.assertEqual(ProjectCatalog(self.root).get(item["id"]), item)
        raw = (self.root / "catalog.json").read_text()
        self.assertNotIn("export const", raw)
        self.assertNotIn("A small example", raw)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        for path in self.root.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        first = self.catalog.capture(item["id"])
        self.assertEqual(first["source_hash"], source_hash(first["files"]))
        self.assertEqual(first, self.catalog.capture(item["id"]))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        (self.source / "src" / "score.ts").write_text("export const score = 2;\n")
        second = self.catalog.capture(item["id"])
        self.assertNotEqual(first["source_hash"], second["source_hash"])
        self.assertEqual(second["files"][1]["sha256"], hashlib.sha256(b"export const score = 2;\n").hexdigest())

    def test_only_explicit_files_are_read_without_directory_scanning(self):
        (self.source / ".env").write_text("NEVER_READ")
        with patch("pathlib.Path.iterdir", side_effect=AssertionError("no listing")), \
             patch("pathlib.Path.rglob", side_effect=AssertionError("no recursion")), \
             patch("os.listdir", side_effect=AssertionError("no listing")), \
             patch("os.scandir", side_effect=AssertionError("no scanning")):
            item = self.register()
            captured = self.catalog.capture(item["id"])
        self.assertEqual(len(captured["files"]), 2)
        self.assertNotIn("NEVER_READ", json.dumps(captured))

    def test_paths_and_file_lists_fail_before_reading_source(self):
        invalid_lists = [[], ["../README.md"], ["/README.md"], ["src//score.ts"], ["src/../README.md"],
                         [".git/config"], [".env"], ["secrets.json"], ["credentials.json"], ["id_rsa"],
                         ["image.png"], ["README.md", "readme.md"], ["src\\score.ts"], ["x" * 81 + ".txt"],
                         [f"a{number}.py" for number in range(13)], "README.md", [None]]
        with patch.object(self.catalog, "_capture", side_effect=AssertionError("must validate first")):
            for files in invalid_lists:
                with self.subTest(files=files):
                    self.assertCode("invalid_project_files", lambda: self.catalog.register("Example", str(self.source), files))
        for path in ["relative", "/", str(self.source / ".." / "example"), None]:
            self.assertCode("invalid_project_path", lambda: self.catalog.register("Example", path, ["README.md"]))
        self.assertEqual(self.catalog.list(), [])

    def test_symlink_components_and_hardlinks_cannot_escape_explicit_source(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "ordinary.txt").write_text("outside")
        (self.source / "linked.txt").symlink_to(outside / "ordinary.txt")
        (self.source / "linked").symlink_to(outside, target_is_directory=True)
        linked_root = self.base / "linked-root"
        linked_root.symlink_to(self.source, target_is_directory=True)
        ancestor = self.base / "linked-ancestor"
        ancestor.symlink_to(self.base, target_is_directory=True)
        for path, files in [(str(self.source), ["linked.txt"]), (str(self.source), ["linked/ordinary.txt"]),
                            (str(linked_root), ["README.md"]), (str(ancestor / "example"), ["README.md"])]:
            self.assertCode("project_unsafe_path", lambda: self.catalog.register("Example", path, files))
        os.link(outside / "ordinary.txt", self.source / "hard.txt")
        self.assertCode("project_unsafe_path", lambda: self.catalog.register("Example", str(self.source), ["hard.txt"]))
        item = self.register()
        (self.source / "README.md").unlink()
        (self.source / "README.md").symlink_to(outside / "ordinary.txt")
        self.assertCode("project_unsafe_path", lambda: self.catalog.capture(item["id"]))

    def test_binary_sensitive_limits_and_missing_files_fail_closed(self):
        samples = [(b"a\0b", "invalid_project_files"), (b"\xff", "invalid_project_files"),
                   (b"\x01", "invalid_project_files"), (b"x" * 16385, "project_limit_exceeded"),
                   (b"api_key = 'private-example-value'", "project_sensitive_content"),
                   (b"-----BEGIN OPENSSH PRIVATE KEY-----", "project_sensitive_content"),
                   (b"Authorization: Bearer abcdefghijklmnopqrstuv", "project_sensitive_content")]
        for raw, code in samples:
            (self.source / "sample.txt").write_bytes(raw)
            self.assertCode(code, lambda: self.catalog.register("Example", str(self.source), ["sample.txt"]))
        for number in range(4):
            (self.source / f"s{number}.txt").write_text("x" * 16384)
        self.assertCode("project_limit_exceeded", lambda: self.catalog.register(
            "Example", str(self.source), [f"s{number}.txt" for number in range(4)]))
        self.assertCode("project_file_unavailable", lambda: self.catalog.register("Example", str(self.source), ["missing.py"]))
        self.assertEqual(self.catalog.list(), [])

    def test_capture_rejects_changes_during_the_read(self):
        item = self.register()
        original = os.read
        mutated = False

        def read_and_change(descriptor, count):
            nonlocal mutated
            raw = original(descriptor, count)
            if not mutated:
                mutated = True
                (self.source / "README.md").write_text("# Changed during capture\n")
            return raw

        with patch("local_agent_coordinator.project_catalog.os.read", side_effect=read_and_change):
            self.assertCode("project_file_unavailable", lambda: self.catalog.capture(item["id"]))

    def test_catalog_corruption_never_resets_or_overwrites_existing_data(self):
        self.register()
        metadata = self.root / "catalog.json"
        original = metadata.read_bytes()
        bad_values = [b"{}", b'{"format":"bad","projects":[]}', b'{"format":null,"format":1,"projects":[]}',
                      b'{"format":"beanest-project-catalog-v1","projects":[null]}', b"{" + b" " * 1048576]
        for raw in bad_values:
            metadata.write_bytes(raw)
            self.assertCode("project_catalog_corrupt", self.catalog.list)
            self.assertCode("project_catalog_corrupt", lambda: ProjectCatalog(self.root))
            self.assertCode("project_catalog_corrupt", self.register)
            self.assertEqual(metadata.read_bytes(), raw)
        metadata.write_bytes(original)
        value = json.loads(original)
        for key, bad in [("files", [".env"]), ("path", "relative"), ("id", "oops"), ("created_at", True), ("name", "")]:
            changed = json.loads(original)
            changed["projects"][0][key] = bad
            metadata.write_text(json.dumps(changed))
            self.assertCode("project_catalog_corrupt", self.catalog.list)
        metadata.write_bytes(original)
        self.assertEqual(len(self.catalog.list()), 1)
        metadata.unlink()
        self.assertCode("project_catalog_corrupt", self.catalog.list)
        self.assertCode("project_catalog_corrupt", lambda: ProjectCatalog(self.root))
        self.assertFalse(metadata.exists())

    def test_private_store_symlinks_hardlinks_and_public_permissions_are_refused(self):
        public = self.base / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        self.assertCode("project_unsafe_path", lambda: ProjectCatalog(public))
        linked = self.base / "linked-catalog"
        linked.symlink_to(self.root, target_is_directory=True)
        self.assertCode("project_unsafe_path", lambda: ProjectCatalog(linked))
        item = self.register()
        metadata = self.root / "catalog.json"
        saved = self.base / "saved.json"
        metadata.rename(saved)
        metadata.symlink_to(saved)
        self.assertCode("project_unsafe_path", self.catalog.list)
        metadata.unlink()
        os.link(saved, metadata)
        self.assertCode("project_unsafe_path", self.catalog.list)
        self.assertEqual(json.loads(saved.read_text())["projects"][0], item)

    def test_concurrent_registration_does_not_lose_entries_and_unknown_is_explicit(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            entries = list(pool.map(lambda number: ProjectCatalog(self.root).register(
                f"Example {number}", str(self.source), ["README.md"]), range(8)))
        self.assertEqual({item["id"] for item in self.catalog.list()}, {item["id"] for item in entries})
        self.assertCode("invalid_project_request", lambda: self.catalog.get(None))
        self.assertCode("invalid_project_request", lambda: self.catalog.get("bad"))
        self.assertCode("project_not_found", lambda: self.catalog.get(str(uuid4())))

    def test_publish_failure_preserves_state_and_post_rename_failure_is_uncertain(self):
        before = self.register()
        with patch("local_agent_coordinator.project_catalog.os.replace", side_effect=OSError("private detail")):
            self.assertCode("project_storage_unavailable", self.register)
        self.assertEqual(self.catalog.list(), [before])
        original = os.replace

        def replace_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("rename result unconfirmed")

        with patch("local_agent_coordinator.project_catalog.os.replace", side_effect=replace_then_fail):
            self.assertCode("project_commit_unconfirmed", self.register)
        self.assertEqual(len(self.catalog.list()), 2)
        self.assertFalse(list(self.root.glob(".catalog-*")))


if __name__ == "__main__":
    unittest.main()
