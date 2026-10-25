"""Offline workflow contracts; these fixtures do not measure model accuracy."""
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_initialization as fixtures
from tests.test_cognition import review_judgment
from zest.cognition import CognitionEngine
from zest.cognition_context import review_payload
from zest.cognition_review import validate_judgments
from zest.cognition_store import CognitionStore
from zest.cognition_workflow import WORKFLOW_CONTRACT
from zest.files import FileReadError
from zest.resources import record_state


class WorkflowTest(unittest.TestCase):
    setUp = fixtures.InitializationTest.setUp
    setUpBase = fixtures.InitializationTest.setUpBase
    git = fixtures.InitializationTest.git
    commit = fixtures.InitializationTest.commit
    prepare = fixtures.InitializationTest.prepare
    approve = fixtures.InitializationTest.approve
    finish = fixtures.InitializationTest.finish

    def test_format_repair_uses_saved_text_once_without_source_reinvestigation(self):
        original = self.model.complete
        corrupted = False
        def complete(payload, *, purpose='qa'):
            nonlocal corrupted
            result = original(payload, purpose=purpose)
            if purpose == 'project_review' and not corrupted:
                corrupted = True
                value = json.loads(result['text'])
                value['judgments'][0]['checks'][0]['evidence_ids'] = ['foreign']
                result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=complete):
            result = self.finish()
        self.assertEqual(result['status'], 'completed', result['research'].get('error'))
        self.assertEqual(len(result['research']['jobs']), 2)
        fixes = [p for kind, p in self.model.calls if 'local_review_repair' in p]
        self.assertEqual(len(fixes), 1)
        self.assertEqual(len(fixes[0]['findings']), 1)
        self.assertEqual(sum(kind in ('project_research', 'project_report') for kind, _ in self.model.calls), 4)
        self.assertEqual(result['summary']['readiness'], 'ready')
        self.assertEqual(result['research']['workflow']['source_repair_rounds'], 0)

    def test_persistent_bad_citations_finish_without_reinvestigation_or_false_facts(self):
        original = self.model.complete
        def complete(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            if purpose == 'project_review':
                value = json.loads(result['text'])
                for j in value['judgments']:
                    j['checks'][0]['evidence_ids'] = ['foreign']
                result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=complete):
            result = self.finish()
        self.assertEqual(result['stage'], 'published')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['summary']['readiness'], 'unavailable')
        self.assertEqual(result['summary']['reviewed_findings'], 0)
        self.assertEqual(len(result['research']['jobs']), 2)
        self.assertEqual(sum('local_review_repair' in p for _, p in self.model.calls), 1)
        count = len(self.model.calls)
        self.store.run(self.project['id'], model=self.model)
        self.assertEqual(len(self.model.calls), count)

    def test_crash_after_local_repair_receipt_reuses_the_saved_response(self):
        original = self.model.complete
        corrupted = False
        def complete(payload, *, purpose='qa'):
            nonlocal corrupted
            result = original(payload, purpose=purpose)
            if purpose == 'project_review' and not corrupted:
                corrupted = True
                value = json.loads(result['text'])
                value['judgments'][0]['checks'][0]['evidence_ids'] = ['foreign']
                result['text'] = json.dumps(value)
            return result
        ready = self.approve()
        with patch.object(self.model, 'complete', side_effect=complete):
            with patch.object(CognitionStore, 'save_review', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                CognitionEngine(self.state_dir, model=self.model).run(ready['run_id'])
            store = CognitionStore(self.state_dir)
            self.assertEqual(store.show(ready['run_id'])['status'], 'reviewing')
            store.recover(ready['run_id'])
            result = CognitionEngine(self.state_dir, model=self.model).run(ready['run_id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(sum('local_review_repair' in p for _, p in self.model.calls), 1)
        self.assertEqual(len(result['findings']), 6)

    def test_ambiguous_identity_response_is_not_accepted_until_exact_local_review(self):
        original = self.model.complete
        corrupted = False
        def complete(payload, *, purpose='qa'):
            nonlocal corrupted
            result = original(payload, purpose=purpose)
            if purpose == 'project_review' and not corrupted:
                corrupted = True
                result['text'] = json.dumps({'judgments': []})
            return result
        with patch.object(self.model, 'complete', side_effect=complete):
            result = self.finish()
        self.assertEqual(result['status'], 'completed')
        repair = next(p for _, p in self.model.calls if 'local_review_repair' in p)
        self.assertEqual(len(repair['findings']), 2)
        self.assertEqual(result['summary']['reviewed_findings'], 6)

    def test_known_invalid_json_in_local_repair_is_bounded_and_does_not_block_valid_peers(self):
        import httpx
        from tests.test_model_transport import reply
        from zest.models import DeepSeekModel
        sent = []
        def handle(request):
            sent.append(request)
            return reply(json.loads(request.content), {}, argument_text='{} }')
        client = self.enterContext(httpx.Client(transport=httpx.MockTransport(handle)))
        sdk = DeepSeekModel(self.model.settings, http_client=client)
        original = self.model.complete
        corrupted = False
        def complete(payload, *, purpose='qa'):
            nonlocal corrupted
            if 'local_review_repair' in payload:
                return sdk.complete(payload, purpose=purpose)
            result = original(payload, purpose=purpose)
            if purpose == 'project_review' and not corrupted:
                corrupted = True
                value = json.loads(result['text'])
                value['judgments'][0]['checks'][0]['evidence_ids'] = ['foreign']
                result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=complete):
            result = self.finish()
        self.assertEqual(result['stage'], 'published', result.get('error'))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(sent), 2)  # One known provider-format retry, no third attempt.
        self.assertEqual(result['summary']['reviewed_findings'], 5)
        self.assertEqual(len(result['research']['jobs']), 2)
        failures = result['research']['review_failures']
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0]['actor'].startswith('review_fix_'))

    def partial_model(self, *, conflict=False, inferred=False, external=False):
        original = self.model.complete
        def complete(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            if purpose in ('project_research', 'project_report'):
                value = json.loads(result['text'])
                if value.get('report'):
                    if payload['task'].get('followup_finding'):
                        value['report'].update(status='partial', findings=[], unknowns=['仍缺少相关实现依据。'])
                    else:
                        for f in value['report']['findings']:
                            if f['section'] == 'flows':
                                f.update(statement='add 返回 a+b；其结果已部署到线上。' if external else 'add 返回 a+b；后续会写入审计日志。', kind='inferred' if inferred else 'observed')
                    result['text'] = json.dumps(value)
            elif purpose == 'project_review':
                value = json.loads(result['text'])
                for j, f in zip(value['judgments'], payload['findings']):
                    if f['section'] != 'flows':
                        continue
                    positive = copy.deepcopy(j['checks'][0])
                    positive.update(subject='add', attribute='返回值', condition='调用 add(a,b) 时', assertion='返回 a+b')
                    missing = {**positive, 'attribute': '线上状态' if external else '后续处理',
                               'assertion': '结果已部署到线上' if external else '写入审计日志',
                               'verdict': 'insufficient_evidence',
                               'reason': '[外部资料] 仓库没有部署记录。' if external else '[补读源码] 需要读取后续处理实现。'}
                    j.update(checks=[positive, missing], reason='返回表达式有依据，线上状态未知。')
                    if conflict:
                        peer = next(p for p in payload['other_findings'] if p['kind'] == 'observed')
                        j['conflicts'] = [{'reference_id': peer['finding_id'], 'reason': '离线注入的矛盾。'}]
                result['text'] = json.dumps(value)
            return result
        return complete

    def test_supported_parts_are_usable_and_only_missing_attributes_get_one_source_pass(self):
        with patch.object(self.model, 'complete', side_effect=self.partial_model()):
            result = self.finish()
        self.assertEqual(result['stage'], 'published', result.get('error'))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['summary']['readiness'], 'ready')
        self.assertEqual(result['summary']['knowledge_status'], 'has_gaps')
        source_jobs = [j for j in result['research']['jobs'] if j['spec'].get('followup_finding')]
        self.assertEqual(len(source_jobs), 1)
        self.assertEqual(source_jobs[0]['spec']['sections'], ['flows'])
        self.assertNotIn('返回 a+b', source_jobs[0]['spec']['goal'])
        self.assertIn('后续处理', source_jobs[0]['spec']['goal'])
        self.assertEqual(result['research']['workflow']['source_repair_rounds'], 1)
        store = CognitionStore(self.state_dir)
        dossier = store.dossier(result['run_id'])
        flows = next(s for s in dossier['sections'] if s['id'] == 'flows')
        self.assertEqual(len(flows['facts']), 1)
        fact = flows['facts'][0]
        self.assertIn('调用 add(a,b) 时', fact['finding']['statement'])
        self.assertNotIn('部署', fact['finding']['statement'])
        self.assertEqual(store.reviewed_finding(result['run_id'], fact['id'])['body'], fact['finding']['statement'])
        pending = next(p for p in flows['pending'] if p.get('supported_parts_published'))
        self.assertEqual(len(pending['remaining_statements']), 1)
        api = (self.repo / '.zest/docs/initialization/api.md').read_text()
        self.assertIn('独立核对通过的部分', api)
        self.assertIn('其余部分待补充', api)
        self.assertLessEqual(result['research']['call_count'], result['research']['policy']['max_model_calls'])
        # Re-publication reuses the ledger and does not duplicate derived parts.
        count = len(self.model.calls)
        self.store.republish(self.project['id'])
        self.assertEqual(len(store.dossier(result['run_id'])['sections'][2]['facts']), 1)
        self.assertEqual(len(self.model.calls), count)

    def test_inferences_never_publish_supported_parts(self):
        with patch.object(self.model, 'complete', side_effect=self.partial_model(inferred=True)):
            result = self.finish()
        self.assertFalse([f for f in result['research']['findings'] if f['finding'].get('partial_origin')])

    def test_conflicted_parents_never_publish_supported_parts(self):
        with patch.object(self.model, 'complete', side_effect=self.partial_model(conflict=True)):
            result = self.finish()
        self.assertFalse([f for f in result['research']['findings'] if f['finding'].get('partial_origin')])

    def test_external_unknowns_are_retained_without_wasting_a_source_repair_pass(self):
        with patch.object(self.model, 'complete', side_effect=self.partial_model(external=True)):
            result = self.finish()
        self.assertEqual(result['summary']['readiness'], 'ready')
        self.assertEqual(result['research']['workflow']['source_repair_rounds'], 0)
        self.assertEqual(len(result['research']['jobs']), 2)
        self.assertIn('external_material', [c['category'] for c in result['summary']['pending_categories']])

    def test_parent_invalidation_or_later_conflict_quarantines_its_parts(self):
        with patch.object(self.model, 'complete', side_effect=self.partial_model()):
            result = self.finish()
        store = CognitionStore(record_state(self.state_dir, 'cognition', result['run_id']))
        child = next(f for f in result['research']['findings'] if f['finding'].get('partial_origin'))
        parent_id = child['finding']['partial_origin']['finding_id']
        store.invalidate(result['run_id'], parent_id, '离线回归：来源判断失效。')
        with self.assertRaises(FileReadError):
            store.reviewed_finding(result['run_id'], child['id'])
        self.assertNotIn(child['id'], [f['id'] for f in store.for_context(self.project['id'], result['commit'], query='add')])

    def test_later_peer_conflict_with_parent_also_blocks_already_published_parts(self):
        with patch.object(self.model, 'complete', side_effect=self.partial_model()):
            result = self.finish()
        store = CognitionStore(record_state(self.state_dir, 'cognition', result['run_id']))
        child = next(f for f in result['research']['findings'] if f['finding'].get('partial_origin'))
        parent_id = child['finding']['partial_origin']['finding_id']
        peer = next(f for f in result['research']['findings'] if f['status'] == 'auto_reviewed' and f['id'] != child['id'])
        parent = next(f for f in result['research']['findings'] if f['id'] == parent_id)
        payload = review_payload(result['commit'], [peer], [{'finding_id': parent_id, **parent['finding']}])
        judgment = review_judgment(payload['findings'][0])
        judgment['conflicts'] = [{'reference_id': parent_id, 'reason': '离线模拟稍后批次发现相反陈述。'}]
        store.save_review(result['run_id'], validate_judgments([judgment], payload))
        with self.assertRaises(FileReadError):
            store.reviewed_finding(result['run_id'], child['id'])

    def test_unknown_local_repair_response_is_never_replayed(self):
        original = self.model.complete
        def complete(payload, *, purpose='qa'):
            if 'local_review_repair' in payload:
                raise TimeoutError('offline simulated lost response')
            result = original(payload, purpose=purpose)
            if purpose == 'project_review':
                value = json.loads(result['text'])
                value['judgments'][0]['checks'][0]['evidence_ids'] = ['foreign']
                result['text'] = json.dumps(value)
            return result
        with patch.object(self.model, 'complete', side_effect=complete):
            result = self.finish()
        self.assertEqual(result['status'], 'pending_execution')
        count = len(self.model.calls)
        self.store.begin(self.project['id'])
        self.assertEqual(len(self.model.calls), count)
        with self.assertRaises(FileReadError):
            CognitionStore(self.state_dir).recover(result['run_id'])

    def test_new_contract_is_frozen_without_rewriting_historical_packages(self):
        ready = self.prepare()
        store = CognitionStore(self.state_dir)
        envelope = store.show(ready['run_id'], include_envelope=True)['envelope']
        self.assertEqual(envelope['workflow_contract'], WORKFLOW_CONTRACT)
        old = store.create(self.project['id'], target=ready['research']['target'])
        self.assertNotIn('workflow_contract', store.show(old['id'], include_envelope=True)['envelope'])


if __name__ == '__main__':
    unittest.main()
