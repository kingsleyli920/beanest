import tempfile
from pathlib import Path
import unittest
from uuid import uuid4

from local_agent_coordinator.project_workspace import ProjectWorkspace, ProjectWorkspaceError


class TextArtifactWorkspaceTests(unittest.TestCase):
    def test_opt_in_creates_svg_and_document_without_importing_a_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root, room = Path(directory).resolve() / 'files', str(uuid4())
            workspace = ProjectWorkspace(root, room, kind='text', allow_new_text_files=True)
            first = workspace.apply('codex', 'svg', 0, [
                {'path': 'bean.svg', 'content': '<svg/>', 'previous_sha256': None}])
            self.assertIsNone(first['source'])
            self.assertEqual(first['changes'][0]['status'], 'added')
            workspace.apply('claude', 'document', 0, [
                {'path': 'guide.md', 'content': '# 豆窝\n', 'previous_sha256': None}], allow_disjoint=True)
            reopened = ProjectWorkspace(root, room, kind='text', create=False, allow_new_text_files=True)
            self.assertEqual([item['path'] for item in reopened.snapshot()['files']], ['bean.svg', 'guide.md'])
            self.assertEqual(reopened.snapshot(1)['files'][0]['content'], '<svg/>')
            # A source-project workspace cannot accidentally reopen or widen it.
            with self.assertRaises(ProjectWorkspaceError):
                ProjectWorkspace(root, room, kind='text', create=False)

    def test_existing_text_project_still_requires_explicit_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = ProjectWorkspace(Path(directory).resolve() / 'files', str(uuid4()), kind='text')
            with self.assertRaises(ProjectWorkspaceError) as raised:
                workspace.apply('codex', 'new', 0, [
                    {'path': 'guide.md', 'content': 'hello', 'previous_sha256': None}])
            self.assertEqual(raised.exception.code, 'project_not_seeded')
