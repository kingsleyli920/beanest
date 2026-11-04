"""Pure media boundaries, using only isolated temporary files."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator import room_media
from local_agent_coordinator.room_media import (MediaError, local_references,
                                               mime_kind, open_verified, parse_range)


class RoomMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def assert_media_error(self, code, function, *args):
        with self.assertRaises(MediaError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)

    def test_binary_video_stream_is_verified_and_rewound(self):
        raw = b"\x00\xffvideo" * 400_000
        path = self.root / "demo.MP4"
        path.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
        stream, metadata = open_verified(path, expected)
        with stream:
            self.assertEqual(stream.tell(), 0)
            self.assertEqual(stream.read(), raw)
            self.assertEqual(metadata["sha256"], expected)
            self.assertEqual(metadata["bytes"], len(raw))
            self.assertEqual((metadata["mime"], metadata["kind"]), ("video/mp4", "video"))
            self.assertEqual(metadata["name"], path.name)
            observed = os.fstat(stream.fileno())
            self.assertEqual(metadata["identity"], (
                observed.st_dev, observed.st_ino, observed.st_mode, observed.st_nlink,
                observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns))

    def test_symlink_ancestors_leaf_hardlinks_and_fifo_are_rejected(self):
        directory = self.root / "real"
        directory.mkdir()
        path = directory / "demo.png"
        path.write_bytes(b"image")
        ancestor = self.root / "linked"
        ancestor.symlink_to(directory, target_is_directory=True)
        leaf = self.root / "leaf.png"
        leaf.symlink_to(path)
        for candidate in (ancestor / path.name, leaf):
            with self.subTest(candidate=candidate):
                self.assert_media_error("media_unsafe_path", open_verified, candidate)
        hardlink = self.root / "hardlink.png"
        os.link(path, hardlink)
        self.assert_media_error("media_unsafe_path", open_verified, hardlink)
        fifo = self.root / "pipe.mp4"
        os.mkfifo(fifo)
        self.assert_media_error("media_unsafe_path", open_verified, fifo)

    def test_sensitive_components_are_rejected_even_under_cache(self):
        for relative in (".env", ".cache/.env/output.png", "credentials/demo.mp4", "key.pem"):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"private")
                self.assert_media_error("media_unsafe_path", open_verified, path)
        for candidate in ("relative.png", self.root / ".." / "elsewhere.png", "/"):
            with self.subTest(candidate=candidate):
                self.assert_media_error("media_invalid_path", open_verified, candidate)
        self.assert_media_error("media_unavailable", open_verified, self.root / "missing.png")

    def test_hash_mismatch_and_change_during_read_are_rejected(self):
        path = self.root / "demo.mp4"
        path.write_bytes(b"abcdef")
        self.assert_media_error("media_changed", open_verified, path, "0" * 64)
        original_fstat = room_media.os.fstat
        calls = 0

        def mutate_before_final_fstat(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_bytes(b"changed")
            return original_fstat(descriptor)

        with patch.object(room_media.os, "fstat", side_effect=mutate_before_final_fstat):
            self.assert_media_error("media_changed", open_verified, path)

    def test_empty_file_and_media_types(self):
        path = self.root / "empty.png"
        path.touch()
        stream, metadata = open_verified(path)
        with stream:
            self.assertEqual(stream.read(), b"")
            self.assertEqual(metadata["bytes"], 0)
            self.assertEqual(metadata["sha256"], hashlib.sha256(b"").hexdigest())
        for name, expected in {
            "photo.JPEG": ("image/jpeg", "image"),
            "figure.svg": ("image/svg+xml", "image"),
            "movie.webm": ("video/webm", "video"),
            "sound.mp3": ("audio/mpeg", "audio"),
            "page.html": ("application/octet-stream", "file"),
            "unknown.weird": ("application/octet-stream", "file"),
        }.items():
            with self.subTest(name=name):
                self.assertEqual(mime_kind(name), expected)

    def test_single_byte_ranges_and_empty_response(self):
        for value, total, expected in (
            (None, 10, (200, 0, 9)),
            (None, 0, (200, 0, -1)),
            ("bytes=2-5", 10, (206, 2, 5)),
            ("bytes=2-", 10, (206, 2, 9)),
            ("bytes=0-99", 10, (206, 0, 9)),
            ("bytes=-3", 10, (206, 7, 9)),
            ("bytes=-99", 10, (206, 0, 9)),
            (" bytes=0-0 ", 1, (206, 0, 0)),
        ):
            with self.subTest(value=value, total=total):
                self.assertEqual(parse_range(value, total), expected)

    def test_invalid_ranges_are_416_candidates(self):
        for value, total in (
            ("bytes=0-0", 0), ("bytes=10-", 10), ("bytes=3-2", 10),
            ("bytes=-0", 10), ("bytes=-", 10), ("bytes=0-1,3-4", 10),
            ("items=0-1", 10), ("bytes=+1-2", 10), ("bytes=1-2\nextra", 10),
            ("bytes=0-0", -1), (None, True), (123, 10),
        ):
            with self.subTest(value=value, total=total):
                self.assert_media_error("media_range_invalid", parse_range, value, total)

    def test_local_references_support_markdown_code_angles_and_plain_paths(self):
        text = ('[录像](clips/demo.mp4) `screens/shot.png` '
                '< /tmp/my clip.mov > report.pdf '
                '[附件](<file:///tmp/my%20clip.mp4>) `screens/shot.png` '
                '演示视频是 deliveries/demo.mp4，12 倍慢放')
        self.assertEqual(local_references(text), [
            "clips/demo.mp4", "screens/shot.png", "/tmp/my clip.mov", "report.pdf",
            "file:///tmp/my%20clip.mp4", "deliveries/demo.mp4",
        ])

    def test_local_references_exclude_remote_and_executable_uris(self):
        text = ('[网站](https://example.test/a.mp4) <data:image/png;base64,AAAA> '
                '`javascript:alert.png` //example.test/a.png #photo.png '
                '[报告](results/report.md "报告") `not a command`')
        self.assertEqual(local_references(text), ["results/report.md"])


if __name__ == "__main__":
    unittest.main()
