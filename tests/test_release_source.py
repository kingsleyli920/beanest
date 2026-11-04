"""Exercise source candidates with isolated, fictional repositories only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare-source.py"
SPEC = importlib.util.spec_from_file_location("source_candidate_exporter", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)

ROOT_README = """# Public fixture

[Extensions](docs/extensions.md), [Room contract](docs/room-contract.md), [License](LICENSE),
[Third-party notices](THIRD_PARTY_NOTICES.md), [Contributing](CONTRIBUTING.md),
[Security](SECURITY.md), [中文](README.zh-CN.md).
"""
CHINESE_README = "# 中文入口\n\n[English](README.md)\n"
PUBLIC_README = "# Public documentation entry\n\n[English](../README.md) · [中文](../README.zh-CN.md)\n"

# A tiny stdlib-only build backend keeps this fixture install entirely offline;
# the real project's packaging/install verification remains a separate check.
BUILD_BACKEND = '''from pathlib import Path
import zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "release_probe-0.1.0-py3-none-any.whl"
    files = {
        "release_probe/__init__.py": (Path(__file__).parent / "release_probe" / "__init__.py").read_text(),
        "release_probe-0.1.0.dist-info/METADATA": "Metadata-Version: 2.1\\nName: release-probe\\nVersion: 0.1.0\\n",
        "release_probe-0.1.0.dist-info/WHEEL": "Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
    }
    record = "release_probe-0.1.0.dist-info/RECORD"
    files[record] = "".join(path + ",,\\n" for path in [*files, record])
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as wheel:
        for path, text in files.items():
            wheel.writestr(path, text)
    return name
'''


class ReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        for name in exporter.FIXED:
            self.write(name, "Public source fixture\n")
        self.write("LICENSE", "MIT License\n\nCopyright (c) 2026 Example Contributor\n\n" + exporter.MIT_BODY + "\n")
        self.write("scripts/prepare-source.py", SCRIPT.read_text())
        self.write("docs/public-readme.md", PUBLIC_README)
        self.write("README.md", ROOT_README)
        self.write("README.zh-CN.md", CHINESE_README)
        for name in exporter.BINARY_FILES:
            if name.endswith(".png"):
                (self.root / name).write_bytes(b"\x89PNG\r\n\x1a\n")
            elif name.endswith(".jpg"):
                (self.root / name).write_bytes(b"\xff\xd8\xff\xe0\xff\xd9")
        (self.root / "desktop/assets/beanest.icns").write_bytes(b"icns" + (8).to_bytes(4, "big"))
        self.write("pyproject.toml", '[build-system]\nrequires = []\nbuild-backend = "fixture_backend"\nbackend-path = ["src"]\n')
        self.write("src/fixture_backend.py", BUILD_BACKEND)
        self.write("src/release_probe/__init__.py", 'VALUE = "a clean export"\n')
        self.write("tests/test_smoke.py", 'import unittest\nfrom release_probe import VALUE\n'
                   'class Smoke(unittest.TestCase):\n    def test_value(self):\n'
                   '        self.assertEqual(VALUE, "a clean export")\n')
        self.write("tests/ui.test.cjs", "// A public browser test\n")

    def tearDown(self):
        self.directory.cleanup()

    def write(self, path, text):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        return destination

    def collect(self, **kwargs):
        return exporter.collect_source(self.root, **kwargs)

    def assertCode(self, code, function):
        with self.assertRaises(exporter.ExportError) as error:
            function()
        self.assertEqual(error.exception.code, code)

    def test_allowlist_excludes_private_files_and_preserves_bilingual_readme(self):
        private = "PRIVATE-FIXTURE-" + secrets.token_hex(16)
        for path in ("runtime/messages.sqlite3", ".env", "AGENTS.md", "START-HERE.md", "CLAUDE.md", "local-config/settings.json",
                     "docs/status.md", "docs/private.md", "desktop/node_modules/dependency.js",
                     "src/__pycache__/cached.py", "tests/__pycache__/cached.py",
                     "src/node_modules/dependency.js", "src/.env", "desktop/unlisted.js"):
            self.write(path, private)
        files = self.collect(deny_text=(private,))
        entries = {item.path: item.content for item in files}
        expected = {*exporter.FIXED, "README.md", "src/fixture_backend.py", "src/release_probe/__init__.py",
                    "tests/test_smoke.py", "tests/ui.test.cjs"}
        self.assertEqual(set(entries), expected)
        self.assertEqual(entries["README.md"], ROOT_README.encode("utf-8"))
        self.assertEqual(entries["docs/public-readme.md"], PUBLIC_README.encode("utf-8"))
        self.assertEqual(entries["README.zh-CN.md"], CHINESE_README.encode("utf-8"))
        for name in ("CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md"):
            self.assertIn(name, entries)
        self.assertFalse({"CONTRIBUTING", "SECURITY", "THIRD_PARTY_NOTICES"} & set(entries))

    def test_readme_links_exist_and_candidate_dry_run_reproduces_manifest(self):
        output = self.root / "runtime" / "candidate"
        manifest = exporter.write_candidate(self.root, output, self.collect())
        for relative in ("README.md", "README.zh-CN.md", "docs/public-readme.md"):
            document = output / relative
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if not target.startswith("https://"):
                    self.assertTrue((document.parent / target).is_file(), (relative, target))
        self.assertEqual((self.root / "docs/public-readme.md").read_text(), PUBLIC_README)
        self.assertEqual((output / "docs/public-readme.md").read_text(), PUBLIC_README)
        self.assertEqual((output / "README.md").read_text(), ROOT_README)
        recheck = subprocess.run([sys.executable, str(output / "scripts/prepare-source.py"), "--dry-run"],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(recheck.returncode, 0, recheck.stderr)
        self.assertEqual(json.loads(recheck.stdout), manifest)
        for entry in manifest["files"]:
            self.assertEqual(hashlib.sha256((output / entry["path"]).read_bytes()).hexdigest(),
                             entry["sha256"])

    def test_each_readme_is_scanned_without_template_replacement(self):
        for name in ("README.md", "README.zh-CN.md"):
            with self.subTest(name=name):
                original = (self.root / name).read_text()
                denied = "private-" + secrets.token_hex(12)
                self.write(name, denied)
                self.assertCode("denied_text_found", lambda: self.collect(deny_text=(denied,)))
                self.write(name, original)

    def test_reviewed_binary_icons_and_protocol_fixtures_are_preserved(self):
        fixture = "tests/fixtures/native_protocol_v1.json"
        self.write(fixture, '{"version":1,"fictional":true}\n')
        self.write("tests/fixtures/unreviewed.json", "must not export")
        self.write("src/release_probe/translations/en.json", '{"ready":"Ready"}\n')
        self.write("desktop/assets/unreviewed.png", "must not export")
        self.write("docs/assets/unreviewed.png", "must not export")
        entries = {item.path: item.content for item in self.collect()}
        for name in exporter.BINARY_FILES | {fixture}:
            self.assertEqual(entries[name], (self.root / name).read_bytes())
        self.assertNotIn("tests/fixtures/unreviewed.json", entries)
        self.assertNotIn("src/release_probe/translations/en.json", entries)
        self.assertNotIn("desktop/assets/unreviewed.png", entries)
        self.assertNotIn("docs/assets/unreviewed.png", entries)
        self.write("desktop/assets/beanest.icns", "not an icon")
        self.assertCode("source_invalid_binary_asset", self.collect)

    def test_default_cli_dry_run_only_prints_relative_paths_and_hashes(self):
        result = subprocess.run([sys.executable, str(self.root / "scripts/prepare-source.py")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        manifest = json.loads(result.stdout)
        self.assertEqual(set(manifest), {"files"})
        for entry in manifest["files"]:
            self.assertEqual(set(entry), {"path", "sha256"})
            self.assertFalse(Path(entry["path"]).is_absolute())
            self.assertEqual(len(entry["sha256"]), 64)
        self.assertNotIn(str(self.root), result.stdout)
        self.assertFalse((self.root / "runtime").exists())

    def test_selected_file_or_directory_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.py"
            target.write_text("must not be read")
            link = self.root / "src" / "linked.py"
            link.symlink_to(target)
            self.assertCode("source_missing_or_unsafe", self.collect)
            link.unlink()
            link.symlink_to(Path(outside), target_is_directory=True)
            self.assertCode("source_missing_or_unsafe", self.collect)
            link.unlink()
            license_path = self.root / "LICENSE"
            license_path.unlink()
            license_path.symlink_to(target)
            self.assertCode("source_missing_or_unsafe", self.collect)

    def test_sensitive_text_refuses_export_without_echoing_the_value(self):
        source = "src/release_probe/__init__.py"
        denied = "private-" + secrets.token_hex(12)
        self.write(source, denied)
        self.assertCode("denied_text_found", lambda: self.collect(deny_text=(denied,)))
        self.write(source, str(Path.home()) + "/private-file")
        self.assertCode("personal_path_found", self.collect)
        key = "sk-" + secrets.token_urlsafe(36)
        self.write(source, 'API_KEY = "' + key + '"\n')
        result = subprocess.run([sys.executable, str(self.root / "scripts/prepare-source.py"), "--dry-run"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("credential_shape_found", result.stderr)
        self.assertNotIn(key, result.stderr + result.stdout)
        self.assertFalse((self.root / "runtime").exists())
        self.write(source, 'API_KEY = "YOUR_API_KEY"\nauth_token = "example-token"\n')
        self.collect()

    def test_repeatable_local_deny_terms_fail_before_creating_an_output(self):
        denied = "local-" + secrets.token_hex(12)
        self.write("docs/public-readme.md", denied.upper())
        output = self.root / "runtime" / "candidate"
        result = subprocess.run([sys.executable, str(self.root / "scripts/prepare-source.py"),
                                 "--output", str(output), "--deny-text", "another-private-term",
                                 "--deny-text", denied], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("denied_text_found", result.stderr)
        self.assertNotIn(denied, result.stdout + result.stderr)
        self.assertNotIn(str(output), result.stdout + result.stderr)
        self.assertFalse(output.exists())

    def test_full_license_and_all_explicit_public_files_are_required(self):
        self.write("LICENSE", "MIT License\nCopyright (c) 2026 Example Contributor\n" + exporter.MIT_BODY[:100])
        self.assertCode("license_incomplete", self.collect)
        (self.root / "LICENSE").unlink()
        self.assertCode("source_missing_or_unsafe", self.collect)

    def test_output_refuses_existing_paths_outside_runtime_and_symlink_ancestors(self):
        files = self.collect()
        output = self.root / "runtime" / "candidate"
        manifest = exporter.write_candidate(self.root, output, files)
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o700)
        for entry in manifest["files"]:
            content = (output / entry["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
            self.assertFalse(os.stat(output / entry["path"]).st_mode & 0o077)
        original = (output / "manifest.json").read_bytes()
        self.assertCode("output_exists", lambda: exporter.write_candidate(self.root, output, files))
        self.assertEqual((output / "manifest.json").read_bytes(), original)
        self.assertCode("output_outside_runtime", lambda: exporter.write_candidate(self.root, self.root / "other", files))
        self.assertCode("invalid_output_path", lambda: exporter.write_candidate(self.root, Path("relative"), files))
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "runtime" / "link").symlink_to(outside, target_is_directory=True)
            self.assertCode("unsafe_output_path", lambda: exporter.write_candidate(
                self.root, self.root / "runtime" / "link" / "new", files))
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_candidate_unpacks_elsewhere_installs_offline_and_runs_its_test(self):
        output = self.root / "runtime" / "candidate"
        exporter.write_candidate(self.root, output, self.collect())
        recheck = subprocess.run([sys.executable, str(output / "scripts/prepare-source.py"), "--dry-run"],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(recheck.returncode, 0, recheck.stderr)
        archive = shutil.make_archive(str(self.root / "runtime" / "candidate"), "zip", output)
        with tempfile.TemporaryDirectory() as separate:
            extracted = Path(separate) / "source"
            with zipfile.ZipFile(archive) as source:
                source.extractall(extracted)
            installed = Path(separate) / "installed"
            result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", "--no-index",
                                     "--no-deps", "--no-build-isolation", "--target", str(installed), str(extracted)],
                                    cwd=separate, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                    cwd=extracted, env={**os.environ, "PYTHONPATH": str(installed)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Ran 1 test", result.stderr)


if __name__ == "__main__":
    unittest.main()
