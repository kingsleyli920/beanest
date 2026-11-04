"""Real media HTTP boundaries with temporary files and synthetic native work.

The MP4 fixture is arbitrary binary data: these checks prove byte transport and
authorization, not codec support or browser playback.
"""
import base64
import hashlib
import http.client
import io
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import zipfile

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
import test_long_team_result_http as long_result
import test_native_room_service as native
from test_team_store import proposal


class RoomMediaHTTPTests(unittest.TestCase):
    request = long_result.LongTeamResultHTTPTests.request
    tearDown = long_result.LongTeamResultHTTPTests.tearDown
    create = long_result.LongTeamResultHTTPTests.create
    settled = long_result.LongTeamResultHTTPTests.settled

    def setUp(self):
        long_result.LongTeamResultHTTPTests.setUp(self)
        self.png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII=")
        self.video = b"\x00\x00\x00\x18ftypmp42\x00\xff" + bytes(range(256)) * 40
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("readme.txt", "A temporary downloadable attachment.\n")
        self.zip = archive.getvalue()
        self.files = {"media/pixel.png": self.png, "media/demo.mp4": self.video,
                      "media/archive.zip": self.zip, "中文 空格/截图.png": self.png + b"\x00"}
        for relative, raw in self.files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.image_path = self.source / "media/pixel.png"
        self.video_path = self.source / "media/demo.mp4"
        (self.source / "media/unmentioned.png").write_bytes(self.png)
        outside = self.root / "outside"
        outside.mkdir()
        self.external = outside / "external.png"
        self.sibling = outside / "sibling.png"
        self.external.write_bytes(self.png + b"external")
        self.sibling.write_bytes(self.png + b"sibling")
        self.result = (
            "Published report: `results/report.md`\n"
            "[Image](media/pixel.png) [Video](media/demo.mp4) [Archive](media/archive.zip)\n"
            "[中文截图](<中文 空格/截图.png>)\n"
            f"External selected file: <{self.external}>\n"
            f"External sibling remains separate: <{self.sibling}>\n"
            "Unresolved linked file: `media/linked.png`\n")
        self.document = (
            "# Attachment report\n"
            "[Image](../media/pixel.png) [Video](../media/demo.mp4)\n"
            "The supplied MP4 bytes are only an HTTP fixture, not a playback claim.\n")

    def handler(self, agent, context, cwd):
        phase = context["job"]["job_phase"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not context["team"]["tasks"] and agent == context["team"]["plan_author"]:
                commands = [proposal("K1", paths=["results/report.md"]), {"type": "claim", "task_id": "K1"}]
            return native.native_reply(context, [*commands, {"type": "plan_ready"}])
        if phase == "work":
            (cwd / "results").mkdir(exist_ok=True)
            (cwd / "results/report.md").write_text(self.document, encoding="utf-8")
            return native.native_reply(context, [{"type": "complete", "task_id": "K1", "result": self.result}])
        return long_result.LongTeamResultHTTPTests.handler(self, agent, context, cwd)

    def bootstrap(self, *, video_source=None, image_source=None):
        """Reusable normal HTTP/native seed; no database fabrication or old gates."""
        for source, destination, field, relative in (
            (video_source, self.video_path, "video", "media/demo.mp4"),
            (image_source, self.image_path, "png", "media/pixel.png"),
        ):
            if source is not None:
                raw = Path(source).read_bytes()
                destination.write_bytes(raw)
                setattr(self, field, raw)
                self.files[relative] = raw
        created = self.create()
        snapshot = self.settled(created["room"]["id"])
        self.assertEqual(snapshot["objective"]["status"], "complete")
        self.assertTrue(all(task["status"] == "accepted" for task in snapshot["team"]["tasks"]))
        self.assertFalse(any(job["error"] for job in snapshot["jobs"]))
        return snapshot

    def assets(self, room_id):
        code, value = self.request("GET", f"/api/rooms/{room_id}/media")
        self.assertEqual(code, 200, value)
        return value["assets"]

    def resolve(self, room_id, reference):
        return self.request("GET", f"/api/rooms/{room_id}/media/resolve?" + urlencode({"reference": reference}))

    def access(self, room_id, asset):
        code, value = self.request("POST", f"/api/rooms/{room_id}/media/{asset['id']}/access",
                                   {"sha256": asset["sha256"]})
        self.assertEqual(code, 200, value)
        self.assertGreater(value["expires_at"], 0)
        self.assertTrue(value["url"].startswith("/room-media/"))
        return value

    def resource(self, path, *, method="GET", headers=None):
        """Browser-style byte request: deliberately send no API token header."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def attachment_body(self, snapshot, *, path=None, reference=None):
        task = snapshot["team"]["tasks"][0]
        return {"path": str(path or self.external), "reference": reference or str(self.external),
                "sha256": hashlib.sha256((path or self.external).read_bytes()).hexdigest(),
                "source": {"run_id": snapshot["current_run"]["id"], "task_id": task["id"],
                           "result_version": task["result_version"]}}

    def reopen(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
                                   native_invoke=self.native, registry=default_registry())
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def test_listing_and_resolve_publish_referenced_baseline_media_without_binary_delivery(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        assets = self.assets(room_id)
        self.assertTrue(assets)
        for reference, raw in self.files.items():
            with self.subTest(reference=reference):
                code, value = self.resolve(room_id, reference)
                self.assertEqual(code, 200, value)
                asset = value["asset"]
                self.assertEqual(asset["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(asset["bytes"], len(raw))
                self.assertFalse(asset["verified"])
                self.assertNotIn("_path", asset)
                self.assertIn(asset["id"], {item["id"] for item in assets})
        for reference in (str(self.image_path), self.image_path.as_uri(),
                          (self.source / "中文 空格/截图.png").as_uri()):
            with self.subTest(equivalent=reference):
                self.assertEqual(self.resolve(room_id, reference)[0], 200)
        project = self.service._project(room_id).snapshot()
        self.assertEqual(project["revision"], 2)  # Folder baseline plus the one Markdown work commit.
        binary_metadata = self.service._project(room_id).metadata_for_paths(list(self.files))
        self.assertEqual(len(binary_metadata), len(self.files))
        self.assertEqual(binary_metadata, self.service._project(room_id).metadata_for_paths(list(self.files), revision=1))
        for item in binary_metadata:
            self.assertFalse(item["text"])
            self.assertEqual(item["sha256"], hashlib.sha256(self.files[item["path"]]).hexdigest())
        delivery = final["objective"]["delivery"]
        self.assertEqual({item["path"] for item in delivery["files"]}, {"results/report.md"})
        self.assertEqual(self.service._project(room_id).files_for_paths(["results/report.md"])[0]["content"], self.document)
        # Native accepted exact references are deliveries too, even when the
        # file is outside the originally captured project directory.
        self.assertEqual(self.resolve(room_id, str(self.external))[0], 200)

    def test_resource_get_head_ranges_conditions_and_download_are_actual_bytes(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        code, resolved = self.resolve(room_id, "media/demo.mp4")
        self.assertEqual(code, 200, resolved)
        ticket = self.access(room_id, resolved["asset"])
        url = ticket["url"]
        status, headers, body = self.resource(url)
        self.assertEqual((status, body), (200, self.video))
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(headers["content-length"], str(len(self.video)))
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", headers["content-security-policy"])
        etag = headers["etag"]
        status, head, body = self.resource(url, method="HEAD")
        self.assertEqual((status, body, head["content-length"]), (200, b"", str(len(self.video))))
        for requested, expected, content_range in (
            ("bytes=2-8", self.video[2:9], f"bytes 2-8/{len(self.video)}"),
            ("bytes=-4", self.video[-4:], f"bytes {len(self.video)-4}-{len(self.video)-1}/{len(self.video)}"),
            (f"bytes={len(self.video)-3}-", self.video[-3:], f"bytes {len(self.video)-3}-{len(self.video)-1}/{len(self.video)}"),
        ):
            with self.subTest(requested=requested):
                status, headers, body = self.resource(url, headers={"Range": requested, "If-Range": etag})
                self.assertEqual((status, body, headers["content-range"]), (206, expected, content_range))
        self.assertEqual(self.resource(url, headers={"If-None-Match": etag})[::2], (304, b""))
        self.assertEqual(self.resource(url, headers={"Range": "bytes=0-1", "If-Range": '"old"'})[::2], (200, self.video))
        status, headers, body = self.resource(url, headers={"Range": "bytes=999999-"})
        self.assertEqual((status, body, headers["content-range"]), (416, b"", f"bytes */{len(self.video)}"))
        status, headers, body = self.resource(url + "?download=1")
        self.assertEqual((status, body), (200, self.video))
        self.assertTrue(headers["content-disposition"].startswith("attachment;"))
        code, resolved = self.resolve(room_id, "media/archive.zip")
        self.assertEqual(code, 200, resolved)
        status, headers, body = self.resource(self.access(room_id, resolved["asset"])["url"])
        self.assertEqual((status, body), (200, self.zip))
        self.assertTrue(headers["content-disposition"].startswith("attachment;"))

    def test_resolution_rejects_unpublished_paths_symlinks_and_cross_room_ids(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        (self.source / "media/linked.png").symlink_to(self.external)
        for reference in ("media/unmentioned.png", "media/linked.png", str(self.root / "unmentioned.png"),
                          "../outside/external.png", "https://example.test/a.mp4"):
            with self.subTest(reference=reference):
                code, value = self.resolve(room_id, reference)
                self.assertIn(code, {403, 404, 409}, value)
                self.assertNotIn("asset", value)
        self.assertEqual(self.resolve(room_id, "http://[broken")[0], 400)
        code, resolved = self.resolve(room_id, "media/pixel.png")
        self.assertEqual(code, 200, resolved)
        another = self.bootstrap()["room"]["id"]
        code, value = self.request("POST", f"/api/rooms/{another}/media/{resolved['asset']['id']}/access",
                                   {"sha256": resolved["asset"]["sha256"]})
        self.assertEqual((code, value["error"]), (404, "media_not_found"))
        self.assertEqual(self.resource("/room-media/" + "A" * 43)[0], 403)

    def test_external_attachment_is_user_bound_persistent_and_grants_only_one_file(self):
        # A blocked, unaccepted report is not automatic native delivery. The
        # existing explicit registration still grants exactly the selected file.
        normal = self.handler

        def unaccepted(agent, context, cwd):
            if context["job"]["job_phase"] == "review":
                return native.native_reply(context, [{"type": "block", "task_id": "K1",
                    "reason": "A real product choice remains unresolved.", "cause": "user_decision"}])
            return normal(agent, context, cwd)

        self.native.handler = unaccepted
        final = self.settled(self.create()["room"]["id"])
        self.assertEqual(final["team"]["tasks"][0]["status"], "blocked")
        room_id = final["room"]["id"]
        route = f"/api/rooms/{room_id}/media/attach"
        original = self.attachment_body(final)
        self.assertEqual(self.resolve(room_id, str(self.external))[0], 404)
        for changed in (
            {**original, "sha256": "0" * 64},
            {**original, "reference": "not-a-result-reference.png"},
            {**original, "source": {**original["source"], "result_version": 2}},
        ):
            with self.subTest(changed=sorted(key for key in changed if changed[key] != original[key])):
                self.assertEqual(self.request("POST", route, changed)[0], 409)
        code, value = self.request("POST", route, original)
        self.assertEqual(code, 200, value)
        asset = value["asset"]
        self.assertEqual(len(self.service.store.room_attachments(room_id)), 1)
        self.assertEqual(self.resolve(room_id, str(self.sibling))[0], 404)
        self.reopen()
        code, persisted = self.resolve(room_id, str(self.external))
        self.assertEqual(code, 200, persisted)
        self.assertEqual(persisted["asset"]["id"], asset["id"])
        self.assertEqual(self.resource(self.access(room_id, persisted["asset"])["url"])[::2],
                         (200, self.external.read_bytes()))
        self.assertEqual(self.resolve(room_id, str(self.sibling))[0], 404)

    def test_hash_drift_and_expiry_invalidate_access_without_native_calls(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        code, resolved = self.resolve(room_id, "media/demo.mp4")
        self.assertEqual(code, 200, resolved)
        asset = resolved["asset"]
        code, value = self.request("POST", f"/api/rooms/{room_id}/media/{asset['id']}/access", {"sha256": "0" * 64})
        self.assertEqual((code, value["error"]), (409, "media_changed"))
        ticket = self.access(room_id, asset)
        calls = len(self.native.calls)
        (self.source / "media/demo.mp4").write_bytes(self.video + b"changed")
        status, _, body = self.resource(ticket["url"])
        self.assertEqual(status, 409, body)
        with patch("local_agent_coordinator.room_server.time.time", return_value=ticket["expires_at"] + 1):
            self.assertEqual(self.resource(ticket["url"])[0], 403)
        code, image = self.resolve(room_id, "media/pixel.png")
        self.assertEqual(code, 200, image)
        image_url = self.access(room_id, image["asset"])["url"]
        for action, expected in (("archive", 200), ("remove", 409)):
            code, value = self.request("POST", f"/api/rooms/{room_id}/lifecycle", {"action": action})
            self.assertEqual(code, 200, value)
            self.assertEqual(self.resource(image_url)[0], expected)
        self.assertEqual(len(self.native.calls), calls)

    def test_mcp_principal_cannot_list_resolve_mint_or_register_media(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        code, resolved = self.resolve(room_id, "media/pixel.png")
        self.assertEqual(code, 200, resolved)
        asset = resolved["asset"]
        root = f"/api/mcp/rooms/{room_id}/media"
        for method, route, body in (
            ("GET", root, None),
            ("GET", root + "/resolve?" + urlencode({"reference": "media/pixel.png"}), None),
            ("POST", root + f"/{asset['id']}/access", {"sha256": asset["sha256"]}),
            ("POST", root + "/attach", self.attachment_body(final)),
        ):
            with self.subTest(method=method, route=route):
                code, value = self.request(method, route, body, mcp=True)
                self.assertEqual((code, value["error"]), (403, "user_media_required"))
        self.assertEqual(self.service.store.room_attachments(room_id), [])


if __name__ == "__main__":
    unittest.main()
