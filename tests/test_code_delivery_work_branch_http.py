"""The delivery card's push verdict on a work branch, over real HTTP and real Git."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from local_agent_coordinator import code_delivery, git_status
from test_code_delivery import local_github_transport
import test_code_status_http as http_fixture


class WorkBranchDeliveryHTTPTests(unittest.TestCase):
    committed_at = http_fixture.CodeStatusHTTPTests.committed_at
    git = http_fixture.CodeStatusHTTPTests.git
    repository = http_fixture.CodeStatusHTTPTests.repository
    write = http_fixture.CodeStatusHTTPTests.write
    commit = http_fixture.CodeStatusHTTPTests.commit
    request = http_fixture.CodeStatusHTTPTests.request
    _close_server = http_fixture.CodeStatusHTTPTests._close_server
    tearDown = http_fixture.CodeStatusHTTPTests.tearDown

    def setUp(self):
        http_fixture.CodeStatusHTTPTests.setUp(self)
        self.git(self.source, "restore", "--staged", "tracked.txt")
        self.git(self.source, "restore", "tracked.txt")
        (self.source / "未跟踪 说明.txt").unlink()
        self.git(self.source, "push", "origin", "main")
        # The real room compares against origin/main through origin/HEAD.
        self.git(self.source, "remote", "set-head", "origin", "main")

    def summary(self, refresh=True):
        query = "?refresh_remote=1" if refresh else ""
        code, result = self.request("GET", f"/api/rooms/{self.room_id}/code-status{query}")
        self.assertEqual(code, 200, result)
        return result["delivery_summary"]

    def commits(self, count, stem):
        for index in range(count):
            self.write(self.source, f"{stem}-{index}.py", f"{stem} {index}\n")
            self.commit(self.source, f"{stem} {index}")

    def branch(self, name="codex/ranger-world-time"):
        self.git(self.source, "checkout", "-b", name)

    def test_pushed_work_branch_is_confirmed_from_its_own_tracking_ref(self):
        """Catches a green GitHub verdict that comes from mainline instead of the branch being worked on."""
        self.branch()
        self.commits(2, "pushed")
        self.git(self.source, "push", "--set-upstream", "origin", "codex/ranger-world-time")
        with patch.object(git_status, "read_github_main", side_effect=AssertionError("mainline must not decide")):
            value = self.summary()
        self.assertEqual(value["remote"]["state"], "confirmed")
        self.assertIsNone(value["remote"]["reason"])
        self.assertIsNone(value["remote"]["checked_at"])
        self.assertEqual(value["branch"], {"name": "codex/ranger-world-time", "detached": False, "mainline": "main",
            "relation": "unmerged", "ahead": 2, "behind": 0, "work_branch": True,
            "upstream": {"state": "ready", "name": "origin/codex/ranger-world-time", "ahead": 0, "behind": 0}})
        self.assertEqual((value["main"]["state"], value["main"]["reason"]), ("pending", "branch_not_on_mainline"))

    def test_unpushed_commits_are_pending_even_when_mainline_is_fully_synced(self):
        """Catches the 2026-11-08 room: unpushed commits while the card showed GitHub as synced."""
        self.branch()
        self.commits(1, "pushed")
        self.git(self.source, "push", "--set-upstream", "origin", "codex/ranger-world-time")
        self.commits(3, "local")
        with local_github_transport(self.bare_upstream):
            value = self.summary()
        self.assertEqual((value["remote"]["state"], value["remote"]["reason"]), ("pending", "work_branch_not_pushed"))
        self.assertEqual(value["branch"]["upstream"]["ahead"], 3)
        self.assertEqual(value["branch"]["ahead"], 4)
        self.assertEqual(value["state"], "pending")
        self.assertIn("work_branch_not_pushed", [issue["reason"] for issue in value["issues"] if issue["scope"] == "remote"])

    def test_branch_without_upstream_needs_a_reminder(self):
        """Catches a never-pushed branch reading as synced because there is nothing to compare."""
        self.branch("feature/no-remote")
        self.commits(1, "local")
        value = self.summary()
        self.assertEqual((value["remote"]["state"], value["remote"]["reason"]), ("pending", "work_branch_no_upstream"))
        self.assertEqual(value["branch"]["upstream"], {"state": "none", "name": None, "ahead": None, "behind": None})

    def test_missing_tracking_ref_reports_unknown(self):
        """Catches a deleted remote-tracking ref being guessed as pushed or as unpushed."""
        self.branch("feature/gone")
        self.commits(1, "work")
        self.git(self.source, "push", "--set-upstream", "origin", "feature/gone")
        self.git(self.source, "update-ref", "-d", "refs/remotes/origin/feature/gone")
        value = self.summary()
        self.assertEqual((value["remote"]["state"], value["remote"]["reason"]), ("unknown", "work_branch_upstream_unknown"))
        self.assertEqual(value["branch"]["upstream"]["state"], "unknown")

    def test_on_mainline_the_card_keeps_checking_github_main(self):
        """Catches the work-branch rule leaking onto mainline, where GitHub main is the real question."""
        self.commits(1, "main-local")
        with local_github_transport(self.bare_upstream):
            value = self.summary()
        self.assertEqual((value["remote"]["state"], value["remote"]["reason"]), ("pending", "local_mainline_not_on_remote"))
        self.assertIsNotNone(value["remote"]["checked_at"])
        self.assertEqual((value["branch"]["name"], value["branch"]["relation"], value["branch"]["work_branch"]), ("main", "same_branch", False))
        self.assertEqual(value["main"]["state"], "confirmed")

    def test_detached_head_stays_a_mainline_warning(self):
        """Catches relaxed handling for commits on no branch, which nothing keeps reachable."""
        self.git(self.source, "checkout", "--detach")
        self.commits(1, "detached")
        with local_github_transport(self.bare_upstream):
            value = self.summary()
        self.assertEqual((value["branch"]["name"], value["branch"]["detached"], value["branch"]["work_branch"]), (None, True, False))
        self.assertEqual((value["main"]["state"], value["main"]["reason"]), ("pending", "branch_not_on_mainline"))
        self.assertEqual(value["remote"]["state"], "confirmed")

    def test_branch_with_nothing_beyond_mainline_is_not_a_work_branch(self):
        """Catches a false no-remote reminder on a new branch that holds no commits of its own."""
        self.branch("feature/just-created")
        with local_github_transport(self.bare_upstream):
            value = self.summary()
        self.assertEqual((value["branch"]["relation"], value["branch"]["work_branch"]), ("merged", False))
        self.assertEqual((value["remote"]["state"], value["remote"]["reason"]), ("confirmed", None))

    def test_commits_already_on_local_mainline_keep_the_github_main_verdict(self):
        """Catches local main being ahead of GitHub hidden behind a pushed work branch."""
        self.branch("feature/merged-locally")
        self.commits(1, "merged")
        self.git(self.source, "push", "--set-upstream", "origin", "feature/merged-locally")
        self.git(self.source, "checkout", "main")
        self.git(self.source, "merge", "--ff-only", "feature/merged-locally")
        self.git(self.source, "checkout", "feature/merged-locally")
        with local_github_transport(self.bare_upstream):
            value = self.summary()
        self.assertEqual((value["branch"]["relation"], value["branch"]["work_branch"]), ("unmerged", False))
        self.assertEqual(value["main"]["state"], "confirmed")
        self.assertEqual((value["remote"]["state"], value["remote"]["reason"]), ("pending", "local_mainline_not_on_remote"))

    def test_published_mismatch_is_not_relaxed_on_a_work_branch(self):
        """Catches a real snapshot mismatch being softened along with the normal unmerged state."""
        self.branch("feature/snapshot")
        self.commits(1, "work")
        self.git(self.source, "push", "--set-upstream", "origin", "feature/snapshot")
        workspace = {"id": "project", "kind": "project", "label": "Project", "path": str(self.source)}
        value = code_delivery.summarize([workspace], statuses={"project": git_status.read_status(self.source)},
            native_states=[], canonical={"absent.py": "0" * 64}, canonical_changes={"absent.py": {"sha256": "0" * 64, "executable": False}},
            now=time.time, delivery_repository={"state": "bound", "workspace_id": "project", "reason": None})
        self.assertEqual((value["published"]["state"], value["published"]["reason"]), ("pending", "published_work_not_on_mainline"))
        self.assertEqual(value["published"]["files"]["missing"], 1)
        self.assertTrue(value["branch"]["work_branch"])
        self.assertEqual(value["state"], "pending")


if __name__ == "__main__":
    unittest.main()
