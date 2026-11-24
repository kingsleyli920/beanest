import base64
import copy
import hashlib
import io
import json
import unittest
import zipfile

from local_agent_coordinator.artifact_export import ArtifactExportError, export_project


def fixture():
    raw = "print('你好')\n"
    return {"revision": 4, "files": [{"path": "src/example.py", "content": raw,
            "sha256": hashlib.sha256(raw.encode()).hexdigest(), "owner": "claude"}],
            "source": {"private_path": "/private/example", "session_id": "private-session"}}


class ArtifactExportTests(unittest.TestCase):
    def test_download_contains_exact_files_and_public_manifest_only(self):
        source = fixture()
        before = copy.deepcopy(source)
        result = export_project(source, expected_revision=4)
        raw = base64.b64decode(result["content_base64"], validate=True)
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["files_count"], 1)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.namelist(), ["src/example.py", ".beanest-export.json"])
            self.assertEqual(archive.read("src/example.py"), "print('你好')\n".encode())
            manifest = json.loads(archive.read(".beanest-export.json"))
            self.assertEqual(set(manifest), {"format", "revision", "snapshot_sha256", "files"})
            self.assertEqual(manifest["revision"], 4)
            self.assertEqual(manifest["files"], [{"path": "src/example.py", "sha256": source["files"][0]["sha256"]}])
            self.assertNotIn(b"private", archive.read(".beanest-export.json"))
            self.assertEqual(archive.getinfo("src/example.py").external_attr >> 16 & 0o777, 0o600)
        self.assertEqual(source, before)
        self.assertEqual(export_project(source, expected_revision=4), result)

    def test_stale_or_invalid_revision_cannot_download_new_files(self):
        for revision in (3, True, -1, "4"):
            with self.subTest(revision=revision), self.assertRaises(ArtifactExportError):
                export_project(fixture(), expected_revision=revision)

    def test_corrupt_hash_and_unsafe_archive_names_are_refused(self):
        for path in ("../escape.py", "/tmp/escape.py", "a\\escape.py", ".env", "auth.json"):
            source = fixture()
            source["files"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ArtifactExportError):
                export_project(source, expected_revision=4)
        source = fixture()
        source["files"][0]["content"] += "# changed"
        with self.assertRaises(ArtifactExportError):
            export_project(source, expected_revision=4)

    def test_empty_or_duplicate_archive_is_refused(self):
        source = fixture()
        for files in ([], [source["files"][0], source["files"][0]]):
            with self.subTest(files=len(files)), self.assertRaises(ArtifactExportError):
                export_project({**source, "files": files}, expected_revision=4)

    def test_forty_text_files_of_2_mib_download_in_full(self):
        # Catches the old 16-file / 64 KiB download cap refusing a large text_artifacts room.
        source = fixture()
        files = []
        for number in range(40):
            content = "页" * 17476 + "\n"  # 52429 UTF-8 bytes
            files.append({"path": f"book/chapter{number:02d}.md", "owner": "codex", "content": content,
                          "sha256": hashlib.sha256(content.encode()).hexdigest()})
        exported = export_project({**source, "files": files}, expected_revision=4)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content_base64"], validate=True))) as archive:
            names = [name for name in archive.namelist() if not name.endswith(".beanest-export.json")]
            self.assertEqual(len(names), 40)
            self.assertEqual(sum(len(archive.read(name)) for name in names), 2097160)
