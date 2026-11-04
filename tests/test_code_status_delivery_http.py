"""Accepted exact delivery files locate temporary Git repositories over HTTP.

Native work and acceptance use the existing synthetic transport. The default
MP4 data proves transport only; bootstrap can take a real browser-test video.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from local_agent_coordinator import git_status
import test_git_status as git_fixture
import test_room_media_native_delivery_http as delivery_fixture


class CodeStatusDeliveryHTTPTests(unittest.TestCase):
    request = delivery_fixture.NativeDeliveryMediaHTTPTests.request
    tearDown = delivery_fixture.NativeDeliveryMediaHTTPTests.tearDown
    create = delivery_fixture.NativeDeliveryMediaHTTPTests.create
    handler = delivery_fixture.NativeDeliveryMediaHTTPTests.handler
    settled = delivery_fixture.NativeDeliveryMediaHTTPTests.settled
    assets = delivery_fixture.NativeDeliveryMediaHTTPTests.assets
    resolve = delivery_fixture.NativeDeliveryMediaHTTPTests.resolve
    git = git_fixture.GitStatusTests.git
    repository = git_fixture.GitStatusTests.repository
    write = git_fixture.GitStatusTests.write
    commit = git_fixture.GitStatusTests.commit
    assert_counts = git_fixture.GitStatusTests.assert_counts

    def setUp(self):
        delivery_fixture.NativeDeliveryMediaHTTPTests.setUp(self)
        self.git_binary = shutil.which("git")
        self.assertIsNotNone(self.git_binary)
        self.git_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.git_environment.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Temporary Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Temporary Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": git_fixture.GitStatusTests.committed_at,
            "GIT_COMMITTER_DATE": git_fixture.GitStatusTests.committed_at,
        })
        self.delivery_repository = self.repository("真实交付 仓库")
        self.first_delivery = self.delivery_repository / "录像 子目录/第一段.mp4"
        self.second_delivery = self.delivery_repository / "录像 子目录/第二段 视频.mp4"
        self.first_delivery.parent.mkdir()
        self.delivery_bytes = {self.first_delivery: self.video + b"first",
                               self.second_delivery: self.video + b"second"}
        for path, raw in self.delivery_bytes.items():
            path.write_bytes(raw)
        self.write(self.delivery_repository, "src/app.py", "print('baseline')\n")
        self.delivery_commit = self.commit(self.delivery_repository, "Accepted media baseline")
        self.write(self.delivery_repository, "src/app.py", "print('uncommitted source change')\n")
        self._reference_deliveries()

    def _reference_deliveries(self):
        self.result = f"已完成第一份示例：<{self.first_delivery}>；登记图仍在 `media/pixel.png`。"
        self.summary = f"第二份示例是 {self.second_delivery}，两份均为已接受的精确交付文件。"

    @staticmethod
    def delivery_id(repository):
        return "delivery:" + hashlib.sha256(str(repository).encode("utf-8")).hexdigest()

    def bootstrap(self, *, video_source=None, image_source=None):
        snapshot = delivery_fixture.NativeDeliveryMediaHTTPTests.bootstrap(
            self, video_source=video_source, image_source=image_source)
        self.room_id = snapshot["room"]["id"]
        return snapshot

    def status(self, workspace=None):
        query = "" if workspace is None else "?" + urlencode({"workspace": workspace})
        code, result = self.request("GET", f"/api/rooms/{self.room_id}/code-status" + query)
        self.assertEqual(code, 200, {"error": result.get("error")})
        return result

    def deliveries(self, result):
        return [item for item in result["workspaces"] if item["kind"] == "delivery"]

    def assert_delivery_candidate(self, candidate, repository, snapshot):
        self.assertEqual({key: candidate[key] for key in ("id", "kind", "label", "path")}, {
            "id": self.delivery_id(repository), "kind": "delivery",
            "label": "交付所在仓库 · " + repository.name, "path": str(repository)})
        assets = {asset["id"]: asset for asset in self.assets(self.room_id)}
        source = candidate["source"]
        self.assertIn(source["asset_id"], assets)
        self.assertEqual(source["run_id"], snapshot["current_run"]["id"])
        if "task_id" in source:
            self.assertEqual(source["task_id"], "K1")

    def test_unique_accepted_delivery_requires_user_binding_for_default_and_full_scope(self):
        final = self.bootstrap()
        calls = len(self.native.calls)
        result = self.status()
        candidates = self.deliveries(result)
        self.assertEqual(len(candidates), 1, [(item["kind"], item["id"]) for item in result["workspaces"]])
        self.assert_delivery_candidate(candidates[0], self.delivery_repository, final)
        self.assertEqual(result["delivery_repository"]["state"], "unbound")
        code, binding = self.request("POST", f"/api/rooms/{self.room_id}/delivery-repository",
            {"workspace_id":self.delivery_id(self.delivery_repository)})
        self.assertEqual(code, 200, binding)
        result = self.status()
        self.assertEqual((result["state"], result["workspace_id"], result["repository_root"], result["project_path"]),
            ("ready", self.delivery_id(self.delivery_repository), str(self.delivery_repository), "."))
        self.assert_counts(result, total=1, unstaged=1)
        self.assertEqual([item["path"] for item in result["files"]], ["src/app.py"])
        self.assertEqual(result["last_commit"]["hash"], self.delivery_commit)
        registered = self.status("project")
        self.assertEqual((registered["state"], registered["workspace_id"]), ("not_git", "project"))
        self.assertIsNone(registered["uncommitted"]["total"])
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(self.service.store.room_attachments(self.room_id), [])

    def test_multiple_delivery_repositories_require_an_explicit_selection(self):
        second_repository = self.repository("另一交付 仓库")
        self.second_delivery = second_repository / "第二段 视频.mp4"
        self.second_delivery.write_bytes(self.video + b"other repository")
        self.commit(second_repository, "Second repository media")
        self.delivery_bytes[self.second_delivery] = self.second_delivery.read_bytes()
        self._reference_deliveries()
        final = self.bootstrap()
        result = self.status()
        expected = {self.delivery_id(self.delivery_repository), self.delivery_id(second_repository)}
        self.assertEqual({item["id"] for item in self.deliveries(result)}, expected)
        self.assertNotIn(result["workspace_id"], expected)
        for repository in (self.delivery_repository, second_repository):
            selected = self.status(self.delivery_id(repository))
            self.assertEqual((selected["state"], selected["repository_root"]), ("ready", str(repository)))
            candidate = next(item for item in self.deliveries(selected) if item["id"] == selected["workspace_id"])
            self.assert_delivery_candidate(candidate, repository, final)

    def test_unaccepted_chat_neighbors_and_known_roots_do_not_add_ancestor_repositories(self):
        self.bootstrap()
        neighbor = self.repository("未引用 邻居仓库")
        neighbor_file = neighbor / "普通聊天.mp4"
        neighbor_file.write_bytes(self.video + b"unaccepted")
        self.commit(neighbor, "Unaccepted neighbor media")
        # A registered/private media path must not authorize this outer repo.
        self.git(self.root, "init", "--initial-branch=main", "--object-format=sha1")
        self.write(self.root, "ancestor-only.txt", "Unrelated ancestor metadata.\n")
        self.git(self.root, "add", "ancestor-only.txt")
        self.git(self.root, "commit", "-m", "Unrelated ancestor")
        self.native.hold = asyncio.Event()
        code, response = self.request("POST", f"/api/rooms/{self.room_id}/messages", {
            "text": f"普通聊天提到 <{neighbor_file}>；这不是已接受的交付。",
            "target": "claude", "request_id": str(uuid4())})
        self.assertEqual(code, 200, {"error": response.get("error")})
        with patch.object(git_status, "_run", wraps=git_status._run) as commands:
            result = self.status()
        self.assertEqual({item["id"] for item in self.deliveries(result)}, {self.delivery_id(self.delivery_repository)})
        self.assertNotIn(self.delivery_id(self.root), {item["id"] for item in result["workspaces"]})
        self.assertFalse(any(Path(call.args[1]).is_relative_to(neighbor) for call in commands.call_args_list))
        self.assertEqual(self.resolve(self.room_id, str(neighbor_file))[0], 404)

    def test_registered_ready_or_unknown_status_is_not_replaced_by_a_delivery(self):
        self.git(self.source, "init", "--initial-branch=main", "--object-format=sha1")
        registered_commit = self.commit(self.source, "Registered source repository")
        self.bootstrap()
        result = self.status()
        self.assertEqual((result["state"], result["workspace_id"], result["last_commit"]["hash"]),
            ("ready", "project", registered_commit))
        self.assertEqual(len(self.deliveries(result)), 1)
        inspect = git_status.read_status
        for reason in ("git_timeout", "git_filter_required"):
            def registered_unknown(path, *, now, deadline=None, reason=reason):
                if Path(path) == self.source:
                    return git_status.empty_status(reason, now=now)
                return inspect(path, now=now, deadline=deadline)

            with self.subTest(reason=reason), patch.object(git_status, "read_status", side_effect=registered_unknown):
                unknown = self.status()
                self.assertEqual((unknown["workspace_id"], unknown["state"], unknown["reason"]),
                    ("project", "unavailable", reason))
                self.assertIsNone(unknown["uncommitted"]["total"])

    def bootstrap_private_document_followup(self):
        """Accept a private-document checkpoint after a real external delivery.

        The followup accepts the summary and its exact private file reference;
        it does not claim new file work or fabricate an accepted task.
        """
        self.document = "# 私有规划文档\n\n后续仅核对既有交付的证据边界。\n"
        self.previous_delivery_snapshot = self.bootstrap()
        self.result = "本轮仅说明私有规划文档 `results/report.md`。"
        self.summary = "本轮接受私有规划文档 `results/report.md`，不重复外部交付路径。"
        code, response = self.request("POST", f"/api/rooms/{self.room_id}/messages", {
            "text": "后续只确认私有规划文档中的步骤，保留既有交付。", "target": "all",
            "request_id": str(uuid4()), "intent": "continue",
            "binding": self.previous_delivery_snapshot["continuation"]["binding"]})
        self.assertEqual(code, 200, {"error": response.get("error")})
        current = self.settled(self.room_id)
        self.assertNotEqual(current["current_run"]["id"], self.previous_delivery_snapshot["current_run"]["id"])
        self.assertEqual(current["objective"]["status"], "complete", current["team"]["stop_reason"])
        self.assertEqual(current["objective"]["next_step"]["summary"], self.summary)
        self.assertTrue(any(event["kind"] == "objective_checkpoint_finished"
                            and event.get("run_id") == current["current_run"]["id"]
                            and event.get("decision") == "complete" for event in current["events"]))
        for path in self.delivery_bytes:
            self.assertNotIn(str(path), current["objective"]["next_step"]["summary"])
            self.assertTrue(all(str(path) not in (task["result"] or "") for task in current["team"]["tasks"]))
        code, private = self.resolve(self.room_id, "results/report.md")
        self.assertEqual(code, 200, {"error": private.get("error")})
        self.assertEqual(private["asset"]["source"]["delivery"]["run_id"], current["current_run"]["id"])
        return current

    def test_private_document_followup_keeps_the_previous_candidate_for_explicit_binding(self):
        self.bootstrap_private_document_followup()
        calls = len(self.native.calls)
        result = self.status()
        candidates = self.deliveries(result)
        self.assertEqual(len(candidates), 1, [(item["kind"], item["id"]) for item in result["workspaces"]])
        self.assert_delivery_candidate(candidates[0], self.delivery_repository, self.previous_delivery_snapshot)
        self.assertEqual(result["delivery_repository"]["state"], "unbound")
        code, binding = self.request("POST", f"/api/rooms/{self.room_id}/delivery-repository",
            {"workspace_id":self.delivery_id(self.delivery_repository)})
        self.assertEqual(code, 200, binding)
        result = self.status()
        self.assertEqual((result["state"], result["workspace_id"], result["repository_root"]),
            ("ready", self.delivery_id(self.delivery_repository), str(self.delivery_repository)))
        self.assert_counts(result, total=1, unstaged=1)
        registered = self.status("project")
        self.assertEqual((registered["state"], registered["workspace_id"]), ("not_git", "project"))
        self.assertIsNone(registered["uncommitted"]["total"])
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(self.service.store.room_attachments(self.room_id), [])


if __name__ == "__main__":
    unittest.main()
