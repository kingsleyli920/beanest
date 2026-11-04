"""Host capacity observations over real SQLite and immutable workspaces."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.room_service import RoomService, _project_digest
from tests.test_team_service import task


class TeamProjectCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()

        async def forbidden_provider(*args, **kwargs):
            raise AssertionError("This test must not invoke a provider")

        self.service = RoomService(root / "rooms.sqlite3", root / "workspaces", invoke=forbidden_provider)
        self.store = self.service.store
        self.room_id, _ = self.store.create_room("Capacity-aware collaboration", "claude", str(uuid4()),
            collaboration=True, mode="python_artifacts", participants=["claude", "codex"])
        self.project = self.service._project(self.room_id, create=True)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temporary.cleanup()

    def context(self, job):
        return json.loads(self.service._team_prompt(job).split("CONTEXT DATA:\n", 1)[1])

    def finish(self, job, commands, **kwargs):
        self.store.finish_job(job["id"], message="Concrete result", team_commands=commands, **kwargs)

    def plan(self):
        author = self.store.claim(self.room_id, "claude")
        peer = self.store.claim(self.room_id, "codex")
        alpha = task("alpha", "alpha.py")
        alpha["write_paths"].append("alpha_extra.py")
        self.finish(author, [alpha, task("beta", "beta.py"),
            {"type": "claim", "task_id": "alpha"}, {"type": "plan_ready"}])
        self.finish(peer, [{"type": "claim", "task_id": "beta"}, {"type": "plan_ready"}])
        confirmation = self.store.claim(self.room_id, "codex")
        self.assertEqual(confirmation["kind"], "team_coordinate")
        self.finish(confirmation, [{"type": "plan_ready"}])
        return self.store.claim(self.room_id, "claude"), self.store.claim(self.room_id, "codex")

    def complete(self, job, contents):
        self.project.apply(job["provider"], str(uuid4()), self.project.snapshot()["revision"],
            [{"path": path, "previous_sha256": None, "content": content} for path, content in contents.items()])
        current = self.project.snapshot()
        self.finish(job, [{"type": "complete", "task_id": job["team"]["task_id"], "result": "Delivered owned files"}],
            artifact_revision=current["revision"], artifact_digest=_project_digest(current),
            artifact_files=[{"path": file["path"], "sha256": file["sha256"]}
                            for file in current["files"] if file["path"] in contents])

    def assert_capacity(self, context):
        files = {file["path"]: file for file in context["current_project_files"]}
        self.assertEqual({path: file["source_bytes"] for path, file in files.items()},
                         {"alpha.py": 10, "alpha_extra.py": 5, "beta.py": 8})
        self.assertEqual(context["current_project_capacity"]["source_bytes_by_owner"], {"claude": 15, "codex": 8})
        self.assertEqual(context["current_project_capacity"]["source_bytes"], 23)
        self.assertEqual(context["current_project_capacity"]["file_count"], 3)
        self.assertEqual(context["current_project_capacity"]["unit"], "UTF-8 bytes")
        self.assertEqual(files["alpha.py"]["sha256"], hashlib.sha256("# 汉🙂\n".encode()).hexdigest())

    def test_empty_planning_capacity_has_no_invented_owner_allocation(self):
        job = self.store.claim(self.room_id, "claude")
        context = self.context(job)
        self.assertEqual(context["proposal_base_revision"], 0)
        self.assertEqual(context["current_project_files"], [])
        self.assertEqual(context["current_project_capacity"]["source_bytes"], 0)
        self.assertEqual(context["current_project_capacity"]["source_bytes_by_owner"], {})

    def test_utf8_capacity_aggregates_multiple_files_for_each_actual_owner(self):
        alpha, beta = self.plan()
        self.complete(alpha, {"alpha.py": "# 汉🙂\n", "alpha_extra.py": "# é\n"})
        self.project.apply("codex", str(uuid4()), 1,
            [{"path": "beta.py", "previous_sha256": None, "content": "# 🚀\r\n"}])
        before = self.project.snapshot()
        context = self.context(beta)
        self.assert_capacity(context)
        self.assertEqual(context["proposal_base_revision"], 2)
        self.assertEqual(self.project.snapshot(), before)
        self.assertFalse(any("source_bytes" in file for file in before["files"]))

    def test_old_result_review_capacity_stays_bound_to_current_proposal_base(self):
        alpha, beta = self.plan()
        self.complete(alpha, {"alpha.py": "# 汉🙂\n", "alpha_extra.py": "# é\n"})
        self.complete(beta, {"beta.py": "# 🚀\r\n"})
        review = self.store.claim(self.room_id, "codex")
        self.assertEqual(review["team"]["job_phase"], "review")
        context = self.context(review)
        self.assertEqual(context["task"]["id"], "alpha")
        self.assertEqual(context["task"]["result_revision"], 1)
        self.assertEqual(context["project"]["revision"], 1)
        self.assertEqual({file["path"] for file in context["project"]["files"]}, {"alpha.py", "alpha_extra.py"})
        self.assertEqual(context["proposal_base_revision"], 2)
        self.assert_capacity(context)


if __name__ == "__main__":
    unittest.main()
