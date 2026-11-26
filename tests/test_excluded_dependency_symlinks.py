"""Excluded dependency links never become imported files or followed inputs."""
from pathlib import Path
import unittest
from unittest.mock import patch

from local_agent_coordinator import native_workspace as workspace
import test_native_workspace as fixtures


class ExcludedDependencySymlinkTests(unittest.TestCase):
    setUp = fixtures.NativeWorkspaceTests.setUp
    export = fixtures.NativeWorkspaceTests.export

    def test_new_excluded_links_do_not_block_authorized_source_or_read_targets(self):
        for name, dangling in (('.claude/worktrees/fixture/node_modules', False),
                               ('code/.cache/generated', True)):
            with self.subTest(name=name):
                receipt = workspace.prepare_workspace(self.root / ('native-' + str(dangling)),
                    *self.export('source-' + str(dangling), {'own.py': 'old\n'}))
                project = Path(receipt['project'])
                (project / 'own.py').write_text('new\n')
                outside = self.root / ('outside-' + str(dangling))
                if not dangling:
                    outside.mkdir()
                    (outside / 'dependency.js').write_text('external bytes stay private\n')
                link = project / name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                original = workspace._file_digest
                def digest(path):
                    self.assertFalse(path.is_symlink(), 'No excluded link target may be hashed')
                    self.assertTrue(path.is_relative_to(project), 'No external target may be hashed')
                    return original(path)
                with patch.object(workspace, '_file_digest', side_effect=digest):
                    result = workspace.collect_changes(project, receipt, ['own.py'])
                self.assertEqual([item['path'] for item in result['files']], ['own.py'])
                self.assertEqual(result['files'][0]['content'], 'new\n')
                self.assertEqual(result['blocked'], [])
                self.assertFalse(result.get('generated'), 'A skipped link is not a hashed generated file')
                self.assertTrue(link.is_symlink())
                if not dangling:
                    self.assertEqual((outside / 'dependency.js').read_text(), 'external bytes stay private\n')

    def test_source_sensitive_and_historically_captured_links_remain_refused(self):
        for name, captured in (('source.py', False), ('.env', False),
                               ('code/.cache/captured.py', True)):
            with self.subTest(name=name):
                source = self.export('input-' + str(captured) + '-' + name.replace('/', '-'),
                                     {name: 'historical\n'} if captured else {'own.py': 'old\n'})
                receipt = workspace.prepare_workspace(self.root / ('native-' + name.replace('/', '-')), *source)
                project = Path(receipt['project'])
                link = project / name
                link.parent.mkdir(parents=True, exist_ok=True)
                if captured:
                    link.unlink()
                link.symlink_to(source[0] / (name if captured else 'own.py'))
                with self.assertRaisesRegex(workspace.NativeWorkspaceError, 'native_symlink_refused'):
                    workspace.collect_changes(project, receipt, [name])


if __name__ == '__main__':
    unittest.main()
