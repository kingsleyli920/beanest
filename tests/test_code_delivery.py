"""Task delivery summaries over real disposable Git and HTTP boundaries."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from local_agent_coordinator import code_delivery, git_status, native_workspace
import test_git_status as git_fixture
import test_native_workspace as native_fixture
import test_code_status_http as http_fixture


@contextmanager
def local_github_transport(bare):
    """Only replace the network destination; keep real ls-remote and parser."""
    original = git_status._run
    def transport(command, directory, arguments, deadline, **kwargs):
        if "ls-remote" in arguments:
            arguments = ["-c", "protocol.file.allow=always", *arguments]
        return original(command, directory, arguments, deadline, **kwargs)
    with patch.object(git_status, "_github_url", return_value=str(bare)), patch.object(git_status, "_run", side_effect=transport):
        yield


class DeliverySummaryHTTPTests(http_fixture.CodeStatusHTTPTests):
    # Inherited tests deliberately remain a separate reusable fixture group.
    def clean_main(self):
        self.git(self.source, "restore", "--staged", "tracked.txt")
        self.git(self.source, "restore", "tracked.txt")
        (self.source / "未跟踪 说明.txt").unlink()

    def summary(self, refresh=False):
        code, result = self.request("GET", f"/api/rooms/{self.room_id}/code-status" + ("?refresh_remote=1" if refresh else ""))
        self.assertEqual(code, 200, result)
        return result["delivery_summary"]

    def test_explicit_remote_refresh_and_main_push_are_independent(self):
        self.clean_main()
        self.assertEqual(git_status.read_github_main(self.source, "main", self.local_commit)["state"], "unknown")
        with local_github_transport(self.bare_upstream), patch.object(git_status, "_run", wraps=git_status._run) as commands:
            value = self.summary()
            self.assertEqual(value["main"]["state"], "confirmed")
            self.assertEqual(value["remote"]["reason"], "not_checked")
            self.assertFalse(any("ls-remote" in call.args[2] for call in commands.call_args_list),
                             "A local read may inspect identity/configuration, never contact the remote")
            value = self.summary(True)
            self.assertEqual(value["remote"]["state"], "pending")
            self.git(self.source, "push", "origin", "main")
            self.assertEqual(self.summary(True)["state"], "confirmed")
            self.write(self.source, "new-source.py", "untracked source is not automatically excluded\n")
            value = self.summary(True)
            self.assertEqual((value["main"]["state"], value["remote"]["state"], value["state"]),
                ("confirmed", "confirmed", "needs_attention"))
            self.assertEqual(value["workspace"]["untracked"], 1)
            self.assertEqual(value["request"]["targets"][0]["target_branch"], "main")
        self.assertEqual(self.invocations, [])
        for query in ("refresh_remote=0", "refresh_remote=1&refresh_remote=1", "refresh_remote="):
            code, _ = self.request("GET", f"/api/rooms/{self.room_id}/code-status?{query}")
            self.assertEqual(code, 400)
        self.assertFalse((self.source / ".git/FETCH_HEAD").exists())

    def test_related_worktree_branch_is_included_without_scanning_other_branches(self):
        self.clean_main()
        worktree = self.root / "linked-worktree"
        self.git(self.source, "worktree", "add", "-b", "linked", str(worktree))
        self.write(worktree, "linked.py", "linked work\n")
        self.commit(worktree, "linked work")
        # This exact accepted-source candidate is injected at the existing source
        # boundary, not a synthetic summary or a scan of arbitrary Git branches.
        candidate = {"id": "delivery:linked", "kind": "delivery", "label": "Linked", "path": str(worktree)}
        with patch.object(self.service, "_code_delivery_workspaces", return_value=([candidate], [])):
            # A report reference alone does not make another branch a delivery target.
            self.assertEqual(self.summary()["main"]["state"], "confirmed")
            code, binding = self.request("POST", f"/api/rooms/{self.room_id}/delivery-repository", {"workspace_id":"delivery:linked"})
            self.assertEqual(code, 200, binding)
            self.assertEqual(self.summary()["main"]["reason"], "branch_not_on_mainline")
            self.git(self.source, "merge", "--ff-only", "linked")
            self.assertEqual(self.summary()["main"]["state"], "confirmed")
        self.git(self.source, "checkout", "-b", "unbound")
        self.write(self.source, "unbound.py", "unbound\n")
        self.commit(self.source, "unbound")
        self.git(self.source, "checkout", "main")
        self.assertEqual(self.summary()["main"]["state"], "confirmed")

    def test_missing_mainline_keeps_user_request_available(self):
        self.clean_main()
        self.git(self.source, "branch", "-m", "main", "trunk")
        value = self.summary()
        self.assertEqual(value["main"]["state"], "unknown")
        self.assertTrue(value["request"]["available"])
        self.assertEqual(value["request"]["repository_roots"], [str(self.source)])


class PrivateDeliveryTests(unittest.TestCase):
    committed_at = git_fixture.GitStatusTests.committed_at
    setUp = git_fixture.GitStatusTests.setUp
    git = git_fixture.GitStatusTests.git
    repository = git_fixture.GitStatusTests.repository
    write = git_fixture.GitStatusTests.write
    commit = git_fixture.GitStatusTests.commit
    export = native_fixture.NativeWorkspaceTests.export

    def test_retained_old_tree_is_checked_after_current_workspace_replacement(self):
        source = self.repository()
        self.write(source, "one.py", "old\n")
        self.commit(source)
        retained_source = self.export("retained", {"one.py": "retained\n"})
        expected = hashlib.sha256(b"retained\n").hexdigest()
        retained = {"source_project": str(retained_source[0]), "observation": {"files": [
            {"path": "one.py", "sha256": expected, "previous_sha256": hashlib.sha256(b"old\n").hexdigest()}]}}
        def summary(canonical, retained_states=(retained,), changes=None):
            return code_delivery.summarize([{"id": "project", "kind": "project", "path": str(source)}],
                statuses={"project": git_status.read_status(source)}, native_states=[], canonical=canonical,
                canonical_changes=changes or {}, retained_states=retained_states, now=lambda: 1234)
        baseline = {"one.py": hashlib.sha256(b"old\n").hexdigest()}
        self.assertEqual(summary(baseline)["private"]["reason"], "retained_work_not_published")
        self.assertEqual(summary(baseline)["main"]["state"], "confirmed")
        self.assertEqual(summary(baseline)["state"], "pending")
        current = {"one.py": expected}
        self.write(source, "one.py", "retained\n")
        self.commit(source)
        self.assertEqual(summary(current, changes={"one.py": {"sha256": expected}})["main"]["state"], "confirmed")
        (retained_source[0] / "one.py").write_text("unexpected drift\n")
        self.assertEqual(summary(current)["private"]["state"], "unknown")
        self.assertEqual(summary(current)["main"]["state"], "confirmed")
        self.assertEqual(summary(current)["state"], "unknown")
        (retained_source[0] / "one.py").unlink()
        self.assertEqual(summary(current)["private"]["state"], "unknown")
        self.assertEqual(summary(current)["main"]["state"], "confirmed")
        self.assertEqual(summary(current)["state"], "unknown")
        # The store's owner-aware helper can supersede the old proposal; an
        # empty resolved list must not resurrect raw historic retained paths.
        self.assertEqual(summary(current, retained_states=())["main"]["state"], "confirmed")

    def test_private_baseline_is_not_user_branch_and_published_dirty_is_not_pending(self):
        source = self.repository()
        self.write(source, "one.py", "old\n")
        self.commit(source)
        exported = self.export("exported", {"one.py": "old\n"})
        state = native_workspace.prepare_workspace(self.root / "private", *exported)
        private = Path(state["project"])
        workspaces = [{"id": "project", "kind": "project", "path": str(source)},
            {"id": "native:claude", "kind": "native", "path": str(private)}]
        def summary(canonical):
            return code_delivery.summarize(workspaces, statuses={"project": git_status.read_status(source)},
                native_states=[state], canonical=canonical,
                canonical_changes={name: {"sha256": sha} for name, sha in canonical.items()
                    if name != "one.py" or sha != hashlib.sha256(b"old\n").hexdigest()}, now=lambda: 1234)
        baseline = {"one.py": hashlib.sha256(b"old\n").hexdigest()}
        self.assertEqual(summary(baseline)["main"]["state"], "confirmed")
        (private / "one.py").write_text("new\n")
        self.assertEqual(summary(baseline)["private"]["reason"], "private_work_not_published")
        self.assertEqual(summary(baseline)["main"]["state"], "confirmed")
        self.assertEqual(summary(baseline)["state"], "pending")
        current = {"one.py": hashlib.sha256(b"new\n").hexdigest()}
        self.assertEqual(summary(current)["published"]["reason"], "published_work_not_on_mainline")
        self.write(source, "one.py", "new\n")
        self.commit(source, "publish actual source")
        self.assertEqual(summary(current)["main"]["state"], "confirmed")
        (private / "one.py").write_text("old\n")
        self.assertEqual(summary(current)["main"]["state"], "confirmed", "An old unchanged copy is not new unpublished work")
        (private / "one.py").write_text("new\n")
        no_mapping = [dict(item, kind="delivery") if item["kind"] == "project" else item for item in workspaces]
        unknown = code_delivery.summarize(no_mapping, statuses={"project": git_status.read_status(source)},
            native_states=[state], canonical=current,
            canonical_changes={"one.py": {"sha256": current["one.py"]}}, now=lambda: 1234)
        self.assertEqual(unknown["published"]["reason"], "published_work_mapping_unknown")
        self.assertEqual(unknown["main"]["state"], "unknown")
        self.assertEqual(unknown["main"]["reason"], "delivery_repository_unbound")
        self.assertEqual(unknown["state"], "unknown")
        # A newly created source file can already be published while its private
        # receipt still precedes the publication; compare actual bytes to S.
        (private / "added.py").write_text("published new\n")
        current["added.py"] = hashlib.sha256(b"published new\n").hexdigest()
        self.assertEqual(summary(current)["published"]["reason"], "published_work_not_on_mainline")
        self.write(source, "added.py", "published new\n")
        self.commit(source, "publish added source")
        self.assertEqual(summary(current)["main"]["state"], "confirmed")
        (private / "one.py").unlink()
        self.assertEqual(summary(current)["private"]["state"], "pending")
        private.rename(private.with_name("unavailable"))
        self.assertEqual(summary(current)["private"]["state"], "unknown")
        self.assertEqual(summary(current)["main"]["state"], "confirmed")
        self.assertEqual(summary(current)["state"], "unknown")

    def test_github_parser_and_remote_ahead_use_actual_objects(self):
        source = self.repository()
        self.write(source, "one.py", "base\n")
        base = self.commit(source)
        bare = self.root / "remote.git"
        bare.mkdir()
        self.git(bare, "init", "--bare", "--initial-branch=main")
        self.git(source, "remote", "add", "origin", str(bare))
        self.git(source, "push", "origin", "main")
        self.write(source, "one.py", "remote ahead\n")
        remote = self.commit(source)
        self.git(source, "push", "origin", "main")
        with local_github_transport(bare):
            result = git_status.read_github_main(source, "main", base)
        self.assertEqual(result["state"], "confirmed", result)
        self.assertNotEqual(base, remote)
        for url, valid in (("https://github.com/example/repository.git", True),
                ("git@github.com:example/repository.git", True), ("ssh://git@github.com/example/repository.git", True),
                ("ext::sh dangerous", False), ("https://notgithub.com/a/b", False), ("https://secret@github.com/a/b", False)):
            self.git(source, "remote", "set-url", "origin", url)
            if valid:
                expected_url = "https://github.com/example/repository.git" if url.startswith("https:") else "ssh://git@github.com/example/repository.git"
                self.assertEqual(git_status._github_url(self.git_binary, source, time.monotonic()+5), expected_url)
            else:
                with self.assertRaises(git_status._GitError):
                    git_status._github_url(self.git_binary, source, time.monotonic()+5)
        self.git(source, "remote", "set-url", "origin", "git@github.com:example/repository.git")
        self.git(source, "config", "url.ext::dangerous.insteadOf", "git@github.com:")
        with self.assertRaises(git_status._GitError):
            git_status._github_url(self.git_binary, source, time.monotonic()+5)
        commands = []
        original = git_status._run
        def observe(command, directory, arguments, deadline, **kwargs):
            if "ls-remote" in arguments:
                commands.append(arguments)
                arguments = ["-c", "protocol.file.allow=always", *arguments]
            return original(command, directory, arguments, deadline, **kwargs)
        with patch.object(git_status, "_run", side_effect=observe):
            with patch.object(git_status, "_github_url", return_value=str(bare)):
                result = git_status.read_github_main(source, "main", base)
        self.assertEqual(result["state"], "confirmed")
        self.assertIn("core.sshCommand=ssh -oBatchMode=yes", commands[0])
        self.assertIn("protocol.allow=never", commands[0])
