"""Event checks, durable automatic refresh and no idle Git/model polling."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from tests import test_projects as fixtures
from tests.test_cognition import OfflineCognitionModel
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.initialization import InitializationStore
from zest.maintenance import MaintenanceStore
from zest.cognition_store import CognitionStore
from zest.models import ModelSettings
from zest.resources import project_states
from zest.storage import ProjectRegistry
from zest.web import create_app


class MaintenanceTest(unittest.TestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def setUp(self):
        self.setUpBase()
        (self.repo / 'README.md').write_text('# Offline addition fixture\n')
        (self.repo / 'app.py').write_text('def add(a, b):\n    return a + b\n')
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        self.model = OfflineCognitionModel()
        configuration = patch('zest.models.ModelSettings.from_env', return_value=self.model.settings)
        configuration.start(); self.addCleanup(configuration.stop)
        self.initial = InitializationStore(self.state_dir)
        prepared = self.initial.prepare(self.project['id'])
        with patch.object(InitializationStore, 'launch'):
            self.initial.decide(self.project['id'], approved=True, sha256=prepared['research']['approval_sha256'])
        self.baseline = self.initial.run(self.project['id'], model=self.model)
        self.store = MaintenanceStore(self.state_dir)
        self.client = TestClient(create_app(self.state_dir))
        self.headers = {'Origin': 'http://127.0.0.1:5173'}

    def changed(self):
        (self.repo / 'app.py').write_text('def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n')
        return self.commit('add multiplication')

    def queue(self):
        self.store.enable(self.project['id'])
        commit = self.changed()
        with patch.object(MaintenanceStore, 'launch'):
            result = self.store.check(self.project['id'], reason='manual')
        return commit, result

    def test_reconfirm_changed_model_from_web_command_queues_existing_changes_once(self):
        from zest.understanding_approval import UnderstandingApproval
        from zest.web_commands import execute
        self.store.enable(self.project['id'])
        target_commit = self.changed()
        before = len(self.model.calls)
        settings = replace(self.model.settings, name='updated-fixture-model')
        with patch('zest.models.ModelSettings.from_env', return_value=settings), patch.object(MaintenanceStore, 'launch'):
            detected = self.store.check(self.project['id'], reason='manual')
            self.assertEqual(detected['status'], 'permission_required')
            self.assertNotEqual(detected['knowledge_commit'], target_commit)
            parameters = {'project_id': self.project['id'], 'enabled': True}
            enabled = execute(self.state_dir, 'understanding.configure', parameters)
            self.assertTrue(enabled['active'])
            queued = self.store.show(self.project['id'])
            self.assertEqual(queued['status'], 'queued')
            self.assertEqual(queued['target_commit'], target_commit)
            self.assertEqual(queued['knowledge_commit'], detected['knowledge_commit'])
            self.assertTrue(UnderstandingApproval(self.state_dir).show(self.project['id'])['active'])
            execute(self.state_dir, 'understanding.configure', parameters)
            self.assertEqual(self.store.show(self.project['id'])['active_run_id'], queued['active_run_id'])
        self.assertEqual(len(self.model.calls), before)

    def test_each_allowed_event_checks_locally_without_model_when_unchanged(self):
        before = len(self.model.calls)
        with patch.object(MaintenanceStore, 'launch') as launch:
            for reason in ('open', 'switch', 'task', 'manual'):
                result = self.store.check(self.project['id'], reason=reason)
                self.assertEqual(result['status'], 'current')
                self.assertEqual(result['last_check']['reason'], reason)
                receipt = result['last_check']['summary']
                self.assertEqual(receipt['file_count'], 0)
                self.assertEqual(receipt['base_commit'], receipt['target_commit'])
                self.assertEqual(receipt['commit_subjects'], [])
            launch.assert_not_called()
        self.assertEqual(len(self.model.calls), before)

    def test_uncommitted_changes_do_not_start_model_refresh(self):
        (self.repo / 'app.py').write_text('unfinished local work\n')
        with patch.object(MaintenanceStore, 'launch') as launch:
            result = self.store.check(self.project['id'], reason='manual')
        self.assertTrue(result['last_check']['dirty'])
        self.assertEqual(result['last_check']['summary']['file_count'], 0)
        self.assertEqual(result['status'], 'current')
        launch.assert_not_called()

    def test_new_commit_freezes_one_update_and_repeated_events_do_not_duplicate(self):
        commit, first = self.queue()
        self.assertEqual(first['status'], 'queued', first)
        self.assertEqual(first['target_commit'], commit)
        summary = first['last_check']['summary']
        self.assertEqual(summary['base_commit'], self.baseline['commit'])
        self.assertEqual(summary['target_commit'], commit)
        self.assertEqual(summary['counts']['modified'], 1)
        self.assertEqual(summary['files'], [{'path': 'app.py', 'change': 'modified'}])
        self.assertEqual(summary['commit_subjects'], ['add multiplication'])
        with patch.object(MaintenanceStore, 'launch') as launch:
            second = self.store.check(self.project['id'], reason='switch')
            launch.assert_not_called()
        self.assertEqual(first['active_run_id'], second['active_run_id'])
        self.assertNotIn(self.model.settings.api_key, json.dumps(first))

    def test_automatic_delta_updates_documents_index_and_keeps_initialization_identity(self):
        commit, prepared = self.queue()
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['knowledge_commit'], commit)
        self.assertEqual(result['automatic_decision']['scope_sha256'], result['research']['approval_sha256'])
        self.assertEqual(result['summary']['documents'], 7)
        self.assertIn(commit, (self.repo / '.zest/docs/initialization/api.md').read_text())
        self.assertEqual(self.initial.show(self.project['id'])['id'], self.baseline['id'])
        calls = len(self.model.calls)
        checked = self.store.check(self.project['id'], reason='open')
        self.assertEqual(checked['status'], 'current')
        self.assertEqual(checked['last_check']['summary']['file_count'], 0)
        self.assertEqual(checked['last_check']['summary']['files'], [])
        self.assertEqual(checked['last_check']['summary']['commit_subjects'], [])
        self.assertEqual(len(self.model.calls), calls)

    def test_generated_document_commit_does_not_loop_back_into_model_research(self):
        self.store.enable(self.project['id'])
        self.commit('share generated knowledge')
        before = len(self.model.calls)
        with patch.object(MaintenanceStore, 'launch') as launch:
            result = self.store.check(self.project['id'], reason='open')
        self.assertEqual(result['status'], 'generated_only', result)
        self.assertEqual(result['last_check']['summary']['file_count'], 0)
        self.assertGreater(result['last_check']['summary']['excluded_count'], 0)
        launch.assert_not_called()
        self.assertEqual(len(self.model.calls), before)

    def test_human_changes_in_shared_docs_are_meaningful(self):
        self.store.enable(self.project['id'])
        file = self.repo / '.zest/docs/initialization/api.md'
        file.write_text(file.read_text() + '\n人工确认：此处添加团队接口约定。\n')
        self.commit('team contract update')
        with patch.object(MaintenanceStore, 'launch'):
            result = self.store.check(self.project['id'], reason='open')
        self.assertEqual(result['status'], 'queued')
        self.assertIn('.zest/docs/initialization/api.md', [c['path'] for c in result['changes']])

    def test_existing_users_need_one_explicit_enable_before_new_model_calls(self):
        self.changed()
        before = len(self.model.calls)
        with patch.object(MaintenanceStore, 'launch') as launch:
            result = self.store.check(self.project['id'], reason='open')
        self.assertEqual(result['status'], 'permission_required')
        launch.assert_not_called()
        self.assertEqual(len(self.model.calls), before)

    def test_changed_model_target_pauses_instead_of_using_old_grant(self):
        self.store.enable(self.project['id']); self.changed()
        with patch('zest.models.ModelSettings.from_env', return_value=replace(self.model.settings, name='different-model')):
            with patch.object(MaintenanceStore, 'launch') as launch:
                result = self.store.check(self.project['id'], reason='manual')
        self.assertEqual(result['status'], 'permission_required')
        launch.assert_not_called()

    def test_progress_reads_do_not_inspect_git_write_or_call_models(self):
        self.queue()
        directory = project_states(self.state_dir, self.project['id'])[0]
        before = (directory / 'maintenance.json').read_bytes()
        with patch('zest.maintenance.inspect_repository', side_effect=AssertionError('no Git polling')):
            with patch('zest.initialization.inspect_repository', side_effect=AssertionError('no Git polling')):
                for route in ('maintenance', 'initialization'):
                    self.assertEqual(self.client.get(f"/api/projects/{self.project['id']}/{route}").status_code, 200)
        self.assertEqual((directory / 'maintenance.json').read_bytes(), before)

    def test_post_origin_validation_and_allowed_reasons(self):
        route = f"/api/projects/{self.project['id']}/check"
        self.assertEqual(self.client.post(route, json={'reason': 'manual'}).status_code, 403)
        for reason in ('timer', 'focus', [], None):
            self.assertEqual(self.client.post(route, headers=self.headers, json={'reason': reason}).status_code, 400)
        self.assertEqual(self.client.post(route, headers=self.headers, json={'reason': 'manual'}).status_code, 200)

    def test_task_completion_checks_once_and_reading_task_does_not_trigger(self):
        from zest.tasks import TaskRunner
        runner = TaskRunner(self.state_dir, model=self.model)
        task = runner.create(self.project, 'add 做什么？', files=['app.py'])
        with patch.object(MaintenanceStore, 'check', wraps=self.store.check) as check:
            result = runner.decide(task['task_id'], approved=True, payload_sha256=task['payload_sha256'])
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(check.call_count, 1)
            self.assertEqual(check.call_args.kwargs['reason'], 'task')
            runner.show(task['task_id'])
            self.assertEqual(check.call_count, 1)

    def test_unknown_update_is_not_replayed_by_manual_or_automatic_checks(self):
        _, prepared = self.queue()
        class Unknown(OfflineCognitionModel):
            def complete(self, payload, **kwargs):
                self.calls.append(payload)
                raise FileReadError('model_request_failed', 'offline uncertain outcome')
        model = Unknown()
        result = self.store.run(self.project['id'], model=model)
        self.assertEqual(result['status'], 'pending_execution', result)
        with patch.object(MaintenanceStore, 'launch') as launch:
            for reason in ('manual', 'open', 'task'):
                self.assertEqual(self.store.check(self.project['id'], reason=reason)['active_run_id'], prepared['active_run_id'])
            launch.assert_not_called()
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result['knowledge_commit'], self.baseline['commit'])
        with self.assertRaises(FileReadError) as refused:
            self.store.reprepare(self.project['id'])
        self.assertEqual(refused.exception.code, 'maintenance_reprepare_not_allowed')
        self.assertEqual(len(model.calls), 1)

    def test_oversized_unsent_update_is_blocked_and_reprepares_only_after_explicit_action(self):
        from zest.cognition import CognitionEngine
        _, first = self.queue()
        with patch.object(CognitionEngine, 'plan', side_effect=FileReadError('cognition_prompt_budget', '完整请求过长，未外发。')):
            result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['research']['call_count'], 0)
        self.assertTrue(result['can_reprepare'])
        before_calls = len(self.model.calls)
        with patch.object(MaintenanceStore, 'launch') as launch:
            repeated = self.store.check(self.project['id'], reason='manual')
            self.assertEqual(repeated['active_run_id'], first['active_run_id'])
            launch.assert_not_called()
            prepared = self.store.reprepare(self.project['id'])
            launch.assert_called_once()
        self.assertNotEqual(prepared['active_run_id'], first['active_run_id'])
        self.assertEqual(len(self.model.calls), before_calls)
        old = CognitionStore(self.state_dir).show(first['active_run_id'])
        self.assertEqual(old['status'], 'partial')
        self.assertEqual(old['call_count'], 0)
        refreshed = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(refreshed['status'], 'completed', refreshed)
        self.assertEqual(refreshed['knowledge_commit'], prepared['target_commit'])

    def test_check_error_does_not_turn_a_completed_task_into_a_failed_task(self):
        from zest.git import RepositoryError
        from zest.maintenance import after_task
        result = {'status': 'completed', 'project_id': self.project['id']}
        with patch.object(MaintenanceStore, 'check', side_effect=RepositoryError('fixture repository unavailable')):
            after_task(self.state_dir, result)
        self.assertEqual(result['status'], 'completed')
        self.assertIn('maintenance_warning', result)

    def test_switching_back_to_a_known_version_reuses_its_understanding(self):
        current, _ = self.queue()
        updated = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(updated['status'], 'completed')
        calls = len(self.model.calls)
        # Checkout also removes documents first committed after this baseline.
        # Missing artifacts must remain visible until the saved old pages are restored.
        self.commit('save generated update for fixture checkout')
        self.git('checkout', '--detach', self.baseline['commit'])
        with patch.object(MaintenanceStore, 'launch') as launch:
            missing = self.store.check(self.project['id'], reason='switch')
            self.assertEqual(missing['status'], 'partial')
            self.assertTrue(missing.get('document_integrity'))
            self.initial.republish(self.project['id'])
            restored = self.store.check(self.project['id'], reason='switch')
        self.assertEqual(restored['status'], 'current')
        self.assertEqual(restored['knowledge_commit'], self.baseline['commit'])
        self.assertEqual(len(self.model.calls), calls)
        launch.assert_not_called()

    def test_a_version_moving_during_preparation_is_not_sent_under_wrong_identity(self):
        _, _ = self.queue()
        from zest.cognition_refresh import prepare_refresh
        def moved(*args, **kwargs):
            result = prepare_refresh(*args, **kwargs)
            return {**result, 'commit': '0' * 40}
        before = len(self.model.calls)
        with patch('zest.cognition_refresh.prepare_refresh', side_effect=moved):
            result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'maintenance_target_changed')
        self.assertEqual(len(self.model.calls), before)
