"""Git status against isolated temporary repositories; no user repository reads."""
from datetime import datetime
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from local_agent_coordinator import git_status
from local_agent_coordinator.git_status import read_status


class GitStatusTests(unittest.TestCase):
    def test_expired_shared_deadline_never_starts_a_git_process(self):
        with patch.object(git_status.subprocess, "Popen") as spawn:
            with self.assertRaises(git_status._GitError) as caught:
                git_status._run("git", self.root, ["status"], time.monotonic() - 1)
        self.assertEqual(caught.exception.code, "git_timeout")
        spawn.assert_not_called()

    def test_delivery_ref_root_mismatch_is_typed_unknown_without_resolving_aliases(self):
        for root in (b"relative-root\n", os.fsencode(str(self.root / "other")) + b"\n"):
            with self.subTest(root=root), patch.object(git_status, "_run", return_value=root):
                with self.assertRaises(git_status._GitError) as caught:
                    git_status.local_delivery_refs(self.root, "main")
                self.assertEqual(caught.exception.code, "git_invalid_output")

    committed_at = "2026-11-01T12:34:56+00:00"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.git_binary = shutil.which("git")
        self.assertIsNotNone(self.git_binary, "Real temporary-repository tests require Git")
        self.git_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.git_environment.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Temporary Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Temporary Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": self.committed_at, "GIT_COMMITTER_DATE": self.committed_at,
        })
        environment = patch.dict(os.environ, self.git_environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, repository, *arguments, expected=0):
        result = subprocess.run([self.git_binary, *arguments], cwd=repository,
            env=self.git_environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, f"Temporary Git {arguments[:2]}: {result.stderr}")
        return result.stdout.strip()

    def repository(self, name="repository"):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "--initial-branch=main", "--object-format=sha1")
        return path

    def write(self, repository, name, content):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def commit(self, repository, subject="baseline"):
        self.git(repository, "add", "--all")
        self.git(repository, "commit", "-m", subject)
        return self.git(repository, "rev-parse", "HEAD")

    def ready(self, path):
        result = read_status(path, now=lambda: 1234.0)
        self.assertEqual(result["state"], "ready", result.get("reason"))
        return result

    def assert_counts(self, result, *, total, staged=0, unstaged=0, untracked=0, conflicted=0):
        self.assertEqual(result["uncommitted"], {"total": total, "staged": staged,
            "unstaged": unstaged, "untracked": untracked, "conflicted": conflicted, "tracked": total - untracked})

    def seed_local_upstream(self, repository):
        """Diverge two local refs by one commit each, with no remote or fetch."""
        base = self.git(repository, "rev-parse", "HEAD")
        self.git(repository, "checkout", "-b", "upstream")
        self.write(repository, "upstream.txt", "Only the upstream branch.\n")
        self.commit(repository, "upstream side")
        self.git(repository, "checkout", "main")
        self.write(repository, "local.txt", "Only the local branch.\n")
        local = self.commit(repository, "local ahead: 中文 subject")
        self.git(repository, "config", "branch.main.remote", ".")
        self.git(repository, "config", "branch.main.merge", "refs/heads/upstream")
        return base, local

    def test_staged_and_unstaged_share_one_total_and_file_list_is_bounded(self):
        repository = self.repository()
        tracked = self.write(repository, "tracked.txt", "original\n")
        self.commit(repository)
        tracked.write_text("staged\n")
        self.git(repository, "add", "tracked.txt")
        tracked.write_text("staged plus unstaged\n")
        self.write(repository, "未跟踪 空格.txt", "untracked\n")
        result = self.ready(repository)
        self.assert_counts(result, total=2, staged=1, unstaged=1, untracked=1)
        tracked_status = next(item for item in result["files"] if item["path"] == "tracked.txt")
        self.assertEqual((tracked_status["index"], tracked_status["worktree"]), ("M", "M"))
        self.assertFalse(result["files_truncated"])
        for index in range(25):
            self.write(repository, f"extra-{index:02}.txt", "additional untracked file\n")
        result = self.ready(repository)
        self.assert_counts(result, total=27, staged=1, unstaged=1, untracked=26)
        self.assertEqual(len(result["files"]), 20)
        self.assertTrue(result["files_truncated"])

    def test_rename_nul_second_path_preserves_original_without_extra_file(self):
        repository = self.repository()
        self.write(repository, "旧 名.txt", "unchanged bytes across rename\n")
        self.commit(repository)
        self.git(repository, "mv", "旧 名.txt", "新 名.txt")
        result = self.ready(repository)
        self.assert_counts(result, total=1, staged=1)
        self.assertEqual(len(result["files"]), 1)
        renamed = result["files"][0]
        self.assertEqual((renamed["path"], renamed["original_path"]), ("新 名.txt", "旧 名.txt"))
        self.assertEqual((renamed["index"], renamed["worktree"]), ("R", "."))

    def test_conflict_is_separate_from_staged_and_unstaged_counts(self):
        repository = self.repository()
        self.write(repository, "conflict.txt", "base\n")
        self.commit(repository)
        self.git(repository, "checkout", "-b", "other")
        self.write(repository, "conflict.txt", "other branch\n")
        self.commit(repository, "other conflict side")
        self.git(repository, "checkout", "main")
        self.write(repository, "conflict.txt", "main branch\n")
        self.commit(repository, "main conflict side")
        self.git(repository, "merge", "other", expected=1)
        result = self.ready(repository)
        self.assert_counts(result, total=1, conflicted=1)
        self.assertEqual(result["files"][0]["path"], "conflict.txt")
        self.assertEqual((result["files"][0]["index"], result["files"][0]["worktree"]), ("U", "U"))

    def test_empty_repository_missing_upstream_and_failures_are_distinct(self):
        repository = self.repository()
        result = self.ready(repository)
        self.assertTrue(result["unborn"])
        self.assertFalse(result["detached"])
        self.assertEqual(result["branch"], "main")
        self.assert_counts(result, total=0)
        self.assertIsNone(result["last_commit"])
        self.assertEqual(result["upstream"]["state"], "none")
        self.assertIsNone(result["upstream"]["ahead"])
        self.assertIsNone(result["upstream"]["behind"])
        outside = self.root / "not-git"
        outside.mkdir()
        result = read_status(outside)
        self.assertEqual(result["state"], "not_git")
        self.assertIsNone((result.get("uncommitted") or {}).get("total"))
        empty_path = self.root / "no-executables"
        empty_path.mkdir()
        with patch.dict(os.environ, {"PATH": str(empty_path)}):
            result = read_status(repository)
        self.assertEqual(result["state"], "unavailable")
        self.assertTrue(result["reason"])
        self.assertIsNone((result.get("uncommitted") or {}).get("total"))
        self.assertEqual(result["upstream"]["state"], "unknown")
        self.write(repository, ".gitignore", "isolated/\n")
        self.commit(repository, "Ignore an isolated directory")
        ignored = repository / "isolated"
        ignored.mkdir()
        self.write(ignored, "notes.txt", "No tracked history in this scoped directory.\n")
        result = read_status(ignored)
        self.assertEqual((result["state"], result["reason"]), ("unavailable", "no_tracked_project"))
        self.assertIsNone(result["uncommitted"]["total"])

    def test_timeout_oversized_and_empty_output_remain_unknown(self):
        repository = self.repository()
        self.write(repository, "tracked.txt", "baseline\n")
        self.commit(repository)

        def empty_status_output(command, directory, arguments, deadline, **kwargs):
            if arguments == ["rev-parse", "--show-toplevel"]:
                return (str(repository) + "\n").encode()
            if arguments[0] == "config":
                return b""
            self.assertEqual(arguments[0], "status")
            return b""

        cases = (
            ("timeout", patch.object(git_status, "READ_SECONDS", 0), "git_timeout"),
            ("output limit", patch.object(git_status, "MAX_OUTPUT_BYTES", 1), "git_output_too_large"),
            ("empty status", patch.object(git_status, "_run", side_effect=empty_status_output), "git_invalid_output"),
        )
        for name, override, reason in cases:
            with self.subTest(name=name), override:
                result = read_status(repository)
                self.assertEqual((result["state"], result["reason"]), ("unavailable", reason))
                self.assertEqual(result["uncommitted"], dict.fromkeys(
                    ("total", "staged", "unstaged", "untracked", "conflicted", "tracked")))
                self.assertEqual(result["upstream"]["state"], "unknown")
                self.assertIsNone(result["last_commit"])

    def test_active_clean_filter_is_not_executed_but_unused_or_smudge_only_is_readable(self):
        repository = self.repository()
        self.write(repository, "tracked.txt", "baseline\n")
        self.write(repository, ".gitattributes", "tracked.txt filter=guard\n")
        self.commit(repository)
        self.write(repository, "tracked.txt", "working changes\n")
        marker = self.root / "filter-was-called"
        executable = self.root / "filter-command"
        executable.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
            f"Path({str(marker)!r}).touch()\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
        executable.chmod(0o700)
        command = shlex.quote(str(executable))
        self.git(repository, "config", "filter.unused.clean", command)
        with self.subTest(filter="unused"):
            self.assert_counts(self.ready(repository), total=1, unstaged=1)
            self.assertFalse(marker.exists())
        self.git(repository, "config", "filter.guard.clean", command)
        with self.subTest(filter="active clean"):
            result = read_status(repository)
            self.assertEqual((result["state"], result["reason"]), ("unavailable", "git_filter_required"))
            self.assertIsNone(result["uncommitted"]["total"])
            self.assertFalse(marker.exists())
        self.git(repository, "config", "--unset", "filter.guard.clean")
        self.git(repository, "config", "filter.guard.smudge", command)
        with self.subTest(filter="smudge only"):
            self.assert_counts(self.ready(repository), total=1, unstaged=1)
            self.assertFalse(marker.exists())

    def test_local_upstream_worktree_and_subdirectory_scope_ignore_git_environment(self):
        repository = self.repository("登记 项目")
        self.write(repository, "child/owned.txt", "child baseline\n")
        self.write(repository, "sibling.txt", "outside child\n")
        base = self.commit(repository)
        _, local = self.seed_local_upstream(repository)
        result = self.ready(repository)
        self.assertEqual(result["branch"], "main")
        self.assertEqual((result["upstream"]["state"], result["upstream"]["name"],
                          result["upstream"]["ahead"], result["upstream"]["behind"], result["upstream"]["scope"]),
                         ("ready", "upstream", 1, 1, "repository_local_refs"))
        self.assertEqual({key: result["last_commit"][key] for key in ("hash", "short_hash", "subject")}, {"hash": local,
            "short_hash": self.git(repository, "rev-parse", "--short", "HEAD"),
            "subject": "local ahead: 中文 subject"})
        self.assertEqual(datetime.fromisoformat(result["last_commit"]["committed_at"]),
                         datetime.fromisoformat(self.committed_at))
        worktree = self.root / "原生 工作树"
        self.git(repository, "worktree", "add", "-b", "feature", str(worktree), "main")
        self.write(worktree, "child/owned.txt", "child changed\n")
        self.write(worktree, "sibling.txt", "parent change must not count for child\n")
        foreign = self.repository("foreign")
        self.write(foreign, "bait.txt", "unrelated repo\n")
        self.commit(foreign)
        marker = self.root / "fsmonitor-was-called"
        monitor = self.root / "unsafe-fsmonitor"
        monitor.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
        monitor.chmod(0o700)
        self.git(repository, "config", "core.fsmonitor", str(monitor))
        with patch.dict(os.environ, {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                                     "GIT_INDEX_FILE": str(foreign / ".git/index")}):
            child = self.ready(worktree / "child")
        self.assertEqual(child["repository_root"], str(worktree))
        self.assertEqual(child["project_path"], "child")
        self.assertEqual(child["branch"], "feature")
        self.assert_counts(child, total=1, unstaged=1)
        self.assertEqual(Path(child["files"][0]["path"]).name, "owned.txt")
        self.assertEqual(child["last_commit"]["hash"], base)
        self.assertFalse(marker.exists(), "Read-only status must disable configured fsmonitor execution")
        self.assertFalse((repository / ".git/FETCH_HEAD").exists(), "Status must use existing local refs without fetching")

    def test_mainline_ancestry_and_untracked_assets_are_separate(self):
        repository = self.repository()
        self.write(repository, "code.py", "baseline\n")
        baseline = self.commit(repository)
        self.git(repository, "checkout", "-b", "feature")
        self.write(repository, "code.py", "implemented\n")
        feature = self.commit(repository, "feature")
        self.write(repository, "preview.mp4", "untracked media\n")
        result = self.ready(repository)
        self.assert_counts(result, total=1, untracked=1)
        self.assertEqual(result["mainline"], {"state": "ready", "reason": None, "name": "main",
            "ref": "refs/heads/main", "source": "single_local_conventional", "relation": "unmerged",
            "ahead": 1, "behind": 0, "head": baseline, "scope": "repository_local_refs"})
        self.assertTrue(result["merge_request"]["available"])
        self.assertEqual(result["merge_request"]["head"], feature)
        self.assertEqual(result["merge_request"]["mainline_head"], baseline)
        self.git(repository, "branch", "-f", "main", feature)
        result = self.ready(repository)
        self.assertEqual(result["mainline"]["relation"], "merged")
        self.assertEqual(result["merge_request"]["reason"], "already_merged")
        self.assertFalse(result["merge_request"]["available"])
        self.assertTrue((repository / "preview.mp4").exists())
        self.assertFalse((repository / ".git/FETCH_HEAD").exists())

    def test_origin_head_is_authoritative_local_record_not_remote_refresh(self):
        repository = self.repository()
        self.write(repository, "code.py", "baseline\n")
        baseline = self.commit(repository)
        self.git(repository, "branch", "master")
        self.git(repository, "update-ref", "refs/remotes/origin/production", baseline)
        self.git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/production")
        self.git(repository, "checkout", "-b", "feature")
        self.write(repository, "code.py", "feature\n")
        self.commit(repository, "feature")
        result = self.ready(repository)
        self.assertEqual((result["mainline"]["name"], result["mainline"]["source"]),
                         ("production", "origin_head"))
        self.assertEqual(result["mainline"]["scope"], "repository_local_refs")
        self.assertTrue(result["merge_request"]["available"])
        self.assertFalse((repository / ".git/FETCH_HEAD").exists())

    def test_ambiguous_missing_unborn_and_detached_do_not_offer_merge(self):
        repository = self.repository()
        result = self.ready(repository)
        self.assertEqual(result["merge_request"]["reason"], "unborn_branch")
        self.write(repository, "code.py", "baseline\n")
        self.commit(repository)
        self.git(repository, "branch", "master")
        self.assertEqual(self.ready(repository)["mainline"]["reason"], "mainline_ambiguous")
        self.git(repository, "branch", "-d", "master")
        self.assertEqual(self.ready(repository)["merge_request"]["reason"], "already_on_mainline")
        self.git(repository, "checkout", "--detach")
        self.write(repository, "code.py", "detached commit\n")
        self.commit(repository, "detached")
        result = self.ready(repository)
        self.assertEqual(result["mainline"]["relation"], "unmerged")
        self.assertFalse(result["merge_request"]["available"])
        self.assertEqual(result["merge_request"]["reason"], "detached_head")
        self.git(repository, "branch", "-m", "main", "trunk")
        self.assertEqual(self.ready(repository)["mainline"]["reason"], "mainline_unknown")

    def test_diverged_mainline_counts_are_not_upstream_push_counts(self):
        repository = self.repository()
        self.write(repository, "base.txt", "baseline\n")
        self.commit(repository)
        self.git(repository, "checkout", "-b", "feature")
        self.write(repository, "feature.txt", "feature\n")
        self.commit(repository, "feature")
        self.git(repository, "checkout", "main")
        self.write(repository, "main.txt", "main\n")
        self.commit(repository, "main update")
        self.git(repository, "checkout", "feature")
        result = self.ready(repository)
        self.assertEqual((result["mainline"]["ahead"], result["mainline"]["behind"]), (1, 1))
        self.assertEqual(result["upstream"]["state"], "none")
        self.assertTrue(result["merge_request"]["available"])

    def test_mainline_read_failure_keeps_file_status_available(self):
        repository = self.repository()
        self.write(repository, "code.py", "baseline\n")
        self.commit(repository)
        self.write(repository, "code.py", "dirty\n")
        original = git_status._run

        def fail_ancestry(command, directory, arguments, deadline, **kwargs):
            if arguments[0] == "rev-list":
                raise git_status._GitError("git_timeout")
            return original(command, directory, arguments, deadline, **kwargs)

        with patch.object(git_status, "_run", side_effect=fail_ancestry):
            result = self.ready(repository)
        self.assert_counts(result, total=1, unstaged=1)
        self.assertEqual(result["mainline"]["reason"], "git_timeout")
        self.assertFalse(result["merge_request"]["available"])

    def test_on_mainline_can_still_be_ahead_of_recorded_remote_mainline(self):
        repository = self.repository()
        self.write(repository, "code.py", "baseline\n")
        baseline = self.commit(repository)
        self.git(repository, "update-ref", "refs/remotes/origin/main", baseline)
        self.git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.write(repository, "code.py", "local main update\n")
        self.commit(repository, "local main update")
        result = self.ready(repository)
        self.assertEqual(result["mainline"]["relation"], "same_branch")
        self.assertEqual((result["mainline"]["ahead"], result["mainline"]["behind"]), (1, 0))
        self.assertEqual(result["mainline"]["ref"], "refs/remotes/origin/main")
        self.assertFalse(result["merge_request"]["available"], "Being ahead on main is not a branch merge")
        self.assertFalse((repository / ".git/FETCH_HEAD").exists())

    def test_directory_files_and_last_commit_are_scoped_but_mainline_is_whole_branch(self):
        repository = self.repository()
        self.write(repository, "child/code.py", "child baseline\n")
        baseline = self.commit(repository)
        self.git(repository, "checkout", "-b", "feature")
        self.write(repository, "sibling.py", "only sibling changed\n")
        self.commit(repository, "sibling feature")
        self.write(repository, "untracked-outside.txt", "outside\n")
        result = self.ready(repository / "child")
        self.assert_counts(result, total=0)
        self.assertEqual(result["project_path"], "child")
        self.assertEqual(result["last_commit"]["hash"], baseline)
        self.assertEqual((result["mainline"]["relation"], result["mainline"]["ahead"]), ("unmerged", 1))
        self.assertTrue(result["merge_request"]["available"])


if __name__ == "__main__":
    unittest.main()
