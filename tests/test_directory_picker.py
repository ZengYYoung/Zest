"""Native selection is explicit, cancellable and separate from project import."""
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from zest.directory_picker import choose_project_directory, picker_lock
from zest.files import FileReadError
from zest.storage import ProjectRegistry
from zest.web import create_app
from zest.web_operations import WebOperations


class DirectoryPickerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zest-picker-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.folder = self.root / '项目 空格 $(literal)'
        self.folder.mkdir()
        platform = patch('zest.directory_picker.sys.platform', 'darwin')
        platform.start(); self.addCleanup(platform.stop)

    def test_selection_preserves_path_and_never_interpolates_it_into_a_script(self):
        with patch('zest.directory_picker.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=str(self.folder) + '/\n')) as run:
            result = choose_project_directory(self.state)
        self.assertEqual(result, {'cancelled': False, 'path': str(self.folder)})
        self.assertNotIn(str(self.folder), str(run.call_args))
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertEqual(ProjectRegistry(self.state).list_projects(), [])
        self.assertFalse((self.folder / '.zest').exists())

    def test_cancel_is_successful_and_does_not_choose_a_default_directory(self):
        with patch('zest.directory_picker.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='\n')):
            self.assertEqual(choose_project_directory(self.state), {'cancelled': True, 'path': None})

    def test_failure_timeout_and_unsupported_platform_have_manual_fallback(self):
        for side_effect, code in [(OSError('private system detail'), 'directory_picker_unavailable'),
                                  (subprocess.TimeoutExpired('fixture', 300), 'directory_picker_timed_out')]:
            with patch('zest.directory_picker.subprocess.run', side_effect=side_effect):
                with self.assertRaises(FileReadError) as raised:
                    choose_project_directory(self.state)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn('private system detail', str(raised.exception))
        with patch('zest.directory_picker.sys.platform', 'linux'), patch('zest.directory_picker.subprocess.run') as run:
            with self.assertRaises(FileReadError) as raised:
                choose_project_directory(self.state)
            self.assertEqual(raised.exception.code, 'directory_picker_unsupported')
            run.assert_not_called()

    def test_only_one_system_picker_can_be_active(self):
        with picker_lock(self.state), patch('zest.directory_picker.subprocess.run') as run:
            with self.assertRaises(FileReadError) as raised:
                choose_project_directory(self.state)
            self.assertEqual(raised.exception.code, 'directory_picker_busy')
            run.assert_not_called()

    def test_web_picker_requires_local_origin_and_reuses_request_identity(self):
        client = TestClient(create_app(self.state))
        body = {'request_id': str(uuid4()), 'action': 'project.choose-directory', 'parameters': {}}
        with patch.object(WebOperations, 'launch'), patch('zest.directory_picker.choose_project_directory', return_value={'path': str(self.folder), 'cancelled': False}) as choose:
            self.assertEqual(client.post('/api/operations', json=body).status_code, 403)
            self.assertEqual(client.get('/api/projects').status_code, 200)
            choose.assert_not_called()
            headers = {'Origin': 'http://127.0.0.1:5173'}
            response = client.post('/api/operations', headers=headers, json=body)
            self.assertEqual(response.status_code, 202)
            operations = WebOperations(self.state)
            operations.run(body['request_id'])
            self.assertEqual(operations.show(body['request_id'])['result']['path'], str(self.folder))
            client.post('/api/operations', headers=headers, json=body)
            operations.run(body['request_id'])
            self.assertEqual(choose.call_count, 1)
        self.assertEqual(ProjectRegistry(self.state).list_projects(), [])
