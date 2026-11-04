import unittest

from local_agent_coordinator.agent_registry import (
    AgentRegistry, AgentSpec, bean_styles, bounded_session_id, default_registry,
)


async def unused(*args, **kwargs):
    raise AssertionError("Registry inspection must not invoke an agent")


class AgentRegistryTests(unittest.TestCase):
    def test_defaults_describe_only_verified_adapters_without_invocation(self):
        registry = default_registry(unused)
        self.assertEqual(registry.ids(), ("claude", "codex"))
        descriptors = registry.describe()
        self.assertEqual([item["avatar"] for item in descriptors], ["C", "X"])
        self.assertTrue(all(set(item) == {"id", "name", "avatar", "capabilities", "usage_source", "usage_fields", "adapter", "provider", "bean_style"}
                            for item in descriptors))
        self.assertEqual([(item["adapter"], item["provider"]) for item in descriptors],
                         [("claude", "anthropic"), ("codex", "openai")])
        self.assertEqual([item["bean_style"] for item in descriptors], ["peach", "sprout"])
        self.assertTrue(all(item["capabilities"] == {"text_only": True, "tools": False} for item in descriptors))
        descriptors[0]["capabilities"]["tools"] = True
        self.assertFalse(registry.describe()[0]["capabilities"]["tools"])
        self.assertIsNone(registry.get("unregistered"))

    def test_registry_rejects_identity_collisions_paths_and_permission_upgrades(self):
        for agent_id in ("all", "both", "user", "system", "mcp", "../outside", "a/b", "A", "", "a" * 33):
            with self.assertRaises(ValueError):
                AgentSpec(agent_id, "Test", unused)
        spec = AgentSpec("helper", "Local Helper", unused)
        for specs in ([], [spec, spec], [object()]):
            with self.assertRaises(ValueError):
                AgentRegistry(specs)
        for capabilities in ({"text_only": True, "tools": True}, {"text_only": 1, "tools": False}, None):
            with self.assertRaises(ValueError):
                AgentSpec("helper", "Test", unused, capabilities=capabilities)
        with self.assertRaises(ValueError):
            AgentSpec("helper", "Test", unused, usage_fields=("units",))

    def test_sessions_have_adapter_specific_normalization_and_universal_safe_bounds(self):
        registry = default_registry(unused)
        self.assertIsNone(registry.get("codex").normalize_session_id("opaque-session:1"))
        custom = AgentSpec("helper", "Local Helper", unused)
        self.assertEqual(custom.describe()["adapter"], "")
        self.assertEqual(custom.normalize_session_id("opaque-session:1"), "opaque-session:1")
        self.assertEqual(custom.describe()["avatar"], "L")
        for unsafe in ("../elsewhere", "a/b", "a b", "a\n", "a" * 129, "", None):
            self.assertIsNone(bounded_session_id(unsafe))

    def test_structured_transport_is_explicit_and_does_not_change_custom_invokers(self):
        self.assertTrue(all(callable(default_registry().get(key).invoke_structured)
                            for key in ('claude', 'codex')))
        self.assertTrue(all(default_registry(unused).get(key).invoke_structured is None
                            for key in ('claude', 'codex')))
        self.assertIsNone(AgentSpec('helper', 'Helper', unused).invoke_structured)
        with self.assertRaises(ValueError):
            AgentSpec('helper', 'Helper', unused, invoke_structured=True)

    def test_ten_bean_styles_are_decorative_validated_and_described_independently(self):
        styles = bean_styles()
        self.assertEqual([item['id'] for item in styles],
                         ['sprout', 'peach', 'sky', 'lavender', 'sunshine', 'cocoa',
                          'mint', 'coral', 'ocean', 'rose'])
        for item in styles:
            spec = AgentSpec('helper', 'Helper', unused, bean_style=item['id'])
            self.assertEqual(spec.describe()['bean_style'], item['id'])
            self.assertIs(spec.invoke, unused)
        styles[0]['name'] = 'Changed locally'
        self.assertEqual(bean_styles()[0]['name'], '芽芽豆')
        for invalid in ('unknown', '<script>', None, [], 1):
            with self.assertRaises(ValueError):
                AgentSpec('helper', 'Helper', unused, bean_style=invalid)


if __name__ == "__main__":
    unittest.main()
