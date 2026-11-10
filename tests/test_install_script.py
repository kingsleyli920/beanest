"""scripts/install-macos.sh against a local fictional release: real bash, curl, ditto and codesign."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-macos.sh"
APP = "豆窝.app"


def fictional_app(parent, label):
    """A tiny signed bundle whose Resources/label tells which package it came from."""
    app = parent / APP
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
        "<key>CFBundleExecutable</key><string>beanest</string>"
        "<key>CFBundleIdentifier</key><string>test.fictional.beanest</string>"
        "<key>CFBundlePackageType</key><string>APPL</string></dict></plist>\n", encoding="utf-8")
    executable = app / "Contents" / "MacOS" / "beanest"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (app / "Contents" / "Resources" / "label").write_text(label, encoding="utf-8")
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True, capture_output=True)
    return app


@unittest.skipUnless(sys.platform == "darwin", "macOS installer")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.release = self.root / "release v0.3.0"
        self.release.mkdir()
        self.applications = self.root / "应用 程序"
        sums = []
        for arch in ("arm64", "x64"):
            build = self.root / f"build-{arch}"
            build.mkdir()
            app = fictional_app(build, f"fictional-{arch}")
            package = self.release / f"Beanest-0.3.0-macos-{arch}.zip"
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(package)], check=True)
            sums.append(f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n")
        (self.release / "SHA256SUMS").write_text("".join(sums), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, arch="arm64", **extra):
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.root),
                       "BEANEST_VERSION": "0.3.0", "BEANEST_APPLICATIONS": str(self.applications),
                       "BEANEST_DOWNLOAD_BASE": self.release.as_uri(), "BEANEST_ARCH": arch,
                       "BEANEST_NO_OPEN": "1", **extra}
        # Piped like `curl ... | bash`, so the script never relies on its own path.
        return subprocess.run(["/bin/bash"], input=SCRIPT.read_bytes(), env=environment,
                              capture_output=True, timeout=60)

    def installed_label(self):
        return (self.applications / APP / "Contents" / "Resources" / "label").read_text(encoding="utf-8")

    def test_a_fresh_install_lands_signed_and_without_quarantine(self):
        # Catches: an install that leaves no app, a broken signature, or a quarantine flag
        # that would bring back the "cannot verify" prompt.
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.installed_label(), "fictional-arm64")
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(self.applications / APP)], check=True)
        attributes = subprocess.run(["xattr", "-r", str(self.applications / APP)], capture_output=True, text=True)
        self.assertNotIn("com.apple.quarantine", attributes.stdout)

    def test_an_intel_mac_gets_the_intel_package(self):
        # Catches: the chip mapping sending x86_64 Macs the Apple-silicon build.
        result = self.install(arch="x86_64")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.installed_label(), "fictional-x64")

    def test_running_it_again_replaces_the_old_app_entirely(self):
        # Catches: an update merged over the old bundle, leaving stale files that break the seal.
        self.assertEqual(self.install().returncode, 0)
        stale = self.applications / APP / "Contents" / "Resources" / "stale-from-old-version"
        stale.write_text("old", encoding="utf-8")
        result = self.install(arch="x64")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.installed_label(), "fictional-x64")
        self.assertFalse(stale.exists())

    def test_a_mismatched_download_installs_nothing_and_keeps_the_old_app(self):
        # Catches: installing a corrupted or swapped package, or deleting the working app first.
        self.assertEqual(self.install().returncode, 0)
        (self.release / "SHA256SUMS").write_text(
            "0" * 64 + "  Beanest-0.3.0-macos-arm64.zip\n", encoding="utf-8")
        result = self.install(arch="arm64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("下载的文件和发布的不一致", result.stderr.decode())
        self.assertEqual(self.installed_label(), "fictional-arm64")

    def test_a_package_missing_from_the_checksums_is_refused(self):
        # Catches: an empty expected checksum comparing equal to nothing and passing.
        (self.release / "SHA256SUMS").write_text("", encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("校验文件里没有", result.stderr.decode())
        self.assertFalse((self.applications / APP).exists())

    def test_a_package_with_a_broken_signature_is_refused(self):
        # Catches: installing a bundle whose contents no longer match its signature.
        build = self.root / "tampered"
        build.mkdir()
        app = fictional_app(build, "tampered")
        (app / "Contents" / "Resources" / "label").write_text("changed after signing", encoding="utf-8")
        package = self.release / "Beanest-0.3.0-macos-arm64.zip"
        package.unlink()
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(package)], check=True)
        (self.release / "SHA256SUMS").write_text(
            f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n", encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签名校验没通过", result.stderr.decode())
        self.assertFalse((self.applications / APP).exists())


if __name__ == "__main__":
    unittest.main()
