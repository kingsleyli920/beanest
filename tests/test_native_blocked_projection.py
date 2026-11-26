"""Old native import warnings are current only while their byte evidence differs."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_coordinator import native_workspace as workspace


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class NativeBlockedProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'private'
        self.root.mkdir()

    def manifest(self, names, reason='native_path_not_owned'):
        return {'project': str(self.root), 'blocked': [{'path': name, 'reason': reason} for name in names]}

    def test_twenty_five_published_files_leave_one_current_issue_without_rewriting_history(self):
        names = [f'file-{number}.py' for number in range(26)]
        canonical = {name: digest('published\n') for name in names}
        for name in names:
            (self.root / name).write_text('published\n' if name != names[-1] else 'not published\n')
        manifest = self.manifest(names)
        original = copy.deepcopy(manifest)
        result = workspace.pending_blocked(manifest, canonical)
        self.assertEqual(result['published_count'], 25)
        self.assertEqual(result['blocked'], [{'path': names[-1], 'reason': 'native_path_not_owned',
            'sha256': digest('not published\n'), 'canonical_sha256': digest('published\n')}])
        self.assertEqual(manifest, original)
        # Evidence changes only when the bytes do; an ordinary UI poll is stable.
        self.assertEqual(workspace.pending_blocked(manifest, canonical), result)
        (self.root / names[-1]).write_text('new unpublished version\n')
        self.assertNotEqual(workspace.pending_blocked(manifest, canonical)['blocked'][0]['sha256'],
                            result['blocked'][0]['sha256'])

    def test_links_missing_files_and_unreadable_evidence_are_never_hidden_or_followed(self):
        outside = self.root.parent / 'outside.py'
        outside.write_text('same\n')
        (self.root / 'linked.py').symlink_to(outside)
        (self.root / 'linked-parent').symlink_to(self.root.parent, target_is_directory=True)
        (self.root / 'unreadable.py').write_text('same\n')
        names = ['linked.py', 'linked-parent/outside.py', 'missing.py', 'unreadable.py']
        manifest = self.manifest(names)
        original = workspace._file_digest
        def read(path):
            self.assertFalse(path.is_symlink())
            self.assertNotIn('linked-parent', path.parts)
            if path.name == 'unreadable.py':
                raise PermissionError('fixture unreadable')
            return original(path)
        with patch.object(workspace, '_file_digest', side_effect=read) as reads:
            result = workspace.pending_blocked(manifest, {name: digest('same\n') for name in names})
        self.assertEqual(result['published_count'], 0)
        self.assertEqual([issue['path'] for issue in result['blocked']], names)
        self.assertTrue(all(issue['sha256'] is None for issue in result['blocked']))
        self.assertEqual(reads.call_count, 1, 'Only the safe regular file was attempted')
        self.assertEqual(outside.read_text(), 'same\n')

    def test_missing_canonical_or_mode_and_conflict_reasons_cannot_be_claimed_published(self):
        (self.root / 'same.py').write_text('same\n')
        for reason in ['native_path_not_owned', 'native_mode_change_not_imported',
                       'native_unresolved_conflict', 'unrecognized_reason']:
            with self.subTest(reason=reason):
                manifest = self.manifest(['same.py'], reason)
                canonical = {} if reason == 'native_path_not_owned' else {'same.py': digest('same\n')}
                result = workspace.pending_blocked(manifest, canonical)
                self.assertEqual(result['published_count'], 0)
                self.assertEqual(result['blocked'][0]['reason'], reason)
                self.assertEqual(result['blocked'][0]['sha256'], digest('same\n'))
        result = workspace.pending_blocked(self.manifest(['same.py']), None)
        self.assertIsNone(result['blocked'][0]['canonical_sha256'])

    def test_invalid_paths_and_missing_registered_root_stay_visible(self):
        names = ['../outside.py', '/absolute.py']
        result = workspace.pending_blocked(self.manifest(names), {name: digest('same\n') for name in names})
        self.assertEqual(result['published_count'], 0)
        self.assertEqual(len(result['blocked']), 2)
        self.assertTrue(all(issue['sha256'] is None for issue in result['blocked']))
        manifest = self.manifest(['source.py'])
        manifest['project'] = str(self.root / 'absent')
        result = workspace.pending_blocked(manifest, {'source.py': digest('same\n')})
        self.assertEqual(result['published_count'], 0)
        self.assertIsNone(result['blocked'][0]['sha256'])

    def test_a_file_changed_during_comparison_is_unknown_rather_than_published(self):
        (self.root / 'active.py').write_text('published\n')
        original = workspace._file_digest
        def changing(path):
            observed = original(path)
            path.write_text('new unpublished work\n')
            return observed
        with patch.object(workspace, '_file_digest', side_effect=changing):
            result = workspace.pending_blocked(self.manifest(['active.py']),
                                               {'active.py': digest('published\n')})
        self.assertEqual(result['published_count'], 0)
        self.assertEqual(len(result['blocked']), 1)
        self.assertIsNone(result['blocked'][0]['sha256'])


if __name__ == '__main__':
    unittest.main()
