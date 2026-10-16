"""Offline acceptance gates using independent properties, not model accuracy."""
from copy import deepcopy
import unittest

from tests import test_cognition as fixtures
from zest.cognition_context import review_payload, review_unknowns
from zest.cognition_review import validate_judgments
from zest.files import FileReadError


def payload(statement='使用内存库；生命周期为 session。'):
    proof = {'text': '@pytest.fixture\ndef connection(): ...', 'evidence': {'path': 'tests/conftest.py'}}
    return review_payload('fixed', [{'id': 'T1:F1', 'finding': {
        'statement': statement, 'kind': 'observed', 'evidence': [proof]}}],
        [{'finding_id': 'T2:F1', 'statement': '生命周期为 function。', 'kind': 'observed'}],
        [{'reference_id': 'T1:U1', 'statement': '无 scope 参数（函数级默认）。'}])


def judgment(p):
    return fixtures.review_judgment(p['findings'][0])


class AttributeReviewTest(unittest.TestCase):
    def test_mixed_composite_is_blocked_and_correct_subclaim_is_retained(self):
        p = payload()
        j = judgment(p)
        first = deepcopy(j['checks'][0]); first.update(attribute='存储', assertion='内存库')
        second = deepcopy(first); second.update(attribute='生命周期', assertion='session', verdict='contradicted')
        j['checks'] = [first, second]
        result = validate_judgments([j], p)[0]
        self.assertFalse(result['supported'])
        self.assertEqual(result['checks'][0]['verdict'], 'supported')

    def test_three_counterexamples_and_corrected_controls(self):
        cases = [('fixture 生命周期为 session。', 'contradicted'),
                 ('空金额走 Decimal 解析错误。', 'contradicted'),
                 ('testpaths 无条件限定收集目录。', 'insufficient_evidence'),
                 ('fixture 装饰器未指定 scope。', 'supported'),
                 ('空金额先报 required。', 'supported'),
                 ('配置声明 testpaths=[tests]。', 'supported')]
        for statement, verdict in cases:
            with self.subTest(statement=statement):
                p = payload(statement); j = judgment(p); j['checks'][0]['verdict'] = verdict
                self.assertEqual(validate_judgments([j], p)[0]['supported'], verdict == 'supported')

    def test_incomplete_semantic_split_or_inference_is_not_accepted(self):
        for kind, complete in [('observed', False), ('inferred', True)]:
            p = payload(); p['findings'][0]['kind'] = kind
            j = judgment(p); j['coverage_complete'] = complete
            self.assertFalse(validate_judgments([j], p)[0]['supported'])

    def test_inspection_completeness_and_evidence_support_are_independent(self):
        p = payload('配送查询支持地区、承运人、开始日期和结束日期过滤。')
        p['evidence']['P1']['text'] = 'if region: filters.append(("region", region))'
        j = judgment(p)
        j['checks'] = [{**deepcopy(j['checks'][0]), 'subject': '配送查询', 'attribute': attribute,
                        'assertion': f'支持{attribute}过滤', 'verdict': verdict,
                        'reason': '显式标注的门控夹具，不是模型判断。'}
                       for attribute, verdict in [('地区', 'supported'), ('承运人', 'insufficient_evidence'),
                                                   ('开始日期', 'insufficient_evidence'), ('结束日期', 'insufficient_evidence')]]
        for complete, verdicts, accepted in (
                (True, ['supported', *(['insufficient_evidence'] * 3)], False),
                (True, ['supported', 'contradicted', 'supported', 'supported'], False),
                (False, ['supported'] * 4, False),
                (True, ['supported'] * 4, True)):
            with self.subTest(complete=complete, verdicts=verdicts):
                j['coverage_complete'] = complete
                for check, verdict in zip(j['checks'], verdicts):
                    check['verdict'] = verdict
                result = validate_judgments([j], p)[0]
                self.assertEqual(result['coverage_complete'], complete)
                self.assertEqual([c['verdict'] for c in result['checks']], verdicts)
                self.assertEqual(result['supported'], accepted)

    def test_old_offsets_and_borrowed_evidence_are_rejected(self):
        for change in ({'start': True}, {'start': 1}, {'end': 2}, {'end': 1000},
                       {'evidence_ids': ['P999']}, {'evidence_ids': []}):
            p = payload(); j = judgment(p); j['checks'][0].update(change)
            with self.subTest(change=change), self.assertRaises(FileReadError):
                validate_judgments([j], p)

    def test_unknown_hint_is_located_visible_and_conflict_blocks_acceptance(self):
        hints = review_unknowns([{'id': 'T1', 'result': {'report': {'unknowns': ['无 scope 参数。']}}}])
        self.assertEqual(hints[0]['reference_id'], 'T1:U1')
        p = payload(); j = judgment(p)
        j['conflicts'] = [{'reference_id': 'T1:U1', 'reason': '同一生命周期的陈述不一致。'}]
        self.assertFalse(validate_judgments([j], p)[0]['supported'])
        for ref in ('T1:F1', 'missing'):
            j['conflicts'][0]['reference_id'] = ref
            with self.assertRaises(FileReadError): validate_judgments([j], p)

    def test_missing_duplicate_and_legacy_boolean_judgments_are_rejected(self):
        p = payload(); j = judgment(p)
        for values in ([], [j, j], [{'finding_id': 'T1:F1', 'supported': True, 'reason': '大致支持'}]):
            with self.subTest(values=values), self.assertRaises(FileReadError): validate_judgments(values, p)


class ConflictStorageTest(unittest.TestCase):
    setUp = fixtures.CognitionTest.setUp
    setUpBase = fixtures.CognitionTest.setUpBase
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def test_later_batch_conflict_blocks_both_sides_and_survives_later_review(self):
        run = self.approved()['id']
        result = self.engine.run(run)
        findings = result['findings']
        a, b = findings[:2]
        p = review_payload(result['commit'], [a], [{'finding_id': b['id'], 'statement': b['finding']['statement'], 'kind': 'observed'}])
        j = judgment(p); j['conflicts'] = [{'reference_id': b['id'], 'reason': '离线注入同条件下的相反断言。'}]
        self.store.save_review(run, validate_judgments([j], p))
        result = self.store.show(run)
        self.assertTrue(all(f['status'] == 'needs_investigation' for f in result['findings'][:2]))
        b = result['findings'][1]
        p = review_payload(result['commit'], [b], [])
        self.store.save_review(run, validate_judgments([judgment(p)], p))
        self.assertEqual(self.store.show(run)['findings'][1]['status'], 'needs_investigation')
        self.assertNotIn(b['id'], [f['id'] for f in self.store.for_context(self.project['id'], result['commit'])])
