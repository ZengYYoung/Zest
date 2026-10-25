"""Offline page lifecycle tests; these do not measure real model writing quality."""
import copy
import hashlib
import json
from unittest.mock import patch
import unittest

from tests import test_initialization as fixtures
from zest.cognition import CognitionEngine
from zest.cognition_store import CognitionStore, digest
from zest.document_pages import DocumentPages, facts_for, validate_page, validate_plan, validate_review
from zest.document_contracts import document_contract
from zest.files import FileReadError
from zest.initialization import InitializationStore


class DocumentPagesTest(unittest.TestCase):
    setUp = fixtures.InitializationTest.setUp
    setUpBase = fixtures.InitializationTest.setUpBase
    git = fixtures.InitializationTest.git
    commit = fixtures.InitializationTest.commit
    prepare = fixtures.InitializationTest.prepare
    approve = fixtures.InitializationTest.approve
    finish = fixtures.InitializationTest.finish

    def wrap(self, transform):
        original = self.model.complete
        def complete(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            value = transform(purpose, payload, json.loads(result['text']))
            return {**result, 'text': json.dumps(value, ensure_ascii=False)}
        return patch.object(self.model, 'complete', side_effect=complete)

    def test_human_index_is_preserved_with_a_readable_owned_candidate(self):
        self.approve()
        index=self.repo/'.zest/docs/initialization/index.md'
        index.parent.mkdir(parents=True,exist_ok=True)
        index.write_text('# Team index\nPreserve this edited directory.\n')
        result=self.store.run(self.project['id'],model=self.model)
        self.assertEqual(index.read_text(),'# Team index\nPreserve this edited directory.\n')
        candidate=self.repo/result['document_generation']['index_candidate']
        self.assertTrue(candidate.is_file())
        self.assertIn('(../../',candidate.read_text())
        count=len(self.model.calls)
        candidate.write_text('# Human edited candidate\n')
        self.store.republish(self.project['id'])
        self.assertEqual(candidate.read_text(),'# Human edited candidate\n')
        self.assertEqual(len(self.model.calls),count)

    def test_topic_plan_is_not_six_fixed_pages_and_persists_review_links(self):
        def themes(purpose, payload, value):
            if purpose == 'document_plan':
                ids = [f['id'] for f in payload['findings']]
                value = {'pages': [dict(slug=slug, title=title, description='理解加法实现及其使用边界。',
                                       goal='理解模块如何协作。', finding_ids=part, related_pages=[other])
                                   for slug, title, part, other in [('plan', '加法的实现与协作', ids[:3], 'usage'),
                                                                    ('usage', '如何使用加法', ids[3:], 'plan')]],
                         'coverage_gaps': ['没有运行环境的实际验证记录。']}
            elif purpose == 'document_write':
                value['blocks'][0]['markdown'] = ('加法通过一个明确的返回表达式完成计算，调用者可以由此理解实现边界。\n\n'
                                                  '| 模块 | 行为 |\n| --- | --- |\n| `add` | 返回 `a + b` |')
            return value
        with self.wrap(themes):
            result = self.finish()
        self.assertEqual(result['stage'], 'published')
        self.assertEqual(result['summary']['documents'], 3)
        self.assertEqual(result['document_generation']['completed'], 2)
        directory = self.repo / '.zest/docs/initialization'
        page = (directory / 'plan.md').read_text()
        self.assertTrue(page.startswith('---\ntitle: "加法的实现与协作"'))
        self.assertIn('| 模块 | 行为 |', page)
        self.assertNotIn('## 自动核对的发现', page)
        self.assertIn('[如何使用加法](usage.md)', page)
        index = (directory / 'index.md').read_text()
        self.assertIn('plan.md', index)
        self.assertNotIn('(architecture.md)', index)
        self.assertIn('没有运行环境', index)
        ledger = DocumentPages(self.state_dir, result['run_id'])
        facts = facts_for(CognitionStore(self.state_dir).dossier(result['run_id']))
        self.assertEqual(ledger.value['facts_sha256'], digest(facts))
        self.assertTrue(all(p['review']['checks'][0]['verdict'] == 'supported' for p in ledger.value['pages']))
        self.assertEqual(len(ledger.value['pages'][0]['content']['blocks'][0]['finding_ids']), 3)
        from zest.documents import workspace_documents
        catalog = workspace_documents(self.project)['documents']
        self.assertEqual(next(d['title'] for d in catalog if d['path'].endswith('plan.md')), '加法的实现与协作')

    def test_one_semantically_rejected_page_keeps_old_text_and_other_pages_publish(self):
        self.approve()
        old = self.repo / '.zest/docs/initialization/api.md'
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text('# Team contract\nPreserve me.\n')
        def reject(purpose, payload, value):
            if purpose == 'document_review' and payload['page']['slug'] == 'api':
                value['checks'][0].update(verdict='insufficient_evidence', reason='正文扩大了调用结果。')
            return value
        with self.wrap(reject):
            result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['document_generation']['completed'], 5)
        self.assertEqual(result['document_generation']['failed'], 1)
        self.assertEqual(old.read_text(), '# Team contract\nPreserve me.\n')
        self.assertIn('保留已有版本；本轮页面尚未更新', (old.parent / 'index.md').read_text())
        self.assertEqual(result['summary']['pending_items'], 0, 'page execution failures are not knowledge gaps')
        before = len(self.model.calls)
        self.store.republish(self.project['id'])
        self.assertEqual(len(self.model.calls), before)

    def test_unknown_writer_is_not_replayed_by_status_republish_or_recovery(self):
        original = self.model.complete
        def uncertain(payload, *, purpose='qa'):
            if purpose == 'document_write' and payload['page']['slug'] == 'api':
                self.model.calls.append((purpose, payload))
                raise FileReadError('model_request_failed', 'fixture unknown')
            return original(payload, purpose=purpose)
        with patch.object(self.model, 'complete', side_effect=uncertain):
            result = self.finish()
        self.assertEqual(result['document_generation']['unknown'], 1)
        self.assertEqual(result['document_generation']['completed'], 5)
        before = len(self.model.calls)
        self.store.show(self.project['id'])
        self.store.republish(self.project['id'])
        with patch.object(InitializationStore, 'launch'):
            self.store.recover(self.project['id'])
        resumed = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(resumed['document_generation']['unknown'], 1)
        self.assertEqual(len(self.model.calls), before)
        calls = CognitionStore(self.state_dir).show(result['run_id'], include_calls=True)['calls']
        self.assertEqual(sum(c['status'] == 'uncertain' for c in calls), 1)

    def test_crash_after_provider_receipt_reuses_writer_before_continuing(self):
        from zest.document_pages import validate_page as original
        stopped = False
        def crash(value, spec, contract):
            nonlocal stopped
            if not stopped:
                stopped = True
                raise RuntimeError('fixture process interruption after saved response')
            return original(value, spec, contract)
        self.approve()
        with patch('zest.document_pages.validate_page', side_effect=crash):
            first = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(first['status'], 'publication_failed')
        writers = [p for purpose, p in self.model.calls if purpose == 'document_write']
        self.assertEqual(len(writers), 1)
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed')
        writers = [p['page']['slug'] for purpose, p in self.model.calls if purpose == 'document_write']
        self.assertEqual(len(writers), 6)
        self.assertEqual(len(set(writers)), 6)

    def test_disk_failure_is_per_page_and_recovers_without_model_calls(self):
        from zest.documents import _create
        def broken(root, path, raw):
            if path == '.zest/docs/initialization/api.md':
                raise OSError('fixture no disk space for this page')
            return _create(root, path, raw)
        self.approve()
        with patch('zest.documents._create', side_effect=broken):
            first = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(first['document_generation']['completed'], 5)
        self.assertEqual(first['document_generation']['failed'], 1)
        calls = len(self.model.calls)
        with patch.object(InitializationStore, 'launch'):
            self.store.recover(self.project['id'])
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['document_generation']['completed'], 6)
        self.assertEqual(len(self.model.calls), calls)

    def test_human_edits_produce_candidate_and_survive_local_republication(self):
        self.approve()
        destination = self.repo / '.zest/docs/initialization/api.md'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text('# Human API\nKeep this contract.\n')
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['document_generation']['skipped'], 1)
        self.assertIn(str(destination.relative_to(self.repo)), result['conflicts'])
        candidate = self.repo / next(p['candidate'] for p in result['document_generation']['pages'] if p['slug'] == 'api')
        self.assertIn('[文档总览](../../index.md)', candidate.read_text())
        candidate.write_text(candidate.read_text() + '\nCandidate edited by human.\n')
        original = destination.read_text()
        count = len(self.model.calls)
        again = self.store.republish(self.project['id'])
        self.assertIn(str(candidate.relative_to(self.repo)), again['conflicts'])
        self.assertIn('Candidate edited by human.', candidate.read_text())
        self.assertEqual(destination.read_text(), original)
        self.assertEqual(len(self.model.calls), count)

    def test_page_calls_share_total_budget_and_raw_request_ledger(self):
        result = self.finish()
        from zest.resources import record_state
        bound = CognitionStore(record_state(self.state_dir, 'cognition', result['run_id']))
        record = bound.show(result['run_id'], include_calls=True)
        self.assertEqual(record['call_count'], len(self.model.calls))
        self.assertLessEqual(record['call_count'], 32)
        with bound.open() as conn:
            rows = conn.execute("SELECT request FROM cognition_request_bodies WHERE run_id=? AND actor LIKE 'document%'", (result['run_id'],)).fetchall()
        self.assertEqual(len(rows), 13)
        for row in rows:
            request = json.loads(row['request'])
            self.assertLessEqual(request['chars'], 48000)
            self.assertEqual(request['body_sha256'], hashlib.sha256(request['body_text'].encode()).hexdigest())
            self.assertNotIn(self.model.settings.api_key, request['body_text'])

    def test_exhausted_budget_never_gets_separate_page_allowance(self):
        run = self.approve()
        CognitionEngine(self.state_dir, model=self.model).run(run['run_id'])
        from zest.resources import record_state
        bound = CognitionStore(record_state(self.state_dir, 'cognition', run['run_id']))
        with bound.open(write=True) as conn, conn:
            conn.execute('UPDATE cognition_runs SET call_count=32 WHERE id=?', (run['run_id'],))
        before = len(self.model.calls)
        result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['document_generation']['error']['code'], 'cognition_call_budget')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.model.calls), before)

    def test_legacy_envelope_still_uses_local_fact_publisher(self):
        from zest.cognition_store import prepare_envelope
        def legacy(*args, **kwargs):
            envelope = prepare_envelope(*args, **kwargs)
            envelope.pop('document_generation', None)
            return envelope
        with patch('zest.cognition_store.prepare_envelope', side_effect=legacy):
            result = self.finish()
        self.assertEqual(result['summary']['documents'], 7)
        self.assertNotIn('document_generation', result)
        self.assertFalse(any(purpose.startswith('document_') for purpose, _ in self.model.calls))
        self.assertIn('## 自动核对的发现', (self.repo / '.zest/docs/initialization/api.md').read_text())

    def test_contract_validators_reject_unknown_facts_paths_omissions_and_incomplete_review(self):
        contract = document_contract()
        facts = {'F1': {'statement': '已核对事实'}}
        plan = {'pages': [{'slug':'flow', 'title':'流程', 'description':'说明流程。', 'goal':'了解流程。',
                          'finding_ids':['F1'], 'related_pages':[]}], 'coverage_gaps':[]}
        for mutate in (lambda v: v['pages'][0].update(slug='../escape'),
                       lambda v: v['pages'][0].update(finding_ids=['unknown']),
                       lambda v: v['pages'][0].update(related_pages=['missing']),
                       lambda v: v['pages'][0].update(finding_ids=[])):
            bad = copy.deepcopy(plan); mutate(bad)
            with self.assertRaises(FileReadError): validate_plan(bad, facts, contract)
        page = {'blocks':[{'markdown':'这里通过连贯的正文说明模块如何返回已经核对的结果。', 'finding_ids':['F1']}], 'limits':[]}
        validate_page(page, plan['pages'][0], contract)
        example = copy.deepcopy(page)
        example['blocks'][0]['markdown'] += '\n\n```python\n# 模块说明中的示例注释\nreturn a + b\n```'
        validate_page(example, plan['pages'][0], contract)
        for markdown in ('# 改写标题\n\n这里通过连贯的正文说明模块如何返回已经核对的结果。',
                         '正文中的越界链接应该被拒绝。[危险](javascript:alert)',
                         '## 重复\n\n这里通过连贯的正文说明模块如何返回已经核对的结果。\n\n## 重复'):
            bad = copy.deepcopy(page); bad['blocks'][0]['markdown'] = markdown
            with self.assertRaises(FileReadError): validate_page(bad, plan['pages'][0], contract)
        with self.assertRaises(FileReadError): validate_review({'checks':[], 'issues':[]}, page)
        with self.assertRaises(FileReadError): validate_review({'checks':[{'block_id':'B1','verdict':'supported','reason':'支持'}], 'issues':['存在不支持的限制表述。']}, page)

    def test_private_ledger_corruption_stops_local_republish_without_touching_pages(self):
        result = self.finish()
        ledger = DocumentPages(self.state_dir, result['run_id'])
        before = (self.repo / '.zest/docs/initialization/api.md').read_bytes()
        count = len(self.model.calls)
        raw = json.loads(ledger.path.read_bytes())
        raw['payload']['pages'][0]['spec']['slug'] = '../outside'
        ledger.path.write_text(json.dumps(raw))
        with self.assertRaises(FileReadError) as caught:
            self.store.republish(self.project['id'])
        self.assertEqual(caught.exception.code, 'document_ledger_error')
        self.assertEqual((self.repo / '.zest/docs/initialization/api.md').read_bytes(), before)
        self.assertEqual(len(self.model.calls), count)

    def test_invalidated_facts_cannot_be_republished_from_saved_prose(self):
        result = self.finish()
        knowledge = CognitionStore(self.state_dir)
        finding = knowledge.show(result['run_id'])['findings'][0]
        original = (self.repo / '.zest/docs/initialization/architecture.md').read_bytes()
        count = len(self.model.calls)
        knowledge.invalidate(result['run_id'], finding['id'], 'fixture evidence is no longer accepted')
        with self.assertRaises(FileReadError) as caught:
            self.store.republish(self.project['id'])
        self.assertEqual(caught.exception.code, 'document_context_changed')
        self.assertEqual((self.repo / '.zest/docs/initialization/architecture.md').read_bytes(), original)
        self.assertEqual(len(self.model.calls), count)


class IncrementalDocumentPagesTest(unittest.TestCase):
    from tests import test_maintenance as maintenance_fixtures
    setUp = maintenance_fixtures.MaintenanceTest.setUp
    setUpBase = maintenance_fixtures.MaintenanceTest.setUpBase
    git = maintenance_fixtures.MaintenanceTest.git
    commit = maintenance_fixtures.MaintenanceTest.commit
    changed = maintenance_fixtures.MaintenanceTest.changed
    queue = maintenance_fixtures.MaintenanceTest.queue

    def test_refresh_failed_page_keeps_previous_version_and_resumes_without_new_calls(self):
        from zest.initialization import atomic
        from zest.maintenance import MaintenanceStore
        destination = self.repo / '.zest/docs/initialization/api.md'
        previous = destination.read_text()
        commit, _ = self.queue()
        def broken(path, raw):
            if path.name == 'api.md' and path.parent.name == 'initialization':
                raise OSError('fixture page replacement failed')
            return atomic(path, raw)
        with patch('zest.initialization.atomic', side_effect=broken):
            result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'partial', result.get('error'))
        self.assertEqual(result['knowledge_commit'], commit)
        self.assertEqual(result['document_generation']['failed'], 1)
        self.assertEqual(destination.read_text(), previous)
        self.assertIn('保留已有版本；本轮页面尚未更新', (destination.parent / 'index.md').read_text())
        before = len(self.model.calls)
        with patch.object(MaintenanceStore, 'launch'):
            self.store.resume(self.project['id'])
        restored = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(restored['status'], 'completed', restored.get('error'))
        self.assertEqual(restored['document_generation']['completed'], 6)
        self.assertEqual(len(self.model.calls), before)
        self.assertIn(commit, destination.read_text())

    def test_failed_refresh_plan_retains_previous_navigation_and_all_pages(self):
        self.queue()
        directory = self.repo / '.zest/docs/initialization'
        previous = {p.name: p.read_bytes() for p in directory.glob('*.md')}
        original = self.model.complete
        def bad_plan(payload, *, purpose='qa'):
            response = original(payload, purpose=purpose)
            if purpose in ('document_plan', 'document_update'):
                return {**response, 'text': json.dumps({'pages': [], 'coverage_gaps': []})}
            return response
        with patch.object(self.model, 'complete', side_effect=bad_plan):
            result = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['document_generation']['error']['code'], 'invalid_document_output')
        self.assertEqual(previous, {p.name: p.read_bytes() for p in directory.glob('*.md')})


if __name__ == '__main__':
    unittest.main()
