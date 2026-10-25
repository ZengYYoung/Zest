"""Offline regressions for partial review delivery and bounded supplementation."""
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_cognition as fixtures
from tests import test_initialization as initialization_fixtures
from zest.cognition import CognitionEngine
from zest.cognition_quality import classify_pending, partition_judgments
from zest.cognition_repair import check_repair_context, prepare_repair, repair_view
from zest.cognition_store import CognitionStore
from zest.files import FileReadError
from zest.initialization import InitializationStore


class JudgmentPartitionTest(unittest.TestCase):
    def setUp(self):
        self.payload = {'findings': [
            {'finding_id': 'T1:F1', 'statement': 'first', 'kind': 'observed', 'evidence_ids': ['P1']},
            {'finding_id': 'T1:F2', 'statement': 'second', 'kind': 'observed', 'evidence_ids': ['P2']}],
            'other_findings': [], 'report_unknowns': []}
        self.response = {'judgments': [fixtures.review_judgment(f) for f in self.payload['findings']]}

    def test_one_wrong_reference_does_not_accept_it_or_discard_valid_peer(self):
        self.response['judgments'][0]['checks'][0]['evidence_ids'] = ['P2']
        good, issues = partition_judgments(self.response, self.payload)
        self.assertEqual([v['finding_id'] for v in good], ['T1:F2'])
        self.assertTrue(good[0]['supported'])
        self.assertEqual([v['finding_id'] for v in issues], ['T1:F1'])
        self.assertEqual(self.response['judgments'][0]['checks'][0]['evidence_ids'], ['P2'])

    def test_ambiguous_missing_duplicate_or_foreign_identity_rejects_batch(self):
        for identity in ('T1:F2', 'foreign', None, []):
            response = copy.deepcopy(self.response)
            response['judgments'][0]['finding_id'] = identity
            with self.subTest(identity=identity), self.assertRaises(FileReadError):
                partition_judgments(response, self.payload)
        with self.assertRaises(FileReadError):
            partition_judgments({'judgments': self.response['judgments'][:1]}, self.payload)

    def test_bad_judgment_conflict_quarantines_otherwise_valid_peer(self):
        self.response['judgments'][0]['checks'][0]['evidence_ids'] = ['invalid']
        self.response['judgments'][0]['conflicts'] = [{'reference_id': 'T1:F2', 'reason': 'conflict'}]
        good, issues = partition_judgments(self.response, self.payload)
        self.assertFalse(good)
        self.assertEqual(len(issues), 2)

    def test_batch_peer_conflicts_are_valid_and_block_acceptance(self):
        self.response['judgments'][0]['conflicts'] = [{'reference_id': 'T1:F2', 'reason': 'conflict'}]
        good, issues = partition_judgments(self.response, self.payload)
        self.assertFalse(issues)
        self.assertFalse(good[0]['supported'])

    def test_malformed_conflict_identity_is_isolated_without_type_error(self):
        self.response['judgments'][0]['conflicts'] = [{'reference_id': [], 'reason': 'malformed'}]
        good, issues = partition_judgments(self.response, self.payload)
        self.assertEqual([v['finding_id'] for v in good], ['T1:F2'])
        self.assertEqual([v['finding_id'] for v in issues], ['T1:F1'])

    def test_pending_counts_deduplicate_across_sections_without_hiding_category(self):
        question = {'question': 'read the remaining method', 'source_gap': True}
        sections = [{'pending': [question, question]}, {'pending': [question, {'question': 'need deployment record', 'scope_expansion': True}]}]
        count, summary = classify_pending(sections)
        self.assertEqual(count, 2)
        self.assertEqual(len(sections[0]['pending']), 1)
        self.assertEqual({v['category']: v['count'] for v in summary}, {'source_gap': 1, 'external_material': 1})


class QualityWorkflowTest(unittest.TestCase):
    setUp = fixtures.CognitionTest.setUp
    setUpBase = fixtures.CognitionTest.setUpBase
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    approved = fixtures.CognitionTest.approved
    prepare = fixtures.CognitionTest.prepare

    def test_review_issue_and_valid_results_commit_atomically(self):
        run = self.approved()['id']
        envelope = self.store.show(run, include_envelope=True)['envelope']
        from zest.cognition_tools import PackageTools
        tools = PackageTools(envelope)
        self.store.set_status(run, 'planning')
        self.engine.plan(run, envelope, tools)
        self.engine.dispatch(run, envelope, tools, self.store.show(run)['plan'])
        finding = self.store.show(run)['findings'][0]
        with self.assertRaises(FileReadError):
            self.store.save_review(run, [{'finding_id': 'foreign'}], issues=[
                {'finding_id': finding['id'], 'code': 'invalid_cognition_reference', 'reason': 'wrong reference'}])
        restored = next(f for f in self.store.show(run)['findings'] if f['id'] == finding['id'])
        self.assertEqual(restored['status'], 'pending_review')
        self.assertIsNone(restored['review'])

    def test_invalid_reference_isolated_while_later_batches_complete(self):
        original = self.model.complete
        reviews = []
        def complete(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            if purpose == 'project_review':
                reviews.append(payload)
                if len(reviews) == 1:
                    value = json.loads(result['text'])
                    value['judgments'][0]['checks'][0]['evidence_ids'] = ['outside']
                    result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=complete), patch.object(CognitionEngine, 'followups', return_value=False):
            result = self.engine.run(self.approved()['id'])
        self.assertEqual(result['status'], 'partial', result.get('error'))
        self.assertEqual(len(reviews), 3)
        self.assertTrue(all(len(p['findings']) == 2 for p in reviews))
        self.assertEqual(sum(f['status'] == 'auto_reviewed' for f in result['findings']), 5)
        failed = next(f for f in result['findings'] if f['status'] == 'needs_investigation')
        self.assertIn('validation_error', failed['review'])
        context = self.store.for_context(self.project['id'], result['commit'])
        self.assertTrue(context)
        self.assertNotIn(failed['id'], [f['id'] for f in context])
        for item in result['findings']:
            if item['status'] == 'auto_reviewed':
                self.store.reviewed_finding(result['id'], item['id'])

    def test_repair_preparation_reuses_exact_inventory_without_calls_or_old_mutations(self):
        self.model.reject = True
        source = self.engine.run(self.approved()['id'])
        before = self.store.show(source['id'], include_calls=True)
        count = len(self.model.calls)
        ready = prepare_repair(self.state_dir, source['id'], target=self.target)
        full = self.store.show(ready['id'], include_envelope=True)
        original = self.store.show(source['id'], include_envelope=True)
        self.assertEqual(full['envelope']['files'], original['envelope']['files'])
        self.assertEqual(len(full['envelope']['repair_context']['leads']), 4)
        view = repair_view(full['envelope']['repair_context'], full['envelope']['files'], planning=True)
        self.assertEqual(view['version'], 2)
        self.assertEqual(len(view['selected_issues']), 4)
        detail = repair_view(full['envelope']['repair_context'], full['envelope']['files'], ['flows'], store=self.store)
        self.assertTrue(all(item['section'] == 'flows' and item['statement'] for item in detail['leads']))
        self.assertEqual(full['status'], 'awaiting_approval')
        self.assertNotEqual(full['approval_sha256'], original['approval_sha256'])
        self.assertEqual(self.store.show(source['id'], include_calls=True), before)
        self.assertEqual(len(self.model.calls), count)
        self.store.invalidate(source['id'], source['findings'][0]['id'], 'fixture changed')
        with self.assertRaises(FileReadError) as caught:
            check_repair_context(self.store, full['envelope'])
        self.assertEqual(caught.exception.code, 'cognition_repair_source_changed')

    def test_unknown_old_execution_cannot_be_bypassed_by_new_package(self):
        source = self.engine.run(self.approved()['id'])
        with self.store.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_calls SET status='uncertain' WHERE run_id=? AND actor='coordinator'", (source['id'],))
        with self.assertRaises(FileReadError) as caught:
            prepare_repair(self.state_dir, source['id'], target=self.target)
        self.assertEqual(caught.exception.code, 'cognition_execution_unknown')
        self.assertEqual(len(self.store.list_runs(self.project['id'])), 1)


class InitializationRepairTest(unittest.TestCase):
    setUp = initialization_fixtures.InitializationTest.setUp
    setUpBase = initialization_fixtures.InitializationTest.setUpBase
    git = initialization_fixtures.InitializationTest.git
    commit = initialization_fixtures.InitializationTest.commit
    prepare = initialization_fixtures.InitializationTest.prepare
    approve = initialization_fixtures.InitializationTest.approve
    finish = initialization_fixtures.InitializationTest.finish

    def test_local_republish_and_repair_have_separate_idempotent_approval(self):
        self.model.reject = True
        original = self.model.complete
        def partial(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            if purpose in ('project_report', 'project_research'):
                value = json.loads(result['text'])
                if value.get('report'):
                    value['report']['status'] = 'partial'
                    value['report']['unknowns'] = ['尚需真实部署记录']
                    result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=partial):
            finished = self.finish()
        before = len(self.model.calls)
        republished = self.store.republish(self.project['id'])
        self.assertEqual(republished['summary']['pending_items'], 7)
        self.assertEqual(len(self.model.calls), before)
        ready = self.store.repair(self.project['id'])
        self.assertNotEqual(ready['run_id'], finished['run_id'])
        self.assertEqual(ready['repair_source_run_id'], finished['run_id'])
        self.assertEqual(self.store.repair(self.project['id'])['run_id'], ready['run_id'])
        self.assertEqual(self.prepare()['run_id'], ready['run_id'])
        self.assertEqual(len(self.model.calls), before)
        replaced = self.store.repair(self.project['id'], replace_pending=True)
        self.assertNotEqual(replaced['run_id'], ready['run_id'])
        self.assertEqual(replaced['repair_source_run_id'], finished['run_id'])
        self.assertEqual(CognitionStore(self.state_dir).show(ready['run_id'])['status'], 'rejected')
        ready = replaced
        self.model.reject = False
        with patch.object(InitializationStore, 'launch'):
            self.store.decide(self.project['id'], approved=True, sha256=ready['research']['approval_sha256'])
        result = self.store.run(self.project['id'], model=self.model, check_updates=False)
        self.assertEqual(result['stage'], 'published', result.get('error'))
        self.assertEqual(result['status'], 'partial')  # The historical question is not silently closed.
        self.assertEqual(result['summary']['pending_items'], 3)
        self.assertEqual(CognitionStore(self.state_dir).show(finished['run_id'])['status'], 'partial')
        research_calls = [p for kind, p in self.model.calls[before:] if kind in ('project_plan', 'project_research', 'project_report')]
        self.assertTrue(research_calls)
        self.assertTrue(all('repair_context' in p for p in research_calls))
