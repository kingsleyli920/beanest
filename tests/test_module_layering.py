"""Static import rules that keep the room modules from growing cycles again.

Every import counts, including ones placed inside a function: a deferred import
still makes the two modules depend on each other, it only hides the cycle from
the interpreter until that function first runs.
"""
import ast
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "local_agent_coordinator"
STORES = {"room_store", "team_store", "objective_store"}


def imported_modules(name):
    """Sibling modules named by any import statement in this module."""
    siblings = {path.stem for path in PACKAGE.glob("*.py")}
    found = set()
    for node in ast.walk(ast.parse((PACKAGE / f"{name}.py").read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("local_agent_coordinator."):
                    found.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                found.add(node.module.split(".")[0])
            elif node.level == 1:
                found.update(alias.name for alias in node.names)
            elif node.level == 0 and (node.module or "").startswith("local_agent_coordinator."):
                found.add(node.module.split(".")[1])
            elif node.level == 0 and node.module == "local_agent_coordinator":
                found.update(alias.name for alias in node.names)
    return found & siblings


def reachable(start, graph):
    seen, pending = set(), list(graph[start])
    while pending:
        module = pending.pop()
        if module not in seen:
            seen.add(module)
            pending.extend(graph[module])
    return seen


class StoreLayeringTests(unittest.TestCase):
    def test_team_and_objective_stores_do_not_import_room_store(self):
        # Catches a store going back to room_store for an error or a validator.
        self.assertNotIn("room_store", imported_modules("team_store"))
        self.assertNotIn("room_store", imported_modules("objective_store"))

    def test_shared_contract_stays_below_every_store(self):
        # Catches room_contract picking up any dependency that could lead back to a store.
        self.assertEqual(imported_modules("room_contract"), {"message_capacity"})
        self.assertEqual(imported_modules("message_capacity"), set())

    def test_response_schema_does_not_import_a_store(self):
        # Catches the schema reaching into a store for its private constants again.
        self.assertEqual(imported_modules("room_response_schema") & STORES, set())

    def test_no_store_can_reach_itself(self):
        # Catches a cycle through any intermediate module, not just direct imports.
        graph = {path.stem: imported_modules(path.stem) for path in PACKAGE.glob("*.py")}
        for store in sorted(STORES):
            self.assertNotIn(store, reachable(store, graph), store)


if __name__ == "__main__":
    unittest.main()
