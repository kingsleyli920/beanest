"""A partner's normal native work is imported whole: deletions, binaries, chmod,
secret-looking text. Real SQLite, private trees and job loop; synthetic native
transport and a fictional project, no models."""
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
import zipfile

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.project_catalog import _SECRET
from local_agent_coordinator.room_service import RoomService
from test_native_room_service import NativeTransport, native_reply
from test_team_store import proposal
from wait_support import within


OLD_LOGO = b"\x89PNG\r\n\x1a\n\x00old-logo\xff"
NEW_LOGO = b"\x89PNG\r\n\x1a\n\x00new-logo\xfe\x00"
NEW_ART = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRfictional\x00"
SECRET_LOOKING = "export const client = makeClient({ apiKey: config.apiKey });\n"
PATHS = ["old.py", "art/logo.png", "art/new.png", "run.sh", "config.js"]


class NativeImportAllChangesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        (self.source / "art").mkdir(parents=True)
        (self.source / "main.py").write_text("VALUE = 1\n")
        (self.source / "old.py").write_text("OLD = True\n")
        (self.source / "art/logo.png").write_bytes(OLD_LOGO)
        (self.source / "run.sh").write_text("#!/bin/sh\necho fictional\n")
        (self.source / "run.sh").chmod(0o644)
        (self.source / "config.js").write_text("module.exports = {};\n")
        self.fake = NativeTransport()
        self.fake.handler = self.handler
        self.review_contexts = []
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", native_invoke=self.fake,
                                   registry=default_registry())
        self.registration = await self.service.register_project("Fictional game", str(self.source), selection_kind="folder")

    async def asyncTearDown(self):
        await self.service.shutdown()
        await asyncio.sleep(0)
        self.temp.cleanup()

    async def settle(self):
        async def wait():
            for _ in range(100):
                tasks = [task for task in self.service._workers.values() if not task.done()]
                if not tasks:
                    return
                for result in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
            self.fail("scheduler did not settle")
        await within(wait(), 20)
        self.assertIsNone(self.service._background_error)

    def handler(self, agent, context, cwd):
        phase = context["job"]["job_phase"]
        board, task = context["team"], context["task"]
        if phase in {"plan", "coordinate"}:
            commands = []
            if not board["tasks"] and agent == board["plan_author"]:
                commands += [proposal("T1", paths=PATHS, capability="python"), {"type": "claim", "task_id": "T1"}]
            if board["tasks"] or commands:
                commands.append({"type": "plan_ready"})
            return native_reply(context, commands)
        if phase == "work":
            # Ordinary game work: drop a dead module, add and replace art,
            # make a script runnable, wire a config key, keep a local .env.
            (cwd / "old.py").unlink()
            (cwd / "art/new.png").write_bytes(NEW_ART)
            (cwd / "art/logo.png").write_bytes(NEW_LOGO)
            (cwd / "run.sh").chmod(0o755)
            (cwd / "config.js").write_text(SECRET_LOOKING)
            (cwd / ".env").write_text("FICTIONAL_SETTING=not-a-real-credential\n")
            return native_reply(context, [{"type": "complete", "task_id": task["id"], "result": "Refactored assets and config."}])
        if phase == "review":
            self.review_contexts.append(context)
            # The reviewer's own private copy shows the imported result exactly.
            self.assertFalse((cwd / "old.py").exists())
            self.assertEqual((cwd / "art/new.png").read_bytes(), NEW_ART)
            self.assertEqual((cwd / "art/logo.png").read_bytes(), NEW_LOGO)
            self.assertTrue((cwd / "run.sh").stat().st_mode & 0o100)
            self.assertEqual((cwd / "config.js").read_text(), SECRET_LOOKING)
            self.assertFalse((cwd / ".env").exists())
            return native_reply(context, [{"type": "review", "task_id": task["id"], "verdict": "accept", "reason": "Checked every file."}])
        if phase == "checkpoint":
            return native_reply(context, [{"type": "checkpoint", "decision": "complete", "summary": "Assets and config done.",
                "next_goal": None, "scope_reference": context["objective"]["original_goal"], "question": None}])
        if phase == "checkpoint_review":
            return native_reply(context, [{"type": "checkpoint_review", "verdict": "accept", "reason": "Matches the goal.",
                "proposal_digest": context["objective"]["checkpoint_proposal"]["proposal_digest"]}])
        self.fail(phase)

    async def run_room(self):
        created = await self.service.create_room("Refactor the game's assets and config.", "claude", str(uuid4()),
            mode="native_project", project_id=self.registration["project"]["id"], participants=["claude", "codex"],
            work_mode="build", runtime_settings={"permission": "full_access", "members": {}},
            execution_policy={"mode": "autonomous"})
        await self.settle()
        return created["room"]["id"], await self.service.read_room(created["room"]["id"])

    async def test_ordinary_native_work_is_imported_reviewed_and_accepted(self):
        # Catches deletions, binaries, chmod, secret-looking text or a local .env failing the task or stopping the room.
        self.assertTrue(_SECRET.search(SECRET_LOOKING), "fixture must trip the old secret filter")
        room_id, result = await self.run_room()
        self.assertEqual(result["objective"]["status"], "complete", (result["team"], [job["error"] for job in result["jobs"]]))
        self.assertIsNone(result["team"]["stop_reason"])
        self.assertEqual([job["error"] for job in result["jobs"] if job["error"]], [])
        task = result["team"]["tasks"][0]
        self.assertEqual(task["status"], "accepted")
        self.assertEqual(task["result_files"], [
            {"path": "art/logo.png", "sha256": hashlib.sha256(NEW_LOGO).hexdigest()},
            {"path": "art/new.png", "sha256": hashlib.sha256(NEW_ART).hexdigest()},
            {"path": "config.js", "sha256": hashlib.sha256(SECRET_LOOKING.encode()).hexdigest()},
            {"path": "old.py", "sha256": None},
            {"path": "run.sh", "sha256": hashlib.sha256(b"#!/bin/sh\necho fictional\n").hexdigest()},
        ])

        project = self.service._project(room_id)
        snapshot = project.snapshot()
        self.assertEqual(snapshot["deleted_files"], [{"path": "old.py", "owner": task["owner"],
                                                      "sha256": hashlib.sha256(b"OLD = True\n").hexdigest()}])
        self.assertEqual(project.metadata_for_paths(["old.py", ".env"]), [])
        binary = {file["path"]: file for file in project.files_for_paths(["art/new.png", "art/logo.png"])}
        self.assertEqual({path: (file["content"], file["text"]) for path, file in binary.items()},
                         {"art/new.png": (None, False), "art/logo.png": (None, False)})
        self.assertEqual(project.raw_files_for_paths(["art/new.png", "art/logo.png"]),
                         {"art/new.png": NEW_ART, "art/logo.png": NEW_LOGO})
        self.assertEqual(project.files_for_paths(["config.js"])[0]["content"], SECRET_LOOKING)
        exported = project.materialize_snapshot(self.root / "exported")
        self.assertTrue((self.root / "exported/run.sh").stat().st_mode & 0o100)
        self.assertEqual({file["path"]: file["executable"] for file in exported["files"]}["run.sh"], True)
        self.assertFalse((self.root / "exported/old.py").exists())
        self.assertFalse((self.root / "exported/.env").exists())

        # The .env stays in the partner's private tree and is reported, not imported.
        state = self.service.store.native_workspace(room_id, task["owner"])
        self.assertEqual([(item["path"], item.get("reason")) for item in state["generated"]], [(".env", "sensitive_path")])
        self.assertEqual(state["blocked"], [])
        self.assertTrue((Path(state["project"]) / ".env").exists())

        # The user's project is never written by the host.
        self.assertEqual((self.source / "old.py").read_text(), "OLD = True\n")
        self.assertEqual((self.source / "art/logo.png").read_bytes(), OLD_LOGO)
        self.assertFalse((self.source / ".env").exists())

    async def test_review_context_names_binary_and_deleted_results_without_content(self):
        # Catches review/checkpoint context building raising project_binary_file on an imported image.
        await self.run_room()
        self.assertEqual(len(self.review_contexts), 1)
        context = self.review_contexts[0]
        files = {file["path"]: file for file in context["task"]["result_files"]}
        self.assertEqual(files["old.py"]["sha256"], None)
        self.assertEqual(files["art/new.png"]["sha256"], hashlib.sha256(NEW_ART).hexdigest())
        self.assertNotIn(base64.b64encode(NEW_ART).decode(), json.dumps(context))

    async def test_export_and_room_view_carry_binaries_and_deletions(self):
        # Catches the ZIP export or room projection failing on, or silently dropping, a binary or a deletion.
        room_id, result = await self.run_room()
        files = {file["path"]: file for file in result["project"]["files"]}
        self.assertEqual((files["art/new.png"]["text"], files["art/new.png"].get("content")), (False, None))
        self.assertEqual([file["path"] for file in result["project"]["deleted_files"]], ["old.py"])
        exported = await self.service.export_room(room_id, result["project"]["revision"])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content_base64"]))) as archive:
            self.assertEqual(archive.read("art/new.png"), NEW_ART)
            self.assertEqual(archive.read("art/logo.png"), NEW_LOGO)
            manifest = json.loads(archive.read(".beanest-export.json"))
        self.assertEqual(manifest["deleted"], ["old.py"])
        self.assertNotIn("old.py", archive.namelist())
        context = self.service._code_status_context(room_id)
        self.assertIsNone(context["canonical_changes"]["old.py"])


if __name__ == "__main__":
    unittest.main()
