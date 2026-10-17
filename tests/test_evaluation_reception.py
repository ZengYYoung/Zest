"""Flexible scoring reception; valid scores survive missing or bad neighbours."""
from copy import deepcopy
import json
import unittest

from tests import test_evaluation_coverage as coverage_controls
from tests import test_evaluation_observation as observation_controls
from zest.evaluation_reception import receive_evaluation


class EvaluationReceptionTest(unittest.TestCase):
    def setUp(self):
        control=coverage_controls.EvaluationCoverageTest();self.payload=control.payload();self.wire=control.wire()

    def receive(self, wire, payload=None):
        return receive_evaluation({'text':json.dumps(wire,ensure_ascii=False)},payload or self.payload)

    def test_missing_inert_fields_are_defaulted_without_inventing_scores(self):
        wire={'coverage':[{'id':'flow/save','finding_ids':['C1'],'evidence_ids':['P1'],'verdict':'partial'}]}
        raw=deepcopy(wire);result=self.receive(wire)
        self.assertTrue(result['reception']['complete'])
        self.assertEqual(result['coverage'][0]['candidate_observation'],'')
        self.assertEqual(result['coverage'][0]['reason'],'')
        self.assertEqual(wire,raw)
        self.assertTrue(result['reception']['defaulted_fields'])

    def test_missing_targets_remain_ungraded_while_returned_targets_survive(self):
        self.payload['requirements'].append({**self.payload['requirements'][0],'id':'flow/return'})
        result=self.receive(self.wire)
        self.assertEqual(len(result['coverage']),1)
        self.assertEqual(result['reception']['missing_coverage_ids'],['flow/return'])
        self.assertFalse(result['reception']['complete'])

    def test_singleton_lists_and_verdict_case_are_presentation_only(self):
        row={'id':'flow/save','finding_ids':'C1','evidence_ids':'P1','verdict':' PARTIAL ',
             'candidate_observation':None,'reason':{'unused':'ignored'}}
        result=self.receive({'coverage':row,'judgments':None})
        self.assertTrue(result['reception']['complete'])
        self.assertEqual(result['coverage'][0]['finding_ids'],['C1'])
        self.assertEqual(result['coverage'][0]['evidence_ids'],['P1'])
        self.assertEqual(result['coverage'][0]['verdict'],'partial')
        self.assertEqual(result['coverage'][0]['reason'],'')

    def test_invalid_reference_or_duplicate_target_does_not_contaminate_valid_target(self):
        self.payload['requirements'].append({**self.payload['requirements'][0],'id':'flow/return'})
        for fault in ('reference','duplicate','unexpected'):
            wire=deepcopy(self.wire);row={**wire['coverage'][0],'id':'flow/return'}
            if fault=='reference':row['evidence_ids']=['P999']
            elif fault=='duplicate':wire['coverage'].append(deepcopy(row))
            else:row['id']='not-approved'
            wire['coverage'].append(row)
            result=self.receive(wire)
            with self.subTest(fault=fault):
                self.assertEqual([r['id'] for r in result['coverage']],['flow/save'])
                self.assertEqual(result['reception']['missing_coverage_ids'],['flow/return'])
                self.assertTrue(result['reception']['rejected_items'])

    def test_bad_whole_structure_keeps_no_scores_and_never_repairs_json(self):
        for receipt in ({'text':'not json'}, {'text':'[]'}, {'text':'{"coverage":null}'},
                        {'raw_response':{'tool_calls':[{'name':'other','args':self.wire}]}},
                        {'raw_response':{'tool_calls':[{'name':'CognitionEvaluationResponse','args':self.wire}]*2}}):
            result=receive_evaluation(receipt,self.payload)
            self.assertEqual(result['coverage'],[])
            self.assertFalse(result['reception']['complete'])

    def test_safe_named_tool_args_recover_valid_items_after_sdk_whole_parse_failure(self):
        wire=deepcopy(self.wire);wire['coverage'].append({'id':'not-approved','verdict':17})
        receipt={'structured_status':'invalid','text':'','raw_response':{'tool_calls':[
            {'name':'CognitionEvaluationResponse','args':wire}], 'invalid_tool_calls':[]}}
        before=deepcopy(receipt);result=receive_evaluation(receipt,self.payload)
        self.assertEqual(len(result['coverage']),1)
        self.assertFalse(result['reception']['complete'])
        self.assertEqual(receipt,before)

    def test_partial_semantic_batch_retains_cross_target_conflict(self):
        payload,wire=next(observation_controls.EvaluationObservationTest().controls())
        other={**payload['findings'][0],'finding_id':'other'}
        payload['findings'].append(other)
        wire['judgments'][0]['conflicts']=[{'reference_id':'other','reason':'人工冲突控制。'}]
        result=self.receive(wire,payload)
        self.assertEqual(len(result['judgments']),1)
        self.assertFalse(result['judgments'][0]['supported'])
        self.assertEqual(result['reception']['missing_judgment_ids'],['other'])
