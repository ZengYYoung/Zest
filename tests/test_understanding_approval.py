"""Standing permission is project scoped, bounded, revocable and offline tested."""
from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
import subprocess

from fastapi.testclient import TestClient
from tests import test_initialization as fixtures
from zest.cognition_store import CognitionStore
from zest.initialization import InitializationStore
from zest.maintenance import MaintenanceStore
from zest.resources import project_states, record_state
from zest.understanding_approval import UnderstandingApproval
from zest.web import create_app
from zest.web_commands import execute, validate_command
from zest.git import inspect_repository, git_environment
from zest.storage import ProjectRegistry
from zest.files import FileReadError


class UnderstandingApprovalTest(unittest.TestCase):
    setUpBase = fixtures.InitializationTest.setUpBase
    git = fixtures.InitializationTest.git
    commit = fixtures.InitializationTest.commit
    prepare = fixtures.InitializationTest.prepare

    def setUp(self):
        fixtures.InitializationTest.setUp(self)
        self.enterContext(patch.object(InitializationStore, 'launch'))
        self.enterContext(patch.object(MaintenanceStore, 'launch'))
        self.approval = UnderstandingApproval(self.state_dir)

    def padded(self):
        import zest.cognition as cognition
        original = cognition.prepare_model_request
        def compile_request(settings, payload, *, purpose):
            result = original(settings, payload, purpose=purpose)
            if purpose in ('project_research', 'project_report'):
                result['body_text'] = result['body_text'].ljust(34000)
                result['chars'] = len(result['body_text'])
                result['body_sha256'] = hashlib.sha256(result['body_text'].encode()).hexdigest()
            return result
        return patch.object(cognition, 'prepare_model_request', side_effect=compile_request)

    def enable(self):
        ready = self.prepare()
        self.approval.configure(self.project['id'], enabled=True)
        return ready

    def test_default_off_and_progress_reads_do_not_write_or_check_git(self):
        ready = self.prepare()
        directory = project_states(self.state_dir, self.project['id'])[0]
        with patch('zest.initialization.inspect_repository', side_effect=AssertionError('read-only')):
            value = self.approval.show(self.project['id'])
        self.assertFalse(value['enabled'])
        self.assertFalse((directory / 'understanding-approval.json').exists())
        self.assertEqual(CognitionStore(self.state_dir).show(ready['run_id'])['status'], 'awaiting_approval')
        self.assertEqual(self.model.calls, [])

    def test_enable_approves_current_scope_once_with_exact_audit_and_private_grant(self):
        ready = self.enable()
        run = CognitionStore(self.state_dir).show(ready['run_id'])
        self.assertEqual(run['status'], 'queued')
        events = [e for e in run['events'] if e['action'] == 'understanding_scope_auto_approved']
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]['detail'])['scope_sha256'], run['approval_sha256'])
        self.approval.configure(self.project['id'], enabled=True)
        self.assertEqual(len(CognitionStore(self.state_dir).list_runs(self.project['id'])), 1)
        directory = project_states(self.state_dir, self.project['id'])[0]
        self.assertEqual((directory / 'understanding-approval.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.model.settings.api_key, json.dumps(self.approval.show(self.project['id'])))
        self.assertEqual(self.model.calls, [])

    def test_one_initialize_action_prepares_approves_and_queues_idempotently(self):
        ready = self.store.begin(self.project['id'])
        self.assertEqual(ready['status'], 'queued')
        self.assertTrue(self.approval.show(self.project['id'])['active'])
        self.assertEqual(self.store.begin(self.project['id'])['run_id'], ready['run_id'])
        self.assertEqual(len(CognitionStore(self.state_dir).list_runs(self.project['id'])), 1)
        finished = self.store.run(self.project['id'], model=self.model, check_updates=False)
        count = len(self.model.calls)
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual(self.store.begin(self.project['id'])['run_id'], ready['run_id'])
        self.assertEqual(len(self.model.calls), count)

    def test_automatic_web_import_is_one_action_without_manual_scope_decision(self):
        result = execute(self.state_dir, 'project.import', {'path':str(self.repo), 'automatic_understanding':True})
        self.assertEqual(result['initialization']['status'], 'queued')
        run_id = result['initialization']['run_id']
        repeated = execute(self.state_dir, 'project.import', {'path':str(self.repo), 'automatic_understanding':True})
        self.assertEqual(repeated['initialization']['run_id'], run_id)
        self.assertEqual(len(CognitionStore(self.state_dir).list_runs(self.project['id'])), 1)
        self.assertEqual(self.model.calls, [])

    def test_missing_key_returns_setup_then_one_initialize_action_continues(self):
        with patch('zest.models.ModelSettings.from_env', side_effect=FileReadError('model_not_configured', '请先填写 API Key')):
            waiting = self.store.begin(self.project['id'])
        self.assertEqual(waiting['status'], 'needs_model')
        self.assertEqual(self.model.calls, [])
        ready = execute(self.state_dir, 'initialization.initialize', {'project_id':self.project['id'], 'resume_setup':True})
        self.assertEqual(ready['status'], 'queued')
        self.assertTrue(self.approval.show(self.project['id'])['active'])

    def test_worker_automatically_approves_exact_long_requests_and_finishes(self):
        ready = self.enable()
        with self.padded():
            result = self.store.run(self.project['id'], model=self.model, check_updates=False)
        self.assertEqual(result['status'], 'completed', result.get('research', {}).get('error'))
        run = CognitionStore(self.state_dir).show(ready['run_id'])
        self.assertEqual(run['context_requests'], [])
        self.assertLessEqual(run['call_count'], 32)
        self.assertTrue(any(e['action'] == 'understanding_scope_auto_approved' for e in run['events']))

    def test_disable_stops_new_approvals_but_does_not_reset_approved_work(self):
        ready = self.enable()
        disabled = self.approval.configure(self.project['id'], enabled=False)
        self.assertFalse(disabled['enabled'])
        self.assertFalse(MaintenanceStore(self.state_dir).show(self.project['id'])['enabled'])
        self.assertEqual(CognitionStore(self.state_dir).show(ready['run_id'])['status'], 'queued')
        with self.padded():
            result = self.store.run(self.project['id'], model=self.model, check_updates=False)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['research']['context_requests'], [])
        # Already approved work finishes; re-enabling never creates another run.
        with patch('zest.models.ModelSettings.from_env', return_value=replace(self.model.settings, revision='rotated-fixture-key')):
            self.approval.configure(self.project['id'], enabled=True)
            self.assertTrue(self.approval.show(self.project['id'])['active'])
        self.assertEqual(self.store.show(self.project['id'])['run_id'], ready['run_id'])
        self.assertEqual(self.store.show(self.project['id'])['research']['status'], 'completed')

    def test_repair_uses_the_switch_and_target_change_requires_new_permission(self):
        self.model.reject = True
        self.enable()
        finished = self.store.run(self.project['id'], model=self.model, check_updates=False)
        # A terminal partial result may be supplemented; no failed requests are replayed.
        project, directory = self.store.location(self.project['id'])
        value = self.store.read(directory)
        value['status'] = 'partial'
        self.store.save(directory, value)
        ready = self.store.repair(self.project['id'])
        self.assertEqual(ready['research']['status'], 'queued')
        self.assertNotEqual(ready['run_id'], finished['run_id'])
        self.store.run(self.project['id'], model=self.model, check_updates=False)
        value = self.store.read(directory); value['status'] = 'partial'; self.store.save(directory, value)
        before = len(self.model.calls)
        with patch('zest.models.ModelSettings.from_env', return_value=replace(self.model.settings, name='other-target')):
            prepared = self.store.repair(self.project['id'])
            self.assertEqual(prepared['research']['status'], 'awaiting_approval')
            self.assertTrue(self.approval.show(self.project['id'])['enabled'])
            self.assertFalse(self.approval.show(self.project['id'])['active'])
        self.assertEqual(len(self.model.calls), before)

    def test_unknown_provider_result_never_restarts_when_enabled_again(self):
        ready = self.enable()
        with patch.object(self.model, 'complete', side_effect=TimeoutError):
            failed = self.store.run(self.project['id'], model=self.model, check_updates=False)
        self.assertEqual(failed['status'], 'pending_execution')
        before = CognitionStore(self.state_dir).show(ready['run_id'], include_calls=True)
        self.approval.configure(self.project['id'], enabled=True)
        after = CognitionStore(self.state_dir).show(ready['run_id'], include_calls=True)
        self.assertEqual(before, after)

    def test_explicit_rejection_is_preserved_even_with_old_partial_summary(self):
        ready = self.prepare()
        self.store.decide(self.project['id'], approved=False)
        _, directory = self.store.location(self.project['id'])
        value = self.store.read(directory)
        value['status'] = 'partial'  # A previous published summary can still exist.
        self.store.save(directory, value)
        self.approval.configure(self.project['id'], enabled=True)
        current = self.store.show(self.project['id'])
        self.assertEqual(current['status'], 'rejected')
        self.assertEqual(current['research']['status'], 'rejected')
        self.assertEqual(current['run_id'], ready['run_id'])
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.store.begin(self.project['id'], resume_setup=True)['run_id'], ready['run_id'])
        self.assertEqual(self.store.show(self.project['id'])['research']['status'], 'rejected')

    def test_existing_call_budget_is_not_reset_by_automatic_permission(self):
        ready = self.enable()
        bound = CognitionStore(record_state(self.state_dir, 'cognition', ready['run_id']))
        with bound.open(write=True) as conn, conn:
            conn.execute('UPDATE cognition_runs SET call_count=32 WHERE id=?', (ready['run_id'],))
        result = self.store.run(self.project['id'], model=self.model, check_updates=False)
        self.assertEqual(result['research']['call_count'], 32)
        self.assertEqual(result['research']['status'], 'partial')
        self.assertEqual(self.model.calls, [])

    def test_copied_permission_never_authorizes_another_project(self):
        ready = self.enable()
        second_root = self.repo.parent / 'second-project'
        subprocess.run(['git', 'clone', '--no-hardlinks', str(self.repo), str(second_root)],
                       env=git_environment(), capture_output=True, check=True)
        other = ProjectRegistry(self.state_dir).register(inspect_repository(second_root), workspace=True)
        other_run = self.store.prepare(other['id'])
        first_directory = project_states(self.state_dir, self.project['id'])[0]
        second_directory = project_states(self.state_dir, other['id'])[0]
        (second_directory / 'understanding-approval.json').write_bytes((first_directory / 'understanding-approval.json').read_bytes())
        self.assertFalse(self.approval.show(other['id'])['active'])
        self.assertFalse(self.approval.approve(other['id'], other_run['run_id']))
        self.assertFalse(self.approval.approve(other['id'], ready['run_id']))
        self.assertEqual(CognitionStore(self.state_dir).show(other_run['run_id'])['status'], 'awaiting_approval')

    def test_incremental_update_and_its_long_requests_are_automatically_approved(self):
        self.enable()
        self.store.run(self.project['id'], model=self.model, check_updates=False)
        (self.repo / 'app.py').write_text('def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n')
        self.commit('new committed feature')
        maintenance = MaintenanceStore(self.state_dir)
        queued = maintenance.check(self.project['id'], reason='manual')
        self.assertEqual(queued['status'], 'queued')
        with self.padded():
            result = maintenance.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['active_run_id'], queued['active_run_id'])
        self.assertTrue(all(r['status'] == 'approved' for r in result['research']['context_requests']))
        self.assertTrue(any(e['action'] == 'understanding_scope_auto_approved' for e in result['research']['events']))

    def test_disabling_a_queued_unprepared_update_cannot_fall_back_to_legacy_approval(self):
        self.enable()
        self.store.run(self.project['id'], model=self.model, check_updates=False)
        (self.repo / 'app.py').write_text('def add(a, b):\n    return a + b\n# committed change\n')
        self.commit('another change')
        maintenance = MaintenanceStore(self.state_dir)
        maintenance.check(self.project['id'], reason='manual')
        before = len(self.model.calls)
        self.approval.configure(self.project['id'], enabled=False)
        result = maintenance.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'maintenance_grant_changed')
        self.assertEqual(len(self.model.calls), before)

    def test_api_setting_uses_existing_origin_guard_and_strict_boolean_command(self):
        client = TestClient(create_app(self.state_dir))
        route = f"/api/projects/{self.project['id']}/understanding-approval"
        self.assertFalse(client.get(route).json()['data']['enabled'])
        body = {'request_id': str(uuid4()), 'action': 'understanding.configure',
                'parameters': {'project_id': self.project['id'], 'enabled': True}}
        self.assertEqual(client.post('/api/operations', json=body).status_code, 403)
        from zest.web_operations import WebOperations
        with patch.object(WebOperations, 'launch'):
            response = client.post('/api/operations', headers={'Origin':'http://127.0.0.1:5173'}, json=body)
        self.assertEqual(response.status_code, 202)
        for value in ('true', 1, [], None):
            with self.subTest(value=value), self.assertRaises(FileReadError):
                validate_command('understanding.configure', {'project_id':self.project['id'], 'enabled':value})
        result = execute(self.state_dir, 'understanding.configure', {'project_id':self.project['id'], 'enabled':True})
        self.assertTrue(result['active'])
