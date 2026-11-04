"""Local Git evidence remains distinct from task publication and private state."""
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from local_agent_coordinator import code_delivery, git_status
import test_git_status as fixture


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class DeliveryUncertaintyTests(unittest.TestCase):
    committed_at = fixture.GitStatusTests.committed_at
    setUp = fixture.GitStatusTests.setUp
    git = fixture.GitStatusTests.git
    repository = fixture.GitStatusTests.repository
    write = fixture.GitStatusTests.write
    commit = fixture.GitStatusTests.commit

    def accepted_delivery(self, path, identifier="delivery:accepted"):
        return {"id":identifier, "kind":"delivery", "path":str(path), "source":{
            "asset_id":"a" * 64, "result_sha256":"b" * 64,
            "run_id":"11111111-1111-4111-8111-111111111111", "task_id":"T1", "result_version":1}}

    def summary(self, workspace, changes):
        return code_delivery.summarize([workspace], statuses={workspace["id"]:git_status.read_status(workspace["path"])},
            native_states=[], canonical={name:value["sha256"] for name,value in (changes or {}).items() if value},
            canonical_changes=changes, now=lambda:1234,
            delivery_repository={"state":"bound", "workspace_id":workspace["id"]})

    def test_partial_accepted_mapping_exposes_counts_without_erasing_known_main(self):
        source = self.repository()
        changes = {f"item-{i}.txt":{"sha256":sha(f"Item {i}\n"), "executable":False} for i in range(26)}
        for i in range(23):
            self.write(source, f"item-{i}.txt", f"Item {i}\n")
        self.commit(source)
        result = self.summary(self.accepted_delivery(source), changes)
        self.assertEqual(result["main"]["state"], "confirmed")
        self.assertEqual(result["private"]["state"], "confirmed")
        self.assertEqual(result["published"]["state"], "pending")
        self.assertEqual(result["published"]["reason"], "published_work_not_on_mainline")
        self.assertEqual(result["published"]["files"], {"total":26,"matched":23,"missing":3,"different":0,"mode_unknown":0})
        self.assertEqual(result["state"], "pending")
        self.assertTrue(any(issue["scope"] == "published" and issue["workspace_id"] == "delivery:accepted" for issue in result["issues"]))

    def test_bound_partial_mapping_is_not_diluted_by_a_reference_repository(self):
        source = self.repository()
        changes = {f"item-{i}.txt":{"sha256":sha(f"Item {i}\n"), "executable":False} for i in range(26)}
        for i in range(23):
            self.write(source, f"item-{i}.txt", f"Item {i}\n")
        self.commit(source)
        evidence = self.repository("evidence")
        self.write(evidence, "unrelated.txt", "Evidence only\n")
        self.commit(evidence)
        workspaces = [self.accepted_delivery(source), self.accepted_delivery(evidence, "delivery:evidence")]
        with patch.object(git_status, "local_delivery_refs", wraps=git_status.local_delivery_refs) as refs:
            result = code_delivery.summarize(workspaces,
                statuses={item["id"]:git_status.read_status(item["path"]) for item in workspaces},
                native_states=[], canonical={name:value["sha256"] for name,value in changes.items()},
                canonical_changes=changes, now=lambda:1234,
                delivery_repository={"state":"bound", "workspace_id":"delivery:accepted"})
        self.assertEqual([Path(call.args[0]) for call in refs.call_args_list], [source],
            "A bound target must not wait for unrelated reference repository metadata")
        self.assertEqual(result["published"]["files"]["matched"], 23)
        self.assertEqual(result["published"]["files"]["missing"], 3)
        self.assertEqual(result["published"]["state"], "pending")
        self.assertEqual([item["workspace_id"] for item in result["main"]["checks"]], ["delivery:accepted"])
        self.assertEqual(result["state"], "pending")

    def test_exact_accepted_mapping_requires_actual_bytes_and_known_mode(self):
        source = self.repository()
        self.write(source, "entry.py", "VALUE = 1\n")
        self.commit(source)
        changes = {"entry.py":{"sha256":sha("VALUE = 1\n"),"executable":False}}
        result = self.summary(self.accepted_delivery(source), changes)
        self.assertEqual(result["published"]["state"], "confirmed")
        self.assertEqual(result["published"]["files"]["matched"], 1)
        historical = self.summary(self.accepted_delivery(source), {"entry.py":{"sha256":sha("VALUE = 1\n")}})
        self.assertEqual(historical["published"]["state"], "confirmed")
        self.assertEqual(historical["published"]["files"]["mode_unknown"], 1)
        for wanted, expected in (({"sha256":sha("Different bytes\n"),"executable":False}, "pending"),
                                 ({"sha256":sha("VALUE = 1\n"),"executable":True}, "pending")):
            with self.subTest(wanted=wanted):
                result = self.summary(self.accepted_delivery(source), {"entry.py":wanted})
                self.assertEqual(result["main"]["state"], "confirmed")
                self.assertEqual(result["published"]["state"], expected)
                self.assertEqual(result["published"]["files"]["different"], 1)

    def test_no_provenance_or_ambiguous_exact_mapping_stays_unknown(self):
        source = self.repository("first")
        self.write(source, "entry.py", "VALUE = 1\n")
        self.commit(source)
        changes = {"entry.py":{"sha256":sha("VALUE = 1\n")}}
        deleted_only = code_delivery.summarize([self.accepted_delivery(source)],
            statuses={"delivery:accepted":git_status.read_status(source)}, native_states=[], canonical={},
            canonical_changes={"absent.py":None}, now=lambda:1234)
        self.assertEqual(deleted_only["published"]["state"], "unknown")
        result = code_delivery.summarize([{"id":"delivery:unproven", "kind":"delivery", "path":str(source)}],
            statuses={"delivery:unproven":git_status.read_status(source)}, native_states=[],
            canonical={"entry.py":changes["entry.py"]["sha256"]}, canonical_changes=changes, now=lambda:1234)
        self.assertEqual(result["published"]["state"], "unknown")
        self.assertIsNone(result["published"]["files"]["matched"])
        other = self.repository("second")
        self.write(other, "entry.py", "VALUE = 1\n")
        self.commit(other)
        workspaces = [self.accepted_delivery(source, "delivery:first"), self.accepted_delivery(other, "delivery:second")]
        result = code_delivery.summarize(workspaces,
            statuses={w["id"]:git_status.read_status(w["path"]) for w in workspaces},
            native_states=[], canonical={"entry.py":changes["entry.py"]["sha256"]}, canonical_changes=changes, now=lambda:1234)
        self.assertEqual(result["published"]["state"], "unknown")
        self.assertEqual(result["published"]["reason"], "published_work_mapping_unknown")

    def test_missing_baseline_is_local_to_published_evidence(self):
        source = self.repository()
        self.write(source, "entry.py", "VALUE = 1\n")
        self.commit(source)
        result = self.summary({"id":"project", "kind":"project", "path":str(source)}, None)
        self.assertEqual(result["main"]["state"], "confirmed")
        self.assertEqual(result["published"]["reason"], "published_work_baseline_unknown")
        self.assertIsNone(result["published"]["files"]["total"])
        self.assertEqual(result["private"]["state"], "confirmed")
        self.assertEqual(result["state"], "unknown")

    def test_uncommitted_tracked_work_does_not_erase_confirmed_branch_refs(self):
        source = self.repository()
        self.write(source, "entry.py", "VALUE = 1\n")
        self.commit(source)
        self.write(source, "entry.py", "VALUE = 2\n")
        result = self.summary({"id":"project", "kind":"project", "path":str(source)}, {})
        self.assertEqual(result["main"]["state"], "confirmed")
        self.assertEqual(result["workspace"]["state"], "pending")
        self.assertEqual(result["workspace"]["reason"], "uncommitted_tracked_changes")
        self.assertEqual(result["workspace"]["tracked"], 1)
        self.assertEqual(result["state"], "pending")
