import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent_coordinator.coordinator import RunStore, parse_object, verify_query


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.cases = {"cases": [{"input": v, "expected": e} for v,e in [
            (None,None), ("",None), ("   ",None), (" Hello ","hello"), ("A B","a b")]]}
        self.query = {"sql": "SELECT nullif(lower(trim(:value)), '')"}

    def test_sql_success_and_reject_external_access(self):
        self.assertEqual(len(verify_query(self.query, self.cases)), 5)
        for sql in ["ATTACH DATABASE '/tmp/forbidden.db' AS x", "SELECT load_extension(:value)",
                    "PRAGMA database_list", "SELECT :value; SELECT 1"]:
            with self.assertRaises((sqlite3.Error, ValueError)):
                verify_query({"sql": sql}, self.cases)

    def test_query_and_cases_must_meet_fixed_contract(self):
        with self.assertRaises(ValueError):
            verify_query({"sql": "SELECT :value"}, self.cases)
        self.cases["cases"][0]["expected"] = "wrong"
        with self.assertRaises(ValueError):
            verify_query(self.query, self.cases)

    def test_sql_memory_limit(self):
        sql = "SELECT :value AS s"
        for _ in range(8):
            sql = f"SELECT s || s AS s FROM ({sql})"
        cases = {"cases": [{"input": "x" * 1024, "expected": "x" * 1024}, *self.cases["cases"]]}
        with self.assertRaises(sqlite3.DataError):
            verify_query({"sql": sql}, cases)

    def test_resource_conflict_and_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            store = RunStore(Path(root))
            with store.lease("integration"):
                with self.assertRaises(FileExistsError):
                    with store.lease("integration"):
                        pass
            with store.lease("integration"):
                store.save("summary.json", {"status": "completed"})
            self.assertEqual(json.loads((store.path / "summary.json").read_text())["status"], "completed")
            with self.assertRaises(ValueError):
                store.save("../escape", {})

    def test_only_one_json_object(self):
        self.assertEqual(parse_object('```json\n{"answer":1}\n```'), {"answer":1})
        for value in ['[]', '{} {}', 'run this command']:
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                parse_object(value)
