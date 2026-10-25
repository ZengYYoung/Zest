"""Recoverable catalog removal keeps project identities and execution routing."""
import importlib.util
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.test_projects import ProjectsTest
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.storage import ProjectRegistry


class ProjectRemovalTest(unittest.TestCase):
    setUp = ProjectsTest.setUp
    git = ProjectsTest.git
    commit = ProjectsTest.commit

    def register(self, workspace=False):
        (self.repo / 'app.py').write_text('print("preserve source")\n')
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=workspace)

    def test_legacy_removal_is_idempotent_and_reimport_restores_same_identity(self):
        registry = ProjectRegistry(self.state_dir)
        project = self.register()
        original = (self.repo / 'app.py').read_bytes()
        result = registry.remove_project(project['id'])
        self.assertEqual(result, {'project_id': project['id'], 'removed': True, 'files_deleted': False, 'records_preserved': True})
        self.assertEqual(registry.list_projects(), [])
        self.assertEqual(registry.list_projects_readonly(), [])
        removed = registry.get_project_readonly(project['id'])
        self.assertTrue(removed['removed_at'])
        registry.remove_project(project['id'])
        self.assertEqual(registry.get_project_readonly(project['id']), removed)
        self.assertEqual((self.repo / 'app.py').read_bytes(), original)
        restored = registry.register(inspect_repository(self.repo))
        self.assertEqual(restored['id'], project['id'])
        self.assertEqual(restored['created_at'], project['created_at'])
        self.assertNotIn('removed_at', restored)
        self.assertEqual(len(registry.list_projects_readonly()), 1)

    def test_workspace_removal_preserves_files_receipts_and_reattachment(self):
        from zest.conversations import ConversationStore
        from zest.resources import record_state
        registry = ProjectRegistry(self.state_dir)
        project = self.register(workspace=True)
        conversation = ConversationStore(self.state_dir).create(project)
        doc = self.repo / '.zest' / 'docs' / 'manual.md'
        doc.parent.mkdir(exist_ok=True)
        doc.write_text('# Keep my edits\n')
        folder = self.repo / '.zest'
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}
        registry.remove_project(project['id'])
        self.assertEqual(registry.list_projects(), [])
        self.assertEqual(len(registry.list_projects_readonly(include_removed=True)), 1)
        self.assertEqual(record_state(self.state_dir, 'conversation', conversation['id']), (folder / 'state').resolve())
        self.assertEqual(ConversationStore(self.state_dir).show(conversation['id'])['id'], conversation['id'])
        self.assertEqual({str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}, before)
        restored = registry.register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(restored, project)
        self.assertEqual(registry.list_projects_readonly(), [project])
        self.assertEqual({str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()}, before)

    def test_unknown_removal_does_not_create_catalog_or_touch_repository(self):
        with self.assertRaises(FileReadError) as failure:
            ProjectRegistry(self.state_dir).remove_project(str(uuid4()))
        self.assertEqual(failure.exception.code, 'project_not_found')
        self.assertFalse(self.state_dir.exists())

    def test_readonly_old_schema_needs_no_migration(self):
        project = self.register()
        registry = ProjectRegistry(self.state_dir)
        with closing(sqlite3.connect(registry.db_path)) as conn, conn:
            conn.execute('ALTER TABLE projects DROP COLUMN removed_at')
        previous = registry.db_path.read_bytes()
        self.assertEqual(registry.list_projects_readonly(), [project])
        self.assertEqual(registry.db_path.read_bytes(), previous)
        registry.remove_project(project['id'])
        self.assertEqual(registry.list_projects(), [])

    @unittest.skipUnless(importlib.util.find_spec('fastapi'), 'Web extras required')
    def test_web_removal_uses_original_receipt_and_rejects_file_deletion_parameters(self):
        from fastapi.testclient import TestClient
        from zest.web import create_app
        from zest.web_operations import WebOperations
        project = self.register(workspace=True)
        client = TestClient(create_app(self.state_dir))
        headers = {'Origin': 'http://127.0.0.1:5173'}
        command = {'request_id': str(uuid4()), 'action': 'project.remove', 'parameters': {'project_id': project['id']}}
        with patch.object(WebOperations, 'launch'):
            self.assertEqual(client.post('/api/operations', json=command).status_code, 403)
            response = client.post('/api/operations', headers=headers, json=command)
            self.assertEqual(response.status_code, 202, response.text)
            operations = WebOperations(self.state_dir)
            operations.run(command['request_id'])
            receipt = operations.show(command['request_id'])
            self.assertEqual(receipt['status'], 'completed', receipt)
            self.assertEqual(client.get('/api/projects').json()['data'], [])
            self.assertTrue((self.repo / 'app.py').exists())
            self.assertTrue((self.repo / '.zest/workspace.json').exists())
            replay = client.post('/api/operations', headers=headers, json=command).json()['data']
            self.assertEqual(replay, receipt)
            restored = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
            operations.run(command['request_id'])
            self.assertEqual(client.get('/api/projects').json()['data'], [restored])
            bad = {**command, 'request_id': str(uuid4()), 'parameters': {**command['parameters'], 'delete_files': True}}
            self.assertEqual(client.post('/api/operations', headers=headers, json=bad).status_code, 400)
