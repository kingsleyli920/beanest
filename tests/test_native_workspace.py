import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from uuid import uuid4

from local_agent_coordinator.native_workspace import (
    NativeWorkspaceError, acknowledge_published, collect_changes, discard_paths, prepare_workspace, sync_workspace,
)


class NativeWorkspaceTests(unittest.TestCase):
    def test_host_baseline_survives_native_git_replacement_and_preserves_crlf(self):
        source = self.export("source", {"one.py": "old\r\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "one.py").write_bytes(b"local\r\n")
        incoming = self.export("incoming", {"one.py": "peer\r\n"})
        subprocess.run(["git", "-C", str(project), "gc", "--prune=now"], check=True,
                       capture_output=True)
        shutil.rmtree(project / ".git" / "objects")
        outside = self.root / "external-objects"
        outside.mkdir()
        (project / ".git" / "objects").symlink_to(outside, target_is_directory=True)
        updated = sync_workspace(project, receipt, *incoming)
        self.assertEqual(Path(updated["conflicts"][0]["baseline_path"]).read_bytes(), b"old\r\n")
        (project / "one.py").write_bytes(b"resolved\r\n")
        published = {"path": "one.py", "sha256": hashlib.sha256(b"resolved\r\n").hexdigest()}
        acknowledge_published(project, updated, [published])
        self.assertEqual(list(outside.iterdir()), [])

    def test_host_context_symlinks_are_refused_before_writing(self):
        source = self.export("source", {"one.py": "old\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        outside = self.root / "external"
        outside.mkdir()
        shutil.rmtree(project.parent / "handoff")
        (project.parent / "handoff").symlink_to(outside, target_is_directory=True)
        incoming = self.export("incoming", {"one.py": "peer\n"})
        (project / "one.py").write_text("local\n")
        with self.assertRaisesRegex(NativeWorkspaceError, "invalid_native_directory"):
            sync_workspace(project, receipt, *incoming)
        self.assertEqual(list(outside.iterdir()), [])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def export(self, name, files):
        root = self.root / name
        root.mkdir(mode=0o700)
        entries = []
        for path, content in files.items():
            raw = content.encode() if isinstance(content, str) else content
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            entries.append({"path": path, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "text": isinstance(content, str),
                "executable": path.endswith(".command")})
        return root, {"revision": 1, "files": entries}

    def prepare(self, source, baseline=None, candidate=None):
        kwargs = {}
        if baseline:
            kwargs.update(baseline_directory=baseline[0], baseline_manifest=baseline[1],
                candidate_directory=candidate[0], candidate_manifest=candidate[1])
        return prepare_workspace(self.root / "native", *source, **kwargs)

    def test_binary_and_executable_source_are_copied_without_aliasing(self):
        source = self.export("source", {"asset.bin": b"\x00\xff\x01", "试玩.command": "echo hello\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        self.assertEqual((project / "asset.bin").read_bytes(), b"\x00\xff\x01")
        self.assertTrue((project / "试玩.command").stat().st_mode & 0o100)
        self.assertNotEqual((source[0] / "asset.bin").stat().st_ino, (project / "asset.bin").stat().st_ino)
        self.assertFalse((source[0] / ".git").exists())
        self.assertEqual(collect_changes(project, receipt, ["试玩.command"]), {"files": [], "blocked": []})

    def test_nonconflicting_previous_change_is_carried_but_current_source_is_not_overwritten(self):
        baseline = self.export("old", {"one.py": "old\n", "two.py": "old\n"})
        candidate = self.export("candidate", {"one.py": "previous\n", "two.py": "old\n"})
        source = self.export("source", {"one.py": "old\n", "two.py": "new source\n"})
        receipt = self.prepare(source, baseline, candidate)
        project = Path(receipt["project"])
        self.assertEqual((project / "one.py").read_text(), "previous\n")
        self.assertEqual((project / "two.py").read_text(), "new source\n")
        self.assertEqual(receipt["carried_paths"], ["one.py"])
        self.assertEqual(receipt["conflicts"], [])

    def test_conflicting_b_s_d_are_preserved_for_the_agent_to_resolve(self):
        baseline = self.export("old", {"one.py": "old\n"})
        candidate = self.export("candidate", {"one.py": "previous\n"})
        source = self.export("source", {"one.py": "new source\n"})
        receipt = self.prepare(source, baseline, candidate)
        self.assertEqual((Path(receipt["project"]) / "one.py").read_text(), "new source\n")
        conflict = receipt["conflicts"][0]
        self.assertEqual(Path(conflict["baseline_path"]).read_text(), "old\n")
        self.assertEqual(Path(conflict["previous_path"]).read_text(), "previous\n")
        self.assertEqual((source[0] / "one.py").read_text(), "new source\n")

    def test_collection_preserves_cas_and_rejects_unowned_edits(self):
        source = self.export("source", {"own.py": "old\n", "peer.py": "peer\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "own.py").write_text("new\n")
        (project / "peer.py").write_text("unowned\n")
        (project / "unowned.py").write_text("new file\n")
        changes = collect_changes(project, receipt, ["own.py"])
        self.assertEqual(changes["files"][0]["previous_sha256"], source[1]["files"][0]["sha256"])
        self.assertEqual(changes["files"][0]["content"], "new\n")
        self.assertEqual({item["path"] for item in changes["blocked"]}, {"peer.py", "unowned.py"})
        self.assertEqual((source[0] / "own.py").read_text(), "old\n")

    def test_owned_binary_changes_deletions_and_mode_flips_are_collected(self):
        # Catches owned deletions, binaries and chmod +x being blocked instead of imported.
        source = self.export("source", {"asset.bin": b"\x00", "old.py": "old\n", "run.sh": "echo hi\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "asset.bin").write_bytes(b"\x00\x01")
        (project / "old.py").unlink()
        (project / "run.sh").chmod(0o755)
        (project / "new.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
        result = collect_changes(project, receipt, ["asset.bin", "old.py", "run.sh", "new.png"])
        self.assertEqual(result["blocked"], [])
        files = {item["path"]: item for item in result["files"]}
        self.assertEqual(files["asset.bin"], {"path": "asset.bin", "previous_sha256": hashlib.sha256(b"\x00").hexdigest(),
            "sha256": hashlib.sha256(b"\x00\x01").hexdigest(), "bytes": 2, "content_base64": "AAE=", "text": False})
        self.assertEqual(files["old.py"], {"path": "old.py", "previous_sha256": hashlib.sha256(b"old\n").hexdigest(),
            "sha256": None, "bytes": 0, "deleted": True})
        self.assertEqual((files["run.sh"]["content"], files["run.sh"]["executable"]), ("echo hi\n", True))
        self.assertEqual((files["new.png"]["previous_sha256"], files["new.png"]["text"]), (None, False))
        self.assertNotIn("executable", files["new.png"])
        self.assertEqual((project / "asset.bin").read_bytes(), b"\x00\x01")

    def test_unowned_deletion_binary_and_mode_flip_stay_blocked(self):
        # Catches an ownership breach riding in as a deletion, binary or chmod.
        source = self.export("source", {"asset.bin": b"\x00", "old.py": "old\n", "run.sh": "echo hi\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "asset.bin").write_bytes(b"\x00\x01")
        (project / "old.py").unlink()
        (project / "run.sh").chmod(0o755)
        result = collect_changes(project, receipt, [])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["blocked"], [{"path": name, "reason": "native_path_not_owned"}
                                             for name in ("asset.bin", "old.py", "run.sh")])

    def test_published_deletion_and_mode_advance_the_private_baseline(self):
        # Catches a published deletion or mode flip being collected again on the next call.
        source = self.export("source", {"old.py": "old\n", "run.sh": "echo hi\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "old.py").unlink()
        (project / "run.sh").chmod(0o755)
        collected = collect_changes(project, receipt, ["old.py", "run.sh"])
        advanced = acknowledge_published(project, receipt, collected["files"])
        self.assertEqual([(item["path"], item["executable"]) for item in advanced["files"]], [("run.sh", True)])
        self.assertEqual(collect_changes(project, advanced, ["old.py", "run.sh"]), {"files": [], "blocked": []})
        (project / "old.py").write_text("back\n")
        with self.assertRaisesRegex(NativeWorkspaceError, "native_export_changed"):
            acknowledge_published(project, receipt, [{"path": "old.py", "sha256": None, "deleted": True}])

    def test_sync_takes_a_published_mode_flip_but_keeps_an_unpublished_one(self):
        # Catches a peer's tree missing a shared chmod +x when the bytes did not change.
        source = self.export("source", {"run.sh": "echo hi\n", "own.sh": "echo own\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "own.sh").chmod(0o755)
        root, manifest = self.export("incoming", {"run.sh": "echo hi\n", "own.sh": "echo own\n"})
        for entry in manifest["files"]:
            entry["executable"] = entry["path"] == "run.sh"
        synced = sync_workspace(project, receipt, root, manifest)
        self.assertTrue((project / "run.sh").stat().st_mode & 0o100)
        self.assertTrue((project / "own.sh").stat().st_mode & 0o100)
        self.assertEqual([item["path"] for item in collect_changes(project, synced, ["own.sh"])["files"]], ["own.sh"])

    def test_withdrawn_deletion_and_mode_flip_are_put_back(self):
        # Catches a withdrawn task's deletion or chmod surviving in the private tree.
        source = self.export("source", {"old.py": "old\n", "run.sh": "echo hi\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "old.py").unlink()
        (project / "run.sh").chmod(0o755)
        _, discarded = discard_paths(project, receipt, ["old.py", "run.sh"])
        self.assertEqual(discarded, ["old.py", "run.sh"])
        self.assertEqual((project / "old.py").read_text(), "old\n")
        self.assertFalse((project / "run.sh").stat().st_mode & 0o111)
        self.assertEqual(collect_changes(project, receipt, ["old.py", "run.sh"]), {"files": [], "blocked": []})

    def test_symlink_cannot_be_collected_as_a_candidate(self):
        source = self.export("source", {"own.py": "old\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        with self.assertRaisesRegex(NativeWorkspaceError, "invalid_native_path"):
            collect_changes(project, receipt, [".GiT/config"])
        (project / "own.py").unlink()
        (project / "own.py").symlink_to(source[0] / "own.py")
        with self.assertRaisesRegex(NativeWorkspaceError, "native_symlink_refused"):
            collect_changes(project, receipt, ["own.py"])

    def test_parent_directory_symlink_is_refused_for_every_nested_path(self):
        # Catches: sharing checked parent directories across one tree walk must
        # still refuse a symlinked directory before reading any file beneath it.
        source = self.export("source", {"pkg/a.py": "a\n", "pkg/deep/b.py": "b\n", "top.py": "t\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        outside = self.root / "outside-pkg"
        shutil.copytree(project / "pkg", outside)
        shutil.rmtree(project / "pkg")
        (project / "pkg").symlink_to(outside, target_is_directory=True)
        # Hide the links from the untracked listing so only the parent check sees them.
        exclude = project / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("/pkg\n/pkg/deep\n")
        with self.assertRaisesRegex(NativeWorkspaceError, "native_symlink_refused"):
            collect_changes(project, receipt, [])
        (project / "pkg").unlink()
        shutil.copytree(outside, project / "pkg")
        (project / "pkg" / "deep").rename(project / "pkg" / "real-deep")
        (project / "pkg" / "deep").symlink_to(project / "pkg" / "real-deep", target_is_directory=True)
        with self.assertRaisesRegex(NativeWorkspaceError, "native_symlink_refused"):
            collect_changes(project, receipt, [])

    def test_reused_digest_never_hides_same_size_content_with_restored_mtime(self):
        # Catches: a stat-keyed digest cache that misses an edit whose size and
        # mtime were restored (ctime still changes), reporting it as unchanged.
        source = self.export("source", {"own.py": "old\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        self.assertEqual(collect_changes(project, receipt, ["own.py"])["files"], [])
        target = project / "own.py"
        before = target.stat()
        target.write_text("new\n")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        changed = collect_changes(project, receipt, ["own.py"])["files"]
        self.assertEqual([(item["path"], item["content"]) for item in changed], [("own.py", "new\n")])
        target.write_text("old\n")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(collect_changes(project, receipt, ["own.py"])["files"], [])

    def test_changed_export_is_rejected_and_only_created_tree_is_cleaned(self):
        source = self.export("source", {"own.py": "old\n"})
        (source[0] / "own.py").write_text("changed after export\n")
        with self.assertRaisesRegex(NativeWorkspaceError, "native_export_changed"):
            self.prepare(source)
        self.assertFalse((self.root / "native").exists())
        self.assertEqual((source[0] / "own.py").read_text(), "changed after export\n")

    def test_preexisting_destination_and_cancel_do_not_delete_user_data(self):
        source = self.export("source", {"own.py": "old\n"})
        target = self.root / "native"
        target.mkdir()
        (target / "keep").write_text("keep")
        with self.assertRaises(FileExistsError):
            self.prepare(source)
        self.assertEqual((target / "keep").read_text(), "keep")
        with self.assertRaisesRegex(NativeWorkspaceError, "native_preparation_cancelled"):
            prepare_workspace(self.root / "cancelled", *source, cancelled=lambda: True)
        self.assertFalse((self.root / "cancelled").exists())

    def test_real_folder_materialization_can_prepare_a_complete_native_tree(self):
        from local_agent_coordinator.folder_workspace import FolderWorkspace
        from local_agent_coordinator.project_catalog import ProjectCatalog

        source, _ = self.export("source", {"code/main.py": "VALUE = 1\n", "image.bin": b"\x00\xff"})
        catalog = ProjectCatalog(self.root / "catalog")
        registration = catalog.register_folder("Synthetic native project", str(source))
        folder = FolderWorkspace(self.root / "folders", str(uuid4()), create=True)
        folder.seed_folder(registration)
        exported = self.root / "exported"
        manifest = folder.materialize_snapshot(exported)
        receipt = self.prepare((exported, manifest))
        project = Path(receipt["project"])
        (project / "code/main.py").write_text("VALUE = 2\n")
        changes = collect_changes(project, receipt, ["code/main.py"])
        self.assertEqual(changes["files"][0]["content"], "VALUE = 2\n")
        self.assertEqual(changes["blocked"], [])
        self.assertEqual((project / "image.bin").read_bytes(), b"\x00\xff")
        self.assertEqual((source / "code/main.py").read_text(), "VALUE = 1\n")

    def test_stable_cwd_sync_updates_peer_files_and_keeps_unpublished_owner_bytes(self):
        source = self.export("source", {"own.py": "old\n", "peer.py": "old peer\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "own.py").write_text("unpublished\n")
        incoming = self.export("incoming", {"own.py": "old\n", "peer.py": "published peer\n"})
        synced = sync_workspace(project, receipt, *incoming)
        self.assertEqual(synced["project"], receipt["project"])
        self.assertEqual((project / "own.py").read_text(), "unpublished\n")
        self.assertEqual((project / "peer.py").read_text(), "published peer\n")
        changes = collect_changes(project, synced, ["own.py"])
        self.assertEqual(changes["blocked"], [])
        self.assertEqual([item["path"] for item in changes["files"]], ["own.py"])

    def test_only_actual_host_published_bytes_advance_the_cas_baseline(self):
        source = self.export("source", {"own.py": "old\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "own.py").write_text("published\n")
        change = collect_changes(project, receipt, ["own.py"])["files"]
        acknowledged = acknowledge_published(project, receipt, change)
        self.assertEqual(collect_changes(project, acknowledged, ["own.py"])["files"], [])
        (project / "own.py").write_text("next revision\n")
        next_change = collect_changes(project, acknowledged, ["own.py"])["files"][0]
        self.assertEqual(next_change["previous_sha256"], change[0]["sha256"])
        with self.assertRaisesRegex(NativeWorkspaceError, "native_export_changed"):
            acknowledge_published(project, acknowledged, change)

    def test_sync_conflict_keeps_both_versions_and_import_compares_to_incoming_sha(self):
        source = self.export("source", {"own.py": "old\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "own.py").write_text("local changes\n")
        incoming = self.export("incoming", {"own.py": "shared changes\n"})
        synced = sync_workspace(project, receipt, *incoming)
        conflict = synced["conflicts"][0]
        self.assertEqual((project / "own.py").read_text(), "local changes\n")
        self.assertEqual(Path(conflict["baseline_path"]).read_text(), "old\n")
        self.assertEqual(Path(conflict["incoming_path"]).read_text(), "shared changes\n")
        blocked = collect_changes(project, synced, ["own.py"])
        self.assertEqual(blocked["blocked"][0]["reason"], "native_unresolved_conflict")
        self.assertEqual(blocked["files"], [])
        change = collect_changes(project, synced, ["own.py"], resolved_conflicts=["own.py"])["files"][0]
        self.assertEqual(change["previous_sha256"], incoming[1]["files"][0]["sha256"])

    def test_replacing_known_binary_with_text_is_collected_as_text(self):
        # Catches a binary-to-text rewrite of an owned file still being refused.
        source = self.export("source", {"asset.bin": b"\x00\xff"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "asset.bin").write_text("now text\n")
        change = collect_changes(project, receipt, ["asset.bin"])
        self.assertEqual(change["blocked"], [])
        self.assertEqual([(item["path"], item["content"]) for item in change["files"]], [("asset.bin", "now text\n")])

    def test_sync_base_keeps_exact_crlf_bytes_even_when_git_attributes_normalize(self):
        source = self.export("source", {".gitattributes": "*.txt text eol=lf\n", "own.txt": "old\r\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "own.txt").write_bytes(b"local\r\n")
        incoming = self.export("incoming", {".gitattributes": "*.txt text eol=lf\n", "own.txt": "shared\r\n"})
        synced = sync_workspace(project, receipt, *incoming)
        self.assertEqual(Path(synced["conflicts"][0]["baseline_path"]).read_bytes(), b"old\r\n")

    def test_published_resolution_clears_active_conflict_but_preserves_context_files(self):
        source = self.export("source", {"own.py": "old\n"})
        receipt = self.prepare(source)
        project = Path(receipt["project"])
        (project / "own.py").write_text("local\n")
        incoming = self.export("incoming", {"own.py": "shared\n"})
        synced = sync_workspace(project, receipt, *incoming)
        with self.assertRaisesRegex(NativeWorkspaceError, "native_resolution_not_published"):
            acknowledge_published(project, synced, [], resolved_paths=["own.py"])
        (project / "own.py").write_text("merged\n")
        change = collect_changes(project, synced, ["own.py"])["files"]
        result = acknowledge_published(project, synced, change)
        self.assertEqual(result["conflicts"], [])
        self.assertTrue(Path(synced["conflicts"][0]["incoming_path"]).exists())

    def test_keeping_new_source_can_explicitly_resolve_the_initial_handoff(self):
        baseline = self.export("old", {"own.py": "old\n"})
        candidate = self.export("candidate", {"own.py": "previous\n"})
        source = self.export("source", {"own.py": "new source\n"})
        receipt = self.prepare(source, baseline, candidate)
        project = Path(receipt["project"])
        synced = sync_workspace(project, receipt, *source)
        self.assertEqual(len(synced["conflicts"]), 1)  # Must still tell the agent about old work.
        result = acknowledge_published(project, synced, [], resolved_paths=["own.py"])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual((project / "own.py").read_text(), "new source\n")
