"""Compact directory projection must not consume a keyed runtime catalog."""
import copy
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from local_agent_coordinator import adapters
import test_partner_lifecycle_operations as fixtures


class RuntimeCatalogCompactHTTPTests(unittest.TestCase):
    setUp = fixtures.PartnerLifecycleOperationsTests.setUp
    tearDown = fixtures.PartnerLifecycleOperationsTests.tearDown
    request = fixtures.PartnerLifecycleOperationsTests.request

    def test_compact_runtime_refresh_preserves_ready_cached_models_efforts_and_scope(self):
        catalogs = {adapter: {
            "models": [{"id": adapter + "-fixture-model", "name": "Opus 5.5" if adapter == "claude" else "Codex fixture",
                        "efforts": ["low", "high"], "default_effort": "high"}],
            "default_model": adapter + "-fixture-model", "catalog_source": "synthetic_metadata",
            "effort_scope": "per_model", "reason": "fixture_only",
        } for adapter in ("claude", "codex")}

        async def metadata(adapter, workspace):
            self.assertEqual(workspace, self.service.workspace)
            return copy.deepcopy(catalogs[adapter])

        with patch.object(adapters, "runtime_options", new=AsyncMock(side_effect=metadata)) as probe:
            status, initial = self.request("GET", "/api/runtime-options")
            self.assertEqual(status, 200, initial)
            probe.assert_not_awaited()
            for adapter in ("claude", "codex"):
                with self.subTest(adapter=adapter):
                    status, refreshed = self.request("POST", f"/api/partners/{adapter}/runtime/refresh?view=compact", {})
                    # The old bug reports HTTP500 AFTER the metadata cache has
                    # successfully become ready; a cache GET must still work.
                    cache_status, cached = self.request("GET", "/api/runtime-options")
                    self.assertEqual(cache_status, 200, cached)
                    self.assertEqual(cached["partners"][adapter]["state"], "ready")
                    self.assertEqual(cached["partners"][adapter]["models"], catalogs[adapter]["models"])
                    self.assertEqual(status, 200, refreshed)
                    self.assertEqual(refreshed, cached)
                    self.assertIsInstance(refreshed["partners"], dict)
                    self.assertEqual(refreshed["scope"], "coordinator_owned_only")
                    self.assertNotIn("projection", refreshed)
                    for key in ("default_model", "catalog_source", "effort_scope", "reason"):
                        self.assertEqual(refreshed["partners"][adapter][key], catalogs[adapter][key])
            self.assertEqual(probe.await_count, 2)
        self.assertEqual(self.calls, [])
        self.assertFalse(any(event.is_set() for event in self.started.values()))

    def test_compact_partner_directory_refresh_and_lifecycle_keep_exact_text_references(self):
        goal = "原文目标不能在伙伴目录中重复。" * 100
        room_id, _ = self.service.store.create_room(goal, "claude-a", str(uuid4()), participants=["claude-a"])
        with patch.object(self.manager, "refresh_discovery") as discovery:
            routes = [("GET", "/api/partners?view=compact", None),
                      ("POST", "/api/partners/discover?view=compact", {}),
                      ("POST", "/api/partners/claude-b/lifecycle?view=compact", {"action": "archive"})]
            for method, route, body in routes:
                with self.subTest(route=route):
                    status, directory = self.request(method, route, body)
                    self.assertEqual(status, 200, directory)
                    self.assertEqual(directory["projection"], "partner_list_v1")
                    self.assertIsInstance(directory["partners"], list)
                    partner = next(item for item in directory["partners"] if item["id"] == "claude-a")
                    task = partner["monitor"]["current_tasks"][0]
                    self.assertEqual(task["room_id"], room_id)
                    self.assertNotIn("room_goal", task)
                    self.assertNotIn("current_task", task)
                    for field, kind in (("room_goal_text_ref", "room_goal"), ("current_task_text_ref", "job_text")):
                        reference = task[field]
                        self.assertEqual(reference["kind"], kind)
                        original = self.service.store.read_text(room_id, {key: value for key, value in reference.items() if key != "room_id"})
                        self.assertEqual(original["text"], goal)
            discovery.assert_called_once()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
