"""Offline same-version focused investigations; no model-quality claims."""
from copy import deepcopy
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from tests import test_cognition as fixtures
from zest.cognition import CognitionEngine
from zest.cognition_followup import prepare_followup
from zest.cognition_store import CognitionStore, canonical, digest
from zest.files import FileReadError
from zest.management_context import build_management_context
from zest.survey import SurveyStore


class FocusedModel(fixtures.OfflineCognitionModel):
    def complete(self, payload, *, purpose='qa'):
        if purpose == 'project_followup_plan':
            self.calls.append((purpose, deepcopy(payload)))
            source = next(s for s in payload['material_catalog'] if s['path'] == 'app.py')
            return {'text': json.dumps({'overview': [], 'source_ids': [source['id']], 'gaps': []})}
        return super().complete(payload, purpose=purpose)


class FollowupTest(unittest.TestCase):
    setUpBase = fixtures.CognitionTest.setUpBase
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    cli = fixtures.CognitionTest.cli
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def setUp(self):
        fixtures.CognitionTest.setUp(self)
        self.original = self.engine.run(self.approved()['id'])
        self.origin = self.original['id']
        self.selected = next(f for f in self.original['findings'] if f['finding']['section'] == 'flows')
        self.finding_id = self.selected['id']
        with self.store.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_runs SET status='partial' WHERE id=?", (self.origin,))
            conn.execute("UPDATE cognition_findings SET status='needs_investigation' WHERE run_id=? AND id=?", (self.origin, self.finding_id))
            conn.execute("UPDATE cognition_jobs SET spec=json_set(spec,'$.goal','OLD_BROAD_GOAL') WHERE run_id=?", (self.origin,))
        self.before = self.store.show(self.origin, include_envelope=True, include_calls=True)

    def prepared(self):
        return prepare_followup(self.state_dir, self.origin, self.finding_id, target=self.target)

    def queued(self):
        record = self.prepared()
        self.store.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
        return record

    def alter(self, field, value, finding_id=None):
        with self.store.open(write=True) as conn, conn:
            identity = finding_id or self.finding_id
            if field in ('status', 'review'):
                conn.execute(f'UPDATE cognition_findings SET {field}=? WHERE run_id=? AND id=?', (canonical(value) if field == 'review' else value, self.origin, identity))
            else:
                finding = json.loads(conn.execute('SELECT finding FROM cognition_findings WHERE run_id=? AND id=?', (self.origin, identity)).fetchone()[0])
                finding[field] = value
                conn.execute('UPDATE cognition_findings SET finding=? WHERE run_id=? AND id=?', (canonical(finding), self.origin, identity))

    def test_complete_same_version_package_no_calls_no_old_task_or_shared_facts(self):
        (self.repo/'app.py').write_text('DIRTY_NOT_APPROVED = True\n')
        with patch('zest.models.DeepSeekModel.complete', side_effect=AssertionError('no model')), patch('zest.cognition.start_worker', side_effect=AssertionError('no worker')):
            record = self.prepared()
        full = self.store.show(record['id'], include_envelope=True, include_calls=True)
        env = full['envelope']; context = env['followup_context']
        self.assertEqual(record['status'], 'awaiting_approval')
        self.assertEqual(record['commit'], self.before['commit'])
        self.assertEqual(full['calls'], []); self.assertEqual(full['jobs'], [])
        self.assertEqual(context['sections'], ['flows'])
        self.assertEqual(env['shared_knowledge'], [])
        self.assertIn('def add', next(s['body'] for s in env['files'] if s['evidence']['path']=='app.py'))
        self.assertEqual({s['evidence']['path'] for s in env['files']}, {'README.md','app.py'})
        self.assertNotIn('DIRTY_NOT_APPROVED', canonical(env))
        self.assertNotIn('OLD_BROAD_GOAL', canonical(context))
        self.assertNotIn('"citations":', canonical(context))
        self.assertNotIn('"text":', canonical(context))
        self.assertNotEqual(record['approval_sha256'], self.before['approval_sha256'])
        self.assertEqual(self.store.show(self.origin, include_envelope=True, include_calls=True), self.before)
        with self.assertRaises(FileReadError): CognitionEngine(self.state_dir, model=FocusedModel()).run(record['id'])
        with self.assertRaises(FileReadError): self.store.decide(record['id'], approved=True, approval_sha256=self.before['approval_sha256'])

    def test_only_unresolved_observations_from_finished_current_version_are_allowed(self):
        for status in ('auto_reviewed','invalidated'):
            self.alter('status',status)
            with self.assertRaises(FileReadError): self.prepared()
        self.alter('status','pending_review'); self.alter('kind','inferred')
        with self.assertRaises(FileReadError): self.prepared()
        self.alter('kind','observed')
        self.assertEqual(self.prepared()['status'],'awaiting_approval')
        with self.store.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_runs SET status='reviewing' WHERE id=?", (self.origin,))
        with self.assertRaises(FileReadError): self.prepared()

    def test_version_movement_before_or_during_preparation_does_not_create_new_run(self):
        build = SurveyStore.build
        def move(store, *args, **kwargs):
            result = build(store,*args,**kwargs)
            (self.repo/'new.txt').write_text('new version\n'); self.commit('move')
            return result
        before = len(self.store.list_runs(self.project['id']))
        with patch.object(SurveyStore,'build',autospec=True,side_effect=move):
            with self.assertRaises(FileReadError) as caught: self.prepared()
        self.assertEqual(caught.exception.code,'cognition_followup_version_changed')
        self.assertEqual(len(self.store.list_runs(self.project['id'])),before)
        with self.assertRaises(FileReadError): self.prepared()

    def test_corrupt_original_proof_is_not_promoted_to_a_followup_package(self):
        ev = deepcopy(self.selected['finding']['evidence']); ev[0]['text']='forged source'
        self.alter('evidence',ev)
        with self.assertRaises(FileReadError) as caught: self.prepared()
        self.assertEqual(caught.exception.code,'cognition_followup_source_invalid')

    def test_conflicts_and_located_unknowns_are_frozen_and_guarded(self):
        related = next(f for f in self.original['findings'] if f['id']!=self.finding_id)
        transitive = next(f for f in self.original['findings'] if f['id'] not in (self.finding_id, related['id']))
        review = {'reason':'两侧陈述有冲突。','conflicts':[{'reference_id':related['id'],'reason':'属性不同。'},
                     {'reference_id':self.selected['job_id']+':U1','reason':'未知项有相反陈述。'}]}
        self.alter('review',review)
        self.alter('review',{'reason':'关联冲突链。','conflict_links':[
            {'reference_id':self.finding_id,'reason':'返回原发现。'},
            {'reference_id':transitive['id'],'reason':'关联另一项陈述。'}]},related['id'])
        with self.store.open(write=True) as conn, conn:
            result = json.loads(conn.execute('SELECT result FROM cognition_jobs WHERE run_id=? AND id=?',(self.origin,self.selected['job_id'])).fetchone()[0])
            result['report']['unknowns']=['待核对相反取值。']
            conn.execute('UPDATE cognition_jobs SET result=? WHERE run_id=? AND id=?',(canonical(result),self.origin,self.selected['job_id']))
        record = self.queued(); context=self.store.show(record['id'],include_envelope=True)['envelope']['followup_context']
        self.assertEqual({f['finding_id'] for f in context['source']['related_findings']},{related['id'],transitive['id']})
        self.assertEqual(context['source']['related_unknowns'][0]['statement'],'待核对相反取值。')
        self.alter('status','invalidated',transitive['id'])
        model=FocusedModel(); result=CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['error']['code'],'cognition_followup_source_changed')
        self.assertEqual(model.calls,[])

    def test_changed_statement_review_or_status_blocks_first_request(self):
        for field,value in [('statement','改写后的结论。'),('review',{'reason':'新的核对意见。'}),('status','auto_reviewed')]:
            with self.subTest(field=field):
                record=self.queued()
                self.alter(field,value)
                model=FocusedModel(); result=CognitionEngine(self.state_dir,model=model).run(record['id'])
                self.assertEqual(result['error']['code'],'cognition_followup_source_changed')
                self.assertEqual(model.calls,[])
                if field=='status': self.alter('status','needs_investigation')
                elif field=='review': self.alter('review',self.selected['review'])
                else: self.alter('statement',self.selected['finding']['statement'])

    def test_focused_approved_flow_rereads_sources_and_completes_one_section_only(self):
        record=self.queued(); model=FocusedModel()
        result=CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['status'],'completed',result['error'])
        self.assertEqual(len(result['jobs']),1)
        self.assertEqual(result['jobs'][0]['spec']['sections'],['flows'])
        self.assertEqual(result['jobs'][0]['spec']['goal'],self.store.show(record['id'],include_envelope=True)['envelope']['followup_context']['goal'])
        self.assertEqual(len(result['findings']),1)
        self.assertTrue(result['findings'][0]['finding']['evidence'][0]['text'])
        self.assertEqual(result['findings'][0]['finding']['evidence'][0]['evidence']['commit'],self.before['commit'])
        from zest.cognition_views import format_dossier
        dossier=self.store.dossier(record['id'])
        self.assertEqual(dossier['followup']['source_run_id'],self.origin)
        self.assertIn('本轮完成仅针对选定问题',format_dossier(dossier))
        for purpose,payload in model.calls:
            if purpose!='project_review':
                self.assertIn('followup_context',payload)
                self.assertEqual(payload['shared_knowledge'],[])
                self.assertNotIn('OLD_BROAD_GOAL',canonical(payload))
        self.assertEqual(self.store.show(self.origin,include_envelope=True,include_calls=True),self.before)
        context=build_management_context(self.state_dir,self.project['id'])
        self.assertIn(self.origin,[a['run_id'] for a in context['cognition']['followup_actions']])

    def test_old_broad_plan_cannot_replace_program_frozen_goal(self):
        record=self.queued(); model=FocusedModel(); original=model.complete
        def broad(payload,*,purpose='qa'):
            if purpose=='project_followup_plan':
                model.calls.append((purpose,payload))
                return {'text':json.dumps({'overview':[],'tasks':[{'goal':'OLD_BROAD_GOAL'}],'gaps':[]})}
            return original(payload,purpose=purpose)
        model.complete=broad
        result=CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['jobs'],[])
        self.assertEqual(len(model.calls),1)

    def test_source_change_after_planning_blocks_further_calls(self):
        record=self.queued(); model=FocusedModel(); original=model.complete
        def change(payload,*,purpose='qa'):
            value=original(payload,purpose=purpose)
            if purpose=='project_followup_plan':self.alter('status','invalidated')
            return value
        model.complete=change
        result=CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['error']['code'],'cognition_followup_source_changed')
        self.assertEqual(len(model.calls),1)
        self.assertEqual(result['findings'],[])

    def test_saved_planning_response_is_reused_after_checkpoint_interruption(self):
        record=self.queued(); model=FocusedModel(); engine=CognitionEngine(self.state_dir,model=model)
        with patch.object(engine.store,'save_plan',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):engine.run(record['id'])
        self.store.recover(record['id'])
        result=engine.run(record['id'])
        self.assertEqual(result['status'],'completed',result['error'])
        self.assertEqual(sum(p=='project_followup_plan' for p,_ in model.calls),1)

    def test_old_lead_cannot_manufacture_a_body_citation(self):
        record=self.queued(); model=FocusedModel(); original=model.complete
        def fake(payload,*,purpose='qa'):
            if purpose in ('project_research','project_report'):
                model.calls.append((purpose,payload))
                return {'text':json.dumps({'actions':[],'report':{'status':'completed','findings':[{'section':'flows','kind':'observed','statement':'直接复用旧线索。','citations':['R1']}],'unknowns':[],'material_requests':[]}})}
            return original(payload,purpose=purpose)
        model.complete=fake
        result=CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['findings'],[])
        self.assertTrue(result['jobs'][0]['result']['report_validation']['rejected_findings'])

    def test_cli_preparation_has_progress_and_never_starts_worker(self):
        action=build_management_context(self.state_dir,self.project['id'])['cognition']['followup_actions'][0]
        self.assertEqual(action['prepare_command'][3:],['cognition','followup-create',self.origin,self.finding_id])
        with patch('zest.models.ModelSettings.from_env',return_value=FocusedModel().settings),patch('zest.cognition.start_worker',side_effect=AssertionError('no worker')):
            code,out,err=self.cli('cognition','followup-create',self.origin,self.finding_id,'--format','text','--progress')
        self.assertEqual(code,0,err)
        self.assertIn('定向调查',out);self.assertIn('完成标准仅针对选定问题',out)
        self.assertIn('等待批准',err)

    def test_legacy_source_and_project_local_new_run_share_no_copied_records(self):
        from zest.storage import ProjectRegistry
        from zest.git import inspect_repository
        from zest.resources import record_state
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo),workspace=True)
        record = self.queued()
        self.assertEqual(record_state(self.state_dir,'cognition',record['id']),Path(project['resource_dir']))
        model = FocusedModel()
        result = CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['status'],'completed',result['error'])
        with self.store.open() as conn:
            self.assertIsNone(conn.execute('SELECT id FROM cognition_runs WHERE id=?',(record['id'],)).fetchone())
        self.assertEqual(self.store.show(self.origin,include_envelope=True,include_calls=True),self.before)

    def test_missing_new_structured_contract_stops_before_any_transmission(self):
        record = self.prepared()
        envelope = self.store.show(record['id'],include_envelope=True)['envelope']
        del envelope['structured_outputs']['project_followup_plan']
        with self.store.open(write=True) as conn,conn:
            conn.execute('UPDATE cognition_runs SET envelope=?,envelope_sha256=? WHERE id=?',
                         (canonical(envelope),digest(envelope),record['id']))
        self.store.decide(record['id'],approved=True,approval_sha256=digest(envelope))
        model=FocusedModel();result=CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['error']['code'],'cognition_contract_changed')
        self.assertEqual(model.calls,[])

    def test_unknown_directed_planning_result_is_not_reissued(self):
        record=self.queued();model=FocusedModel()
        def unknown(payload,*,purpose='qa'):
            model.calls.append((purpose,payload));raise TimeoutError('offline unknown')
        model.complete=unknown
        result=CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(result['status'],'pending_execution')
        with self.assertRaises(FileReadError):self.store.recover(record['id'])
        with self.assertRaises(FileReadError):CognitionEngine(self.state_dir,model=model).run(record['id'])
        self.assertEqual(len(model.calls),1)
        self.assertEqual(self.store.show(self.origin,include_envelope=True,include_calls=True),self.before)
