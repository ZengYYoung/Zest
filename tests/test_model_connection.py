"""Offline contract tests; the real connection probe is recorded separately."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zest.files import FileReadError
from zest.models import ModelSettings
from zest.model_connection import ModelConnectionStore


class ModelConnectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zest-model-check-')
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)
        self.store = ModelConnectionStore(self.state_dir)
        self.settings = ModelSettings('deepseek', 'connection-fixture', 'https://fixture.invalid', 'private-fixture-key')
        self.config = patch('zest.model_connection.ModelSettings.from_env', return_value=self.settings)
        self.config.start()
        self.addCleanup(self.config.stop)

    def fixture(self):
        return {'text': json.dumps({'status': 'answered', 'answer': '12 [E1]', 'citations': ['E1'], 'unknowns': []}),
                'structured_status': 'valid', 'usage': {'total_tokens': 42}, 'reported_model': 'fixture'}

    def test_preparation_freezes_actual_request_without_transmission_or_key(self):
        record = self.store.prepare()
        self.assertEqual(record['status'], 'awaiting_approval')
        self.assertEqual(record['envelope']['policy']['max_calls'], 1)
        self.assertFalse(record['envelope']['policy']['project_data_sent'])
        encoded = json.dumps(record)
        self.assertNotIn(self.settings.api_key, encoded)
        self.assertNotIn(str(Path.cwd()), record['envelope']['request']['body_text'])
        self.assertIn('connection-example.py', record['envelope']['request']['body_text'])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_wrong_hash_rejection_and_changed_target_never_call_model(self):
        record = self.store.prepare()
        with patch('zest.model_connection.DeepSeekModel.complete') as call:
            with self.assertRaises(FileReadError):
                self.store.decide(record['id'], approved=True, sha256='0' * 64)
            self.assertEqual(self.store.decide(record['id'], approved=False)['status'], 'rejected')
            call.assert_not_called()
        record = self.store.prepare()
        changed = ModelSettings('deepseek', 'changed', 'https://fixture.invalid', 'private-fixture-key')
        with patch('zest.model_connection.ModelSettings.from_env', return_value=changed):
            with self.assertRaises(FileReadError) as caught:
                self.store.decide(record['id'], approved=True, sha256=record['approval_sha256'])
        self.assertEqual(caught.exception.code, 'model_check_changed')
        self.assertEqual(self.store.show(record['id'])['status'], 'awaiting_approval')

    def test_approved_single_call_saves_receipt_and_duplicate_decision_never_replays(self):
        record = self.store.prepare()
        # Compilation must retain the real adapter; only replace the actual provider instance.
        from zest.models import DeepSeekModel
        original = DeepSeekModel.complete
        calls = []
        def complete(instance, payload, **kw):
            if instance.http_client is not None:
                return original(instance, payload, **kw)
            calls.append(kw)
            return self.fixture()
        with patch.object(DeepSeekModel, 'complete', complete):
            result = self.store.decide(record['id'], approved=True, sha256=record['approval_sha256'])
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['result']['usage']['total_tokens'], 42)
            with self.assertRaises(FileReadError):
                self.store.decide(record['id'], approved=True, sha256=record['approval_sha256'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['expected_request'], record['envelope']['request'])

    def test_unknown_execution_and_interrupted_process_are_never_requeued(self):
        record = self.store.prepare()
        from zest.models import DeepSeekModel
        original = DeepSeekModel.complete
        def complete(instance, payload, **kw):
            if instance.http_client is not None:
                return original(instance, payload, **kw)
            raise FileReadError('model_request_failed', 'offline unknown response')
        with patch.object(DeepSeekModel, 'complete', complete):
            self.assertEqual(self.store.decide(record['id'], approved=True, sha256=record['approval_sha256'])['status'], 'unknown')
            with self.assertRaises(FileReadError):
                self.store.decide(record['id'], approved=True, sha256=record['approval_sha256'])
        other = self.store.prepare()
        with self.store.open(write=True) as conn, conn:
            conn.execute("UPDATE checks SET status='running' WHERE id=?", (other['id'],))
        self.assertEqual(self.store.show(other['id'])['status'], 'unknown')

    def test_invalid_known_response_preserves_receipt_and_auth_failure_is_known(self):
        from zest.models import DeepSeekModel
        original = DeepSeekModel.complete
        record = self.store.prepare()
        def complete(instance, payload, **kw):
            if instance.http_client is not None:
                return original(instance, payload, **kw)
            return {'text': '', 'structured_status': 'invalid', 'raw_response': {'offline_fixture': True}}
        with patch.object(DeepSeekModel, 'complete', complete):
            result = self.store.decide(record['id'], approved=True, sha256=record['approval_sha256'])
        self.assertEqual(result['status'], 'failed')
        self.assertIsNotNone(result['result']['receipt'])

    def test_status_and_command_endpoints_need_no_project_and_get_never_calls_model(self):
        from fastapi.testclient import TestClient
        from zest.web import create_app
        from zest.web_operations import WebOperations
        client = TestClient(create_app(self.state_dir))
        with patch('zest.model_connection.DeepSeekModel.complete', side_effect=AssertionError('no model calls')):
            state = client.get('/api/model').json()['data']
            self.assertTrue(state['configuration']['configured'])
            self.assertFalse(state['connected'])
            self.assertIsNone(state['latest_check'])
        from uuid import uuid4
        with patch.object(WebOperations, 'launch'):
            op = client.post('/api/operations', headers={'Origin': 'http://127.0.0.1:5173'}, json={
                'request_id': str(uuid4()), 'action': 'model.prepare', 'parameters': {}}).json()['data']
        WebOperations(self.state_dir).run(op['id'])
        result = WebOperations(self.state_dir).show(op['id'])
        self.assertEqual(result['status'], 'completed')
        record = result['result']
        self.assertEqual(client.get('/api/model/checks/' + record['id']).status_code, 200)
        self.assertEqual(client.get('/api/model').json()['data']['latest_check']['id'], record['id'])
