import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from local_agent_coordinator.room_purge import ROOM_BUCKETS, RoomPurgeError, purge_room_files


class RoomPurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'runtime'
        self.root.mkdir()
        self.workspace = self.root / 'workers'
        self.room, self.neighbor, self.execution = str(uuid4()), str(uuid4()), str(uuid4())

    def write(self, path, content='private'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def purge(self, **kwargs):
        return purge_room_files(self.root, self.workspace, self.room, **kwargs)

    def test_deletes_all_owned_trees_locks_and_only_selected_attempt(self):
        preserved = []
        for bucket in (*ROOM_BUCKETS, 'workers'):
            self.write(self.root / bucket / self.room / 'nested' / 'file')
            preserved.append(self.write(self.root / bucket / self.neighbor / 'file'))
        for bucket in ('project-artifacts', 'folder-artifacts'):
            self.write(self.root / bucket / f'.{self.room}.lock', '')
            preserved.append(self.write(self.root / bucket / f'.{self.neighbor}.lock', ''))
        attempt = self.write(self.root / 'python-runner' / 'attempts' / f'{self.execution}.json')
        preserved.append(self.write(self.root / 'python-runner' / 'attempts' / f'{uuid4()}.json'))
        preserved.append(self.write(self.root / 'python-runner' / 'runner-config.json'))
        preserved.append(self.write(self.root / 'projects' / 'registration.json'))
        preserved.append(self.write(self.root / 'partners' / 'registration.json'))
        result = self.purge(execution_ids=[self.execution])
        self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(len(result['removed']), len(ROOM_BUCKETS) + 4)
        self.assertFalse(attempt.exists())
        self.assertTrue(all(path.exists() for path in preserved))
        self.assertEqual(self.purge(execution_ids=[self.execution])['removed'], [])

    def test_nested_and_room_symlinks_are_unlinked_without_traversal(self):
        outside = self.write(self.base / 'original-project' / 'important')
        nested = self.root / 'native-workspaces' / self.room
        nested.mkdir(parents=True)
        (nested / 'escape').symlink_to(outside.parent, target_is_directory=True)
        bucket = self.root / 'folder-artifacts'
        bucket.mkdir()
        (bucket / self.room).symlink_to(outside.parent, target_is_directory=True)
        self.purge()
        self.assertEqual(outside.read_text(), 'private')
        self.assertFalse((bucket / self.room).is_symlink())

    def test_symlink_bucket_refused_before_any_deletion(self):
        victim = self.write(self.root / 'project-artifacts' / self.room / 'file')
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'native-workspaces').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RoomPurgeError):
            self.purge()
        self.assertTrue(victim.exists())

    def test_symlink_runtime_ancestor_refused(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RoomPurgeError):
            purge_room_files(alias, alias / 'workers', self.room)

    def test_invalid_ids_and_roots_have_no_side_effects(self):
        victim = self.write(self.root / 'project-artifacts' / self.room / 'file')
        for invalid in ('../escape', '', self.room.upper(), None):
            with self.subTest(invalid=invalid), self.assertRaises(RoomPurgeError):
                purge_room_files(self.root, self.workspace, invalid)
        for ids in ([self.execution, '../escape'], self.execution, None):
            with self.assertRaises(RoomPurgeError):
                self.purge(execution_ids=ids)
        for invalid in (self.root, self.base / 'outside', self.root / 'projects'):
            with self.assertRaises(RoomPurgeError):
                purge_room_files(self.root, invalid, self.room)
        self.assertTrue(victim.exists())

    def test_busy_artifact_lock_blocks_before_deleting_other_bucket(self):
        victim = self.write(self.root / 'project-artifacts' / self.room / 'file')
        lock = self.write(self.root / 'folder-artifacts' / f'.{self.room}.lock', '')
        with lock.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RoomPurgeError, 'purge_workspace_busy'):
                self.purge()
        self.assertTrue(victim.exists())

    def test_io_failure_is_not_reported_as_success_and_retry_completes(self):
        victim = self.write(self.root / 'native-receipts' / self.room / 'file')
        with patch('local_agent_coordinator.room_purge.os.unlink', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(RoomPurgeError, 'purge_files_incomplete'):
                self.purge()
        self.assertTrue(victim.exists())
        self.assertTrue(self.purge()['cleanup_confirmed'])

    def test_symlink_lock_refused_without_unlinking_external_lock(self):
        victim = self.write(self.root / 'project-artifacts' / self.room / 'file')
        outside = self.write(self.base / 'external.lock')
        (self.root / 'project-artifacts' / f'.{self.room}.lock').symlink_to(outside)
        with self.assertRaises(RoomPurgeError):
            self.purge()
        self.assertTrue(victim.exists())
        self.assertTrue(outside.exists())

    def test_attempt_symlink_unlinked_without_deleting_target(self):
        outside = self.write(self.base / 'original-session.json')
        attempts = self.root / 'python-runner' / 'attempts'
        attempts.mkdir(parents=True)
        receipt = attempts / f'{self.execution}.json'
        receipt.symlink_to(outside)
        self.purge(execution_ids=[self.execution])
        self.assertTrue(outside.exists())
        self.assertFalse(receipt.is_symlink())
