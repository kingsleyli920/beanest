"""Media discovery reuses observations within one view, never authorization."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_agent_coordinator import room_media
from local_agent_coordinator.room_service import RoomService


class RoomMediaListingCostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.service = RoomService.__new__(RoomService)
        self.service.store = SimpleNamespace(room_attachments=lambda room: [])

    def test_repeated_reports_verify_large_file_once_and_keep_latest_delivery_and_aliases(self):
        path = self.root / "demo.mp4"
        path.write_bytes(b"synthetic video" * 600_000)
        texts = [("[Watch](demo.mp4) [Same](./demo.mp4) [Absent](missing.mp4)",
                  {"run_id": "fixture", "delivery": {"run_id": "fixture", "seq": index}}, False)
                 for index in range(40)]
        sources = ({}, texts, [self.root])
        with patch.object(room_media, "open_verified", wraps=room_media.open_verified) as verify, \
                patch.object(room_media, "local_references", wraps=room_media.local_references) as references:
            assets = self.service._room_media_assets("fixture", sources=sources)
        self.assertEqual(len(assets), 1)
        asset = assets[0]
        self.assertEqual(asset["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(asset["source"]["delivery"]["seq"], 39)
        self.assertEqual(asset["reference_aliases"], ["demo.mp4", "./demo.mp4"])
        self.assertEqual(verify.call_count, 2, "same exact candidate was opened repeatedly")
        self.assertEqual(references.call_count, len(texts), "the alias pass parsed all report text again")
        path.unlink()
        path.write_bytes(b"replacement video")
        refreshed = self.service._room_media_assets("fixture", sources=sources)
        self.assertEqual(refreshed[0]["sha256"], hashlib.sha256(b"replacement video").hexdigest())
        path.unlink()
        path.symlink_to(self.root / "missing.mp4")
        self.assertEqual(self.service._room_media_assets("fixture", sources=sources), [])

    def test_request_memo_does_not_authorize_an_unaccepted_external_source(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        external = self.root / "external.mp4"
        external.write_bytes(b"exact accepted external file")
        sources = ({}, [(str(external), {"delivery": {"seq": 1}}, True),
                        (str(external), {"delivery": {"seq": 999}}, False)], [allowed])
        assets = self.service._room_media_assets("fixture", sources=sources)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["source"]["delivery"]["seq"], 1)

    def test_suffix_aliases_cover_shared_names_without_traversal_aliases(self):
        paths = [self.root / "a" / "clip.mp4", self.root / "b" / "clip.mp4"]
        for index, path in enumerate(paths):
            path.parent.mkdir()
            path.write_bytes(str(index).encode())
        sources = ({}, [("a/clip.mp4 b/clip.mp4", {"delivery": {"seq": 1}}, False),
                        ("clip.mp4 ../clip.mp4", {"delivery": {"seq": 2}}, False)], [self.root])
        assets = self.service._room_media_assets("fixture", sources=sources)
        self.assertEqual(len(assets), 2)
        for asset in assets:
            self.assertIn("clip.mp4", asset["reference_aliases"])
            self.assertNotIn("../clip.mp4", asset["reference_aliases"])
            self.assertEqual(asset["source"]["delivery"]["seq"], 2)


if __name__ == "__main__":
    unittest.main()
