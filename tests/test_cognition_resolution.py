"""Offline labelled relations; no claim of measured LLM judgment quality."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_cognition as fixtures
from tests import test_cognition_followup as followup_fixtures
from zest.cognition import CognitionEngine
from zest.cognition_followup import prepare_followup
from zest.cognition_resolution import ResolutionStore, effective_resolutions, format_resolution
from zest.cognition_resolution_contracts import validate_resolution
from zest.cognition_store import CognitionStore, canonical, digest
from zest.files import FileReadError
from zest.management_context import build_management_context


class ResolutionModel:
    def __init__(self):
        self.settings = fixtures.OfflineCognitionModel().settings
        self.calls = []
        self.outcome = 'corrected'
        self.unresolved_refs = set()
        self.unresolved_edges = False
        self.unresolved_pairs = set()
        self.coverage = True
        self.unknown = False
        self.invalid = False

    def complete(self, payload, *, purpose):
        self.calls.append(deepcopy(payload))
        if self.unknown: raise TimeoutError('offline unknown')
        candidate = payload['candidates'][0]
        judgments = []
        for ref in payload['references']:
            outcome = 'unresolved' if ref['reference_id'] in self.unresolved_refs else self.outcome
            judgments.append({'reference_id':ref['reference_id'], 'coverage_complete':self.coverage,
                'reason':'离线人工标注关系，不代表模型质量。', 'checks':[{
                    'subject':'add','attribute':'返回值',
                    'condition':'固定版本','assertion':ref['statement'],'outcome':outcome,
                    'finding_ids':[candidate['finding_id']] if outcome != 'unresolved' else [],
                    'evidence_ids':candidate['evidence_ids'] if outcome != 'unresolved' else [],
                    'reason':'新结论和本次原文回答该属性。'}]})
        conflicts = [{'left':c['left'],'right':c['right'],
                      'verdict':'unresolved' if self.unresolved_edges or (c['left'],c['right']) in self.unresolved_pairs or {c['left'],c['right']} & self.unresolved_refs else 'resolved',
                      'finding_ids':[candidate['finding_id']],'evidence_ids':candidate['evidence_ids'],
                      'reason':'离线标注：按返回值原文解释两侧条件。'} for c in payload['conflicts']]
        if self.invalid: judgments[0]['checks'][0]['evidence_ids'] = ['P999']
        return {'text':json.dumps({'judgments':judgments,'conflicts':conflicts},ensure_ascii=False)}


class ResolutionTest(unittest.TestCase):
    setUpBase = fixtures.CognitionTest.setUpBase
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    cli = fixtures.CognitionTest.cli
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved
    alter = followup_fixtures.FollowupTest.alter

    def setUp(self):
        followup_fixtures.FollowupTest.setUp(self)
        self.scorer = ResolutionModel()
        self.resolver = ResolutionStore(self.state_dir, model=self.scorer)

    def followup(self):
        record = prepare_followup(self.state_dir,self.origin,self.finding_id,target=self.target)
        self.store.decide(record['id'],approved=True,approval_sha256=record['approval_sha256'])
        result = CognitionEngine(self.state_dir,model=followup_fixtures.FocusedModel()).run(record['id'])
        self.assertEqual(result['status'],'completed',result['error'])
        return result

    def created(self, followup=None):
        return self.resolver.create((followup or self.followup())['id'])

    def proposed(self, followup=None):
        record = self.created(followup)
        self.resolver.decide(record['id'],approved=True,approval_sha256=record['approval_sha256'])
        result = self.resolver.run(record['id'])
        self.assertEqual(result['status'],'proposed',result['error'])
        return result

    def adopt(self, record):
        return self.resolver.adopt(record['id'],proposal_sha256=record['proposal_sha256'])

    def graph(self):
        other = next(f for f in self.before['findings'] if f['id'] != self.finding_id)
        a = deepcopy(self.selected['review']); b = deepcopy(other['review'])
        a['conflicts'] = [{'reference_id':other['id'],'reason':'两条返回值断言矛盾。'}]
        b['conflict_links'] = [{'reference_id':self.finding_id,'reason':'保留反向冲突。'}]
        self.alter('review',a);self.alter('review',b,other['id'])
        return other

    def test_prepare_zero_calls_and_separate_permission_exact_request(self):
        followup = self.followup(); before = self.store.db_path.read_bytes()
        record = self.created(followup)
        preview = self.resolver.show(record['id'],preview=True)
        self.assertEqual(record['status'],'awaiting_approval');self.assertEqual(self.scorer.calls,[])
        self.assertEqual(before,self.store.db_path.read_bytes())
        self.assertEqual(preview['envelope']['policy']['max_calls'],1)
        self.assertIn('def add',canonical(preview['envelope']['payload']['evidence']))
        self.assertNotIn(self.scorer.settings.api_key,canonical(preview))
        self.assertEqual(self.resolver.db_path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileReadError):self.resolver.run(record['id'])
        with self.assertRaises(FileReadError):self.resolver.adopt(record['id'],proposal_sha256='wrong')
        with self.assertRaises(FileReadError):self.resolver.decide(record['id'],approved=True,approval_sha256=followup['approval_sha256'])

    def test_correction_only_takes_effect_after_adoption_and_is_idempotent(self):
        record = self.proposed()
        before = self.store.show(self.origin,include_envelope=True,include_calls=True)
        context = build_management_context(self.state_dir,self.project['id'])['cognition']
        self.assertTrue(any(a['finding_id']==self.finding_id for a in context['followup_actions']))
        self.assertTrue(record['result']['selected_closable']);self.assertIsNone(record['adoption'])
        self.assertIn('尚未采纳',format_resolution(record))
        adopted = self.adopt(record)
        self.assertEqual(self.adopt(record)['adoption'],adopted['adoption'])
        context = build_management_context(self.state_dir,self.project['id'])['cognition']
        self.assertFalse(context['followup_actions'])
        self.assertEqual(len(context['resolved_findings']),1)
        self.assertEqual(context['resolved_findings'][0]['resolution']['outcome'],'corrected')
        self.assertEqual(before,self.store.show(self.origin,include_envelope=True,include_calls=True))
        self.assertEqual(len(self.scorer.calls),1)
        with self.resolver.open() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM resolution_adoptions').fetchone()[0],1)
        with self.assertRaises(FileReadError):self.created(self.store.show(record['followup_run_id']))
        with self.assertRaises(FileReadError):prepare_followup(self.state_dir,self.origin,self.finding_id,target=self.target)

    def test_confirmed_and_unknown_or_partial_remain_distinct(self):
        self.scorer.outcome='confirmed'; record=self.proposed();self.adopt(record)
        self.assertTrue(record['result']['selected_closable'])
        self.assertEqual(record['result']['effects'][0]['outcome'],'confirmed')

    def test_partial_attribute_or_incomplete_semantics_cannot_close(self):
        self.scorer.unresolved_refs.add(self.finding_id)
        record=self.proposed();self.adopt(record)
        self.assertFalse(record['result']['selected_closable'])
        self.assertTrue(build_management_context(self.state_dir,self.project['id'])['cognition']['followup_actions'])
        self.scorer.unresolved_refs.clear();self.scorer.coverage=False
        record=self.proposed();self.assertFalse(record['result']['selected_closable'])

    def test_each_conflict_and_both_sides_must_be_explained(self):
        other=self.graph();self.scorer.unresolved_edges=True
        record=self.proposed();self.adopt(record)
        self.assertFalse(record['result']['selected_closable'])
        self.assertEqual(len(record['result']['conflicts']),1)
        facts=[f for s in build_management_context(self.state_dir,self.project['id'])['cognition']['sections'] for f in s['facts']]
        self.assertTrue(any(f['finding_id']==other['id'] and f['run_id']==self.origin for f in facts))

    def test_corrected_accepted_counterpart_leaves_all_effective_knowledge_paths(self):
        other=self.graph(); followup=self.followup()
        old = self.store.reviewed_finding(self.origin,other['id'])
        record=self.proposed(followup);before=self.store.db_path.read_bytes();self.adopt(record)
        self.assertEqual(before,self.store.db_path.read_bytes())
        context=build_management_context(self.state_dir,self.project['id'])['cognition']
        facts=[f for s in context['sections'] for f in s['facts']]
        self.assertFalse(any(f['finding_id']==other['id'] and f['run_id']==self.origin for f in facts))
        self.assertEqual(len(context['resolved_findings']),2)
        with self.assertRaises(FileReadError) as caught:self.store.reviewed_finding(self.origin,other['id'])
        self.assertEqual(caught.exception.code,'cognition_knowledge_changed')
        knowledge=self.store.for_context(self.project['id'],record['commit'],query=old['body'])
        self.assertFalse(any(k['id']==other['id'] and k['run_id']==self.origin for k in knowledge))
        self.assertTrue(any(k['run_id']==followup['id'] for k in knowledge))
        with self.assertRaises(FileReadError):
            self.engine.check_knowledge({'commit':record['commit'],'shared_knowledge':[
                {'id':other['id'],'run_id':self.origin,'finding_sha256':old['finding_sha256']}]})

    def test_one_resolved_edge_does_not_close_remaining_conflict_chain(self):
        other=self.graph()
        third=next(f for f in self.before['findings'] if f['id'] not in (self.finding_id,other['id']))
        review=deepcopy(next(f for f in self.store.show(self.origin)['findings'] if f['id']==other['id'])['review'])
        review['conflicts']=[{'reference_id':third['id'],'reason':'仍待解释的另一属性。'}]
        self.alter('review',review,other['id'])
        self.alter('status','needs_investigation',other['id']);self.alter('status','needs_investigation',third['id'])
        self.scorer.unresolved_pairs.add(tuple(sorted((other['id'],third['id']))))
        record=self.proposed();self.adopt(record)
        self.assertTrue(record['result']['selected_closable'])
        context=build_management_context(self.state_dir,self.project['id'])['cognition']
        self.assertEqual({a['finding_id'] for a in context['followup_actions']},{other['id'],third['id']})
        self.assertEqual([a['finding_id'] for a in context['resolved_findings']],[self.finding_id])

    def test_related_unknown_is_answered_explicitly_and_audit_remains(self):
        review=deepcopy(self.selected['review'])
        review['conflicts']=[{'reference_id':self.selected['job_id']+':U1','reason':'返回值边界尚未查清。'}]
        self.alter('review',review)
        with self.store.open(write=True) as conn,conn:
            row=conn.execute('SELECT result FROM cognition_jobs WHERE run_id=? AND id=?',(self.origin,self.selected['job_id'])).fetchone()
            value=json.loads(row[0]);value['report']['unknowns']=['add 的返回值是什么？']
            conn.execute('UPDATE cognition_jobs SET result=? WHERE run_id=? AND id=?',(canonical(value),self.origin,self.selected['job_id']))
        record=self.proposed();self.adopt(record)
        unknown=next(j for j in record['result']['judgments'] if ':U' in j['reference_id'])
        self.assertEqual(unknown['outcome'],'corrected')
        gaps=build_management_context(self.state_dir,self.project['id'])['cognition']['gaps']
        self.assertTrue(any(g.get('resolution',{}).get('active') for g in gaps))

    def test_missing_attributes_refs_edges_or_fake_proofs_are_rejected(self):
        self.graph();created=self.created();payload=self.resolver.show(created['id'],preview=True)['envelope']['payload']
        raw=json.loads(self.scorer.complete(payload,purpose='cognition_resolution')['text'])
        bad=[]
        v=deepcopy(raw);v['judgments'].pop();bad.append(v)
        v=deepcopy(raw);v['conflicts']=[];bad.append(v)
        v=deepcopy(raw);v['judgments'][0]['checks'][0]['end']=1;bad.append(v)
        v=deepcopy(raw);v['judgments'][0]['checks'][0]['evidence_ids']=['P999'];bad.append(v)
        v=deepcopy(raw);v['judgments'][0]['checks'][0]['finding_ids']=['forged'];bad.append(v)
        v=deepcopy(raw);v['judgments'][0]['checks'][0]['outcome']='unresolved';bad.append(v)
        for v in bad:
            with self.subTest(v=v),self.assertRaises(FileReadError):validate_resolution(v,payload)
        # An otherwise valid candidate cannot be borrowed to explain an edge
        # unless the endpoint attributes actually use it.
        payload=deepcopy(payload);candidate=deepcopy(payload['candidates'][0]);candidate['finding_id']='T2:F999'
        payload['candidates'].append(candidate);v=deepcopy(raw);v['conflicts'][0]['finding_ids']=['T2:F999']
        with self.assertRaises(FileReadError):validate_resolution(v,payload)

    def test_remaining_new_work_blocks_closure_even_if_model_claims_answered(self):
        followup=self.followup()
        with self.store.open(write=True) as conn,conn:
            row=conn.execute('SELECT result FROM cognition_jobs WHERE run_id=?',(followup['id'],)).fetchone()
            value=json.loads(row[0]);value['report']['unknowns']=['还没查清返回条件。']
            conn.execute('UPDATE cognition_jobs SET result=? WHERE run_id=?',(canonical(value),followup['id']))
        record=self.proposed(followup)
        self.assertFalse(record['result']['open_work_clear']);self.assertFalse(record['result']['selected_closable'])
        self.adopt(record)
        self.assertTrue(build_management_context(self.state_dir,self.project['id'])['cognition']['followup_actions'])

    def test_source_mutation_blocks_request_and_adoption(self):
        created=self.created();self.resolver.decide(created['id'],approved=True,approval_sha256=created['approval_sha256'])
        self.alter('statement','已修改的旧问题。')
        result=self.resolver.run(created['id'])
        self.assertEqual(result['error']['code'],'resolution_source_changed');self.assertEqual(self.scorer.calls,[])

    def test_new_review_mutation_after_proposal_cannot_be_adopted(self):
        record=self.proposed()
        item=self.store.show(record['followup_run_id'])['findings'][0]
        self.store.invalidate(record['followup_run_id'],item['id'],'离线撤回')
        with self.assertRaises(FileReadError) as caught:self.adopt(record)
        self.assertEqual(caught.exception.code,'resolution_source_changed')

    def test_competing_proposals_cannot_adopt_two_decisions_for_same_question(self):
        followup=self.followup();first=self.proposed(followup);second=self.proposed(followup)
        self.adopt(first)
        with self.assertRaises(FileReadError) as caught:self.adopt(second)
        self.assertEqual(caught.exception.code,'resolution_already_closed')
        self.assertIsNone(self.resolver.show(second['id'])['adoption'])

    def test_no_reviewed_candidates_or_corrupt_saved_original_cannot_prepare(self):
        followup=self.followup();item=followup['findings'][0]
        self.store.invalidate(followup['id'],item['id'],'离线撤回')
        with self.assertRaises(FileReadError) as caught:self.created(followup)
        self.assertEqual(caught.exception.code,'resolution_no_candidates')
        with self.store.open(write=True) as conn,conn:
            finding=deepcopy(item['finding']);finding['evidence'][0]['text']='伪造原文'
            conn.execute('UPDATE cognition_findings SET finding=? WHERE run_id=? AND id=?',(canonical(finding),followup['id'],item['id']))
        with self.assertRaises(FileReadError):self.created(followup)
        self.assertEqual(self.scorer.calls,[])

    def test_invalidated_replacement_reopens_action_without_reviving_old_error(self):
        other=self.graph();record=self.proposed();self.adopt(record)
        item=self.store.show(record['followup_run_id'])['findings'][0]
        self.store.invalidate(record['followup_run_id'],item['id'],'离线撤回')
        context=build_management_context(self.state_dir,self.project['id'])['cognition']
        self.assertFalse(context['resolved_findings']);self.assertEqual(len(context['followup_actions']),2)
        counterpart=next(a for a in context['followup_actions'] if a['finding_id']==other['id'])
        self.assertNotIn('prepare_command',counterpart)
        self.assertEqual(counterpart['inspect_command'][3:5],['resolution','preview'])
        facts=[f for s in context['sections'] for f in s['facts']]
        self.assertFalse(any(f['run_id']==self.origin and f['finding_id']==other['id'] for f in facts))
        with self.assertRaises(FileReadError):self.store.reviewed_finding(self.origin,other['id'])
        self.assertFalse(effective_resolutions(self.state_dir,self.project['id'],record['commit'])[(self.origin,self.finding_id)]['active'])

    def test_unknown_and_invalid_requests_are_not_reissued(self):
        for mode in ('unknown','invalid'):
            with self.subTest(mode=mode):
                setattr(self.scorer,mode,True);created=self.created()
                self.resolver.decide(created['id'],approved=True,approval_sha256=created['approval_sha256'])
                result=self.resolver.run(created['id']);count=len(self.scorer.calls)
                self.assertEqual(result['status'],'pending_execution' if mode=='unknown' else 'partial')
                with self.assertRaises(FileReadError):self.resolver.recover(created['id'])
                with self.assertRaises(FileReadError):self.resolver.run(created['id'])
                self.assertEqual(len(self.scorer.calls),count);setattr(self.scorer,mode,False)
                if mode=='invalid':self.assertIsNotNone(result['call']['receipt'])

    def test_saved_response_reused_after_checkpoint_interruption(self):
        created=self.created();self.resolver.decide(created['id'],approved=True,approval_sha256=created['approval_sha256'])
        original=self.resolver._call
        def crash(*args):original(*args);raise KeyboardInterrupt()
        with patch.object(self.resolver,'_call',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.resolver.run(created['id'])
        self.resolver.recover(created['id']);record=self.resolver.run(created['id'])
        self.assertEqual(record['status'],'proposed',record['error']);self.assertEqual(len(self.scorer.calls),1)

    def test_version_movement_prevents_adoption_without_touching_old_findings(self):
        record=self.proposed();before=self.store.db_path.read_bytes()
        (self.repo/'new.txt').write_text('changed\n');self.commit('move')
        with self.assertRaises(FileReadError) as caught:self.adopt(record)
        self.assertEqual(caught.exception.code,'resolution_version_changed')
        self.assertEqual(before,self.store.db_path.read_bytes())

    def test_model_and_schema_changes_stop_before_send(self):
        record=self.created();self.resolver.decide(record['id'],approved=True,approval_sha256=record['approval_sha256'])
        self.scorer.settings=replace(self.scorer.settings,name='different')
        result=self.resolver.run(record['id'])
        self.assertEqual(result['error']['code'],'resolution_contract_changed');self.assertEqual(self.scorer.calls,[])

    def test_budget_extension_and_rejection_are_separate(self):
        from zest.models import prepare_model_request
        followup=self.followup()
        def extended(*args,**kwargs):return {**prepare_model_request(*args,**kwargs),'chars':33000}
        with patch('zest.cognition_resolution.prepare_model_request',side_effect=extended):record=self.created(followup)
        with self.assertRaises(FileReadError) as caught:self.resolver.decide(record['id'],approved=True,approval_sha256=record['approval_sha256'])
        self.assertEqual(caught.exception.code,'resolution_context_approval_required')
        self.resolver.decide(record['id'],approved=True,approval_sha256=record['approval_sha256'],allow_extended_requests=True)
        self.assertEqual(self.scorer.calls,[])
        with patch('zest.cognition_resolution.prepare_model_request',return_value={'chars':48001}):
            with self.assertRaises(FileReadError):self.created(followup)
        rejected=self.created(followup);self.resolver.decide(rejected['id'],approved=False)
        with self.assertRaises(FileReadError):self.resolver.run(rejected['id'])

    def test_project_resources_legacy_source_and_model_context_share_overlay(self):
        from zest.storage import ProjectRegistry
        from zest.git import inspect_repository
        from zest.resources import record_state
        other=self.graph()
        project=ProjectRegistry(self.state_dir).register(inspect_repository(self.repo),workspace=True)
        record=self.proposed();self.adopt(record)
        self.assertEqual(record_state(self.state_dir,'resolution',record['id']),Path(project['resource_dir']))
        self.assertEqual(len(build_management_context(self.state_dir,self.project['id'])['cognition']['resolved_findings']),2)
        with self.assertRaises(FileReadError):self.store.reviewed_finding(self.origin,other['id'])
        self.assertFalse(any(k['run_id']==self.origin and k['id']==other['id'] for k in self.store.for_context(self.project['id'],record['commit'])))

    def test_cli_preview_adoption_hash_and_human_readable_proposal(self):
        record=self.proposed()
        code,out,err=self.cli('resolution','preview',record['id'])
        self.assertEqual(code,0,err);self.assertIn('payload',json.loads(out)['envelope'])
        code,out,err=self.cli('resolution','show',record['id'],'--format','text')
        self.assertEqual(code,0,err);self.assertIn('可结案',out);self.assertIn('尚未采纳',out)
        code,out,err=self.cli('resolution','adopt',record['id'],'--proposal-sha256','wrong')
        self.assertEqual(code,1)
        code,out,err=self.cli('resolution','adopt',record['id'],'--proposal-sha256',record['proposal_sha256'])
        self.assertEqual(code,0,err);self.assertIsNotNone(json.loads(out)['adoption'])

    def test_real_adapter_uses_named_schema_and_exact_frozen_request(self):
        import httpx
        from tests.test_model_transport import reply
        from zest.models import DeepSeekModel
        seen=[]
        def handle(request):
            body=json.loads(request.content);seen.append(body)
            payload=json.loads(body['messages'][1]['content'])
            return reply(body,json.loads(self.scorer.complete(payload,purpose='cognition_resolution')['text']))
        followup=self.followup()
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            resolver=ResolutionStore(self.state_dir,model=DeepSeekModel(self.scorer.settings,http_client=client))
            record=resolver.create(followup['id']);resolver.decide(record['id'],approved=True,approval_sha256=record['approval_sha256'])
            proposed=resolver.run(record['id'])
        self.assertEqual(proposed['status'],'proposed',proposed['error'])
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['tools'][0]['function']['name'],'CognitionResolutionResponse')
        self.assertTrue(seen[0]['tools'][0]['function']['strict'])

    def test_legacy_resolution_is_read_with_its_frozen_contract_without_rewriting(self):
        record=self.proposed()
        preview=self.resolver.show(record['id'],preview=True)
        envelope=deepcopy(preview['envelope']);result=deepcopy(preview['result'])
        properties=envelope['contract']['schema']['properties']['judgments']['items']['properties']['checks']['items']['properties']
        properties.update(start={'type':'integer'},end={'type':'integer'})
        schema=envelope['contract']['schema']['properties']['judgments']['items']['properties']['checks']['items']
        schema['required']+=['start','end']
        statements={r['reference_id']:r['statement'] for r in envelope['payload']['references']}
        for judgment in result['judgments']:
            self.assertEqual(len(judgment['checks']),1)
            judgment['checks'][0].update(start=0,end=len(statements[judgment['reference_id']]))
        result['limitations'][1]='character_coverage_not_semantic_proof'
        with self.resolver.open(write=True) as conn,conn:
            conn.execute('UPDATE resolutions SET envelope=?,envelope_sha256=?,result=? WHERE id=?',
                         (canonical(envelope),digest(envelope),canonical(result),record['id']))
        before=self.resolver.db_path.read_bytes()
        historical=self.resolver.show(record['id'])
        self.assertEqual(historical['result'],result)
        self.assertEqual(before,self.resolver.db_path.read_bytes())
        wire=deepcopy(result)
        for judgment in wire['judgments']:judgment.pop('outcome')
        with self.assertRaises(FileReadError):validate_resolution({k:wire[k] for k in ('judgments','conflicts')},envelope['payload'])
