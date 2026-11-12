import hashlib
from pathlib import Path
import unittest

from local_agent_coordinator.native_workspace import collect_changes

import test_native_workspace as workspace_fixtures


class NativeGeneratedOutputTests(unittest.TestCase):
    setUp = workspace_fixtures.NativeWorkspaceTests.setUp
    export = workspace_fixtures.NativeWorkspaceTests.export
    prepare = workspace_fixtures.NativeWorkspaceTests.prepare

    def test_untracked_unowned_cache_is_preserved_with_exact_generated_metadata(self):
        source = self.export("source", {"main.py": "VALUE = 1\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        cache_path = "__pycache__/main.cpython-313.pyc"
        cache_bytes = b"\x00\xffcompiled cache\x01"
        cache = project / cache_path
        cache.parent.mkdir()
        cache.write_bytes(cache_bytes)

        result = collect_changes(project, receipt, [])

        self.assertEqual(result["files"], [])
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["generated"], [{
            "path": cache_path,
            "sha256": hashlib.sha256(cache_bytes).hexdigest(),
            "bytes": len(cache_bytes),
        }])
        self.assertEqual(cache.read_bytes(), cache_bytes)
        self.assertFalse((source[0] / cache_path).exists())

    def test_sensitive_env_is_kept_private_and_similarly_named_source_is_still_blocked(self):
        # Catches a partner's local .env failing the task, being imported, or an unowned cache.py slipping in.
        source = self.export("source", {"main.py": "VALUE = 1\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        contents = {
            ".env": b"SYNTHETIC_SETTING=test-only\n",
            ".cache/.env": b"SYNTHETIC_SETTING=nested-test-only\n",
            "cache.py": b"VALUE = 2\n",
        }
        for name, raw in contents.items():
            (project / name).parent.mkdir(parents=True, exist_ok=True)
            (project / name).write_bytes(raw)

        for allowed in ([], [".env"]):
            with self.subTest(owned=bool(allowed)):
                result = collect_changes(project, receipt, allowed)
                self.assertEqual(result["files"], [])
                self.assertEqual(result["blocked"], [{"path": "cache.py", "reason": "native_path_not_owned"}])
                self.assertEqual(result["generated"], [
                    {"path": name, "sha256": hashlib.sha256(contents[name]).hexdigest(),
                     "bytes": len(contents[name]), "reason": "sensitive_path"}
                    for name in (".cache/.env", ".env")])
        for name, raw in contents.items():
            with self.subTest(path=name):
                self.assertEqual((project / name).read_bytes(), raw)
                self.assertFalse((source[0] / name).exists())

    def test_baseline_cache_changes_stay_guarded_and_owned_cache_output_stays_private(self):
        # Catches an unowned captured cache change being hidden, or an owned cache output failing the task.
        tracked_path = "__pycache__/tracked.pyc"
        source = self.export("source", {tracked_path: b"\x00old cache"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        tracked_bytes = b"\x00changed cache"
        (project / tracked_path).write_bytes(tracked_bytes)
        allowed_path = "__pycache__/allowed.pyc"
        allowed_bytes = b"\x00new explicitly owned cache"
        (project / allowed_path).write_bytes(allowed_bytes)

        result = collect_changes(project, receipt, [allowed_path])

        self.assertEqual(result["files"], [])
        self.assertEqual(result["blocked"], [
            {"path": tracked_path, "reason": "native_path_not_owned"},
        ])
        self.assertEqual(result["generated"], [{"path": allowed_path,
            "sha256": hashlib.sha256(allowed_bytes).hexdigest(), "bytes": len(allowed_bytes)}])
        self.assertEqual((project / tracked_path).read_bytes(), tracked_bytes)
        self.assertEqual((project / allowed_path).read_bytes(), allowed_bytes)
        self.assertEqual((source[0] / tracked_path).read_bytes(), b"\x00old cache")
        self.assertFalse((source[0] / allowed_path).exists())

if __name__ == "__main__":
    unittest.main()
