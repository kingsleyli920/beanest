"""Independent filesystem/Git boundaries; no providers or user workspaces."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from local_agent_coordinator import native_workspace as workspace


class RetainedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.export = self.root / 'canonical'
        self.export.mkdir()
        self.manifest = {'revision': 7, 'files': []}
        for name, raw, text, executable in (
                ('code/old.py', b'canonical\r\n', True, False),
                ('assets/picture.bin', b'\x00\xff', False, False),
                ('run.command', b'echo canonical\n', True, True)):
            target = self.export / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o700 if executable else 0o600)
            self.manifest['files'].append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                'bytes': len(raw), 'text': text, 'executable': executable})
        self.source_state = workspace.prepare_workspace(self.root / 'old-source', self.export, self.manifest)
        self.source = Path(self.source_state['project'])
        (self.source / 'code/old.py').write_bytes(b'retained\r\n')
        (self.source / 'new.py').write_bytes(b'new retained\n')
        self.changes = workspace.collect_changes(self.source, self.source_state,
            ['code/old.py', 'new.py'])['files']
        self.destination = self.root / 'new-receiver'

    def prepare(self, **overrides):
        args = dict(destination=self.destination, canonical_export=self.export,
            canonical_manifest=self.manifest, source_private=self.source, retained_files=self.changes)
        args.update(overrides)
        return workspace.prepare_retained_workspace(**args)

    def test_complete_new_tree_has_s_baseline_d_bytes_and_sync_preserves_them(self):
        source_head = (self.source / '.git/HEAD').read_bytes()
        old_receiver = self.root / 'old-receiver'
        old_receiver.mkdir()
        (old_receiver / 'marker').write_text('unchanged')
        receipt = self.prepare()
        project = Path(receipt['project'])
        self.assertNotEqual(project, self.source)
        self.assertEqual(receipt['files'], sorted(self.manifest['files'], key=lambda item: item['path']))
        self.assertEqual((project / 'assets/picture.bin').read_bytes(), b'\x00\xff')
        self.assertTrue((project / 'run.command').stat().st_mode & 0o100)
        self.assertEqual((project / 'code/old.py').read_bytes(), b'retained\r\n')
        self.assertEqual((project / 'new.py').read_bytes(), b'new retained\n')
        self.assertEqual(set(receipt['retained_paths']), {'code/old.py', 'new.py'})
        synced = workspace.sync_workspace(project, receipt, self.export, self.manifest)
        recollected = workspace.collect_changes(project, synced, ['code/old.py', 'new.py'])
        self.assertEqual(recollected, {'files': self.changes, 'blocked': []})
        self.assertEqual((self.source / '.git/HEAD').read_bytes(), source_head)
        self.assertEqual((old_receiver / 'marker').read_text(), 'unchanged')
        self.assertNotEqual((project / 'code/old.py').stat().st_ino, (self.source / 'code/old.py').stat().st_ino)

    def test_retained_s_base_objects_survive_changed_canonical_sync(self):
        receipt = self.prepare()
        project = Path(receipt['project'])
        changed = copy.deepcopy(self.manifest)
        raw = b'new canonical\n'
        (self.export / 'code/old.py').write_bytes(raw)
        item = next(item for item in changed['files'] if item['path'] == 'code/old.py')
        item.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        synced = workspace.sync_workspace(project, receipt, self.export, changed)
        conflict = synced['conflicts'][0]
        self.assertEqual(Path(conflict['baseline_path']).read_bytes(), b'canonical\r\n')
        self.assertEqual((project / 'code/old.py').read_bytes(), b'retained\r\n')

    def test_existing_destination_is_never_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / 'marker'
        marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(marker.read_text(), 'keep')

    def test_bad_prior_sha_and_new_file_prior_are_refused(self):
        for name in ('code/old.py', 'new.py'):
            changes = copy.deepcopy(self.changes)
            next(item for item in changes if item['path'] == name)['previous_sha256'] = '0' * 64
            with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'native_retained_baseline_changed'):
                self.prepare(retained_files=changes)
            self.assertFalse(self.destination.exists())

    def test_changed_s_export_is_refused_without_leaving_receiver(self):
        (self.export / 'code/old.py').write_bytes(b'drifted S')
        with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'native_export_changed'):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_changed_d_source_is_refused_and_both_old_trees_preserved(self):
        (self.source / 'code/old.py').write_bytes(b'drifted D')
        with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'native_export_changed'):
            self.prepare()
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.source / 'code/old.py').read_bytes(), b'drifted D')
        self.assertEqual((self.export / 'code/old.py').read_bytes(), b'canonical\r\n')

    def test_bad_retained_content_bytes_hash_and_duplicates_are_refused(self):
        for mutation in ({'content': 'fake'}, {'bytes': 1}, {'sha256': '0' * 64}):
            changes = copy.deepcopy(self.changes)
            changes[0].update(mutation)
            with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'invalid_native_retained_files'):
                self.prepare(retained_files=changes)
        with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'invalid_native_retained_files'):
            self.prepare(retained_files=self.changes + self.changes)
        self.assertFalse(self.destination.exists())

    def test_path_escape_and_git_control_are_refused(self):
        for name in ('../outside', '.git/config', '/absolute', 'code/../old.py'):
            changes = copy.deepcopy(self.changes)
            changes[0]['path'] = name
            with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'invalid_native_path'):
                self.prepare(retained_files=changes)
        self.assertFalse(self.destination.exists())

    def test_canonical_case_alias_and_missing_new_prior_binding_are_refused(self):
        changes = copy.deepcopy(self.changes)
        next(item for item in changes if item['path'] == 'code/old.py')['path'] = 'Code/old.py'
        with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'invalid_native_retained_files'):
            self.prepare(retained_files=changes)
        changes = copy.deepcopy(self.changes)
        del next(item for item in changes if item['path'] == 'new.py')['previous_sha256']
        with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'invalid_native_retained_files'):
            self.prepare(retained_files=changes)

    def test_retained_exec_mode_change_is_refused(self):
        for name in ('code/old.py', 'new.py'):
            (self.source / name).chmod(0o700)
            with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'native_retained_mode_changed'):
                self.prepare()
            (self.source / name).chmod(0o600)
        self.assertFalse(self.destination.exists())

    def test_source_and_destination_symlinks_are_refused(self):
        original = self.source / 'code/old.py'
        original.unlink()
        original.symlink_to(self.export / 'code/old.py')
        with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'native_symlink_refused'):
            self.prepare()
        self.assertFalse(self.destination.exists())
        original.unlink()
        original.write_bytes(b'retained\r\n')
        self.destination.symlink_to(self.root / 'old-source', target_is_directory=True)
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertTrue(self.destination.is_symlink())


if __name__ == '__main__':
    unittest.main()
