"""Labelled coverage controls; acceptance tests do not measure LLM accuracy."""
from copy import deepcopy
import unittest

try:
    from pydantic import ValidationError
except ImportError as exc:
    raise unittest.SkipTest('AI extra is not installed') from exc
from zest.evaluation_contracts import validate_evaluation
from zest.files import FileReadError
from zest.model_outputs import CognitionEvaluationResponse, get_output_schema


class EvaluationCoverageTest(unittest.TestCase):
    def payload(self):
        return {'commit': 'static-control',
                'requirements': [{'id': 'flow/save', 'statement': '说明入口调用、保存和返回主干。', 'evidence_ids': ['P1']}],
                'candidates': [{'finding_id': 'C1', 'kind': 'observed', 'statement': '入口调用make_item。'},
                               {'finding_id': 'C2', 'kind': 'observed', 'statement': 'make_item构造记录，保存并返回item。'}],
                'evidence': {'P1': {'text': 'def make_item():\n    item = Item()\n    item.save()\n    return item'}},
                'findings': [], 'other_findings': [], 'report_unknowns': []}

    def wire(self, verdict='partial'):
        return {'coverage': [{'id': 'flow/save', 'finding_ids': ['C1'],
                 'candidate_observation': 'C1只说明入口调用，没有明确写出保存和返回。', 'evidence_ids': ['P1'],
                 'verdict': verdict, 'reason': '原文有保存返回；候选只表达入口，必要主干未完整交付。'}], 'judgments': []}

    def test_candidates_are_usable_when_semantic_findings_are_empty(self):
        payload=self.payload()
        for verdict in ('partial', 'full', 'missing', 'reference_unsupported'):
            wire=self.wire(verdict);row=wire['coverage'][0]
            if verdict=='full':
                row.update(finding_ids=['C1','C2'],candidate_observation='C1说明入口，C2说明构造、保存与返回。',
                           reason='两条候选合起来表达必要主干，静态源码支持。')
            elif verdict in ('missing','reference_unsupported'):
                row.update(finding_ids=[],candidate_observation='人工无表达/参考缺证对照，不是实际模型评分。',
                           reason='人工控制标签，不由本用例判断语义。')
            normalized=CognitionEvaluationResponse.model_validate(wire).model_dump(mode='json')
            result=validate_evaluation(normalized,payload)
            self.assertEqual(result['coverage'][0],row)
            self.assertEqual(result['judgments'],[])

    def test_observation_is_optional_and_malformed_explanation_can_be_ignored(self):
        for value in (None,'','  \n',1,True,[],'x'*1601):
            wire=self.wire();row=wire['coverage'][0]
            if value is None:del row['candidate_observation']
            else:row['candidate_observation']=value
            with self.subTest(value=value):
                parsed=CognitionEvaluationResponse.model_validate(wire).model_dump(mode='json')
                self.assertEqual(validate_evaluation(parsed,self.payload())['coverage'][0]['candidate_observation'], value if isinstance(value,str) else '')

    def test_observation_never_substitutes_for_valid_candidate_identity(self):
        for verdict in ('full','partial'):
            for ids in ([],['not-provided']):
                wire=self.wire(verdict);wire['coverage'][0]['finding_ids']=ids
                wire['coverage'][0]['candidate_observation']='C1已经表达了；这段文字不能补齐身份。'
                normalized=CognitionEvaluationResponse.model_validate(wire).model_dump(mode='json')
                with self.subTest(verdict=verdict,ids=ids),self.assertRaises(FileReadError):
                    validate_evaluation(normalized,self.payload())

    def test_observation_is_preserved_without_keyword_grading_or_literal_quote_matching(self):
        wire=self.wire('full');row=wire['coverage'][0]
        row['candidate_observation']='  完整源码不代表候选已表达；此处故意使用错误full控制标签。\n'
        # A deliberately inconsistent label demonstrates the semantic boundary:
        # the validator must not invent a language/keyword parser to change it.
        wire['unused']='extra metadata';raw=deepcopy(wire)
        normalized=CognitionEvaluationResponse.model_validate(wire).model_dump(mode='json')
        result=validate_evaluation(normalized,self.payload())
        self.assertEqual(result['coverage'][0]['candidate_observation'],row['candidate_observation'])
        self.assertEqual(result['coverage'][0]['verdict'],'full')
        self.assertEqual(wire,raw)
        self.assertNotIn('unused',result)

    def test_schema_places_candidate_observation_before_final_verdict_without_offsets(self):
        schema=get_output_schema('cognition_evaluation')['properties']['coverage']['items']
        props=schema['properties']
        self.assertIn('candidate_observation',schema['required'])
        self.assertLess(list(props).index('finding_ids'),list(props).index('candidate_observation'))
        self.assertLess(list(props).index('candidate_observation'),list(props).index('verdict'))
        self.assertFalse({'start','end','quote','candidate_quote'} & set(props))
        self.assertIs(schema['additionalProperties'],False)
