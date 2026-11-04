"""Exact native delivery references through HTTP, with temporary files only.

Synthetic MP4 bytes test transport and authorization, never codec playback.
"""
import asyncio
import hashlib
from pathlib import Path
import unittest
from uuid import uuid4

import test_native_room_service as native
import test_room_media_http as media


class NativeDeliveryMediaHTTPTests(unittest.TestCase):
    request = media.RoomMediaHTTPTests.request
    tearDown = media.RoomMediaHTTPTests.tearDown
    settled = media.RoomMediaHTTPTests.settled
    assets = media.RoomMediaHTTPTests.assets
    resolve = media.RoomMediaHTTPTests.resolve
    access = media.RoomMediaHTTPTests.access
    resource = media.RoomMediaHTTPTests.resource

    def setUp(self):
        media.RoomMediaHTTPTests.setUp(self)
        self.first_delivery = self.root / "outside/first-delivery.mp4"
        self.second_delivery = self.root / "outside/中文 空格/第二段 视频.mp4"
        self.second_delivery.parent.mkdir()
        self.delivery_bytes = {self.first_delivery: self.video + b"first",
                               self.second_delivery: self.video + b"second"}
        for path, raw in self.delivery_bytes.items():
            path.write_bytes(raw)
        self.neighbor = self.root / "outside/unmentioned-neighbor.mp4"
        self.chat_only = self.root / "outside/chat-only.mp4"
        self.neighbor.write_bytes(self.video + b"neighbor")
        self.chat_only.write_bytes(self.video + b"chat")
        self.result = (f"已完成第一份示例：<{self.first_delivery}>\n"
                       "第二份见最终总结；普通登记图仍在 `media/pixel.png`。")
        # The full bare path includes both Chinese and spaces. The following
        # punctuation belongs to prose, not the filesystem path.
        self.summary = f"第二份示例是 {self.second_delivery}，12 倍慢放。两份仅为测试字节。"

    def handler(self, agent, context, cwd):
        phase = context["job"]["job_phase"]
        if phase == "checkpoint":
            return native.native_reply(context, [{"type": "checkpoint", "decision": "complete",
                "summary": self.summary, "next_goal": None,
                "scope_reference": context["objective"]["scope_reference"], "question": None}])
        return media.RoomMediaHTTPTests.handler(self, agent, context, cwd)

    def create(self):
        code, created = self.request("POST", "/api/rooms", {
            "goal": "Review the exact temporary media references and their stated limits.",
            "target": "claude", "request_id": str(uuid4()), "mode": "native_project",
            "project_id": self.project_id, "participants": ["claude", "codex"],
            "work_mode": "build", "runtime_settings": {"permission": "full_access", "members": {}},
            "execution_policy": {"mode": "autonomous"}})
        self.assertEqual(code, 200, {"error": created.get("error")})
        return created

    def bootstrap(self, *, video_source=None, image_source=None):
        """Accept optional real media for the root's separate browser validation."""
        if video_source is not None:
            raw = Path(video_source).read_bytes()
            for path in self.delivery_bytes:
                path.write_bytes(raw)
                self.delivery_bytes[path] = raw
        return media.RoomMediaHTTPTests.bootstrap(self, video_source=video_source, image_source=image_source)

    def assert_delivery_provenance(self, asset, snapshot):
        source = asset["source"]["delivery"]
        self.assertEqual(set(source), {"run_id", "seq"})
        self.assertEqual(source["run_id"], snapshot["current_run"]["id"])
        event = next(event for event in snapshot["events"] if event["seq"] == source["seq"])
        if event["kind"] == "team_command":
            self.assertEqual(event["command"], "review")
            role = self.service.store.job_roles(snapshot["room"]["id"])[event["job_id"]]
            self.assertEqual((role["phase"], role["run_id"], role["task_id"], role["result_version"]),
                             ("review", source["run_id"], "K1", 1))
        else:
            self.assertEqual((event["kind"], event["decision"]), ("objective_checkpoint_finished", "complete"))
            self.assertEqual(event["objective_id"], snapshot["objective"]["id"])

    def test_accepted_external_result_and_bare_summary_paths_are_discovered_without_attach(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        assets = self.assets(room_id)
        for path, raw in self.delivery_bytes.items():
            with self.subTest(path=path.name):
                matches = [asset for asset in assets if asset["sha256"] == hashlib.sha256(raw).hexdigest()]
                self.assertEqual(len(matches), 1, [asset["name"] for asset in assets])
                asset = matches[0]
                self.assertEqual((asset["name"], asset["bytes"], asset["kind"]), (path.name, len(raw), "video"))
                self.assert_delivery_provenance(asset, final)
                code, resolved = self.resolve(room_id, str(path))
                self.assertEqual(code, 200, {"error": resolved.get("error")})
                self.assertEqual(resolved["asset"]["id"], asset["id"])
                ticket = self.access(room_id, asset)
                code, headers, body = self.resource(ticket["url"], headers={"Range": "bytes=3-17"})
                self.assertEqual((code, body), (206, raw[3:18]))
                self.assertEqual(headers["content-range"], f"bytes 3-17/{len(raw)}")
                code, _, full = self.resource(ticket["url"])
                self.assertEqual((code, hashlib.sha256(full).hexdigest()), (200, asset["sha256"]))
        for path in (self.neighbor, self.chat_only):
            self.assertNotIn(path.name, {asset["name"] for asset in assets})
            self.assertEqual(self.resolve(room_id, str(path))[0], 404)
        self.assertEqual(self.service.store.room_attachments(room_id), [])
        self.assertEqual(self.resolve(room_id, "media/pixel.png")[0], 200)

    def test_unaccepted_ordinary_chat_reference_does_not_authorize_an_external_file(self):
        # Hold the synthetic provider before any task result or completion can
        # be accepted; the HTTP user message is still a real stored chat event.
        self.native.hold = asyncio.Event()
        created = self.create()
        room_id = created["room"]["id"]
        text = f"普通聊天中提到 <{self.chat_only}>；尚未形成交付结果。"
        code, response = self.request("POST", f"/api/rooms/{room_id}/messages", {
            "text": text, "target": "claude", "request_id": str(uuid4())})
        self.assertEqual(code, 200, {"error": response.get("error")})
        code, snapshot = self.request("GET", f"/api/rooms/{room_id}")
        self.assertEqual(code, 200)
        self.assertTrue(any(event.get("sender") == "user" and event.get("text") == text for event in snapshot["events"]))
        self.assertEqual(self.resolve(room_id, str(self.chat_only))[0], 404)
        self.assertNotIn(self.chat_only.name, {asset["name"] for asset in self.assets(room_id)})
        self.assertEqual(self.service.store.room_attachments(room_id), [])

    def test_pending_plan_settings_keep_accepted_external_media_readable(self):
        final = self.bootstrap()
        room_id = final["room"]["id"]
        calls = len(self.native.calls)
        code, staged = self.request("POST", f"/api/rooms/{room_id}/messages", {
            "text": "后续仅用 Plan 讨论，既有交付保留。", "target": "all", "request_id": str(uuid4()),
            "work_mode": "plan", "runtime_settings": {"permission": "read_only", "members": {}}})
        self.assertEqual(code, 200, {"error": staged.get("error")})
        self.assertTrue(staged["runtime_configuration"]["pending"])
        self.assertEqual(staged["runtime_configuration"]["desired"]["work_mode"], "plan")
        self.assertEqual(staged["runtime_configuration"]["desired"]["runtime_settings"]["permission"], "read_only")
        for path, raw in self.delivery_bytes.items():
            code, resolved = self.resolve(room_id, str(path))
            self.assertEqual(code, 200, {"error": resolved.get("error")})
            ticket = self.access(room_id, resolved["asset"])
            self.assertEqual(self.resource(ticket["url"])[::2], (200, raw))
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(self.service.store.room_attachments(room_id), [])

    def test_new_objective_keeps_unopened_previous_delivery_media(self):
        # bootstrap never lists/resolves media or mints access. The old files
        # must survive a new goal even when nobody has opened them beforehand.
        visited = []
        request = self.request

        def record_request(method, path, *args, **kwargs):
            visited.append(path)
            return request(method, path, *args, **kwargs)

        self.request = record_request
        previous = self.bootstrap()
        room_id = previous["room"]["id"]
        original_run = previous["current_run"]["id"]
        original_objective = previous["objective"]["id"]
        old_publications = {event["seq"] for event in previous["events"] if event.get("run_id") == original_run
            and (event["kind"] == "team_command" and event.get("command") == "review"
                 or event["kind"] == "objective_checkpoint_finished" and event.get("decision") == "complete")}
        self.result = "This new objective contains only a new text note."
        self.summary = "独立新目标的文字说明已完成。"
        self.document = "# New independent text note\nNo prior attachment references.\n"
        code, response = self.request("POST", f"/api/rooms/{room_id}/messages", {
            "text": "开始一个独立新目标：补充一条新的文字说明。", "target": "all", "request_id": str(uuid4()),
            "intent": "continue", "binding": previous["continuation"]["binding"]})
        self.assertEqual(code, 200, {"error": response.get("error")})
        current = self.settled(room_id)
        self.assertNotEqual(current["current_run"]["id"], original_run)
        self.assertNotEqual(current["objective"]["id"], original_objective)
        self.assertEqual(current["objective"]["status"], "complete", current["team"]["stop_reason"])
        for path in self.delivery_bytes:
            self.assertNotIn(str(path), current["objective"]["next_step"]["summary"])
            self.assertTrue(all(str(path) not in (task["result"] or "") for task in current["team"]["tasks"]))
        self.assertFalse(any("/media" in path for path in visited))
        code, envelope = self.request("GET", f"/api/rooms/{room_id}/media")
        self.assertEqual((code, envelope["latest_user_seq"]), (200, max((event["seq"] for event in current["events"]
            if event.get("sender") == "user" and event["kind"] in {"message", "objective_decision"}), default=0)))
        assets = envelope["assets"]
        for path, raw in self.delivery_bytes.items():
            with self.subTest(previous_delivery=path.name):
                code, resolved = self.resolve(room_id, str(path))
                self.assertEqual(code, 200, {"error": resolved.get("error")})
                asset = resolved["asset"]
                self.assertIn(asset["id"], {item["id"] for item in assets})
                self.assertEqual(asset["source"]["delivery"]["run_id"], original_run)
                self.assertIn(asset["source"]["delivery"]["seq"], old_publications)
                self.assertEqual(asset["sha256"], hashlib.sha256(raw).hexdigest())
                ticket = self.access(room_id, asset)
                self.assertEqual(self.resource(ticket["url"])[::2], (200, raw))
                status, headers, body = self.resource(ticket["url"], headers={"Range": "bytes=2-11"})
                self.assertEqual((status, body), (206, raw[2:12]))
                self.assertEqual(headers["content-range"], f"bytes 2-11/{len(raw)}")
        self.assertEqual(self.service.store.room_attachments(room_id), [])


if __name__ == "__main__":
    unittest.main()
