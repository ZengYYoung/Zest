"""Explicit fresh document budget, original evidence and requests remain immutable."""
from unittest.mock import patch
import unittest

from tests import test_maintenance as fixtures
from zest.cognition import CognitionEngine
from zest.cognition_store import CognitionStore
from zest.document_pages import DocumentPages, facts_for
from zest.files import FileReadError
from zest.maintenance import MaintenanceStore
from zest.resources import record_state
from zest.web_commands import execute, validate_command
from zest.document_rounds import recovery


class RecoveryPolicyTest(unittest.TestCase):
    def test_context_or_capacity_failures_do_not_offer_another_identical_request(self):
        record = {'id':'source', 'call_count':32, 'policy':{'max_model_calls':32,'max_seconds':1800}}
        for code in ('cognition_prompt_budget', 'document_context_changed', 'model_target_changed'):
            self.assertEqual(recovery({'status':'partial','pages':[],'error':{'code':code}}, record)['action'], 'inspect')

    def test_saved_reviewed_page_can_publish_locally_even_when_calls_are_exhausted(self):
        record = {'id':'source', 'call_count':32, 'policy':{'max_model_calls':32,'max_seconds':1800}}
        value = {'status':'partial','plan':{},'pages':[{'status':'failed','content':{},'review':{},'error':{'code':'document_write_failed'}}]}
        self.assertEqual(recovery(value, record)['action'], 'resume')


class DocumentRoundsTest(unittest.TestCase):
    setUpBase = fixtures.MaintenanceTest.setUpBase
    setUp = fixtures.MaintenanceTest.setUp
    git = fixtures.MaintenanceTest.git
    commit = fixtures.MaintenanceTest.commit
    changed = fixtures.MaintenanceTest.changed
    queue = fixtures.MaintenanceTest.queue

    def exhausted(self, budget_used=32):
        self.queue()
        from zest.document_pages import generate_documents
        def stop(state_dir, project, dossier, **kwargs):
            bound = CognitionStore(record_state(state_dir, 'cognition', dossier['run_id']))
            with bound.open(write=True) as conn, conn:
                conn.execute('UPDATE cognition_runs SET call_count=? WHERE id=?', (budget_used, dossier['run_id']))
            return generate_documents(state_dir, project, dossier, **kwargs)
        with patch('zest.document_pages.generate_documents', side_effect=stop):
            result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['document_generation']['recovery']['action'], 'new_round')
        if budget_used == 32:
            self.assertEqual(result['document_generation']['total'], 0)
        return result

    def start(self, source):
        recovery = source['document_generation']['recovery']
        params = validate_command('maintenance.documents-new-round', {'project_id': self.project['id'],
            'source_run_id': recovery['source_run_id'], 'source_sha256': recovery['source_sha256']})
        with patch.object(MaintenanceStore, 'launch'):
            result = execute(self.state_dir, 'maintenance.documents-new-round', params)
        return result, params

    def test_exhausted_resume_explains_reason_without_queueing_or_resetting_budget(self):
        source = self.exhausted()
        before = len(self.model.calls)
        with patch.object(MaintenanceStore, 'launch') as launch, self.assertRaises(FileReadError) as caught:
            self.store.resume(self.project['id'])
        self.assertEqual(caught.exception.code, 'document_new_round_required')
        self.assertIn('32 / 32', str(caught.exception))
        launch.assert_not_called()
        self.assertEqual(CognitionStore(self.state_dir).show(source['active_run_id'])['call_count'], 32)
        self.assertEqual(len(self.model.calls), before)

    def test_new_round_generates_pages_without_research_and_keeps_original_run_unchanged(self):
        source = self.exhausted()
        store = CognitionStore(self.state_dir)
        original = store.show(source['active_run_id'], include_calls=True)
        original_pages = DocumentPages(self.state_dir, source['active_run_id']).path.read_bytes()
        before = len(self.model.calls)
        queued, params = self.start(source)
        self.assertNotEqual(queued['active_run_id'], source['active_run_id'])
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(store.show(queued['active_run_id'])['call_count'], 0)
        self.assertEqual(facts_for(store.dossier(source['active_run_id'])), facts_for(store.dossier(queued['active_run_id'])))
        with patch.object(MaintenanceStore, 'launch'):
            duplicate = execute(self.state_dir, 'maintenance.documents-new-round', params)
        self.assertEqual(duplicate['active_run_id'], queued['active_run_id'])
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertGreater(result['document_generation']['completed'], 0)
        self.assertTrue(all(purpose.startswith('document_') for purpose, _ in self.model.calls[before:]))
        self.assertEqual(store.show(source['active_run_id'], include_calls=True), original)
        self.assertEqual(DocumentPages(self.state_dir, source['active_run_id']).path.read_bytes(), original_pages)
        self.assertLessEqual(store.show(queued['active_run_id'])['call_count'], 32)

    def test_stale_approval_and_source_invalidation_do_not_authorize_new_calls(self):
        source = self.exhausted()
        stale = {**source, 'document_generation': {**source['document_generation'],
            'recovery': {**source['document_generation']['recovery'], 'source_sha256': '0' * 64}}}
        with self.assertRaises(FileReadError) as caught: self.start(stale)
        self.assertEqual(caught.exception.code, 'document_source_changed')
        queued, _ = self.start(source)
        store = CognitionStore(self.state_dir)
        record = store.show(source['active_run_id'])
        item = next(f for f in record['findings'] if f['status'] == 'auto_reviewed')
        store.invalidate(record['id'], item['id'], reason='fixture source invalidation')
        before = len(self.model.calls)
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.model.calls), before)
        self.assertEqual(store.show(queued['active_run_id'])['call_count'], 0)

    def test_document_round_cannot_spend_its_new_budget_on_research(self):
        source = self.exhausted()
        queued, _ = self.start(source)
        record = CognitionStore(self.state_dir).show(queued['active_run_id'], include_envelope=True)
        with self.assertRaises(FileReadError) as caught:
            CognitionEngine(self.state_dir, model=self.model).call(record['id'], 'coordinator', 0, 'project_plan', {}, record['envelope'])
        self.assertEqual(caught.exception.code, 'document_round_purpose')
        self.assertEqual(CognitionStore(self.state_dir).show(record['id'])['call_count'], 0)

    def test_unknown_original_call_cannot_be_bypassed_by_a_new_round(self):
        source = self.exhausted()
        bound = CognitionStore(record_state(self.state_dir, 'cognition', source['active_run_id']))
        with bound.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_calls SET status='uncertain' WHERE run_id=? AND actor='coordinator' AND sequence=0", (source['active_run_id'],))
        before = len(self.model.calls)
        with self.assertRaises(FileReadError) as caught: self.start(source)
        self.assertEqual(caught.exception.code, 'cognition_execution_unknown')
        self.assertEqual(self.store.show(self.project['id'])['active_run_id'], source['active_run_id'])
        self.assertEqual(len(self.model.calls), before)

    def test_crash_after_child_creation_reuses_durable_pending_identity(self):
        source = self.exhausted()
        save = DocumentPages.save
        def crash(pages):
            if pages.run_id != source['active_run_id']: raise RuntimeError('fixture crash after child insert')
            return save(pages)
        with patch.object(DocumentPages, 'save', crash), self.assertRaises(RuntimeError): self.start(source)
        _, directory = self.initial.location(self.project['id'])
        pending = self.store.read(directory)['document_round_pending']['run_id']
        queued, _ = self.start(source)
        self.assertEqual(queued['active_run_id'], pending)
        runs = CognitionStore(self.state_dir).list_runs(self.project['id'])
        self.assertEqual(sum(run['id'] == pending for run in runs), 1)

    def test_new_round_keeps_completed_page_bytes_and_only_calls_unfinished_pages(self):
        source = self.exhausted(budget_used=29)
        rows = DocumentPages(self.state_dir, source['active_run_id']).value['pages']
        done = next(page for page in rows if page['status'] == 'completed')
        slug = done['spec']['slug']
        path = self.repo / '.zest/docs/initialization' / (slug + '.md')
        original = path.read_bytes(), path.stat().st_mtime_ns
        before = len(self.model.calls)
        queued, _ = self.start(source)
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)
        calls = self.model.calls[before:]
        self.assertTrue(calls)
        self.assertTrue(all(purpose in ('document_write', 'document_review') and payload['page']['slug'] != slug for purpose, payload in calls))
        # A missing completed artifact is restored byte-for-byte from its frozen
        # published body, not rendered with the new round's provenance template.
        from zest.document_pages import publish_pages
        dossier = CognitionStore(self.state_dir).dossier(queued['active_run_id'])
        ledger = DocumentPages(self.state_dir, queued['active_run_id'])
        before_restore = len(self.model.calls)
        path.unlink()
        publish_pages(self.project, dossier, result, ledger)
        self.assertEqual(path.read_bytes(), original[0])
        self.assertEqual(len(self.model.calls), before_restore)

    def test_legacy_failed_plan_recomputes_page_capacity_from_the_new_budget(self):
        with patch('zest.knowledge_snapshots.freeze_context', return_value=None):
            source = self.exhausted()
        old = DocumentPages(self.state_dir, source['active_run_id'])
        old_bytes = old.path.read_bytes()
        self.assertEqual(old.value['plan_payload']['max_pages'], 1)
        queued, _ = self.start(source)
        child = DocumentPages(self.state_dir, queued['active_run_id'])
        self.assertNotIn('plan_payload', child.value)
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['document_generation']['completed'], 6)
        self.assertEqual(DocumentPages(self.state_dir, queued['active_run_id']).value['plan_payload']['max_pages'], 6)
        self.assertEqual(old.path.read_bytes(), old_bytes)

    def test_new_incremental_round_drops_failed_update_payload_but_preserves_completed_plan(self):
        source = self.exhausted()
        old = DocumentPages(self.state_dir, source['active_run_id'])
        self.assertIn('update_payload', old.value)
        queued, _ = self.start(source)
        self.assertNotIn('update_payload', DocumentPages(self.state_dir, queued['active_run_id']).value)

    def test_review_guided_revision_finishes_within_the_same_document_round_budget(self):
        import json
        source = self.exhausted()
        queued, _ = self.start(source)
        complete = self.model.complete
        def first_rejection(payload, *, purpose='qa'):
            result = complete(payload, purpose=purpose)
            if purpose == 'document_review' and 'review_criteria' not in payload:
                value = json.loads(result['text'])
                value['checks'][0]['verdict'] = 'insufficient_evidence'
                value['issues'] = ['fixture: 删除证据外的使用建议。']
                return {**result,'text':json.dumps(value,ensure_ascii=False)}
            return result
        with patch.object(self.model, 'complete', side_effect=first_rejection):
            result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['active_run_id'], queued['active_run_id'])
        record = CognitionStore(self.state_dir).show(queued['active_run_id'])
        self.assertLessEqual(record['call_count'], 32)
        pages = DocumentPages(self.state_dir, queued['active_run_id']).value['pages']
        self.assertTrue(all(p['revision_number'] == 1 and not p['review']['issues'] for p in pages))
