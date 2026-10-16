"""Independent evaluator permissions, accounting, source checks and recovery."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_cognition as fixtures
from zest.cognition_store import canonical, digest
from zest.evaluation import EvaluationStore, format_evaluation, load_criteria
from zest.evaluation_contracts import aggregate, evaluation_units, validate_evaluation
from zest.files import FileReadError
from zest.models import DeepSeekModel


def evaluation_judgment(finding, *, supported=True):
    value = fixtures.review_judgment(finding, supported=supported)
    for check in value['checks']:
        check['evidence_observation'] = '离线人工标注协议夹具，不是模型原文观察或质量评分。'
    return value


class EvaluationFixture:
    """Explicit labelled scorer; these tests are not LLM evaluation quality."""
    def __init__(self):
        self.settings = fixtures.OfflineCognitionModel().settings
        self.calls = []
        self.coverage_verdict = 'full'
        self.semantic_support = True
        self.unknown = False
        self.invalid = False

    def complete(self, payload, *, purpose):
        self.calls.append(deepcopy(payload))
        if self.unknown: raise TimeoutError('offline unknown transport')
        value = {'coverage': [dict(id=r['id'], verdict=self.coverage_verdict,
                    finding_ids=[payload['candidates'][0]['finding_id']] if self.coverage_verdict in ('full', 'partial') else [],
                    candidate_observation='人工标注的候选表达夹具，不是实际模型观察。',
                    evidence_ids=r['evidence_ids'], reason='独立人工标注夹具，非真实评分。') for r in payload['requirements']],
                 'judgments': [evaluation_judgment(f, supported=self.semantic_support) for f in payload['findings']]}
        if self.invalid: value['coverage'].append({'id': 'forged'})
        return {'text': json.dumps(value, ensure_ascii=False), 'reported_model': 'offline-evaluation'}


class EvaluationTest(unittest.TestCase):
    setUpBase = fixtures.CognitionTest.setUpBase
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def setUp(self):
        fixtures.CognitionTest.setUp(self)
        self.source = self.engine.run(self.approved()['id'])
        self.source_full = self.store.show(self.source['id'], include_envelope=True)
        source = next(s for s in self.source_full['envelope']['files'] if s['evidence']['path'] == 'app.py')
        self.criteria = {'version': 1, 'commit': self.source['commit'], 'reference_phase': 'retrospective',
            'items': [{'id': 'G01', 'category': 'facts', 'core': True, 'attributes': [
                {'id': 'return', 'statement': 'add 返回 a + b。', 'sources': [{'source_id': source['id'], 'start_line': 1, 'end_line': 2}]}]}],
            'thresholds': {'facts': 1, 'flows': 0, 'unknowns': 0, 'all_core': True, 'max_flagged_auto_reviewed_findings': 0}}
        self.scorer = EvaluationFixture()
        self.evaluator = EvaluationStore(self.state_dir, model=self.scorer)

    def created(self):
        return self.evaluator.create(self.source['id'], self.criteria)

    def queued(self):
        record = self.created()
        self.evaluator.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
        return record

    def test_prepare_freezes_complete_requests_skill_and_criteria_without_model_calls(self):
        source_before = self.store.db_path.read_bytes()
        record = self.created(); preview = self.evaluator.show(record['id'], preview=True)
        envelope = preview['envelope']
        self.assertEqual(self.scorer.calls, [])
        self.assertEqual(record['status'], 'awaiting_approval')
        self.assertEqual(envelope['criteria_sha256'], digest(self.criteria))
        self.assertIn('通用准则', envelope['skill'])
        self.assertNotIn(self.scorer.settings.api_key, canonical(preview))
        self.assertEqual(len(envelope['batches']), 4)
        self.assertTrue(all(b['request']['chars'] <= 32000 for b in envelope['batches']))
        self.assertEqual(source_before, self.store.db_path.read_bytes())
        self.assertEqual(self.evaluator.db_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileReadError): self.evaluator.run(record['id'])
        with self.assertRaises(FileReadError): self.evaluator.decide(record['id'], approved=True, approval_sha256=self.source['approval_sha256'])

    def test_independent_scoring_aggregates_all_observed_and_never_mutates_source(self):
        record = self.queued(); before = self.store.db_path.read_bytes()
        report = self.evaluator.run(record['id'])
        self.assertEqual(report['workflow_status'], 'completed', report['error'])
        self.assertTrue(report['result']['overall_passed'], report)
        self.assertEqual(report['result']['coverage']['facts'], {'full': 1, 'total': 1})
        self.assertEqual(report['result']['reviewed_observed_findings'], 6)
        self.assertEqual(len(self.scorer.calls), 4)
        self.assertEqual(before, self.store.db_path.read_bytes())
        self.assertIn('不是人工金标准', format_evaluation(report))
        # No evaluator sees the old review opinions or auto_reviewed flags.
        self.assertNotIn('auto_reviewed', canonical(self.scorer.calls))
        self.assertNotIn('原文包含返回表达式', canonical(self.scorer.calls))
        self.assertTrue(list((self.state_dir / 'evaluation-checkpoints').glob('*.sqlite3')))

    def test_false_acceptances_are_not_hidden_by_full_coverage(self):
        self.scorer.semantic_support = False
        result = self.evaluator.run(self.queued()['id'])['result']
        self.assertEqual(result['coverage']['facts']['full'], 1)
        self.assertEqual(len(result['flagged_auto_reviewed_findings']), 6)
        self.assertFalse(result['overall_passed'])
        self.assertEqual(result['untrusted_auto_reviewed_atomic_occurrences'], 6)

    def test_partial_and_invalid_reference_keep_frozen_denominator(self):
        for verdict in ('partial', 'missing', 'reference_unsupported'):
            with self.subTest(verdict=verdict):
                self.scorer.coverage_verdict = verdict
                result = self.evaluator.run(self.queued()['id'])['result']
                self.assertEqual(result['coverage']['facts'], {'full': 0, 'total': 1})
                self.assertFalse(result['overall_passed'])
                self.assertEqual(result['reference_issue'], verdict == 'reference_unsupported')

    def test_invalid_sources_and_references_are_rejected_before_calling(self):
        bad = deepcopy(self.criteria); bad['items'][0]['attributes'][0]['sources'][0]['end_line'] = 999
        with self.assertRaises(FileReadError): self.evaluator.create(self.source['id'], bad)
        bad = deepcopy(self.criteria); bad['commit'] = 'wrong'
        with self.assertRaises(FileReadError): self.evaluator.create(self.source['id'], bad)
        bad = deepcopy(self.criteria); bad['items'].append(deepcopy(bad['items'][0]))
        with self.assertRaises(FileReadError): self.evaluator.create(self.source['id'], bad)
        full = deepcopy(self.source_full); full['findings'][0]['finding']['evidence'][0]['text'] = 'forged'
        with self.assertRaises(FileReadError): evaluation_units(full, self.criteria)
        self.assertEqual(self.scorer.calls, [])

    def test_unknown_transport_stops_and_cannot_be_reissued(self):
        self.scorer.unknown = True; record = self.queued()
        report = self.evaluator.run(record['id'])
        self.assertEqual(report['workflow_status'], 'pending_execution')
        self.assertFalse(report['result']['overall_passed'])
        with self.assertRaises(FileReadError): self.evaluator.recover(record['id'])
        with self.assertRaises(FileReadError): self.evaluator.run(record['id'])
        self.assertEqual(len(self.scorer.calls), 1)

    def test_invalid_output_keeps_receipt_and_is_not_retried(self):
        self.scorer.invalid = True; record = self.queued()
        result = self.evaluator.run(record['id'])
        self.assertEqual(result['workflow_status'], 'partial')
        call = self.evaluator.show(record['id'])['calls'][0]
        self.assertEqual(call['status'], 'invalid'); self.assertIsNotNone(call['receipt'])
        with self.assertRaises(FileReadError): self.evaluator.recover(record['id'])
        self.assertEqual(len(self.scorer.calls), 1)

    def test_saved_response_reused_after_receipt_before_checkpoint_crash(self):
        record = self.queued(); original = self.evaluator._call
        def crash(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(self.evaluator, '_call', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt): self.evaluator.run(record['id'])
        self.assertEqual(len(self.scorer.calls), 1)
        self.evaluator.recover(record['id'])
        result = self.evaluator.run(record['id'])
        self.assertTrue(result['result']['overall_passed'], result)
        self.assertEqual(len(self.scorer.calls), 4)

    def test_changed_target_or_skill_cannot_consume_approval(self):
        record = self.queued()
        self.scorer.settings = replace(self.scorer.settings, name='different-scorer')
        self.assertEqual(self.evaluator.run(record['id'])['error']['code'], 'evaluation_contract_changed')
        self.assertEqual(self.scorer.calls, [])
        self.scorer.settings = fixtures.OfflineCognitionModel().settings
        record = self.queued()
        with patch('zest.evaluation.skill_text', return_value='changed skill'):
            self.assertEqual(self.evaluator.run(record['id'])['error']['code'], 'evaluation_contract_changed')
        self.assertEqual(self.scorer.calls, [])

    def test_budget_or_overlarge_request_stops_preparation_without_sending(self):
        with self.assertRaises(FileReadError): self.evaluator.create(self.source['id'], self.criteria, max_calls=1)
        with patch('zest.evaluation.prepare_model_request', return_value={'chars': 48001}):
            with self.assertRaises(FileReadError): self.created()
        self.assertEqual(self.scorer.calls, [])

    def test_exact_extended_request_list_requires_extra_consent_before_sending(self):
        from zest.models import prepare_model_request
        def extended(*args, **kwargs):
            result = prepare_model_request(*args, **kwargs)
            return {**result, 'chars': 33000}
        with patch('zest.evaluation.prepare_model_request', side_effect=extended):
            record = self.created()
        self.assertTrue(record['extended_requests'])
        with self.assertRaises(FileReadError) as caught:
            self.evaluator.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
        self.assertEqual(caught.exception.code, 'evaluation_context_approval_required')
        self.assertEqual(self.evaluator.show(record['id'])['status'], 'awaiting_approval')
        from contextlib import redirect_stdout
        from io import StringIO
        from zest.cli import main
        with redirect_stdout(StringIO()):
            code = main(['--state-dir', str(self.state_dir), 'evaluation', 'approve', record['id'],
                         '--approval-sha256', record['approval_sha256'], '--allow-extended-requests', '--no-run'])
        self.assertEqual(code, 0)
        self.assertEqual(self.evaluator.show(record['id'])['decision']['allow_extended_requests'], 1)
        self.assertEqual(self.scorer.calls, [])

    def test_rejection_report_and_incomplete_batches_never_pass(self):
        record = self.created(); self.evaluator.decide(record['id'], approved=False)
        self.assertFalse(self.evaluator.report(record['id'])['result']['overall_passed'])
        with self.assertRaises(FileReadError): self.evaluator.run(record['id'])
        env = self.evaluator.show(record['id'], preview=True)['envelope']
        self.assertEqual(aggregate(env, [])['status'], 'partial')

    def test_multiple_required_properties_and_categories_are_not_hardcoded(self):
        second = deepcopy(self.criteria['items'][0]['attributes'][0])
        second.update(id='encoding', statement='明确说明文件编码。')
        self.criteria['items'][0]['attributes'].append(second)
        flow = deepcopy(self.criteria['items'][0]); flow.update(id='F01', category='flows', core=False)
        self.criteria['items'].append(flow)
        self.criteria['thresholds']['flows'] = 1
        record = self.queued(); report = self.evaluator.run(record['id'])
        self.assertEqual(report['result']['coverage']['flows'], {'full': 1, 'total': 1})
        env = self.evaluator.show(record['id'], preview=True)['envelope']
        results = self.evaluator._results(record['id'])
        results[0]['coverage'][1]['verdict'] = 'missing'
        recomputed = aggregate(env, results)
        self.assertEqual(recomputed['coverage']['facts']['full'], 0)
        self.assertFalse(recomputed['overall_passed'])

    def test_new_evaluations_use_project_resources_even_for_legacy_source_runs(self):
        from zest.storage import ProjectRegistry
        from zest.git import inspect_repository
        from zest.resources import record_state
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        before = self.store.db_path.read_bytes()
        record = self.created()
        location = record_state(self.state_dir, 'evaluation', record['id'])
        self.assertEqual(location, Path(project['resource_dir']))
        self.assertNotEqual(location, self.state_dir)
        self.assertEqual(before, self.store.db_path.read_bytes())
        self.evaluator.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
        self.assertTrue(self.evaluator.run(record['id'])['result']['overall_passed'])

    def test_criteria_file_validation_and_cli_readback(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from zest.cli import main
        p = self.state_dir / 'criteria.json'; p.write_text(json.dumps(self.criteria))
        self.assertEqual(load_criteria(p), self.criteria)
        p.write_text('{bad')
        with self.assertRaises(FileReadError): load_criteria(p)
        record = self.created(); out = StringIO()
        with redirect_stdout(out):
            code = main(['--state-dir', str(self.state_dir), 'evaluation', 'preview', record['id']])
        self.assertEqual(code, 0); self.assertIn('skill_sha256', json.loads(out.getvalue()))

    def test_actual_langchain_transport_uses_typed_evaluator_schema_and_frozen_body(self):
        import httpx
        from tests.test_model_transport import reply
        seen = []
        def handle(request):
            body = json.loads(request.content); seen.append(body)
            # Return the fixture's independently labelled values through the SDK.
            payload = json.loads(body['messages'][1]['content'])
            wire = json.loads(self.scorer.complete(payload, purpose='cognition_evaluation')['text'])
            return reply(body, wire)
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            store = EvaluationStore(self.state_dir, model=DeepSeekModel(self.scorer.settings, http_client=client))
            record = store.create(self.source['id'], self.criteria)
            store.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
            report = store.run(record['id'])
        self.assertTrue(report['result']['overall_passed'], report)
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(b['tools'][0]['function']['name'] == 'CognitionEvaluationResponse' and b['tools'][0]['function']['strict'] for b in seen))
        for body in seen:
            check = body['tools'][0]['function']['parameters']['properties']['judgments']['items']['properties']['checks']['items']
            self.assertIn('evidence_observation', check['required'])
            self.assertLess(list(check['properties']).index('evidence_observation'), list(check['properties']).index('verdict'))
        self.assertTrue(all(c['evidence_observation'] for j in report['result']['judgments'] for c in j['checks']))

    def test_native_metadata_is_retained_in_receipts_but_cannot_change_scores(self):
        import httpx
        from tests.test_model_transport import reply
        seen, outputs = [], []
        def handle(request):
            body = json.loads(request.content); seen.append(body)
            payload = json.loads(body['messages'][1]['content'])
            wire = json.loads(self.scorer.complete(payload, purpose='cognition_evaluation')['text'])
            wire['overall_passed'] = True  # A model cannot override computed aggregates.
            for row in wire['coverage']: row['reason_extra'] = ''
            for row in wire['judgments']:
                row['supported'] = True  # Only the business validator derives this value.
                for check in row['checks']: check['note'] = '离线额外备注'
                if payload['other_findings']:
                    row['conflicts'] = [{'reference_id': payload['other_findings'][0]['finding_id'],
                                         'reason': '人工冲突门控夹具。', 'resolved': True}]
            outputs.append(deepcopy(wire))
            return reply(body, wire)
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            store = EvaluationStore(self.state_dir, model=DeepSeekModel(self.scorer.settings, http_client=client))
            record = store.create(self.source['id'], self.criteria)
            before = self.store.db_path.read_bytes()
            store.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
            report = store.run(record['id'])
        self.assertEqual(report['workflow_status'], 'completed', report)
        self.assertEqual(len(seen), 4)
        self.assertEqual(report['result']['coverage']['facts'], {'full': 1, 'total': 1})
        self.assertFalse(report['result']['overall_passed'])  # Preserved conflicts block acceptance.
        for call, raw in zip(store.show(record['id'])['calls'], outputs):
            self.assertEqual(call['status'], 'completed')
            receipt = call['receipt']
            self.assertEqual(receipt['structured_status'], 'valid')
            self.assertEqual(json.loads(receipt['raw_response']['provider_tool_calls'][0]['function']['arguments']), raw)
            self.assertNotIn('overall_passed', json.loads(receipt['text']))
            self.assertNotIn('reason_extra', canonical(call['response']))
            self.assertNotIn('resolved', canonical(call['response']))
            for judgment in call['response']['judgments']:
                self.assertTrue(judgment['conflicts'])
                self.assertFalse(judgment['supported'])
        self.assertEqual(self.store.db_path.read_bytes(), before)

    def test_non_sdk_evaluator_uses_same_projection_and_preserves_original_text(self):
        original = self.scorer.complete
        def complete(payload, *, purpose):
            receipt = original(payload, purpose=purpose)
            raw = json.loads(receipt['text']); raw['unused'] = 'raw-only'
            for row in raw['coverage']: row['reason_extra'] = ''
            receipt['text'] = json.dumps(raw, ensure_ascii=False)
            return receipt
        record = self.queued()
        with patch.object(self.scorer, 'complete', side_effect=complete):
            report = self.evaluator.run(record['id'])
        self.assertTrue(report['result']['overall_passed'], report)
        calls = self.evaluator.show(record['id'])['calls']
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(json.loads(c['receipt']['text'])['unused']=='raw-only' for c in calls))
        self.assertTrue(all('unused' not in c['response'] for c in calls))

    def test_previous_reception_contract_cannot_execute_under_new_policy(self):
        record = self.queued()
        old = self.evaluator.show(record['id'], preview=True)['envelope']
        self.assertEqual(old['contract'].pop('local_reception'), 'discard_unused_evaluation_fields_v1')
        before = self.evaluator.db_path.read_bytes()
        with self.assertRaises(FileReadError) as caught:
            self.evaluator._call(record['id'], 0, old)
        self.assertEqual(caught.exception.code, 'evaluation_contract_changed')
        self.assertEqual(self.scorer.calls, [])
        self.assertEqual(self.evaluator.db_path.read_bytes(), before)

    def test_metadata_does_not_hide_forged_ids_references_or_duplicate_targets(self):
        from zest.model_outputs import CognitionEvaluationResponse
        units = evaluation_units(self.source_full, self.criteria)
        for kind, mutate in (
            ('coverage', lambda w: w['coverage'][0].update(id='forged')),
            ('coverage', lambda w: w['coverage'][0].update(finding_ids=['forged'])),
            ('coverage', lambda w: w['coverage'][0].update(evidence_ids=['P999'])),
            ('coverage', lambda w: w['coverage'].append(deepcopy(w['coverage'][0]))),
            ('semantics', lambda w: w['judgments'][0].update(finding_id='forged')),
            ('semantics', lambda w: w['judgments'][0]['checks'][0].update(evidence_ids=['P999'])),
            ('semantics', lambda w: w['judgments'][0]['conflicts'].append({'reference_id':'forged','reason':'人工夹具。','unused':True})),
        ):
            payload = next(u['payload'] for u in units if u['kind']==kind)
            wire = json.loads(self.scorer.complete(payload, purpose='cognition_evaluation')['text'])
            wire['approved'] = True
            mutate(wire)
            normalized = CognitionEvaluationResponse.model_validate(wire).model_dump(mode='json')
            with self.subTest(kind=kind, wire=wire), self.assertRaises(FileReadError):
                validate_evaluation(normalized, payload)

    def test_conflict_hint_is_preserved_without_becoming_a_scored_target(self):
        unit = next(u for u in evaluation_units(self.source_full, self.criteria) if u['kind'] == 'semantics')
        payload = unit['payload']
        wire = json.loads(self.scorer.complete(payload, purpose='cognition_evaluation')['text'])
        hint = payload['other_findings'][0]
        conflict = {'reference_id': hint['finding_id'], 'reason': '人为构造的直接冲突夹具，不代表实际语义。'}
        wire['judgments'][0]['conflicts'] = [conflict]
        result = validate_evaluation(wire, payload)
        self.assertEqual([j['finding_id'] for j in result['judgments']], [payload['findings'][0]['finding_id']])
        self.assertEqual(result['judgments'][0]['conflicts'], [conflict])
        self.assertFalse(result['judgments'][0]['supported'])

    def test_evidence_level_controls_preserve_complete_but_unsupported_checks(self):
        from zest.model_outputs import CognitionEvaluationResponse
        fixture = json.loads((Path(__file__).parent / 'fixtures/evaluation_evidence_levels.json').read_text())
        source = (Path(__file__).parent / 'fixtures' / fixture['source_file']).read_text()
        payload = {'commit': 'synthetic', 'requirements': [], 'candidates': [], 'other_findings': [],
                   'report_unknowns': [], 'evidence': {'P1': {'text': source}},
                   'findings': [{'finding_id': f['id'], 'kind': 'observed', 'statement': f['statement'],
                                 'evidence_ids': ['P1']} for f in fixture['findings']]}
        wire = {'coverage': [], 'judgments': []}
        for f, labelled in zip(payload['findings'], fixture['findings']):
            j = evaluation_judgment(f)
            j['checks'][0].update(verdict=labelled['expected_verdict'], reason='人工标注的门控夹具，不是模型评分。')
            wire['judgments'].append(j)
        CognitionEvaluationResponse.model_validate(wire)
        result = validate_evaluation(wire, payload)
        self.assertTrue(all(j['coverage_complete'] for j in result['judgments']))
        self.assertEqual([j['supported'] for j in result['judgments']], [True, False, True, False])
        self.assertEqual([j['checks'][0]['assertion'] for j in result['judgments']],
                         [f['statement'] for f in fixture['findings']])
        # The labels are local expectations only, never part of a model payload.
        self.assertNotIn('expected_verdict', canonical(payload))

    def test_native_hint_promotion_or_missing_observation_stops_whole_batch_without_retry(self):
        import httpx
        from tests.test_model_transport import reply
        for fault in ('extra_hint', 'substituted_hint', 'missing_observation', 'empty_observation'):
            with self.subTest(fault=fault):
                seen, raw_outputs = [], []
                def handle(request):
                    body = json.loads(request.content)
                    payload = json.loads(body['messages'][1]['content'])
                    wire = json.loads(self.scorer.complete(payload, purpose='cognition_evaluation')['text'])
                    wire['unused'] = 'ignored without relaxing targets or observations'
                    if payload['findings']:
                        hint = {**payload['other_findings'][0], 'evidence_ids': []}
                        if fault == 'extra_hint':
                            wire['judgments'].append(evaluation_judgment(hint, supported=False))
                        elif fault == 'substituted_hint':
                            wire['judgments'][0] = evaluation_judgment(hint, supported=False)
                        elif fault == 'missing_observation':
                            del wire['judgments'][0]['checks'][0]['evidence_observation']
                        else:
                            wire['judgments'][0]['checks'][0]['evidence_observation'] = '  '
                    seen.append(payload)
                    raw_outputs.append(deepcopy(wire))
                    return reply(body, wire)
                with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                    store = EvaluationStore(self.state_dir, model=DeepSeekModel(self.scorer.settings, http_client=client))
                    record = store.create(self.source['id'], self.criteria)
                    before = self.store.db_path.read_bytes()
                    store.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
                    report = store.run(record['id'])
                    self.assertEqual(report['workflow_status'], 'partial')
                    self.assertFalse(report['result']['overall_passed'])
                    self.assertEqual(report['result']['reviewed_observed_findings'], 0)
                    calls = store.show(record['id'])['calls']
                    self.assertEqual(len(seen), 2)  # One coverage batch, then invalid semantic batch.
                    self.assertEqual([c['status'] for c in calls], ['completed', 'invalid'])
                    self.assertIsNone(calls[1]['response'])  # Never salvage even the valid target portion.
                    receipt = calls[1]['receipt']
                    native = receipt['raw_response']['provider_tool_calls'][0]['function']['arguments']
                    self.assertEqual(json.loads(native), raw_outputs[1])
                    self.assertEqual(receipt['structured_status'], 'invalid' if fault in ('missing_observation', 'empty_observation') else 'valid')
                    with self.assertRaises(FileReadError):
                        store.recover(record['id'])
                    with self.assertRaises(FileReadError):
                        store.run(record['id'])
                    self.assertEqual(len(seen), 2)
                    self.assertEqual(before, self.store.db_path.read_bytes())

    def test_archived_results_remain_readable_without_observation_backfill_or_calls(self):
        record = self.queued()
        self.evaluator.run(record['id'])
        # Simulate a saved earlier contract/result, without running or migrating it.
        with self.evaluator.open(write=True) as conn, conn:
            envelope = json.loads(conn.execute('SELECT envelope FROM evaluations WHERE id=?', (record['id'],)).fetchone()[0])
            props = envelope['contract']['schema']['properties']['judgments']['items']['properties']['checks']['items']
            del props['properties']['evidence_observation']
            props['required'].remove('evidence_observation')
            coverage_props = envelope['contract']['schema']['properties']['coverage']['items']
            del coverage_props['properties']['candidate_observation']
            coverage_props['required'].remove('candidate_observation')
            envelope['contract']['schema_sha256'] = digest(envelope['contract']['schema'])
            conn.execute('UPDATE evaluations SET envelope=?,envelope_sha256=? WHERE id=?',
                         (canonical(envelope), digest(envelope), record['id']))
            for row in conn.execute('SELECT batch,response,receipt FROM evaluation_calls WHERE evaluation_id=?', (record['id'],)).fetchall():
                response, receipt = json.loads(row['response']), json.loads(row['receipt'])
                raw = json.loads(receipt['text'])
                for value in (response, raw):
                    for c in value['coverage']: del c['candidate_observation']
                    for j in value['judgments']:
                        for check in j['checks']: del check['evidence_observation']
                receipt['text'] = canonical(raw)
                conn.execute('UPDATE evaluation_calls SET response=?,receipt=? WHERE evaluation_id=? AND batch=?',
                             (canonical(response), canonical(receipt), record['id'], row['batch']))
        before = self.evaluator.db_path.read_bytes()
        source_before, calls_before = self.store.db_path.read_bytes(), len(self.scorer.calls)
        saved = self.evaluator.show(record['id'], preview=True)
        self.assertTrue(all('candidate_observation' not in c for call in saved['calls'] for c in call['response']['coverage']))
        report = self.evaluator.report(record['id'])
        self.assertTrue(report['result']['overall_passed'])
        self.assertTrue(report['result']['judgments'])
        self.assertTrue(all('evidence_observation' not in c for j in report['result']['judgments'] for c in j['checks']))
        with self.assertRaises(FileReadError) as caught:
            self.evaluator._call(record['id'], 0, saved['envelope'])
        self.assertEqual(caught.exception.code, 'evaluation_contract_changed')
        self.assertEqual(len(self.scorer.calls), calls_before)
        self.assertEqual(self.evaluator.db_path.read_bytes(), before)
        self.assertEqual(self.store.db_path.read_bytes(), source_before)
