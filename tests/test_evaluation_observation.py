"""Scoring observation protocol and labelled controls, never model accuracy."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

try:
    from pydantic import ValidationError
except ImportError as exc:
    raise unittest.SkipTest('AI extra is not installed') from exc

from zest.cognition_review import validate_judgments
from zest.evaluation_contracts import validate_evaluation
from zest.files import FileReadError
from zest.model_outputs import CognitionEvaluationResponse, ProjectReviewResponse, get_output_schema


class EvaluationObservationTest(unittest.TestCase):
    def controls(self):
        folder = Path(__file__).parent / 'fixtures'
        fixture = json.loads((folder / 'evaluation_observation.json').read_text())
        source = (folder / fixture['source_file']).read_text()
        for case in fixture['cases']:
            payload = {'commit': 'synthetic_static', 'requirements': [], 'candidates': [],
                'findings': [{'finding_id': case['id'], 'kind': 'observed', 'statement': case['statement'], 'evidence_ids': ['P1']}],
                'evidence': {'P1': {'text': source}}, 'other_findings': [], 'report_unknowns': []}
            wire = {'coverage': [], 'judgments': [{'finding_id': case['id'], 'coverage_complete': True,
                'conflicts': [], 'reason': '人工标注接收对照，不是模型评分。', 'checks': [
                    {'subject': '静态片段', 'condition': '本片段所示范围', 'evidence_ids': ['P1'],
                     'reason': '人工标注的支持、相反或缺口关系，不是模型判断。', **check} for check in case['checks']]}]}
            yield payload, wire

    def test_labelled_positive_negative_and_missing_evidence_controls_preserve_observations(self):
        results = []
        for payload, wire in self.controls():
            CognitionEvaluationResponse.model_validate(wire)
            reviewed = validate_evaluation(wire, payload)['judgments'][0]
            self.assertEqual(reviewed['checks'], wire['judgments'][0]['checks'])
            self.assertTrue(reviewed['coverage_complete'])
            results.append(reviewed['supported'])
        self.assertEqual(results, [True, False, True, False, True, False])

    def test_scoring_requires_observation_without_fallback_or_extra_fields(self):
        payload, wire = next(self.controls())
        for value in (None, '', '  \n', 1, True, 'x' * 801):
            bad = deepcopy(wire)
            if value is None:
                del bad['judgments'][0]['checks'][0]['evidence_observation']
            else:
                bad['judgments'][0]['checks'][0]['evidence_observation'] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CognitionEvaluationResponse.model_validate(bad)
            with self.subTest(value=value), self.assertRaises(FileReadError):
                validate_evaluation(bad, payload)
        bad = deepcopy(wire)
        bad['judgments'][0]['checks'][0]['evidence_observation'] = 'P999 claims something'
        bad['judgments'][0]['checks'][0]['evidence_ids'] = ['P999']
        with self.assertRaises(FileReadError): validate_evaluation(bad, payload)

    def test_observation_is_retained_verbatim_and_is_not_a_truth_parser(self):
        payload, wire = list(self.controls())[1]
        check = wire['judgments'][0]['checks'][-1]
        check['evidence_observation'] = '  quiet lake 中含内部空格。\n'
        # Deliberate wrong label: no keyword rule is allowed to silently repair it.
        check['verdict'] = 'supported'
        result = validate_evaluation(wire, payload)['judgments'][0]
        self.assertEqual(result['checks'][-1]['evidence_observation'], check['evidence_observation'])
        self.assertTrue(result['supported'])
        self.assertEqual(result['checks'][-1]['verdict'], 'supported')

    def test_project_review_still_uses_original_closed_protocol(self):
        payload, wire = next(self.controls())
        j = deepcopy(wire['judgments'][0])
        with self.assertRaises(ValidationError): ProjectReviewResponse.model_validate({'judgments': [j]})
        with self.assertRaises(FileReadError): validate_judgments([j], payload)
        for check in j['checks']: del check['evidence_observation']
        ProjectReviewResponse.model_validate({'judgments': [j]})
        self.assertTrue(validate_judgments([j], payload)[0]['supported'])
        with self.assertRaises(ValidationError): CognitionEvaluationResponse.model_validate({'coverage': [], 'judgments': [j]})
        props = get_output_schema('cognition_evaluation')['properties']['judgments']['items']['properties']['checks']['items']['properties']
        self.assertLess(list(props).index('evidence_observation'), list(props).index('verdict'))
