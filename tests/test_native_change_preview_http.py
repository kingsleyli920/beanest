"""Read-only exact-workspace previews over real loopback HTTP, with no models."""
import asyncio
import base64
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from local_agent_coordinator import native_workspace
from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService


class NativeChangePreviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.path = "docs/codex~x3/preview.txt"
        self.shared = b"Shared original codex~x3 text.\n"
        self.source = self.root / "source"
        source_file = self.source / self.path
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(self.shared)
        self.invocations = []
        async def forbidden(*args, **kwargs):
            self.invocations.append(True)
            raise AssertionError("No model may run in a preview test")
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   registry=default_registry(), native_invoke=forbidden)
        self.service._wake = lambda *args, **kwargs: None
        async def prepare():
            registration = await self.service.register_project("Preview fixture", str(self.source), selection_kind="folder")
            return await self.service.create_room("Inspect a saved private change", "codex", str(uuid4()),
                mode="native_project", project_id=registration["project"]["id"], participants=["codex"],
                work_mode="build", runtime_settings={"permission": "full_access", "members": {}},
                execution_policy={"mode": "autonomous"})
        self.room = asyncio.run(prepare())["room"]["id"]
        self.project = self.service._project(self.room)
        export = self.root / "export"
        exported = self.project.materialize_snapshot(export)
        self.manifest = native_workspace.prepare_workspace(self.root / "private-copy", export, exported)
        self.private = Path(self.manifest["project"]) / self.path
        self.private.write_text("Private original codex~x3 text.\n")
        self.manifest["blocked"] = [{"path": self.path, "reason": "native_path_not_owned"}]
        self.service.store.save_native_workspace(self.room, "codex~x3", self.manifest)
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.assertEqual(self.invocations, [])
        self.temporary.cleanup()

    def request(self, path, *, method="GET", mcp=False):
        headers = {"X-LAC-Token": self.server.mcp_token if mcp else self.server.token}
        if mcp:
            headers["X-LAC-Instance"] = self.server.instance_id
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            if method == "POST":
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body="{}" if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def route(self, *, workspace="codex~x3", path=None, mcp=False):
        prefix = "/api/mcp" if mcp else "/api"
        return f"{prefix}/rooms/{self.room}/native-changes?" + urlencode({
            "workspace_id": workspace, "path": self.path if path is None else path})

    def publish(self, raw):
        entry = self.project.metadata_for_paths([self.path])[0]
        self.project.apply("codex", str(uuid4()), self.project.metadata_snapshot()["revision"], [{
            "path": self.path, "previous_sha256": entry["sha256"],
            "content_base64": base64.b64encode(raw).decode()}], native=True)

    def test_exact_session_paths_and_text_survive_http_display_folding(self):
        with patch.object(self.project, "snapshot", side_effect=AssertionError("No full project snapshot")):
            code, result = self.request(self.route())
        self.assertEqual(code, 200, result)
        self.assertEqual((result["workspace_id"], result["provider"], result["path"]), ("codex~x3", "codex", self.path))
        self.assertEqual(result["state"], "different")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["canonical"]["content"], self.shared.decode())
        self.assertIn("codex~x3", result["private"]["content"])
        self.assertEqual(result["canonical"]["sha256"], hashlib.sha256(self.shared).hexdigest())
        self.private.write_bytes(self.shared)
        self.assertEqual(self.request(self.route())[1]["state"], "aligned")
        code, view = self.request(f"/api/rooms/{self.room}/state")
        self.assertEqual(code, 200, view)
        workspace, = view["native_workspaces"]
        self.assertEqual(workspace["workspace_id"], "codex~x3")
        self.assertEqual(workspace["provider"], "codex")
        self.assertEqual(self.service.store.native_workspace(self.room, "codex~x3"), self.manifest)

    def test_unlisted_other_workspace_sensitive_and_malformed_requests_are_rejected(self):
        self.service.store.save_native_workspace(self.room, "codex", {
            **self.manifest, "blocked": [{"path": "another.txt", "reason": "native_path_not_owned"}]})
        for route, expected in ((self.route(path="unlisted.txt"), 404),
                (self.route(workspace="codex"), 404), (self.route(workspace="../codex"), 400),
                (self.route(path="../outside.txt"), 400), (self.route(path=".env"), 403),
                (self.route() + "&path=another.txt", 400), (self.route() + "&unexpected=1", 400),
                (f"/api/rooms/{self.room}/native-changes?" + urlencode({"path": self.path}), 400)):
            with self.subTest(route=route):
                self.assertEqual(self.request(route)[0], expected)
        self.assertEqual(self.request(self.route(mcp=True), mcp=True)[0], 403)
        self.assertEqual(self.request(self.route(), method="POST")[0], 405)

    def test_large_text_is_bounded_but_hash_is_complete_and_binary_has_no_content(self):
        raw = ("完整 codex~x3 文本\n" * 12000).encode()
        self.private.write_bytes(raw)
        code, result = self.request(self.route())
        self.assertEqual(code, 200, result)
        side = result["private"]
        self.assertTrue(side["truncated"])
        self.assertLessEqual(len(side["content"].encode()), 65536)
        self.assertEqual(side["sha256"], hashlib.sha256(raw).hexdigest())
        binary = b"\x00\xffbinary" * 12000
        self.publish(binary)
        self.private.write_bytes(binary)
        code, result = self.request(self.route())
        self.assertEqual(code, 200, result)
        self.assertEqual(result["state"], "aligned")
        for key in ("canonical", "private"):
            self.assertFalse(result[key]["text"])
            self.assertIsNone(result[key]["content"])
            self.assertEqual(result[key]["sha256"], hashlib.sha256(binary).hexdigest())

    def test_large_canonical_text_uses_bounded_streaming_memory(self):
        raw = b"Ordinary report line.\n" * 650_000
        entry = self.project.metadata_for_paths([self.path])[0]
        self.project.apply("codex", str(uuid4()), self.project.metadata_snapshot()["revision"], [{
            "path": self.path, "previous_sha256": entry["sha256"], "content": raw.decode()}], native=True)
        tracemalloc.start()
        try:
            with patch.object(self.project, "raw_files_for_paths", side_effect=AssertionError("No full blob allocation")):
                code, result = self.request(self.route())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(code, 200, result)
        self.assertTrue(result["canonical"]["text"])
        self.assertTrue(result["canonical"]["truncated"])
        self.assertLessEqual(len(result["canonical"]["content"].encode()), 65536)
        self.assertEqual(result["canonical"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertLess(peak, 8 * 1024 * 1024, "the complete canonical text was loaded into RAM")

    def test_links_and_writes_during_sampling_are_unavailable_without_leaked_content(self):
        self.private.unlink()
        self.private.symlink_to(self.source / self.path)
        result = self.request(self.route())[1]
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["private"]["reason"], "unsafe_path")
        self.assertIsNone(result["private"]["content"])
        self.private.unlink()
        self.private.write_text("before\n")
        parent = self.private.parent
        retained = parent.with_name("retained-parent")
        parent.rename(retained)
        parent.symlink_to(retained, target_is_directory=True)
        result = self.request(self.route())[1]
        self.assertEqual(result["private"]["reason"], "unsafe_path")
        self.assertIsNone(result["private"]["content"])
        parent.unlink()
        retained.rename(parent)
        original = native_workspace.preview_stream
        def changing(stream, sha, size, **kwargs):
            result = original(stream, sha, size, **kwargs)
            self.private.write_text("changed during sampling\n")
            return result
        with patch.object(native_workspace, "preview_stream", side_effect=changing):
            result = self.request(self.route())[1]
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["private"]["reason"], "changed")
        self.assertIsNone(result["private"]["content"])

    def test_confirmed_absence_differs_from_corrupt_canonical_or_sensitive_content(self):
        self.private.unlink()
        result = self.request(self.route())[1]
        self.assertEqual(result["state"], "different")
        self.assertFalse(result["private"]["exists"])
        self.assertEqual(result["private"]["reason"], "not_present")
        self.private.write_text("private\n")
        unregistered = "docs/unpublished.txt"
        (Path(self.manifest["project"]) / unregistered).write_text("New unshared file\n")
        self.manifest["blocked"].append({"path": unregistered, "reason": "native_path_not_owned"})
        self.service.store.save_native_workspace(self.room, "codex~x3", self.manifest)
        result = self.request(self.route(path=unregistered))[1]
        self.assertEqual(result["state"], "different")
        self.assertFalse(result["canonical"]["exists"])
        sha = self.project.metadata_for_paths([self.path])[0]["sha256"]
        (self.project.root / self.room / "blobs" / sha).write_bytes(b"damaged canonical blob")
        result = self.request(self.route())[1]
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["canonical"]["exists"])
        self.private.write_text('password = "fictional-sensitive-value"\n')
        result = self.request(self.route())[1]
        self.assertEqual(result["private"]["reason"], "sensitive_content")
        self.assertIsNone(result["private"]["content"])


if __name__ == "__main__":
    unittest.main()
