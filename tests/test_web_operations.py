"""Exercise the Web workflow with temporary Git projects and explicit offline models."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.storage import ProjectRegistry
from zest.web_operations import WebOperations


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("langgraph"), "Web/AI extras required")
class WebOperationsTest(unittest.TestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def setUp(self):
        from fastapi.testclient import TestClient
        from zest.web import create_app
        self.setUpBase()
        (self.repo / 'app.py').write_text('def add(a, b):\n    return a - b\n')
        (self.repo / 'README.md').write_text('# Offline Web fixture\n')
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.operations = WebOperations(self.state_dir)
        self.client = TestClient(create_app(self.state_dir))
        self.headers = {'Origin': 'http://127.0.0.1:5173'}

    def submit(self, action, parameters, request_id=None):
        with patch.object(WebOperations, 'launch'):
            response = self.client.post('/api/operations', headers=self.headers, json={
                'request_id': request_id or str(uuid4()), 'action': action, 'parameters': parameters})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['data']

    def finish(self, operation):
        self.operations.run(operation['id'])
        record = self.client.get('/api/operations/' + operation['id']).json()['data']
        self.assertEqual(record['status'], 'completed', record)
        return record['result']

    def test_shared_document_import_runs_as_a_durable_operation_without_a_model(self):
        from zest.models import DeepSeekModel
        with patch.object(DeepSeekModel, 'complete', side_effect=AssertionError('no model calls')):
            project = self.finish(self.submit('project.import', {'path': str(self.repo)}))
            operation = self.submit('project.documents.init', {'project_id': project['id']})
            result = self.finish(operation)
            self.assertIn('.zest/docs/index.md', project['initialization']['migration']['created'])
            self.assertEqual(result['created'], [])
            self.assertFalse(result['committed'])
            index = self.repo / '.zest/docs/index.md'
            before = index.stat().st_mtime_ns
            self.assertEqual(self.finish(operation), result)
            self.assertEqual(before, index.stat().st_mtime_ns)
            url = '/api/projects/' + project['id'] + '/documents'
            self.assertEqual(self.client.get(url).json()['data']['documents'], [])
            self.commit('review portable docs')
            self.assertTrue(self.client.get(url).json()['data']['documents'])

    def test_conversation_rounds_use_original_operation_and_task_approval_receipts(self):
        from tests.test_tasks import FixtureModel
        model = FixtureModel()
        with patch('zest.tasks.ModelSettings.from_env', return_value=model.settings), patch('zest.tasks.DeepSeekModel', return_value=model):
            conversation = self.finish(self.submit('conversation.create', {'project_id': self.project['id']}))
            pending = self.submit('conversation.send', {'id': conversation['id'], 'question': '项目语言？', 'message_id': str(uuid4())})
            turn = self.finish(pending)
            self.assertEqual(turn['status'], 'completed')
            task = turn['task']
            self.assertTrue(any(e['step'] == 'approved' for e in task['events']))
            self.assertEqual(len(model.calls), 1)
            again = self.finish(pending)
            self.assertEqual(again['task_id'], turn['task_id'])
            self.assertEqual(len(model.calls), 1)
            next_turn = self.finish(self.submit('conversation.send', {'id': conversation['id'], 'question': '它怎么用？', 'message_id': str(uuid4())}))
            self.assertEqual(next_turn['task']['context']['conversation_context']['history'][0]['user'], '项目语言？')
            response = self.client.get('/api/conversations/' + conversation['id'])
            self.assertEqual(len(response.json()['data']['turns']), 2)
            self.assertEqual(len(model.calls), 2)

    def test_origin_host_validation_and_schema_precede_dispatch(self):
        body = {'request_id': str(uuid4()), 'action': 'project.import', 'parameters': {'path': str(self.repo)}}
        for origin in (None, 'null', 'https://evil.example', 'http://127.0.0.1:9999'):
            headers = {} if origin is None else {'Origin': origin}
            response = self.client.post('/api/operations', headers=headers, json=body)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/api/operations', headers={**self.headers, 'Host': 'evil.example'}, json=body).status_code, 400)
        for parameters in ({}, {'path': str(self.repo), 'shell': 'bad'}, {'path': 1}):
            response = self.client.post('/api/operations', headers=self.headers, json={**body, 'parameters': parameters})
            self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/operations', headers=self.headers, json={**body, 'action': 'shell.execute'})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.operations.path.exists())

    def test_idempotent_creation_and_conflict_survive_new_server(self):
        request_id = str(uuid4())
        params = {'project_id': self.project['id'], 'title': 'Web fixture', 'body': 'A decision'}
        op = self.submit('knowledge.add', params, request_id)
        note = self.finish(op)
        replay = self.submit('knowledge.add', params, request_id)
        self.assertEqual(replay['result']['id'], note['id'])
        with patch('zest.web_commands.execute', side_effect=AssertionError('never replay')):
            WebOperations(self.state_dir).run(request_id)
        response = self.client.post('/api/operations', headers=self.headers, json={
            'request_id': request_id, 'action': 'knowledge.add', 'parameters': {**params, 'title': 'changed'}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], 'request_identity_conflict')
        self.assertEqual(len(self.client.get('/api/projects/' + self.project['id'] + '/knowledge').json()['data']), 1)

    def test_two_concurrent_claims_dispatch_once_and_crash_is_not_replayed(self):
        op = self.submit('index.build', {'project_id': self.project['id']})
        with patch('zest.web_commands.execute', return_value={'fixture': True}) as execute:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: WebOperations(self.state_dir).run(op['id']), range(2)))
            self.assertEqual(execute.call_count, 1)
        other = self.submit('index.build', {'project_id': self.project['id']})
        with self.operations.open() as conn, conn:
            conn.execute("UPDATE operations SET status='running' WHERE id=?", (other['id'],))
        self.assertEqual(self.operations.show(other['id'])['status'], 'unknown')
        with patch('zest.web_commands.execute', side_effect=AssertionError('no replay')):
            self.operations.run(other['id'])
            self.operations.launch(other['id'])
        self.assertEqual(self.operations.show(other['id'])['status'], 'unknown')

    def test_project_import_and_creation_share_cli_resource_state(self):
        op = self.submit('project.import', {'path': str(self.repo)})
        project = self.finish(op)
        self.assertEqual(project['id'], self.project['id'])
        self.assertTrue((self.repo / '.zest' / 'AGENTS.md').is_file())
        new_path = self.root / 'web-created'
        created = self.finish(self.submit('project.init', {'path': str(new_path), 'name': 'Web demo',
            'goal': 'Offline test', 'author_name': 'Fixture', 'author_email': 'fixture@example.test'}))
        self.assertEqual(created['project']['mode'], 'development')
        self.assertEqual(created['goal_note']['status'], 'draft')
        self.assertEqual(len(ProjectRegistry(self.state_dir).list_projects()), 2)
        self.assertEqual(self.client.get('/api/projects/' + project['id'] + '/context').status_code, 200)

    def test_question_preview_hash_reject_and_approval_are_separate(self):
        from tests.test_cognition import OfflineCognitionModel
        model = OfflineCognitionModel()
        with patch('zest.models.ModelSettings.from_env', return_value=model.settings), patch('zest.tasks.DeepSeekModel', return_value=model):
            task = self.finish(self.submit('task.create', {'project_id': self.project['id'], 'question': 'Explain add', 'files': ['app.py']}))
            preview = self.client.get('/api/tasks/' + task['task_id'] + '/preview').json()['data']
            self.assertEqual(preview['target'], task['target'])
            self.assertIn('a - b', json.dumps(preview['payload']))
            self.assertEqual(model.calls, [])
            bad = self.submit('task.decide', {'id': task['task_id'], 'approved': True, 'sha256': '0' * 64})
            self.operations.run(bad['id'])
            self.assertEqual(self.operations.show(bad['id'])['status'], 'failed')
            self.assertEqual(model.calls, [])
            answer = self.finish(self.submit('task.decide', {'id': task['task_id'], 'approved': True, 'sha256': preview['payload_sha256']}))
            self.assertEqual(answer['status'], 'completed')
            self.assertEqual(len(model.calls), 1)
            rejected = self.finish(self.submit('task.create', {'project_id': self.project['id'], 'question': 'Another?'}))
            self.assertEqual(self.finish(self.submit('task.decide', {'id': rejected['task_id'], 'approved': False}))['status'], 'rejected')
            self.assertEqual(len(model.calls), 1)

    def test_repair_suggestion_candidate_verification_and_release_plans(self):
        from tests.test_repair_agent import OfflineRepairModel
        from tests.test_verification import OfflineVerificationEngine
        model = OfflineRepairModel()
        before = self.git('rev-parse', 'HEAD')
        with patch('zest.models.ModelSettings.from_env', return_value=model.settings), patch('zest.tasks.DeepSeekModel', return_value=model):
            task = self.finish(self.submit('task.create', {'project_id': self.project['id'], 'question': 'Fix add', 'files': ['app.py'], 'purpose': 'repair_proposal'}))
            task = self.finish(self.submit('task.decide', {'id': task['task_id'], 'approved': True, 'sha256': task['payload_sha256']}))
        repair = self.client.get('/api/repairs/' + task['result']['repair_id']).json()['data']
        self.assertEqual(repair['status'], 'awaiting_approval')
        candidate = self.finish(self.submit('repair.decide', {'id': repair['id'], 'approved': True, 'sha256': repair['plan_sha256']}))
        self.assertEqual(candidate['status'], 'candidate_ready')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        engine = OfflineVerificationEngine()
        with patch('zest.verification.DockerEngine', return_value=engine):
            verification = self.finish(self.submit('verification.create', {'id': repair['id'], 'image': 'offline-fixture'}))
            self.assertEqual(engine.calls, [])
            verification = self.finish(self.submit('verification.decide', {'id': verification['id'], 'approved': True, 'sha256': verification['plan_sha256']}))
            self.assertEqual(verification['status'], 'passed')
        # Fixture evidence is deliberately insufficient for a real source promotion.
        promote = self.submit('release.promote', {'id': verification['id']})
        self.operations.run(promote['id'])
        self.assertEqual(self.operations.show(promote['id'])['status'], 'failed')
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)

    def test_cognition_preview_context_and_background_start_use_existing_services(self):
        from tests.test_cognition import OfflineCognitionModel
        from zest.cognition import worker
        model = OfflineCognitionModel()
        with patch('zest.models.ModelSettings.from_env', return_value=model.settings):
            run = self.finish(self.submit('cognition.create', {'project_id': self.project['id']}))
        preview = self.client.get('/api/cognition/' + run['id'] + '/preview').json()['data']
        self.assertIn('a - b', json.dumps(preview['envelope']))
        self.assertEqual(self.client.get('/api/cognition/' + run['id'] + '/context-requests').json()['data'], [])
        self.assertEqual(model.calls, [])
        bad = self.submit('cognition.decide', {'id': run['id'], 'approved': True, 'sha256': '0' * 64})
        with patch('zest.cognition.start_worker', return_value={'status': 'offline-fixture'}) as launch:
            self.operations.run(bad['id'])
            launch.assert_not_called()
            good = self.finish(self.submit('cognition.decide', {'id': run['id'], 'approved': True, 'sha256': preview['approval_sha256']}))
            self.assertEqual(good['status'], 'queued')
            launch.assert_called_once()
        worker(self.state_dir, once=True, model=model)
        self.assertEqual(self.client.get('/api/cognition/' + run['id']).json()['data']['status'], 'completed')
        self.assertEqual(self.client.get('/api/cognition/' + run['id'] + '/dossier').status_code, 200)
        self.assertGreater(len(self.client.get('/api/cognition/' + run['id'] + '/calls').json()['data']['calls']), 0)

    def test_knowledge_lifecycle_and_real_detached_local_worker(self):
        note = self.finish(self.submit('knowledge.add', {'project_id': self.project['id'], 'title': 'Constraint', 'body': 'Local only'}))
        self.assertEqual(self.finish(self.submit('knowledge.confirm', {'id': note['id']}))['status'], 'confirmed')
        self.assertEqual(self.finish(self.submit('knowledge.invalidate', {'id': note['id'], 'reason': 'Old'}))['status'], 'invalidated')
        op = self.submit('index.build', {'project_id': self.project['id']})
        self.operations.launch(op['id'])
        for _ in range(100):
            result = self.operations.show(op['id'])
            if result['status'] not in ('queued', 'running'):
                break
            time.sleep(0.05)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(self.operations.path.stat().st_mode & 0o777, 0o600)

    def test_body_limit_and_lightweight_operation_history(self):
        response = self.client.post('/api/operations', headers=self.headers,
                                    content=b'x' * (1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        op = self.submit('knowledge.add', {'project_id': self.project['id'], 'title': 'History', 'body': 'private body'})
        self.finish(op)
        history = self.client.get('/api/operations').json()['data']
        self.assertEqual(history[0]['id'], op['id'])
        self.assertIsNone(history[0]['parameters'])
        self.assertIsNone(history[0]['result'])
        self.assertNotIn('private body', json.dumps(history))

    def test_independent_evaluation_and_resolution_are_exposed_without_extra_calls(self):
        from fastapi.testclient import TestClient
        from zest.web import create_app
        from tests import test_evaluation as evaluation_fixtures
        from tests import test_cognition_resolution as resolution_fixtures
        evaluator = evaluation_fixtures.EvaluationTest()
        evaluator.setUp()
        self.addCleanup(evaluator.doCleanups)
        client = TestClient(create_app(evaluator.state_dir))
        operations = WebOperations(evaluator.state_dir)

        def dispatch(action, parameters):
            with patch.object(WebOperations, 'launch'):
                response = client.post('/api/operations', headers=self.headers, json={
                    'request_id': str(uuid4()), 'action': action, 'parameters': parameters})
            self.assertEqual(response.status_code, 202, response.text)
            identity = response.json()['data']['id']
            operations.run(identity)
            operation = operations.show(identity)
            self.assertEqual(operation['status'], 'completed', operation)
            return operation['result']

        from zest.evaluation import EvaluationStore
        with patch('zest.models.ModelSettings.from_env', return_value=evaluator.scorer.settings), patch('zest.evaluation.EvaluationStore', side_effect=lambda directory, **kw: EvaluationStore(directory, model=evaluator.scorer)):
            record = dispatch('evaluation.create', {'id': evaluator.source['id'], 'criteria': evaluator.criteria})
            self.assertEqual(evaluator.scorer.calls, [])
            preview = client.get('/api/evaluations/' + record['id'] + '/preview').json()['data']
            self.assertIn('envelope', preview)
            project_id = evaluator.source['project_id']
            listing = client.get('/api/projects/' + project_id + '/evaluations').json()['data']
            self.assertEqual(listing[0]['id'], record['id'])
            report = dispatch('evaluation.decide', {'id': record['id'], 'approved': True, 'sha256': record['approval_sha256']})
            self.assertEqual(report['workflow_status'], 'completed')
            self.assertGreater(len(evaluator.scorer.calls), 0)
            self.assertEqual(client.get('/api/evaluations/' + record['id'] + '/report').status_code, 200)

        resolver = resolution_fixtures.ResolutionTest()
        resolver.setUp()
        self.addCleanup(resolver.doCleanups)
        followup = resolver.followup()
        client = TestClient(create_app(resolver.state_dir))
        operations = WebOperations(resolver.state_dir)
        from zest.cognition_resolution import ResolutionStore
        with patch('zest.models.ModelSettings.from_env', return_value=resolver.scorer.settings), patch('zest.cognition_resolution.ResolutionStore', side_effect=lambda directory, **kw: ResolutionStore(directory, model=resolver.scorer)):
            record = dispatch('resolution.create', {'id': followup['id']})
            self.assertEqual(resolver.scorer.calls, [])
            self.assertIn('envelope', client.get('/api/resolutions/' + record['id'] + '/preview').json()['data'])
            listing = client.get('/api/projects/' + record['project_id'] + '/resolutions').json()['data']
            self.assertEqual(listing[0]['id'], record['id'])
            proposed = dispatch('resolution.decide', {'id': record['id'], 'approved': True, 'sha256': record['approval_sha256']})
            self.assertEqual(proposed['status'], 'proposed')
            adopted = dispatch('resolution.adopt', {'id': proposed['id'], 'sha256': proposed['proposal_sha256']})
            self.assertTrue(adopted['adoption'])
            self.assertEqual(len(resolver.scorer.calls), 1)
