"""Lifecycle changes retain registrations and identities, not source deletions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.agent_registry import default_registry
from local_agent_coordinator.partner_manager import PartnerError, PartnerManager
from local_agent_coordinator.project_catalog import ProjectCatalog, ProjectCatalogError
from local_agent_coordinator.room_service import RoomService


async def unused(*args, **kwargs):
    raise AssertionError("Lifecycle inspection must not invoke a model")


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "README.md").write_text("# Original\n", encoding="utf-8")
        self.catalog = ProjectCatalog(self.base / "private")

    def test_legacy_json_is_active_in_memory_without_rewriting_it(self):
        item = self.catalog.register("Example", str(self.source), ["README.md"])
        path = self.catalog.root / "catalog.json"
        old = json.loads(path.read_text())
        del old["projects"][0]["lifecycle_state"]
        path.write_text(json.dumps(old))
        before = path.read_bytes()
        reopened = ProjectCatalog(self.catalog.root)
        self.assertEqual(reopened.list()[0]["lifecycle_state"], "active")
        self.assertEqual(reopened.get(item["id"])["lifecycle_state"], "active")
        self.assertEqual(reopened.capture(item["id"])["files"][0]["content"], "# Original\n")
        self.assertEqual(path.read_bytes(), before)

    def test_capture_keeps_the_existing_frozen_seed_shape(self):
        item = self.catalog.register("Example", str(self.source), ["README.md"])
        capture = self.catalog.capture(item["id"])
        self.assertEqual(set(capture), {"id", "name", "path", "files", "created_at", "source_hash"})
        self.catalog.set_lifecycle(item["id"], "archive")
        self.catalog.set_lifecycle(item["id"], "restore")
        self.assertEqual(self.catalog.capture(item["id"]), capture)

    def test_archive_remove_and_restore_keep_source_and_previous_capture(self):
        item = self.catalog.register("Example", str(self.source), ["README.md"])
        frozen = self.catalog.capture(item["id"])
        original = (self.source / "README.md").read_bytes()
        for action, expected in (("archive", "archived"), ("remove", "removed")):
            changed = self.catalog.set_lifecycle(item["id"], action)
            self.assertEqual(changed, {**item, "lifecycle_state": expected})
            reopened = ProjectCatalog(self.catalog.root)
            self.assertEqual(reopened.list(), [changed])
            with patch.object(reopened, "_capture", side_effect=AssertionError("must not read source")):
                for operation in (reopened.get, reopened.capture):
                    with self.assertRaisesRegex(ProjectCatalogError, "project_inactive"):
                        operation(item["id"])
            self.assertEqual(reopened.set_lifecycle(item["id"], action), changed)
            self.assertEqual((self.source / "README.md").read_bytes(), original)
            self.assertEqual(frozen["files"][0]["content"], "# Original\n")
        self.assertEqual(self.catalog.set_lifecycle(item["id"], "restore"), item)
        self.assertEqual(self.catalog.capture(item["id"]), frozen)

    def test_archived_folder_cannot_be_resolved_for_a_new_capture(self):
        item = self.catalog.register_folder("Folder", str(self.source))
        self.catalog.set_lifecycle(item["id"], "archive")
        with self.assertRaisesRegex(ProjectCatalogError, "project_inactive"):
            self.catalog.get(item["id"])
        restored = self.catalog.set_lifecycle(item["id"], "restore")
        self.assertEqual(restored["selection_kind"], "folder")
        self.assertEqual(self.catalog.get(item["id"]), restored)

    def test_invalid_requests_and_bad_persisted_states_fail_without_reset(self):
        item = self.catalog.register_folder("Example", str(self.source))
        path = self.catalog.root / "catalog.json"
        before = path.read_bytes()
        for action in (None, True, [], {}, "delete", "ACTIVE", ""):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ProjectCatalogError, "invalid_lifecycle_action"):
                    self.catalog.set_lifecycle(item["id"], action)
                self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ProjectCatalogError, "project_not_found"):
            self.catalog.set_lifecycle(str(uuid4()), "remove")
        with self.assertRaisesRegex(ProjectCatalogError, "invalid_project_request"):
            self.catalog.set_lifecycle("bad", "archive")
        for state in (None, [], {}, True, "deleted", ""):
            data = json.loads(before)
            data["projects"][0]["lifecycle_state"] = state
            path.write_text(json.dumps(data))
            bad = path.read_bytes()
            with self.assertRaisesRegex(ProjectCatalogError, "project_catalog_corrupt"):
                self.catalog.list()
            self.assertEqual(path.read_bytes(), bad)
        path.write_bytes(before)

    def test_two_instances_do_not_lose_other_project_updates(self):
        items = [self.catalog.register_folder(str(index), str(self.source)) for index in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda item: ProjectCatalog(self.catalog.root).set_lifecycle(item["id"], "archive"), items))
        self.assertEqual({item["id"] for item in self.catalog.list()}, {item["id"] for item in items})
        self.assertTrue(all(item["lifecycle_state"] == "archived" for item in self.catalog.list()))


class PartnerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "partners"
        self.base = default_registry(unused)
        self.manager = PartnerManager(self.root, self.base)

    def test_legacy_profiles_gain_active_without_inspection_rewrite(self):
        self.manager.add(id="bean", name="Bean", adapter="claude")
        path = self.root / "profiles.json"
        data = json.loads(path.read_text())
        for item in data["profiles"]:
            del item["lifecycle_state"]
            del item["bean_style"]
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        reopened = PartnerManager(self.root, self.base)
        self.assertTrue(all(item["lifecycle_state"] == "active" for item in reopened.profiles()))
        self.assertEqual(reopened.enabled_ids(), ("claude", "codex", "bean"))
        self.assertEqual(path.read_bytes(), before)

    def test_lifecycle_retains_identity_and_both_enabled_preferences(self):
        self.manager.add(id="bean", name="Bean", adapter="claude", bean_style="lavender")
        self.manager.set_enabled("bean", False)
        for partner_id, enabled in (("claude", True), ("bean", False)):
            original = self.manager.get(partner_id)
            original_spec = self.manager.registry().get(partner_id)
            for action, state in (("archive", "archived"), ("remove", "removed")):
                changed = self.manager.set_lifecycle(partner_id, action)
                self.assertEqual(changed["lifecycle_state"], state)
                self.assertEqual(changed["enabled"], enabled)
                self.assertFalse(changed["selectable"])
                reopened = PartnerManager(self.root, self.base)
                self.assertNotIn(partner_id, reopened.enabled_ids())
                self.assertNotIn(partner_id, reopened.available_ids())
                historical = reopened.registry().get(partner_id)
                self.assertEqual(historical.describe(), original_spec.describe())
                self.assertIs(historical.invoke, original_spec.invoke)
                self.assertEqual(reopened.set_lifecycle(partner_id, action), changed)
                for value in (True, False):
                    with self.assertRaisesRegex(PartnerError, "partner_inactive"):
                        reopened.set_enabled(partner_id, value)
            restored = self.manager.set_lifecycle(partner_id, "restore")
            self.assertEqual(restored, original)
            self.assertEqual(partner_id in self.manager.enabled_ids(), enabled)

    def test_discovery_and_duplicate_add_do_not_resurrect_hidden_profiles(self):
        self.manager.add(id="bean", name="Bean", adapter="codex")
        self.manager.set_lifecycle("claude", "archive")
        self.manager.set_lifecycle("codex", "remove")
        self.manager.set_lifecycle("bean", "remove")
        path = self.root / "profiles.json"
        before = path.read_bytes()
        with patch("local_agent_coordinator.partner_manager.adapters.find_claude", return_value="/virtual/claude"), \
             patch("local_agent_coordinator.partner_manager.adapters.find_codex", return_value="/virtual/codex"), \
             patch("local_agent_coordinator.partner_manager.adapters._run", side_effect=AssertionError("no executable")):
            reopened = PartnerManager(self.root, self.base, startup_discovery=True)
            reopened.refresh_discovery()
            self.assertEqual(reopened.available_ids(), ())
            self.assertEqual(reopened.enabled_ids(), ())
            self.assertEqual(reopened.add(id="bean", name="Bean", adapter="codex")["lifecycle_state"], "removed")
            self.assertTrue(all(not item["selectable"] for item in reopened.profiles()))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.manager.registry().ids()), {"claude", "codex", "bean"})

    def test_connector_configuration_is_retained_after_removal_and_restore(self):
        connection = {"protocol": "beanest-json-v1", "executable": "/nonexistent/connector",
                      "args": ["--json"], "billing": "local"}
        original = self.manager.add(id="helper", name="Helper", adapter="local_json", connection=connection)
        removed = self.manager.set_lifecycle("helper", "remove")
        self.assertEqual(removed["connection"], connection)
        self.assertFalse(removed["selectable"])
        reopened = PartnerManager(self.root, self.base)
        self.assertEqual(reopened.set_lifecycle("helper", "restore"), original)

    def test_invalid_actions_and_persisted_states_fail_closed(self):
        self.manager.set_enabled("claude", False)
        path = self.root / "profiles.json"
        before = path.read_bytes()
        for action in (None, True, [], {}, "delete", "ACTIVE", ""):
            with self.subTest(action=action):
                with self.assertRaisesRegex(PartnerError, "invalid_lifecycle_action"):
                    self.manager.set_lifecycle("claude", action)
                self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(PartnerError, "unknown_partner"):
            self.manager.set_lifecycle("unknown", "remove")
        for state in (None, [], {}, True, "deleted", ""):
            data = json.loads(before)
            data["profiles"][0]["lifecycle_state"] = state
            path.write_text(json.dumps(data))
            bad = path.read_bytes()
            with self.assertRaisesRegex(PartnerError, "invalid_partner_storage"):
                self.manager.profiles()
            self.assertEqual(path.read_bytes(), bad)
        path.write_bytes(before)

    def test_two_instances_keep_independent_profile_lifecycle_updates(self):
        other = PartnerManager(self.root, self.base)
        self.manager.set_lifecycle("claude", "archive")
        other.set_lifecycle("codex", "remove")
        self.assertEqual({item["id"]: item["lifecycle_state"] for item in self.manager.profiles()},
                         {"claude": "archived", "codex": "removed"})


class ProjectLifecycleRoomIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_files_creation_keeps_seed_and_idempotent_request_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            source.mkdir()
            path = source / "README.md"
            path.write_text("# Frozen source\n", encoding="utf-8")
            calls = []

            async def fake(*args, **kwargs):
                calls.append(args)
                raise AssertionError("This capture test does not dispatch inference")

            service = RoomService(root / "rooms.sqlite3", root / "workspaces", invoke=fake)
            # Capture and actual ProjectWorkspace.seed must run, but queued
            # inference is outside this lifecycle/seed contract check.
            service._wake = lambda _: None
            try:
                project = (await service.register_project("Example", str(source), ["README.md"]))["project"]
                request_id = str(uuid4())
                arguments = {"mode": "project_files", "project_id": project["id"], "work_mode": "plan"}
                snapshot = await service.create_room("Read the selected source", "claude", request_id, **arguments)
                self.assertTrue(snapshot["project"]["available"])
                self.assertEqual(snapshot["project"]["revision"], 1)
                self.assertEqual(snapshot["project"]["files"][0]["content"], "# Frozen source\n")
                self.assertNotIn("lifecycle_state", snapshot["project"]["source"])
                await service.set_project_lifecycle(project["id"], "remove")
                replay = await service.create_room("Read the selected source", "claude", request_id, **arguments)
                self.assertEqual(replay["room"]["id"], snapshot["room"]["id"])
                self.assertEqual(replay["project"], snapshot["project"])
                with self.assertRaisesRegex(ProjectCatalogError, "project_inactive"):
                    await service.create_room("New capture", "claude", str(uuid4()), **arguments)
                self.assertEqual(path.read_text(encoding="utf-8"), "# Frozen source\n")
                self.assertEqual(calls, [])
            finally:
                await service.shutdown()
