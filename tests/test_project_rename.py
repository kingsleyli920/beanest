"""Renaming a registered project changes only its display name."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.project_catalog import ProjectCatalog, ProjectCatalogError
from local_agent_coordinator.room_server import create_room_server
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.room_store import RoomError
from wait_support import scaled


async def unused(*args, **kwargs):
    raise AssertionError("Renaming a project must not invoke a model")


class ProjectRenameCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "README.md").write_text("# Original\n", encoding="utf-8")
        self.catalog = ProjectCatalog(self.base / "private")
        self.stored = self.catalog.root / "catalog.json"

    def test_rename_changes_only_the_name_and_persists(self):
        # Catches a rename that touches the path, files, id, creation time or lifecycle,
        # or one that only changes the in-memory copy and is lost after a restart.
        item = self.catalog.register("Old name", str(self.source), ["README.md"])
        renamed = self.catalog.rename(item["id"], "  New name  ")
        self.assertEqual(renamed, {**item, "name": "New name"})
        reopened = ProjectCatalog(self.catalog.root)
        self.assertEqual(reopened.list(), [{**item, "name": "New name"}])
        self.assertEqual(reopened.capture(item["id"])["name"], "New name")
        self.assertEqual((self.source / "README.md").read_text(encoding="utf-8"), "# Original\n")
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), ["README.md"])

    def test_archived_project_can_be_renamed_and_stays_archived(self):
        # Catches a rename that silently restores an archived project.
        item = self.catalog.register_folder("Folder", str(self.source))
        self.catalog.set_lifecycle(item["id"], "archive")
        renamed = self.catalog.rename(item["id"], "Archived folder")
        self.assertEqual(renamed["name"], "Archived folder")
        self.assertEqual(renamed["lifecycle_state"], "archived")
        with self.assertRaisesRegex(ProjectCatalogError, "project_inactive"):
            self.catalog.get(item["id"])

    def test_invalid_names_are_rejected_without_writing(self):
        # Catches a missing _name check: blank, too long, control characters,
        # non-strings and credential-looking text must never reach catalog.json.
        item = self.catalog.register_folder("Keep me", str(self.source))
        before = self.stored.read_bytes()
        for name in ("", "   ", "x" * 81, "line\nbreak", "tab\there", None, 42, ["Name"],
                     "sk-abcdefghijklmnopqrstuv"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ProjectCatalogError, "invalid_project_request"):
                    self.catalog.rename(item["id"], name)
        self.assertEqual(self.stored.read_bytes(), before)
        self.assertEqual(self.catalog.list()[0]["name"], "Keep me")
        self.assertEqual(self.catalog.rename(item["id"], "y" * 80)["name"], "y" * 80)

    def test_unknown_or_malformed_id_is_rejected_without_writing(self):
        # Catches a rename of a missing id that appends a record or rewrites the file.
        self.catalog.register_folder("Only project", str(self.source))
        before = self.stored.read_bytes()
        with self.assertRaisesRegex(ProjectCatalogError, "project_not_found"):
            self.catalog.rename(str(uuid4()), "Other")
        for identifier in ("not-a-uuid", None, 7, "11111111-1111-4111-8111-11111111111X"):
            with self.subTest(identifier=identifier):
                with self.assertRaisesRegex(ProjectCatalogError, "invalid_project_request"):
                    self.catalog.rename(identifier, "Other")
        self.assertEqual(self.stored.read_bytes(), before)

    def test_same_name_does_not_write(self):
        # Catches a rename to the same name rewriting catalog.json (and so racing other writers).
        item = self.catalog.register_folder("Same", str(self.source))
        with patch.object(self.catalog, "_write", side_effect=AssertionError("must not write")):
            self.assertEqual(self.catalog.rename(item["id"], "Same")["name"], "Same")
            self.assertEqual(self.catalog.rename(item["id"], " Same ")["name"], "Same")

    def test_rename_leaves_other_registrations_alone(self):
        # Catches a rename that matches the wrong record or renames every record.
        first = self.catalog.register_folder("First", str(self.source))
        second = self.catalog.register_folder("Second", str(self.source))
        self.catalog.rename(second["id"], "Second renamed")
        self.assertEqual([item["name"] for item in self.catalog.list()], ["First", "Second renamed"])
        self.assertEqual(self.catalog.list()[0], first)


class ProjectRenameServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=unused,
                                   clock=lambda: 1000)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "README.md").write_text("# Keep the copy\n", encoding="utf-8")

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temporary.cleanup()

    async def test_rename_updates_the_list_and_keeps_the_room_copy(self):
        # Catches a service that returns a stale list after renaming, or a rename
        # that rewrites the copy an existing room already captured.
        project = (await self.service.register_project("Before", str(self.source), ["README.md"]))["project"]
        self.service._wake = lambda _: None
        room = await self.service.create_room("Read it", "claude", str(uuid4()), mode="project_files",
                                              project_id=project["id"], work_mode="plan")
        result = await self.service.rename_project(project["id"], "After")
        self.assertEqual(result["project"]["name"], "After")
        self.assertEqual([item["name"] for item in result["projects"]], ["After"])
        self.assertEqual([item["name"] for item in (await self.service.list_projects())["projects"]], ["After"])
        after = await self.service.read_room(room["room"]["id"])
        self.assertEqual(after["room"]["project_id"], project["id"])
        self.assertEqual(after["project"]["files"], room["project"]["files"])
        self.assertEqual((self.source / "README.md").read_text(encoding="utf-8"), "# Keep the copy\n")

    async def test_pending_deletion_blocks_rename(self):
        # Catches a rename racing a permanent deletion and bringing the record back.
        project = (await self.service.register_project("Going", str(self.source), selection_kind="folder"))["project"]
        with patch.object(self.service.store, "project_deletion_pending", return_value=True):
            with self.assertRaisesRegex(RoomError, "project_deletion_pending"):
                await self.service.rename_project(project["id"], "Gone")
        self.assertEqual(self.service.catalog.list()[0]["name"], "Going")


class ProjectRenameHTTPTests(unittest.TestCase):
    """Real loopback router and real RoomService."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.service = RoomService(self.root / "rooms.sqlite3", self.root / "workspaces", invoke=unused)
        self.server = create_room_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        status, registered = self.request("POST", "/api/projects",
                                          {"name": "Fictional", "path": str(self.source), "selection_kind": "folder"})
        self.assertEqual(status, 200)
        self.project = registered["project"]
        self.path = f"/api/projects/{self.project['id']}/name"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, body=None, *, mcp=False):
        data = json.dumps(body).encode() if body is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=scaled(3))
        try:
            connection.request(method, path, body=data, headers={
                "X-LAC-Token": self.server.mcp_token if mcp else self.server.token,
                "X-LAC-Instance": self.server.instance_id, "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def names(self):
        status, listed = self.request("GET", "/api/projects")
        self.assertEqual(status, 200)
        return [item["name"] for item in listed["projects"]]

    def test_rename_route_returns_the_new_name_and_list(self):
        # Catches a route that is missing, renames the wrong id, or answers with a stale list.
        status, result = self.request("POST", self.path, {"name": "改过的名字"})
        self.assertEqual(status, 200)
        self.assertEqual(result["project"]["id"], self.project["id"])
        self.assertEqual(result["project"]["name"], "改过的名字")
        self.assertEqual(result["project"]["path"], str(self.source))
        self.assertEqual([item["name"] for item in result["projects"]], ["改过的名字"])
        self.assertEqual(self.names(), ["改过的名字"])

    def test_rename_errors_map_to_stable_codes(self):
        # Catches a wrong status mapping or a malformed body reaching the catalog.
        cases = (
            (self.path, {"name": ""}, 400, "invalid_project_request"),
            (self.path, {"name": "x" * 81}, 400, "invalid_project_request"),
            (self.path, {"name": "a\u0001b"}, 400, "invalid_project_request"),
            (self.path, {"name": None}, 400, "invalid_arguments"),
            (self.path, {"name": "Ok", "path": "/elsewhere"}, 400, "invalid_arguments"),
            (self.path, {}, 400, "invalid_arguments"),
            (f"/api/projects/{uuid4()}/name", {"name": "Nobody"}, 404, "project_not_found"),
            ("/api/projects/not-a-uuid/name", {"name": "Nobody"}, 404, "route_not_found"),
        )
        for path, body, expected_status, expected_error in cases:
            with self.subTest(body=body, path=path):
                status, result = self.request("POST", path, body)
                self.assertEqual((status, result["error"]), (expected_status, expected_error))
        self.assertEqual(self.names(), ["Fictional"])

    def test_mcp_cannot_rename(self):
        # Catches an agent connected through MCP changing user configuration.
        for path, expected in ((self.path, "token_scope_rejected"),
                               (self.path.replace("/api/", "/api/mcp/"), "user_lifecycle_required")):
            status, result = self.request("POST", path, {"name": "Agent picked"}, mcp=True)
            self.assertEqual((status, result["error"]), (403, expected))
        self.assertEqual(self.names(), ["Fictional"])


if __name__ == "__main__":
    unittest.main()
