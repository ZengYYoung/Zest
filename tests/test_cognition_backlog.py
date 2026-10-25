"""Cross-round convergence and counters; offline fixtures, not model accuracy."""
import json
import unittest
from unittest.mock import patch

from tests import test_initialization as fixtures
from zest.cognition_store import CognitionStore
from zest.cognition_repair import prepare_repair, repair_context, check_repair_context
from zest.initialization import InitializationStore
from zest.files import FileReadError


class BacklogTest(unittest.TestCase):
    setUp = fixtures.InitializationTest.setUp
    setUpBase = fixtures.InitializationTest.setUpBase
    git = fixtures.InitializationTest.git
    commit = fixtures.InitializationTest.commit
    prepare = fixtures.InitializationTest.prepare
    approve = fixtures.InitializationTest.approve
    finish = fixtures.InitializationTest.finish

    def supplement(self):
        ready = self.store.repair(self.project['id'])
        with patch.object(InitializationStore, 'launch'):
            self.store.decide(self.project['id'], approved=True, sha256=ready['research']['approval_sha256'])
        return self.store.run(self.project['id'], model=self.model, check_updates=False)

    def test_two_targeted_rounds_close_original_candidates_and_stop_without_new_calls(self):
        self.model.reject = True
        initial = self.finish()
        self.assertEqual(initial['summary']['repairable_items'], 6)
        self.assertGreater(len(initial['research']['findings']), 6)  # Duplicate followup candidates coalesce.
        self.model.reject = False
        first = self.supplement()
        self.assertEqual(first['summary']['repairable_items'], 2, first.get('research', {}).get('error'))
        self.assertEqual(first['summary']['pending_total'], 2)
        self.assertEqual(first['summary']['repair_progress'], {'processed': 4, 'resolved': 4, 'remaining': 0})
        self.assertFalse(any(c['actor'] == 'coordinator' for c in CognitionStore(self.state_dir).show(first['run_id'], include_calls=True)['calls']))
        second = self.supplement()
        self.assertEqual(second['summary']['pending_total'], 0)
        self.assertEqual(second['summary']['repairable_items'], 0)
        self.assertEqual(second['summary']['readiness'], 'ready')
        count = len(self.model.calls)
        no_work = self.store.repair(self.project['id'])
        self.assertEqual(no_work['run_id'], second['run_id'])
        self.assertEqual(len(self.model.calls), count)
        # History remains intact rather than rewriting the old failed reviews.
        old = CognitionStore(self.state_dir).show(initial['run_id'])
        self.assertTrue(all(f['status'] == 'needs_investigation' for f in old['findings']))

    def test_unsolved_targeted_rounds_do_not_grow_or_repeat_the_same_queue(self):
        self.model.reject = True
        initial = self.finish()
        original = self.model.complete
        def still_unknown(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            if purpose in ('project_research', 'project_report'):
                value = json.loads(result['text'])
                if value.get('report'):
                    value['report'].update(status='partial', unknowns=['针对本次事项仍需确认，表述与旧问题不同。'])
                    result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=still_unknown):
            first = self.supplement()
            self.assertEqual(first['summary']['pending_total'], 6)
            self.assertEqual(first['summary']['backlog']['attempted'], 4)
            self.assertEqual(first['summary']['repair_progress'], {'processed': 4, 'resolved': 0, 'remaining': 4})
            self.assertEqual(first['summary']['repairable_items'], 2)
            second = self.supplement()
        self.assertEqual(second['summary']['pending_total'], 6)
        self.assertEqual(second['summary']['repairable_items'], 0)
        self.assertEqual(second['summary']['backlog']['attempted'], 6)
        count = len(self.model.calls)
        self.assertEqual(self.store.repair(self.project['id'])['run_id'], second['run_id'])
        self.assertEqual(len(self.model.calls), count)

    def test_plan_and_report_gaps_have_one_counter_and_no_blanket_retry(self):
        original = self.model.complete
        def gaps(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            value = json.loads(result['text'])
            if purpose == 'project_plan':
                value['gaps'] = [{'section': 'development', 'question': '需要团队说明线上部署状态。'}]
            if purpose in ('project_research', 'project_report') and value.get('report'):
                value['report'].update(status='partial', unknowns=['需要团队说明线上部署状态。'])
            result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=gaps):
            result = self.finish()
        self.assertEqual(result['summary']['pending_items'], 1)
        self.assertEqual(result['summary']['pending_total'], 1)
        self.assertEqual(result['summary']['unresolved_gaps'], 1)
        self.assertEqual(result['summary']['repairable_items'], 0)
        index = (self.repo / '.zest/docs/initialization/index.md').read_text()
        self.assertIn('待补充知识记录 1 项', index)
        count = len(self.model.calls)
        self.assertEqual(self.store.repair(self.project['id'])['run_id'], result['run_id'])
        self.assertEqual(len(self.model.calls), count)

    def test_legacy_packages_keep_their_original_context_and_pending_semantics(self):
        initial = self.finish()
        store = CognitionStore(self.state_dir)
        context = repair_context(store, initial['run_id'])
        source = store.show(initial['run_id'], include_envelope=True)
        ready = store.create(self.project['id'], survey_id=source['envelope']['survey_id'],
                             target=source['target'], repair_context=context)
        envelope = store.show(ready['id'], include_envelope=True)['envelope']
        check_repair_context(store, envelope)
        self.assertEqual(envelope['repair_context']['version'], 1)
        self.assertEqual(len(envelope['repair_context']['leads']), 6)

    def test_ancestor_change_invalidates_targeted_scope_without_replaying(self):
        self.model.reject = True
        initial = self.finish()
        ready = self.store.repair(self.project['id'])
        store = CognitionStore(self.state_dir)
        envelope = store.show(ready['run_id'], include_envelope=True)['envelope']
        selected = envelope['repair_context']['selected_issues'][0]['references'][0]
        store.invalidate(selected['run_id'], selected['finding_id'], '离线模拟历史来源发生变化。')
        with self.assertRaises(FileReadError) as caught:
            check_repair_context(store, envelope)
        self.assertEqual(caught.exception.code, 'cognition_repair_source_changed')

    def test_local_review_repair_identity_can_use_the_existing_exact_context_api(self):
        from zest.web_commands import validate_command
        value = validate_command('initialization.context-decide-many', {'project_id': self.project['id'],
            'approved': True, 'requests': [{'actor': 'review_fix_' + 'a' * 12, 'sequence': 0, 'sha256': 'b' * 64}]})
        self.assertEqual(value['requests'][0]['actor'], 'review_fix_' + 'a' * 12)
        with self.assertRaises(FileReadError):
            validate_command('initialization.context-decide-many', {'project_id': self.project['id'],
                'approved': True, 'requests': [{'actor': 'arbitrary', 'sequence': 0, 'sha256': 'b' * 64}]})


if __name__ == '__main__':
    unittest.main()
