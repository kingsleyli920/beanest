"""On-demand code status over real HTTP and isolated temporary Git repositories."""
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator import git_status, native_workspace
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
import test_git_status as git_fixture
import test_native_workspace as native_fixture
import test_team_runs_http as http_fixture


class CodeStatusHTTPTests(unittest.TestCase):
    committed_at = git_fixture.GitStatusTests.committed_at
    git = git_fixture.GitStatusTests.git
    repository = git_fixture.GitStatusTests.repository
    write = git_fixture.GitStatusTests.write
    commit = git_fixture.GitStatusTests.commit
    assert_counts = git_fixture.GitStatusTests.assert_counts
    request = http_fixture.TeamRunHTTPTests.request

    def setUp(self):
        git_fixture.GitStatusTests.setUp(self)
        self.source = self.repository("代码 项目")
        self.write(self.source, "tracked.txt", "original\n")
        self.write(self.source, "notes.md", "shared baseline\n")
        self.commit(self.source, "Initial shared commit")
        self.bare_upstream = self.root / "upstream.git"
        self.bare_upstream.mkdir()
        self.git(self.bare_upstream, "init", "--bare", "--initial-branch=main", "--object-format=sha1")
        self.git(self.source, "remote", "add", "origin", str(self.bare_upstream))
        self.git(self.source, "push", "--set-upstream", "origin", "main")
        self.write(self.source, "notes.md", "one local commit beyond the upstream\n")
        self.local_commit = self.commit(self.source, "Local unpushed commit")
        self.write(self.source, "tracked.txt", "staged\n")
        self.git(self.source, "add", "tracked.txt")
        self.write(self.source, "tracked.txt", "staged plus unstaged\n")
        self.write(self.source, "未跟踪 说明.txt", "untracked\n")
        self.invocations = []

        async def fail_if_called(*args, **kwargs):
            self.invocations.append((args, kwargs))
            raise AssertionError("Code status must not invoke an Agent")

        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces",
            invoke=fail_if_called, native_invoke=fail_if_called)
        self.server = create_room_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self._close_server)
        code, project = self.request("POST", "/api/projects", {
            "name": "临时代码 项目", "path": str(self.source), "selection_kind": "folder"})
        self.assertEqual(code, 200, project)
        self.project_id = project["project"]["id"]
        self.room_id, _ = self.service.store.create_room("Inspect temporary code status", "claude", str(uuid4()),
            mode="project_folder", project_id=self.project_id, participants=["claude"])
        # Do not wake this room: status must work before the first Agent invocation.
        self.service._project(self.room_id, create=True)

    def tearDown(self):
        self.doCleanups()

    def _close_server(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def bootstrap(self):
        """Return the full queued-room snapshot; root may reuse this fixture for CUA."""
        code, snapshot = self.request("GET", f"/api/rooms/{self.room_id}")
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(self.invocations, [])
        return snapshot

    def status(self, workspace=None):
        query = "" if workspace is None else "?workspace=" + workspace
        code, result = self.request("GET", f"/api/rooms/{self.room_id}/code-status" + query)
        self.assertEqual(code, 200, result)
        return result

    def test_dirty_to_committed_refresh_uses_local_upstream_without_invocation(self):
        self.bootstrap()
        dirty = self.status()
        self.assertEqual((dirty["state"], dirty["workspace_id"], dirty["branch"]), ("ready", "project", "main"))
        self.assert_counts(dirty, total=2, staged=1, unstaged=1, untracked=1)
        self.assertEqual(dirty["last_commit"]["hash"], self.local_commit)
        self.assertEqual({key: dirty["upstream"][key] for key in ("state", "name", "ahead", "behind", "scope")},
            {"state": "ready", "name": "origin/main", "ahead": 1, "behind": 0, "scope": "repository_local_refs"})
        committed = self.commit(self.source, "Saved temporary working changes")
        clean = self.status("project")
        self.assertEqual(clean["state"], "ready")
        self.assert_counts(clean, total=0)
        self.assertEqual(clean["last_commit"]["hash"], committed)
        self.assertEqual((clean["upstream"]["ahead"], clean["upstream"]["behind"]), (2, 0))
        self.assertEqual(self.invocations, [])
        self.assertFalse((self.source / ".git/FETCH_HEAD").exists())

    def test_merged_commits_and_untracked_items_remain_separate_over_http(self):
        self.bootstrap()
        # Reset only the disposable tracked file; the existing untracked item stays.
        self.git(self.source, "restore", "--staged", "tracked.txt")
        self.git(self.source, "restore", "tracked.txt")
        self.git(self.source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        local_main = self.status()
        self.assert_counts(local_main, total=1, untracked=1)
        self.assertEqual(local_main["mainline"]["relation"], "same_branch")
        self.assertEqual(local_main["mainline"]["ahead"], 1)
        self.assertEqual(local_main["upstream"]["ahead"], 1)
        self.assertFalse(local_main["merge_request"]["available"])
        # Advance only a local record to model a previously fetched/pushed main.
        self.git(self.source, "update-ref", "refs/remotes/origin/main", self.local_commit)
        self.git(self.source, "checkout", "-b", "feature")
        merged = self.status()
        self.assert_counts(merged, total=1, untracked=1)
        self.assertEqual(merged["mainline"]["relation"], "merged")
        self.assertEqual((merged["mainline"]["ahead"], merged["mainline"]["behind"]), (0, 0))
        self.assertFalse(merged["merge_request"]["available"])
        self.assertEqual(merged["mainline"]["scope"], "repository_local_refs")
        self.assertTrue((self.source / "未跟踪 说明.txt").exists())
        self.assertEqual(self.invocations, [])
        self.assertFalse((self.source / ".git/FETCH_HEAD").exists())

    def test_status_is_user_only_and_normal_room_reads_do_not_inspect_git(self):
        with patch.object(git_status, "read_status", wraps=git_status.read_status) as inspect:
            self.bootstrap()
            for path in ("/api/rooms", f"/api/rooms/{self.room_id}/state"):
                code, value = self.request("GET", path)
                self.assertEqual(code, 200, value)
            code, status = self.request("GET", "/api/status")
            self.assertEqual(code, 200, status)
            self.assertTrue(status["capabilities"]["room_code_status_v1"])
            code, denied = self.request("GET", f"/api/mcp/rooms/{self.room_id}/code-status", mcp=True)
            self.assertEqual(code, 403, denied)
            self.assertEqual(denied["error"], "user_code_status_required")
            code, unknown = self.request("GET", f"/api/rooms/{self.room_id}/code-status?workspace=native:unknown")
            self.assertEqual(code, 400, unknown)
            self.assertEqual(unknown["error"], "invalid_code_workspace")
            inspect.assert_not_called()
            self.status()
            self.assertEqual(inspect.call_count, 1)
        self.assertEqual(self.invocations, [])

    def test_missing_project_and_text_room_never_claim_clean(self):
        missing = self.source.with_name("temporarily missing source")
        self.source.rename(missing)
        try:
            unavailable = self.status()
        finally:
            missing.rename(self.source)
        self.assertEqual((unavailable["state"], unavailable["reason"]), ("unavailable", "directory_unavailable"))
        self.assertIsNone(unavailable["uncommitted"]["total"])
        room_id, _ = self.service.store.create_room("Plain text only", "claude", str(uuid4()))
        code, unbound = self.request("GET", f"/api/rooms/{room_id}/code-status")
        self.assertEqual(code, 200, unbound)
        self.assertEqual((unbound["state"], unbound["workspace_id"], unbound["workspaces"]), ("unbound", None, []))
        self.assertIsNone(unbound["uncommitted"]["total"])
        self.assertEqual(self.invocations, [])

    def test_native_workspace_selection_inspects_real_prepared_git_copy(self):
        room_id, _ = self.service.store.create_room("Inspect a private work copy", "claude", str(uuid4()),
            mode="native_project", project_id=self.project_id, participants=["claude"])
        self.service._project(room_id, create=True)
        exported = native_fixture.NativeWorkspaceTests.export(self, "export", {"work.md": "original\n"})
        prepared = native_workspace.prepare_workspace(self.root / "native-cwd", *exported)
        self.service.store.save_native_workspace(room_id, "claude", prepared)
        self.write(self.root / "native-cwd/project", "work.md", "private unstaged change\n")
        code, selected = self.request("GET", f"/api/rooms/{room_id}/code-status?workspace=native%3Aclaude")
        self.assertEqual(code, 200, selected)
        self.assertEqual((selected["state"], selected["workspace_id"]), ("ready", "native:claude"))
        self.assert_counts(selected, total=1, unstaged=1)
        self.assertEqual({item["id"] for item in selected["workspaces"]}, {"project", "native:claude"})
        self.assertEqual(selected["files"][0]["path"], "work.md")
        self.assertEqual(self.invocations, [])


if __name__ == "__main__":
    unittest.main()
