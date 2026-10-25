from copy import deepcopy
import unittest

from zest.document_evidence import shared_proofs


class DocumentEvidenceTest(unittest.TestCase):
    def test_repeated_proofs_are_shared_without_changing_their_text_or_fact_mapping(self):
        proof = {'source_id':'S1','text':'完整源码\n第二行\n','evidence':{'path':'app.py','commit':'abc','start_line':1,'end_line':2}}
        payload = {'findings':[{'id':'F1','statement':'事实一','evidence':[proof]}, {'id':'F2','statement':'事实二','evidence':[proof]}]}
        before = deepcopy(payload)
        result = shared_proofs(payload)
        self.assertEqual(payload, before)
        self.assertEqual(len(result['evidence_registry']), 1)
        for fact in result['findings']:
            self.assertEqual(result['evidence_registry'][fact['evidence'][0]['proof_id']], proof)
        self.assertEqual([f['id'] for f in result['findings']], ['F1','F2'])

    def test_verification_history_retains_provenance_and_shares_the_same_original_proof(self):
        proof = {'text':'完整原文','evidence':{'path':'x.py','start_line':2,'end_line':2}}
        result = shared_proofs({'findings':[{'evidence':[proof]}], 'verification_history':[
            {'revision':3,'last_verified_commit':'old','fact':{'evidence':[proof]}}]})
        self.assertEqual(len(result['evidence_registry']), 1)
        history = result['verification_history'][0]
        self.assertEqual(history['revision'], 3)
        self.assertEqual(history['last_verified_commit'], 'old')
        self.assertEqual(history['fact']['evidence'], result['findings'][0]['evidence'])

    def test_sharing_is_deterministic_and_does_not_merge_different_source_locations(self):
        first = {'text':'同样原文','evidence':{'path':'one.py'}}
        second = {'text':'同样原文','evidence':{'path':'two.py'}}
        payload = {'findings':[{'evidence':[first,second]}]}
        result = shared_proofs(payload)
        self.assertEqual(len(result['evidence_registry']), 2)
        self.assertEqual(result, shared_proofs(payload))
