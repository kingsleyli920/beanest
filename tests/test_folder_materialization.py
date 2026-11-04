"""Host-only immutable export; no project execution, model or source writeback."""
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import test_folder_workspace as fixtures
from local_agent_coordinator.project_workspace import ProjectWorkspaceError


class FolderMaterializationTests(unittest.TestCase):
    setUp = fixtures.FolderWorkspaceTests.setUp
    seed = fixtures.FolderWorkspaceTests.seed
    proposal = fixtures.FolderWorkspaceTests.proposal
    inspect = fixtures.FolderWorkspaceTests.inspect
    assertCode = fixtures.FolderWorkspaceTests.assertCode

    def test_full_binary_tree_and_exact_historical_revision_are_exported(self):
        (self.source/'nested').mkdir()
        (self.source/'nested/main.py').write_text('VALUE = 1\n')
        script=self.source/'start.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o755)
        binary=b'\x00\xff'*(9*1024*1024)
        (self.source/'large.bin').write_bytes(binary)
        seed=self.seed()
        self.workspace.apply('claude','edit',seed['revision'],[self.proposal('nested/main.py','VALUE = 2\n')])
        current=self.workspace.materialize_snapshot(self.base/'current')
        before=self.workspace.materialize_snapshot(self.base/'before',seed['revision'])
        self.assertEqual((self.base/'current/nested/main.py').read_text(),'VALUE = 2\n')
        self.assertEqual((self.base/'before/nested/main.py').read_text(),'VALUE = 1\n')
        self.assertEqual((self.source/'nested/main.py').read_text(),'VALUE = 1\n')
        self.assertEqual((self.base/'current/large.bin').read_bytes(),binary)
        files={item['path']:item for item in current['files']}
        self.assertEqual(files['large.bin']['sha256'],hashlib.sha256(binary).hexdigest())
        self.assertFalse(files['large.bin']['text'])
        self.assertTrue(files['start.sh']['executable'])
        self.assertTrue((self.base/'current/start.sh').stat().st_mode&0o100)
        self.assertNotEqual(current['index_sha256'],before['index_sha256'])

    def test_existing_destination_symlinks_and_nonprivate_parent_are_refused(self):
        (self.source/'a.txt').write_text('original')
        self.seed()
        existing=self.base/'existing'
        existing.mkdir()
        (existing/'keep').write_text('keep')
        self.assertCode('project_file_conflict',lambda:self.workspace.materialize_snapshot(existing))
        alias=self.base/'alias'
        alias.symlink_to(self.source,target_is_directory=True)
        self.assertCode('project_file_conflict',lambda:self.workspace.materialize_snapshot(alias))
        public=self.base/'public'
        public.mkdir(mode=0o755)
        self.assertCode('project_unsafe_path',lambda:self.workspace.materialize_snapshot(public/'copy'))
        self.assertEqual((existing/'keep').read_text(),'keep')
        self.assertEqual((self.source/'a.txt').read_text(),'original')

    def test_corrupt_blob_and_cancellation_remove_only_the_new_export(self):
        (self.source/'a.txt').write_text('original')
        self.seed()
        dest=self.base/'cancelled'
        self.assertCode('project_export_cancelled',lambda:self.workspace.materialize_snapshot(dest,cancelled=lambda:True))
        self.assertFalse(dest.exists())
        entry=self.inspect()['entries'][0]
        blob=self.root/self.room_id/'blobs'/entry['sha256']
        blob.write_bytes(b'changed!')
        dest=self.base/'corrupt'
        self.assertCode('project_storage_corrupt',lambda:self.workspace.materialize_snapshot(dest))
        self.assertFalse(dest.exists())
        self.assertEqual((self.source/'a.txt').read_text(),'original')

    def test_legacy_missing_executable_mode_is_unknown_and_never_guessed(self):
        (self.source/'old.sh').write_text('exit 0\n')
        capture=self.workspace._capture_file
        def legacy(*args,**kwargs):
            entry=capture(*args,**kwargs)
            entry.pop('executable',None)
            return entry
        with patch.object(self.workspace,'_capture_file',side_effect=legacy):
            self.seed()
        result=self.workspace.materialize_snapshot(self.base/'old-export')
        self.assertIsNone(result['files'][0]['executable'])
        self.assertEqual((self.base/'old-export/old.sh').read_text(),'exit 0\n')

    def test_handoff_owner_binding_changes_only_metadata_on_a_fresh_capture(self):
        (self.source / 'main.py').write_text('VALUE = 2\n')
        self.seed()
        entry = self.workspace.metadata_for_paths(['main.py'])[0]
        self.workspace.assign_baseline_owners({'main.py': {'owner': 'claude', 'sha256': entry['sha256']}},
                                             allowed_owners=['claude', 'codex'])
        assigned = self.workspace.metadata_for_paths(['main.py'])[0]
        self.assertEqual(assigned['owner'], 'claude')
        self.assertEqual(assigned['sha256'], entry['sha256'])
        self.assertEqual(self.workspace.snapshot()['files'], [])
        self.assertEqual((self.source / 'main.py').read_text(), 'VALUE = 2\n')
        self.assertCode('project_owner_conflict', lambda: self.workspace.apply('codex', 'wrong-author', 2,
            [{'path': 'main.py', 'previous_sha256': entry['sha256'], 'content': 'VALUE = 3\n'}]))
