"""Real local Git changes, offline model decisions; not semantic quality scores."""
import copy
import hashlib
import json
import re
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from tests.test_cognition import OfflineCognitionModel
from zest.cognition_store import CognitionStore, digest
from zest.document_pages import DocumentPages
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.initialization import InitializationStore
from zest.knowledge_snapshots import MANIFEST, freeze_context, read_record, snapshot_path
from zest.maintenance import MaintenanceStore
from zest.storage import ProjectRegistry


SECTIONS = {'alpha':['purpose','architecture','flows'], 'beta':['dependencies','development','constraints'], 'gamma':['flows']}


class ModuleModel(OfflineCognitionModel):
    linked = False

    def complete(self, payload, *, purpose='qa'):
        if purpose not in ('project_plan', 'project_research', 'project_report', 'document_plan', 'document_update'):
            return super().complete(payload, purpose=purpose)
        self.calls.append((purpose, payload))
        if purpose == 'project_plan':
            catalog = {s['path']: s['id'] for s in payload['material_catalog']}
            changed = payload.get('incremental_impact', {}).get('changed_paths')
            modules = [m for m in SECTIONS if m + '.py' in catalog and (changed is None or m + '.py' in changed)]
            if not modules: modules = ['beta']
            value = {'overview':[], 'gaps':[], 'tasks':[{'id':f'T{i}', 'title':m, 'goal':m,
                     'reason':'独立检查目标模块', 'source_ids':[catalog[m + '.py']], 'sections':SECTIONS[m],
                     'deliverable':'带证据的模块行为', 'acceptance':['核对函数实现'], 'depends_on':[], 'priority':1}
                     for i,m in enumerate(modules,1)]}
            covered={section for m in modules for section in SECTIONS[m]}
            value['gaps']=[{'section':s,'question':'[分析推导] 本轮不重新调查该领域，保留实际核验版本及证据检查记录。'}
                           for s in sorted(set().union(*SECTIONS.values())-covered)]
        elif purpose in ('project_research', 'project_report'):
            m = payload['task']['goal']
            source = next(s for s in payload['material_catalog'] if s['path'] == m + '.py')
            if not payload['tool_results']:
                value = {'actions':[{'tool':'read','args':{'source_id':source['id']}}], 'report':None}
            else:
                proof = next(r for r in payload['tool_results'] if r['tool'] == 'read')
                number = re.search(r'return (\d+)', proof['result']['data']['text'])[1]
                value = {'actions':[], 'report':{'status':'completed', 'findings':[
                    {'section':section,'kind':'observed','statement':f'模块 {m} 的 value 返回 {number}。', 'citations':[proof['citation_id']]}
                    for section in SECTIONS[m]], 'unknowns':[], 'material_requests':[]}}
        elif purpose == 'document_plan':
            pages=[]
            for m in SECTIONS:
                ids=[f['id'] for f in payload['findings'] if f'模块 {m} ' in f['statement']]
                if ids:
                    pages.append({'slug':m,'title':f'{m} 模块说明','description':'理解模块的实现与使用边界。','goal':'理解模块行为。',
                                  'finding_ids':ids,'related_pages':['alpha'] if m == 'beta' and self.linked else []})
            value={'pages':pages,'coverage_gaps':[]}
        else:
            changes=[]; aliases={}; unresolved=set()
            for p in payload['problems']:
                m = re.search(r'模块 (\w+)', p['fact']['statement'])[1]
                matches=[f for f in payload['current_findings'] if f['section'] == p['fact']['section'] and f'模块 {m} ' in f['statement']]
                if matches:
                    f=matches[0]; aliases[f['id']]=p['claim_id']
                    changes.append({'claim_id':p['claim_id'],'decision':'replaced','finding_ids':[f['id']],
                                    'evidence_statement':f['statement'],'reason':'同一模块、函数及条件的返回值被本轮原文明确更新。'})
                else:
                    unresolved.add(p['claim_id'])
                    changes.append({'claim_id':p['claim_id'],'decision':'unresolved','finding_ids':[],
                                    'evidence_statement':'','reason':'没有本轮证据回答该断言，不能按遗漏撤回。'})
            pages=[]; blocked=[]
            for p in payload['existing_pages']:
                if p['slug'] in payload['required_updates']:
                    if set(p['finding_ids']) & unresolved: blocked.append(p['slug'])
                    else: pages.append(copy.deepcopy(p))
            old={x for p in payload['existing_pages'] for x in p['finding_ids']}
            extra=[f['id'] for f in payload['current_findings'] if f['id'] not in aliases and f['id'] not in old]
            if extra:
                pages.append({'slug':'gamma','title':'新增 gamma 模块','description':'解释新增模块的职责。', 'goal':'了解新模块。',
                              'finding_ids':extra,'related_pages':[]})
            value={'claim_changes':changes,'pages':pages,'blocked_pages':blocked,'retired_pages':[],'coverage_gaps':[]}
        return {'text':json.dumps(value,ensure_ascii=False), 'reported_model':'offline-module-fixture'}


class IncrementalDocumentsTest(unittest.TestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def setUp(self):
        self.setUpBase()
        (self.repo/'README.md').write_text('# Two independent modules\n')
        (self.repo/'alpha.py').write_text('def value():\n    return 1\n')
        (self.repo/'beta.py').write_text('def value():\n    return 2\n')
        self.commit()
        self.project=ProjectRegistry(self.state_dir).register(inspect_repository(self.repo),workspace=True)
        self.model=ModuleModel()
        self.model.linked = self._testMethodName == 'test_related_pages_are_rewritten_but_claim_verification_dates_remain_honest'
        config=patch('zest.models.ModelSettings.from_env',return_value=self.model.settings)
        config.start(); self.addCleanup(config.stop)
        self.initial=InitializationStore(self.state_dir)
        prepared=self.initial.prepare(self.project['id'])
        with patch.object(InitializationStore,'launch'):
            self.initial.decide(self.project['id'],approved=True,sha256=prepared['research']['approval_sha256'])
        self.baseline=self.initial.run(self.project['id'],model=self.model,check_updates=False)
        self.assertEqual(self.baseline['document_generation']['completed'],2,self.baseline.get('error'))
        self.maintenance=MaintenanceStore(self.state_dir)
        self.maintenance.enable(self.project['id'])

    def snapshot(self, run_id):
        return read_record(snapshot_path(self.state_dir,run_id))

    def update(self, name='alpha', number=3):
        (self.repo/(name+'.py')).write_text(f'def value():\n    return {number}\n')
        self.commit('update '+name)
        with patch.object(MaintenanceStore,'launch'):
            self.maintenance.check(self.project['id'],reason='manual')
        return self.maintenance.run(self.project['id'],model=self.model)

    def test_single_module_update_preserves_other_page_bytes_mtime_and_actual_baseline(self):
        target=self.repo/'.zest/docs/initialization/beta.md'
        before=(target.read_bytes(),target.stat().st_mtime_ns)
        old=self.snapshot(self.baseline['run_id'])
        calls=len(self.model.calls)
        result=self.update()
        self.assertEqual(result['document_generation']['status'],'completed',result.get('error'))
        self.assertEqual(result['document_generation']['retained'],1)
        self.assertEqual((target.read_bytes(),target.stat().st_mtime_ns),before)
        writers=[p['page']['slug'] for purpose,p in self.model.calls[calls:] if purpose=='document_write']
        self.assertEqual(writers,['alpha'])
        snapshot=self.snapshot(result['active_run_id'])
        self.assertEqual(set(snapshot['claims']),set(old['claims']))
        beta=next(p for p in snapshot['pages'] if p['spec']['slug']=='beta')
        self.assertEqual(beta['last_generated_commit'],old['commit'])
        self.assertEqual(beta['last_verified_commit'],old['commit'])
        self.assertEqual(beta['evidence_checked_commit'],result['knowledge_commit'])
        self.assertEqual(sum(c['status']=='inherited' for c in snapshot['claims'].values()),3)
        self.assertTrue(all(c['revision']==2 for c in snapshot['claims'].values() if 'alpha' in c['fact']['statement']))
        self.assertEqual(result['summary']['reviewed_findings'],3)
        self.assertEqual(result['summary']['inherited_findings'],3)
        public=json.loads((self.repo/MANIFEST).read_bytes())['payload']
        self.assertNotIn('parent_snapshot',public)
        self.assertEqual(public['parent_manifest_sha256'],old['manifest_sha256'])

    def test_sparse_history_survives_two_updates_without_forgetting_untouched_claims(self):
        first=self.update('alpha',3)
        second=self.update('beta',4)
        snapshot=self.snapshot(second['active_run_id'])
        self.assertEqual(len(snapshot['claims']),6)
        self.assertEqual({c['last_verified_commit'] for c in snapshot['claims'].values() if 'alpha' in c['fact']['statement']},{first['knowledge_commit']})
        self.assertTrue(all('返回 3' in c['fact']['statement'] for c in snapshot['claims'].values() if 'alpha' in c['fact']['statement']))

    def test_new_unreferenced_module_gets_new_page_and_unchanged_pages_are_preserved(self):
        before={p.name:p.read_bytes() for p in (self.repo/'.zest/docs/initialization').glob('*.md') if p.name!='index.md'}
        result=self.update('gamma',9)
        self.assertEqual(result['document_generation']['retained'],2)
        self.assertEqual(result['document_generation']['completed'],1)
        self.assertIn('返回 9',(self.repo/'.zest/docs/initialization/gamma.md').read_text())
        for name,body in before.items(): self.assertEqual((self.repo/'.zest/docs/initialization'/name).read_bytes(),body)

    def test_deleted_source_never_retracts_or_silently_drops_old_claims(self):
        old=(self.repo/'.zest/docs/initialization/alpha.md').read_bytes()
        (self.repo/'alpha.py').unlink(); self.commit('remove alpha source')
        with patch.object(MaintenanceStore,'launch'):
            self.maintenance.check(self.project['id'],reason='manual')
        result=self.maintenance.run(self.project['id'],model=self.model)
        snapshot=self.snapshot(result['active_run_id'])
        self.assertEqual(sum(c['status']=='unresolved' for c in snapshot['claims'].values()),3)
        self.assertFalse(any(c['status']=='retracted' for c in snapshot['claims'].values()))
        self.assertEqual((self.repo/'.zest/docs/initialization/alpha.md').read_bytes(),old)
        self.assertEqual(next(p['status'] for p in result['document_generation']['pages'] if p['slug']=='alpha'),'blocked')

    def test_static_import_dependency_marks_caller_even_when_its_own_blob_is_unchanged(self):
        # Add an import, establish one checked generation, then change only beta.
        (self.repo/'alpha.py').write_text('import beta\n\ndef value():\n    return 1\n')
        self.commit('declare dependency')
        with patch.object(MaintenanceStore,'launch'): self.maintenance.check(self.project['id'],reason='manual')
        first=self.maintenance.run(self.project['id'],model=self.model)
        (self.repo/'beta.py').write_text('def value():\n    return 8\n'); self.commit('change dependency')
        from zest.cognition_refresh import prepare_refresh
        run=prepare_refresh(self.state_dir,self.project['id'],base_run_id=first['active_run_id'])
        context=CognitionStore(self.state_dir).show(run['id'],include_envelope=True)['envelope']['incremental_context']
        ids=[key for key,c in context['baseline']['claims'].items() if 'alpha' in c['fact']['statement']]
        self.assertTrue(all(context['checks'][key]['affected'] for key in ids))
        self.assertTrue(all('beta.py' in context['checks'][key]['dependency_paths'] for key in ids))

    def test_line_shift_relocates_exact_text_without_changing_claim_identity(self):
        old=self.snapshot(self.baseline['run_id'])
        (self.repo/'alpha.py').write_text('\n\ndef value():\n    return 1\n'); self.commit('line shift')
        from zest.cognition_refresh import prepare_refresh
        run=prepare_refresh(self.state_dir,self.project['id'],base_run_id=self.baseline['run_id'])
        context=CognitionStore(self.state_dir).show(run['id'],include_envelope=True)['envelope']['incremental_context']
        for key,c in old['claims'].items():
            if 'alpha' in c['fact']['statement']:
                self.assertEqual(context['checks'][key]['status'],'relocated')
                self.assertEqual(context['checks'][key]['evidence'][0]['evidence']['start_line'],3)

    def test_unknown_incremental_planner_is_never_replayed(self):
        original=self.model.complete
        def unknown(payload,*,purpose='qa'):
            if purpose=='document_update':
                self.model.calls.append((purpose,payload)); raise FileReadError('model_request_failed','fixture unknown')
            return original(payload,purpose=purpose)
        with patch.object(self.model,'complete',side_effect=unknown): result=self.update()
        self.assertEqual(result['document_generation']['error']['code'],'cognition_execution_unknown')
        count=len(self.model.calls)
        with patch.object(MaintenanceStore,'launch'): self.maintenance.resume(self.project['id'])
        self.maintenance.run(self.project['id'],model=self.model)
        self.assertEqual(len(self.model.calls),count)

    def test_manifest_is_portable_and_foreign_import_is_not_knowledge_or_write_authority(self):
        raw=(self.repo/MANIFEST).read_bytes()
        portable=json.loads(raw)['payload']
        self.assertNotIn(str(self.repo).encode(),raw)
        self.assertNotIn(self.model.settings.api_key.encode(),raw)
        self.assertFalse(any('text' in p for c in portable['claims'].values() for p in c['fact']['evidence']))
        self.commit('share generated documentation')
        from zest.knowledge_snapshots import shared_context
        # Simulate a new local reader using the committed public manifest only.
        from zest.cognition_store import prepare_envelope
        from zest.survey import SurveyStore
        from zest.models import model_target
        survey=SurveyStore(self.state_dir).build(self.project)
        envelope=prepare_envelope(self.project,survey,model_target(self.model.settings),project_scope=True)
        context=shared_context(self.project,envelope)
        self.assertEqual(context['baseline']['trust'],'shared_unverified')
        self.assertTrue(all(c['authority']=='shared_unverified' for c in context['baseline']['claims'].values()))
        self.assertTrue(all('published_sha256' not in p for p in context['baseline']['pages']))

    def test_related_pages_are_rewritten_but_claim_verification_dates_remain_honest(self):
        count=len(self.model.calls)
        result=self.update()
        writers=[p['page']['slug'] for purpose,p in self.model.calls[count:] if purpose=='document_write']
        self.assertEqual(set(writers),{'alpha','beta'})
        snapshot=self.snapshot(result['active_run_id'])
        beta=next(p for p in snapshot['pages'] if p['spec']['slug']=='beta')
        self.assertEqual(beta['last_verified_commit'],result['knowledge_commit'])
        self.assertTrue(all(c['last_verified_commit']==self.baseline['commit'] for c in snapshot['claims'].values() if 'beta' in c['fact']['statement']))
        self.assertIn('部分事实沿用实际核验版本',(self.repo/'.zest/docs/initialization/beta.md').read_text())

    def test_human_edit_gets_candidate_while_other_page_updates(self):
        target=self.repo/'.zest/docs/initialization/beta.md'
        target.write_text('# Team explanation\nPreserve our notes.\n')
        result=self.update()
        self.assertEqual(target.read_text(),'# Team explanation\nPreserve our notes.\n')
        page=next(p for p in result['document_generation']['pages'] if p['slug']=='beta')
        self.assertEqual(page['status'],'skipped')
        self.assertTrue((self.repo/page['candidate']).exists())
        self.assertIn('返回 3',(self.repo/'.zest/docs/initialization/alpha.md').read_text())

    def test_crash_after_delta_receipt_reuses_it_and_only_finishes_remaining_page(self):
        from zest.incremental_documents import validate_update
        stopped=False
        def crash(*args,**kwargs):
            nonlocal stopped
            if not stopped:
                stopped=True
                raise RuntimeError('fixture process crash after durable delta receipt')
            return validate_update(*args,**kwargs)
        with patch('zest.incremental_documents.validate_update',side_effect=crash), self.assertRaises(RuntimeError):
            self.update()
        self.assertEqual(sum(p=='document_update' for p,_ in self.model.calls),1)
        with patch.object(MaintenanceStore,'launch'): self.maintenance.resume(self.project['id'])
        result=self.maintenance.run(self.project['id'],model=self.model)
        self.assertEqual(result['document_generation']['completed'],1)
        self.assertEqual(result['document_generation']['retained'],1)
        self.assertEqual(sum(p=='document_update' for p,_ in self.model.calls),1)

    def test_same_commit_with_missing_or_edited_document_is_not_reported_fully_current(self):
        count=len(self.model.calls)
        target=self.repo/'.zest/docs/initialization/beta.md'
        target.unlink()
        result=self.maintenance.check(self.project['id'],reason='manual')
        self.assertEqual(result['status'],'partial')
        self.assertIn({'path':'.zest/docs/initialization/beta.md','status':'missing'},result['document_integrity'])
        self.assertEqual(len(self.model.calls),count)

    def test_corrupt_baseline_stops_before_any_new_model_request(self):
        path=snapshot_path(self.state_dir,self.baseline['run_id'])
        value=json.loads(path.read_bytes()); value['payload']['claims'].clear()
        path.write_text(json.dumps(value))
        count=len(self.model.calls)
        result=self.update()
        self.assertEqual(result['error']['code'],'knowledge_snapshot_error')
        self.assertEqual(len(self.model.calls),count)

    def test_fresh_clone_rebuilds_shared_ids_without_promoting_uninvestigated_claims_or_overwriting_docs(self):
        import subprocess
        self.commit('share project documentation')
        clone=self.root/'replica'
        subprocess.run(['git','clone','--quiet',str(self.repo),str(clone)],check=True,capture_output=True)
        state=self.root/'replica-state'
        project=ProjectRegistry(state).register(inspect_repository(clone),workspace=True)
        before=(clone/'.zest/docs/initialization/alpha.md').read_bytes()
        initial=InitializationStore(state)
        prepared=initial.prepare(project['id'])
        envelope=CognitionStore(state).show(prepared['run_id'],include_envelope=True)['envelope']
        self.assertEqual(envelope['incremental_context']['baseline']['trust'],'shared_unverified')
        with patch.object(InitializationStore,'launch'):
            initial.decide(project['id'],approved=True,sha256=prepared['research']['approval_sha256'])
        result=initial.run(project['id'],model=ModuleModel(),check_updates=False)
        self.assertEqual(result['stage'],'published',result.get('error'))
        snapshot=read_record(snapshot_path(state,result['run_id']))
        alpha=[c for c in snapshot['claims'].values() if 'alpha' in c['fact']['statement']]
        self.assertTrue(all(c['status']=='unresolved' for c in alpha))
        self.assertTrue(all(c['authority']=='shared_unverified' for c in alpha))
        self.assertEqual((clone/'.zest/docs/initialization/alpha.md').read_bytes(),before)
        self.assertIn(MANIFEST,result['conflicts'])
        self.assertTrue((clone/result['document_generation']['manifest_candidate']).is_file())

    def test_failed_publication_keeps_old_page_claim_revision_until_resume(self):
        from zest.initialization import atomic
        old=self.snapshot(self.baseline['run_id'])
        original=(self.repo/'.zest/docs/initialization/alpha.md').read_bytes()
        def no_disk(path,raw):
            if path.name=='alpha.md': raise OSError('fixture page write failure')
            return atomic(path,raw)
        with patch('zest.initialization.atomic',side_effect=no_disk): result=self.update()
        snapshot=self.snapshot(result['active_run_id'])
        page=next(p for p in snapshot['pages'] if p['spec']['slug']=='alpha')
        self.assertEqual(page['status'],'failed')
        self.assertEqual((self.repo/'.zest/docs/initialization/alpha.md').read_bytes(),original)
        self.assertTrue(all(b['revision']==1 for b in page['claim_bindings'].values()))
        self.assertTrue(all(snapshot['claims'][key]['revision']==2 for key in page['spec']['finding_ids']))
        self.assertEqual(page['last_verified_commit'],old['commit'])
        count=len(self.model.calls)
        with patch.object(MaintenanceStore,'launch'): self.maintenance.resume(self.project['id'])
        restored=self.maintenance.run(self.project['id'],model=self.model)
        page=next(p for p in self.snapshot(restored['active_run_id'])['pages'] if p['spec']['slug']=='alpha')
        self.assertTrue(all(b['revision']==2 for b in page['claim_bindings'].values()))
        self.assertEqual(len(self.model.calls),count)

    def test_invalidated_origin_stops_inheritance_before_new_model_calls(self):
        store=CognitionStore(self.state_dir)
        finding=store.show(self.baseline['run_id'])['findings'][0]
        store.invalidate(self.baseline['run_id'],finding['id'],'fixture revocation')
        before=len(self.model.calls)
        result=self.update()
        self.assertEqual(result['error']['code'],'cognition_knowledge_changed')
        self.assertEqual(len(self.model.calls),before)

    def test_hostile_shared_path_is_rejected_even_with_recomputed_manifest_checksum(self):
        from zest.knowledge_snapshots import shared_context
        value=json.loads((self.repo/MANIFEST).read_bytes())['payload']
        claim=next(iter(value['claims'].values()))
        claim['fact']['evidence'][0]['evidence']['path']='../../private.txt'
        raw=json.dumps({'payload':value,'sha256':digest(value)})
        envelope={'files':[{'evidence':{'path':MANIFEST}}]}
        with patch('zest.project_sources.load_source',return_value={'body':raw}), self.assertRaises(FileReadError) as caught:
            shared_context(self.project,envelope)
        self.assertEqual(caught.exception.code,'shared_knowledge_invalid')


class IncrementalContractTest(unittest.TestCase):
    def fixture(self):
        old={'id':'K'+'1'*24,'revision':1,'origin':'old-run:T1:F1','status':'verified','last_verified_commit':'old',
             'fact':{'id':'K'+'1'*24,'section':'flows','statement':'alpha 返回 1。',
                     'evidence':[{'text':'return 1','evidence':{'path':'alpha.py'}}]}}
        fresh={'id':'K'+'2'*24,'section':'flows','statement':'alpha 已改为返回 3，原返回 1 的断言不成立。',
               'evidence':[{'text':'return 3','evidence':{'path':'alpha.py'}}]}
        from zest.knowledge_snapshots import new_claim
        new=new_claim(fresh,'new'); new['id']=fresh['id']; new['fact']['id']=fresh['id']
        claims={old['id']:old,fresh['id']:new}
        page={'slug':'alpha','title':'模块说明','description':'理解返回规则。','goal':'了解返回行为。','finding_ids':[old['id']],'related_pages':[]}
        context={'baseline':{'claims':{old['id']:copy.deepcopy(old)},'pages':[{'spec':page}]}}
        response={'claim_changes':[{'claim_id':old['id'],'decision':'retracted','finding_ids':[fresh['id']],
                                   'evidence_statement':fresh['statement'],'reason':'同一返回条件下，新事实明确否定旧值。'}],
                  'pages':[dict(page,finding_ids=[fresh['id']])],'blocked_pages':[],'retired_pages':[],'coverage_gaps':[]}
        return old,fresh,claims,context,response

    def test_explicit_retraction_retains_history_and_requires_actual_current_evidence(self):
        from zest.incremental_documents import validate_update
        from zest.document_contracts import document_contract
        old,fresh,claims,context,response=self.fixture()
        _,active,_=validate_update(response,context,claims,{fresh['id']:fresh},[old['id']],{'alpha'},document_contract(),'new')
        self.assertEqual(claims[old['id']]['status'],'retracted')
        self.assertNotIn(old['id'],active)
        self.assertEqual(claims[old['id']]['fact']['statement'],'alpha 返回 1。')
        for mutate in (lambda v:v['claim_changes'][0].update(finding_ids=[]),
                       lambda v:v['claim_changes'][0].update(evidence_statement='无依据的删除理由'),
                       lambda v:v.update(claim_changes=[])):
            old,fresh,claims,context,response=self.fixture(); mutate(response)
            with self.assertRaises(FileReadError): validate_update(response,context,claims,{fresh['id']:fresh},[old['id']],{'alpha'},document_contract(),'new')

    def test_unrelated_page_cannot_be_replanned_and_active_facts_cannot_be_omitted(self):
        from zest.incremental_documents import validate_update
        from zest.document_contracts import document_contract
        old,fresh,claims,context,response=self.fixture()
        with self.assertRaises(FileReadError): validate_update(response,context,claims,{fresh['id']:fresh},[old['id']],set(),document_contract(),'new')


class DependencyHintsTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def test_java_service_implementation_and_mapper_xml_are_traced_as_hints(self):
        contents={'Controller.java':'class Controller { AlphaService service; }',
                  'AlphaService.java':'interface AlphaService { int value(); }',
                  'AlphaServiceImpl.java':'class AlphaServiceImpl implements AlphaService { AlphaMapper mapper; }',
                  'AlphaMapper.java':'interface AlphaMapper { int value(); }',
                  'AlphaMapper.xml':'<mapper namespace="demo.AlphaMapper"><select id="value">SELECT 1</select></mapper>',
                  'Other.java':'class Other { int value() { return 2; } }'}
        for name,body in contents.items(): (self.repo/name).write_text(body)
        self.commit()
        from zest.knowledge_snapshots import closure, dependencies
        from zest.project_sources import inventory
        commit=self.git('rev-parse','HEAD').decode().strip()
        graph=dependencies(self.repo,{'files':inventory(self.repo,commit,[])},['Controller.java'])
        paths=closure(['Controller.java'],[graph])
        self.assertIn('AlphaServiceImpl.java',paths)
        self.assertIn('AlphaMapper.xml',paths)
        self.assertNotIn('Other.java',paths)
        self.assertTrue(graph['complete'])

    def test_unknown_language_is_conservative_and_same_blob_reuses_its_exact_range(self):
        (self.repo/'code.go').write_text('package main\n')
        (self.repo/'duplicate.py').write_text('def first():\n    return 1\n\ndef second():\n    return 1\n')
        self.commit()
        from zest.knowledge_snapshots import dependencies, relocate
        from zest.project_sources import inventory
        from zest.cognition_tools import PackageTools
        from zest.project_sources import SOURCE_SCOPE
        commit=self.git('rev-parse','HEAD').decode().strip()
        envelope={'files':inventory(self.repo,commit,[]),'source_scope':SOURCE_SCOPE}
        self.assertFalse(dependencies(self.repo,envelope,['code.go'])['complete'])
        source=next(s for s in envelope['files'] if s['evidence']['path']=='duplicate.py')
        proof=PackageTools(envelope,root=self.repo).read(source['id'],2,2)
        check=relocate(self.repo,envelope,{'evidence':[proof]})
        self.assertEqual(check['status'],'unchanged')
        self.assertEqual(check['evidence'][0]['evidence']['start_line'],2)


if __name__=='__main__': unittest.main()
