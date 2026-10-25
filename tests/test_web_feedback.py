"""Regressions for stuck approvals, exact batch decisions and import progress."""
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_cognition_context as context_fixture
from zest.files import FileReadError
from zest.models import prepare_model_request
from zest.web_operations import WebOperations, report_progress


class ApprovalFeedbackTest(unittest.TestCase):
    setUpBase = context_fixture.ContextApprovalTest.setUpBase
    setUp = context_fixture.ContextApprovalTest.setUp
    git = context_fixture.ContextApprovalTest.git
    commit = context_fixture.ContextApprovalTest.commit
    cli = context_fixture.ContextApprovalTest.cli
    prepare = context_fixture.ContextApprovalTest.prepare
    approved = context_fixture.ContextApprovalTest.approved
    active = context_fixture.ContextApprovalTest.active
    padded = context_fixture.ContextApprovalTest.padded
    call = context_fixture.ContextApprovalTest.call
    pending = context_fixture.ContextApprovalTest.pending

    def two_requests(self):
        run, envelope, payload, request = self.pending()
        with self.assertRaises(FileReadError):
            self.call(run, envelope, payload, actor='T2')
        requests = self.store.context_requests(run)
        identities = [{'actor':r['actor'], 'sequence':r['sequence'], 'sha256':r['request_sha256']} for r in requests]
        return run, identities

    def test_batch_validates_all_hashes_before_changing_any_request(self):
        run, identities = self.two_requests()
        wrong = [*identities[:-1], {**identities[-1], 'sha256':'0'*64}]
        with self.assertRaises(FileReadError):
            self.store.decide_contexts(run, wrong, approved=True)
        self.assertTrue(all(r['status']=='awaiting_approval' for r in self.store.context_requests(run)))
        for invalid in (identities[:-1], [identities[0],identities[0]]):
            with self.assertRaises(FileReadError):
                self.store.decide_contexts(run, invalid, approved=True)
        result = self.store.decide_contexts(run, identities, approved=True)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['call_count'], 0)
        self.assertTrue(all(r['status']=='approved' for r in self.store.context_requests(run)))

    def test_single_decision_keeps_remaining_request_visible_and_batch_can_reject(self):
        run, identities = self.two_requests()
        first, last = identities
        result = self.store.decide_context(run, first['actor'], first['sequence'], approved=True, request_sha256=first['sha256'])
        self.assertEqual(result['status'], 'awaiting_context_approval')
        self.assertEqual(sum(r['status']=='awaiting_approval' for r in self.store.context_requests(run)), 1)
        result = self.store.decide_contexts(run, [last], approved=False)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['call_count'], 0)

    def failed_job(self):
        run, envelope, _, request = self.pending()
        spec = {'id':'T1', 'title':'接口调查', 'goal':'了解接口行为', 'source_ids':[envelope['files'][0]['id']], 'sections':['flows']}
        self.store.save_plan(run, {'tasks':[spec]})
        with patch.object(self.model, 'complete', return_value={'text':'[]'}):
            with self.assertRaises(FileReadError):
                self.engine.call(run, 'T1', 0, 'project_research', {}, envelope)
        self.store.job_status(run, 'T1', 'failed', {'report': {'findings':[], 'unknowns':[], 'scope_requests':[]}, 'evidence':{}})
        return run, request

    def test_saved_invalid_response_of_failed_job_does_not_block_other_unsent_approval(self):
        run, request = self.failed_job()
        result = self.store.decide_context(run, request['actor'],request['sequence'],approved=True,request_sha256=request['request_sha256'])
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['call_count'], 1)
        self.assertEqual(result['jobs'][0]['status'], 'failed')
        self.assertEqual(self.store.show(run, include_calls=True)['calls'][0]['status'], 'invalid')

    def test_unknown_or_missing_receipt_still_blocks_and_never_authorizes_replay(self):
        for mode in ('uncertain','missing_receipt'):
            with self.subTest(mode=mode):
                run, request = self.failed_job()
                with self.store.open(write=True) as conn, conn:
                    if mode == 'uncertain':
                        conn.execute("UPDATE cognition_calls SET status='uncertain' WHERE run_id=? AND actor='T1'", (run,))
                    else:
                        conn.execute("DELETE FROM cognition_call_receipts WHERE run_id=? AND actor='T1'", (run,))
                with self.assertRaises(FileReadError) as raised:
                    self.store.decide_context(run, request['actor'],request['sequence'],approved=True,request_sha256=request['request_sha256'])
                self.assertEqual(raised.exception.code, 'cognition_execution_unknown')
                self.assertEqual(self.store.context_requests(run)[0]['status'], 'awaiting_approval')

    def test_request_description_uses_job_goal_and_exact_saved_materials(self):
        run, envelope = self.active()
        spec = {'id':'T1','title':'接口调查','goal':'确认接口参数与返回','source_ids':[envelope['files'][0]['id']], 'sections':['flows']}
        self.store.save_plan(run, {'tasks':[spec]})
        request = prepare_model_request(self.model.settings, self.padded(35000), purpose='project_research')
        with self.assertRaises(FileReadError):
            self.store.require_context(run,'T1',0,envelope,request)
        description = self.store.context_requests(run)[0]['description']
        self.assertEqual(description['title'], spec['title'])
        self.assertEqual(description['goal'], spec['goal'])
        self.assertEqual(description['source_paths'], [envelope['files'][0]['evidence']['path']])
        self.assertFalse(description['new_files_authorized'])


class ImportProgressTest(unittest.TestCase):
    def test_import_progress_is_observable_before_operation_finishes(self):
        with tempfile.TemporaryDirectory(prefix='zest-feedback-') as directory:
            store = WebOperations(directory)
            operation, _ = store.submit(str(uuid4()), 'project.import', {'path':'/tmp/offline-fixture'})
            ready, release = threading.Event(), threading.Event()
            def execute(*args):
                report_progress('index','正在建立源码检索索引')
                ready.set(); release.wait(5)
                return {'id':'offline-fixture'}
            with patch('zest.web_commands.execute', side_effect=execute), ThreadPoolExecutor() as pool:
                future = pool.submit(store.run, operation['id'])
                try:
                    self.assertTrue(ready.wait(3))
                    current = store.show(operation['id'])
                    self.assertEqual(current['status'], 'running')
                    self.assertEqual(current['progress']['stage'], 'index')
                finally:
                    release.set()
                future.result()
            self.assertEqual(store.show(operation['id'])['status'], 'completed')
