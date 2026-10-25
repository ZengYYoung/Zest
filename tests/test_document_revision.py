from copy import deepcopy
import unittest

from zest.document_contracts import document_contract
from zest.document_evidence import LAYOUT
from zest.document_revision import write_and_review
from zest.document_rounds import recovery
from zest.files import FileReadError


class Ledger:
    run_id = 'fixture'
    def save(self): pass


class Model:
    def __init__(self, reject_revision=False):
        self.calls = []
        self.reject_revision = reject_revision
    def call(self, run_id, actor, sequence, purpose, payload, envelope):
        if sequence not in self.calls: self.calls.append(sequence)
        if purpose == 'document_write':
            return {'blocks':[{'markdown':'依据当前给定的项目原文，只能作出这一项静态实现说明，尚未执行验证。','finding_ids':['F1']}], 'limits':[]}
        rejected = sequence == 1 or self.reject_revision
        return {'checks':[{'block_id':'B1','verdict':'insufficient_evidence' if rejected else 'supported','reason':'已核对正文范围。'}],
                'issues':['应收窄证据外示例。'] if rejected else []}


class DocumentRevisionTest(unittest.TestCase):
    def page(self): return {'spec':{'slug':'flow','finding_ids':['F1'],'related_pages':[]}, 'status':'pending'}

    def test_one_revision_preserves_feedback_and_requires_a_new_independent_review(self):
        page = self.page(); engine = Model(); envelope = {'document_continuation':{'evidence_layout':LAYOUT}}
        write_and_review(engine, Ledger(), page, {'findings':[]}, document_contract(), envelope)
        self.assertEqual(engine.calls, [0,1,2,3])
        self.assertEqual(page['revision_number'], 1)
        self.assertTrue(page['revision_payload']['revision_request']['review_feedback']['issues'])
        self.assertEqual(page['review']['issues'], [])
        self.assertEqual(page['review']['checks'][0]['verdict'], 'supported')
        before = deepcopy(engine.calls)
        write_and_review(engine, Ledger(), page, {'findings':[]}, document_contract(), envelope)
        self.assertEqual(engine.calls, before)

    def test_second_rejection_never_creates_unbounded_revision_sequences(self):
        page = self.page(); engine = Model(reject_revision=True)
        for _ in range(2):
            with self.assertRaises(FileReadError) as caught:
                write_and_review(engine, Ledger(), page, {}, document_contract(), {'document_continuation':{'evidence_layout':LAYOUT}})
            self.assertEqual(caught.exception.code, 'document_review_rejected')
        self.assertEqual(engine.calls, [0,1,2,3])
        self.assertNotIn('review', page)

    def test_legacy_runs_do_not_gain_automatic_semantic_revision(self):
        page = self.page(); engine = Model()
        with self.assertRaises(FileReadError): write_and_review(engine, Ledger(), page, {}, document_contract(), {})
        self.assertEqual(engine.calls, [0,1])

    def test_recovery_uses_existing_budget_and_never_refills_an_exhausted_round(self):
        record = {'id':'fixture','call_count':12,'document_continuation':{'evidence_layout':LAYOUT},'policy':{'max_model_calls':32,'max_seconds':1800}}
        value = {'status':'partial','plan':{},'pages':[{'status':'failed','content':{},'error':{'code':'document_review_rejected'}}]}
        self.assertEqual(recovery(value, record)['action'], 'resume')
        self.assertEqual(recovery(value, {**record,'call_count':32})['action'], 'new_round')
        value['pages'][0]['revision_payload'] = {}
        self.assertEqual(recovery(value, record)['action'], 'new_round')

    def test_removing_unsupported_redundancy_requires_full_fact_coverage_and_fresh_review(self):
        class ProjectionModel(Model):
            def call(self, run_id, actor, sequence, purpose, payload, envelope):
                if sequence not in self.calls: self.calls.append(sequence)
                if purpose == 'document_write':
                    return {'blocks':[{'markdown':'第一段包含需要去除的解释，但相关事实在后文还有完整说明。','finding_ids':['F1']},
                                      {'markdown':'第二段依据完整原文说明这一项事实的主体、条件与实现边界。','finding_ids':['F1']}], 'limits':[]}
                if sequence == 4:
                    self.projected_blocks = payload['blocks']
                    return {'checks':[{'block_id':'B1','verdict':'supported','reason':'全文及事实覆盖已核对。'}],'issues':[]}
                return {'checks':[{'block_id':'B1','verdict':'insufficient_evidence','reason':'冗余引申不受支持。'},
                                  {'block_id':'B2','verdict':'supported','reason':'事实覆盖完整。'}],'issues':['删除冗余引申。']}
        engine=ProjectionModel(); page=self.page()
        write_and_review(engine, Ledger(), page, {}, document_contract(), {'document_continuation':{'evidence_layout':LAYOUT}})
        self.assertEqual(engine.calls,[0,1,2,3,4])
        self.assertEqual(len(page['content']['blocks']),1)
        self.assertEqual(engine.projected_blocks[0]['block_id'],'B1')
        self.assertEqual(page['review']['issues'],[])
        self.assertIn('projection_reason',page)

    def test_projection_cannot_remove_the_only_coverage_of_a_required_fact(self):
        from zest.document_revision import supported_projection
        page={'spec':{'finding_ids':['F1','F2'],'related_pages':[]},'revised_content':{'blocks':[
            {'markdown':'这是第一项事实的唯一正文说明，不应为了通过核对而删除。','finding_ids':['F1']},
            {'markdown':'这是第二项事实的完整说明，但不能代替第一项事实的内容。','finding_ids':['F2']}],'limits':[]},
            'revision_review_response':{'checks':[{'block_id':'B1','verdict':'insufficient_evidence'}, {'block_id':'B2','verdict':'supported'}]}}
        self.assertIsNone(supported_projection(page,document_contract()))

    def test_final_targeted_correction_has_fixed_identities_and_cannot_loop(self):
        class LastCorrectionModel(Model):
            def call(self, run_id, actor, sequence, purpose, payload, envelope):
                if sequence not in self.calls:self.calls.append(sequence)
                if purpose=='document_write':
                    blocks=[{'markdown':'这里明确说明原文支持的主体、条件和实现边界，不引入证据外因果。','finding_ids':['F1']}]
                    return {'blocks':blocks if sequence==5 else blocks*2,'limits':[]}
                if sequence in (1,3):
                    return {'checks':[{'block_id':'B1','verdict':'insufficient_evidence','reason':'存在证据外引申。'},
                                      {'block_id':'B2','verdict':'supported','reason':'原文支持。'}],'issues':['移除证据外引申。']}
                rejected=sequence==4 or self.reject_revision
                return {'checks':[{'block_id':'B1','verdict':'insufficient_evidence' if rejected else 'supported','reason':'复核条件和事实覆盖。'}],
                        'issues':['仍须收窄因果条件。'] if rejected else []}
        for rejected in (False,True):
            page=self.page();engine=LastCorrectionModel(reject_revision=rejected)
            for _ in range(2):
                if rejected:
                    with self.assertRaises(FileReadError):write_and_review(engine,Ledger(),page,{},document_contract(),{'document_continuation':{'evidence_layout':LAYOUT}})
                else:write_and_review(engine,Ledger(),page,{},document_contract(),{'document_continuation':{'evidence_layout':LAYOUT}})
            self.assertEqual(engine.calls,[0,1,2,3,4,5,6])
            self.assertEqual('review' in page,not rejected)

    def test_missing_fact_after_correction_requires_exact_coverage_repair_and_review(self):
        class CoverageModel(Model):
            def call(self,run_id,actor,sequence,purpose,payload,envelope):
                if sequence not in self.calls:self.calls.append(sequence)
                if purpose=='document_write':
                    ids=['F2'] if sequence==5 else ['F1','F2']
                    blocks=[{'markdown':'这里依据完整原文说明事实的适用范围；证据不足的部分仍明确保留限制。','finding_ids':ids}]
                    return {'blocks':blocks if sequence in (5,7) else blocks*2,'limits':[]}
                if sequence in (1,3):return {'checks':[{'block_id':'B1','verdict':'insufficient_evidence','reason':'引申不支持。'},
                    {'block_id':'B2','verdict':'supported','reason':'原文支持。'}],'issues':['收窄引申。']}
                return {'checks':[{'block_id':'B1','verdict':'supported' if sequence==8 else 'insufficient_evidence','reason':'核对全文与事实覆盖。'}],
                        'issues':[] if sequence==8 else ['条件须收窄。']}
        page=self.page();page['spec']['finding_ids']=['F1','F2'];engine=CoverageModel()
        write_and_review(engine,Ledger(),page,{},document_contract(),{'document_continuation':{'evidence_layout':LAYOUT}})
        self.assertEqual(engine.calls,[0,1,2,3,4,5,7,8])
        self.assertEqual(page['coverage_revision_payload']['revision_request']['missing_finding_ids'],['F1'])
        self.assertEqual(set(page['content']['blocks'][0]['finding_ids']),{'F1','F2'})
        self.assertEqual(page['review']['issues'],[])
