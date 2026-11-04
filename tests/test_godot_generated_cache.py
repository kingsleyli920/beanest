"""Standard generated directories stay private without exempting captured inputs."""
from pathlib import Path
import unittest
from uuid import uuid4
from unittest.mock import patch
from local_agent_coordinator import folder_workspace
from local_agent_coordinator.project_workspace import ProjectWorkspaceError

from local_agent_coordinator.folder_workspace import FolderWorkspace
from local_agent_coordinator.project_catalog import ProjectCatalog
from local_agent_coordinator.native_workspace import collect_changes, NativeWorkspaceError
import test_native_workspace as fixtures


class GodotGeneratedCacheTests(unittest.TestCase):
    setUp = fixtures.NativeWorkspaceTests.setUp
    export = fixtures.NativeWorkspaceTests.export
    prepare = fixtures.NativeWorkspaceTests.prepare

    def test_folder_capture_excludes_cache_but_keeps_project_and_test_sources(self):
        source = self.root / 'registered'
        (source / 'code/.godot').mkdir(parents=True)
        (source / 'code/.godot/check.lock').write_bytes(b'')
        (source / 'code/project.godot').write_text('config_version=5\n')
        (source / 'code/test.gd').write_text('extends Node\n')
        registration = ProjectCatalog(self.root / 'catalog').register_folder('Fictional engine project', str(source))
        workspace = FolderWorkspace(self.root / 'private', str(uuid4()), create=True)
        workspace.seed_folder(registration)
        # Folder snapshots omit contents; use the actual exported manifest.
        manifest = workspace.materialize_snapshot(self.root / 'export', 1)
        paths = {entry['path'] for entry in manifest['files']}
        self.assertEqual(paths, {'code/project.godot', 'code/test.gd'})
        self.assertTrue((source / 'code/.godot/check.lock').exists())

    def test_new_cache_is_recorded_without_blocking_or_importing_authorized_change(self):
        receipt = self.prepare(self.export('source', {'own.gd':'old\n'}))
        project = Path(receipt['project'])
        (project / 'own.gd').write_text('new\n')
        cache = project / 'code/.godot/check.lock'
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'')
        changes = collect_changes(project, receipt, ['own.gd'])
        self.assertEqual(changes['blocked'], [])
        self.assertEqual([item['path'] for item in changes['files']], ['own.gd'])
        self.assertEqual([item['path'] for item in changes['generated']], ['code/.godot/check.lock'])
        self.assertEqual(cache.read_bytes(), b'')

    def test_captured_legacy_cache_input_keeps_change_deletion_and_mode_guards(self):
        for fault, reason in (('change','native_path_not_owned'),
                              ('delete','native_deletion_not_imported'),
                              ('mode','native_mode_change_not_imported')):
            with self.subTest(fault=fault):
                # A historical manifest may predate the directory exclusion.
                source = self.export(fault, {'code/.godot/captured.txt':'captured input\n'})
                from local_agent_coordinator.native_workspace import prepare_workspace
                receipt = prepare_workspace(self.root / ('native-' + fault), *source)
                path = Path(receipt['project']) / 'code/.godot/captured.txt'
                if fault == 'change': path.write_text('changed input\n')
                elif fault == 'delete': path.unlink()
                else: path.chmod(0o700)
                changes = collect_changes(receipt['project'], receipt, [])
                self.assertEqual(changes['blocked'], [{'path':'code/.godot/captured.txt','reason':reason}])
                self.assertFalse(changes.get('generated'))

    def test_sensitive_symlink_and_ordinary_unowned_changes_remain_rejected(self):
        receipt = self.prepare(self.export('source', {'own.gd':'old\n'}))
        project = Path(receipt['project'])
        directory = project / 'code/.godot'
        directory.mkdir(parents=True)
        (directory / '.env').write_text('FICTIONAL=not-a-credential\n')
        (project / 'unowned.gd').write_text('unauthorized\n')
        changes = collect_changes(project, receipt, ['own.gd'])
        self.assertEqual({item['path'] for item in changes['blocked']}, {'code/.godot/.env','unowned.gd'})
        self.assertFalse(changes.get('generated'))
        (directory / '.env').unlink()
        (directory / 'cache').symlink_to(project / 'own.gd')
        with self.assertRaisesRegex(NativeWorkspaceError, 'native_symlink_refused'):
            collect_changes(project, receipt, ['own.gd'])

    def test_pre_policy_capture_stays_readable_without_granting_new_cache_writes(self):
        source = self.root / 'old-registered'
        (source / 'code/.godot').mkdir(parents=True)
        (source / 'code/.godot/captured.txt').write_text('historical input\n')
        registration = ProjectCatalog(self.root / 'catalog').register_folder('Old registered inputs', str(source))
        workspace = FolderWorkspace(self.root / 'private', str(uuid4()), create=True)
        with patch.object(folder_workspace, '_EXCLUDED_DIRS', folder_workspace._EXCLUDED_DIRS - {'.godot'}):
            workspace.seed_folder(registration)
        snapshot = workspace.snapshot()
        self.assertEqual(snapshot['total_files'], 1)
        request = dict(kind='read', path='code/.godot/captured.txt', query=None, offset=0, limit=20)
        self.assertEqual(workspace.inspect(request)['content'], 'historical input\n')
        with self.assertRaisesRegex(ProjectWorkspaceError, 'project_sensitive_content'):
            workspace.inspect({**request, 'path':'code/.godot/uncaptured.txt'})
        with self.assertRaisesRegex(ProjectWorkspaceError, 'project_sensitive_content'):
            workspace.apply('codex', 'must-not-authorize-cache', snapshot['revision'],
                [{'path':'code/.godot/captured.txt', 'previous_sha256':None, 'content':'changed'}])
        for path in ('code/.godot/.env', 'code/.godot/.git/config', 'code/.godot/__pycache__/cache.pyc'):
            with self.subTest(path=path):
                self.assertIsNotNone(folder_workspace._excluded(path, captured=True))
