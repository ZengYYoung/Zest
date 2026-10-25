"""Project-scoped source discovery, context windows and legacy isolation, offline."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from tests import test_initialization as fixtures
from zest.cognition import CognitionEngine
from zest.cognition_context import compact_proof
from zest.cognition_store import CognitionStore
from zest.cognition_tools import PackageTools
from zest.files import FileReadError
from zest.initialization import InitializationStore
from zest.maintenance import MaintenanceStore
from zest.management_context import _proofs
from zest.project_context import research_view
from zest.project_sources import SOURCE_SCOPE
from zest.resources import record_state
from zest.understanding_approval import UnderstandingApproval


class ProjectSourcesTest(unittest.TestCase):
    setUpBase = fixtures.InitializationTest.setUpBase
    git = fixtures.InitializationTest.git
    commit = fixtures.InitializationTest.commit

    def setUp(self):
        fixtures.InitializationTest.setUp(self)
        self.enterContext(patch.object(InitializationStore, 'launch'))
        self.enterContext(patch.object(MaintenanceStore, 'launch'))

    def full(self):
        ready = self.store.prepare(self.project['id'])
        return CognitionStore(self.state_dir).show(ready['run_id'], include_envelope=True)

    def many_files(self, *, large=False):
        folder = self.repo / 'modules'
        folder.mkdir()
        for number in range(215):
            (folder / f'a{number:03d}.py').write_text('# ' + ('context ' * (2800 if large else 1)) + '\n')
        (folder / 'zz_late.py').write_text('def late_feature():\n    return 42\n')
        self.commit('many source files')

    def test_inventory_exceeds_survey_file_and_byte_limits_without_loading_late_body(self):
        self.many_files(large=True)
        record = self.full()
        envelope = record['envelope']
        self.assertGreater(record['file_count'], 200)
        self.assertFalse(record['scope_review']['requires_discussion'])
        self.assertFalse(envelope['survey_coverage']['inventory_scan_finished'])
        late = next(s for s in envelope['files'] if s['evidence']['path'] == 'modules/zz_late.py')
        self.assertNotIn('body', late)
        self.assertEqual(envelope['source_scope'], SOURCE_SCOPE)
        self.assertNotIn(str(self.repo), json.dumps(envelope))
        before = deepcopy(envelope)
        tools = PackageTools(envelope, root=self.repo)
        self.assertEqual(tools.list_files('zz_late')['matches'][0]['id'], late['id'])
        proof = tools.read(late['id'])
        self.assertIn('return 42', proof['text'])
        self.assertEqual(proof['evidence']['commit'], record['commit'])
        (self.repo / 'modules/zz_late.py').write_text('def late_feature():\n    return 99\n')
        self.assertEqual(PackageTools(envelope, root=self.repo).read(late['id']), proof)
        self.assertEqual(envelope, before)

    def test_full_scope_search_and_symbols_find_late_sources_with_bounded_cache(self):
        self.many_files()
        envelope = self.full()['envelope']
        tools = PackageTools(envelope, root=self.repo)
        self.assertTrue(any(m['path'] == 'modules/zz_late.py' for m in tools.search('late_feature')['matches']))
        self.assertTrue(tools.symbols('late_feature')['matches'])
        self.assertLessEqual(len(tools.cache), 32)

    def test_private_paths_links_and_sensitive_contents_never_become_read_evidence(self):
        self.many_files()
        (self.repo / '.env').write_text('PRIVATE=value\n')
        (self.repo / 'modules/zz_secret.py').write_text('key = "sk-' + 'X' * 30 + '"\n')
        (self.repo / 'linked.py').symlink_to('/etc/passwd')
        self.commit('unsafe material')
        envelope = self.full()['envelope']
        tools = PackageTools(envelope, root=self.repo)
        paths = {s['evidence']['path'] for s in envelope['files']}
        self.assertNotIn('.env', paths)
        self.assertNotIn('linked.py', paths)
        secret = next(s['id'] for s in envelope['files'] if s['evidence']['path'] == 'modules/zz_secret.py')
        with self.assertRaises(FileReadError) as caught:
            tools.read(secret)
        self.assertEqual(caught.exception.code, 'sensitive_content')
        self.assertEqual(tools.search('XXXX')['matches'], [])
        with self.assertRaises(FileReadError):
            tools.read('../outside.py')

    def test_window_retains_durable_proofs_and_reuses_original_citation_identities(self):
        results = []
        for number in range(12):
            proof = {'source_id': f'S{number}', 'evidence': {'path': f'{number}.py', 'start_line': 1, 'end_line': 1},
                     'text': 'x' * 2700, 'lines': [{'number': 1, 'text': 'x' * 2700}]}
            results.append({'tool': 'read', 'citation_id': f'R{number}', 'result': {'ok': True, 'data': proof}})
        original = deepcopy(results)
        view = research_view(results)
        self.assertLess(len(view['tool_results']), len(results))
        self.assertEqual(len(view['evidence_catalog']), len(results))
        self.assertEqual(view['tool_results'][-1]['citation_id'], 'R11')
        self.assertEqual(view['tool_results'][-1]['result']['data'], compact_proof(results[-1]['result']['data']))
        self.assertEqual(results, original)

    def test_unsent_overlarge_planning_context_is_repartitioned_and_receipted_without_approval(self):
        record = self.full()
        store = CognitionStore(record_state(self.state_dir, 'cognition', record['id']))
        store.decide(record['id'], approved=True, approval_sha256=record['approval_sha256'])
        store.set_status(record['id'], 'planning')
        payload = {'material_catalog': [{'id': 'S1', 'path': 'app.py'}],
                   'starting_documents': [{'text': 'context' * 1000} for _ in range(20)]}
        original = deepcopy(payload)
        engine = CognitionEngine(self.state_dir, model=self.model)
        engine.call(record['id'], 'coordinator', 0, 'project_plan', payload, record['envelope'])
        saved = store.show(record['id'], include_calls=True)
        self.assertEqual(saved['call_count'], 1)
        self.assertEqual(saved['context_requests'], [])
        self.assertLessEqual(saved['calls'][0]['request']['chars'], 48000)
        self.assertIn('automatic_context_windows', json.loads(saved['calls'][0]['payload']))
        self.assertEqual(payload, original)
        engine.call(record['id'], 'coordinator', 0, 'project_plan', payload, record['envelope'])
        self.assertEqual(store.show(record['id'])['call_count'], 1)

    def test_project_permission_survives_internal_prompt_and_key_revision_but_not_destination_change(self):
        self.full()
        approval = UnderstandingApproval(self.state_dir)
        approval.configure(self.project['id'], enabled=True)
        with patch('zest.models.ModelSettings.from_env', return_value=replace(self.model.settings, revision='new-key')):
            self.assertTrue(approval.show(self.project['id'])['active'])
        with patch.dict('zest.understanding_approval.COGNITION_PROMPTS', {'internal_revision': 'changed instructions'}):
            self.assertTrue(approval.show(self.project['id'])['active'])
        with patch('zest.models.ModelSettings.from_env', return_value=replace(self.model.settings, base_url='https://another.invalid')):
            self.assertFalse(approval.show(self.project['id'])['active'])

    def test_full_automatic_workflow_discovers_reads_and_publishes_a_late_source(self):
        self.many_files()
        original = self.model.complete
        def investigate(payload, *, purpose='qa'):
            if purpose not in ('project_research', 'project_report'):
                return original(payload, purpose=purpose)
            self.model.calls.append((purpose, payload))
            values = payload['tool_results']
            reads = [r for r in values if r.get('citation_id')]
            if reads:
                result = {'actions': [], 'report': {'status': 'completed', 'findings': [
                    {'section': section, 'kind': 'observed', 'statement': 'late_feature 返回 42。',
                     'citations': [reads[-1]['citation_id']]} for section in payload['task']['sections']],
                    'unknowns': [], 'material_requests': []}}
            elif values:
                identity = values[-1]['result']['data']['matches'][0]['id']
                result = {'actions': [{'tool': 'read', 'args': {'source_id': identity}}], 'report': None}
            else:
                result = {'actions': [{'tool': 'list_files', 'args': {'query': 'zz_late', 'offset': 0}}], 'report': None}
            return {'text': json.dumps(result, ensure_ascii=False)}
        with patch.object(self.model, 'complete', side_effect=investigate):
            ready = self.store.begin(self.project['id'])
            result = self.store.run(self.project['id'], model=self.model, check_updates=False)
        self.assertEqual(result['status'], 'completed', result)
        record = CognitionStore(self.state_dir).show(ready['run_id'], include_envelope=True, include_calls=True)
        self.assertEqual(record['context_requests'], [])
        self.assertEqual(record['call_count'], len(self.model.calls))
        self.assertTrue(all('zz_late.py' in f['finding']['evidence'][0]['evidence']['path'] for f in record['findings']))
        catalog = {s['id']: s['evidence'] for s in record['envelope']['files']}
        self.assertTrue(_proofs(record['findings'][0]['finding'], catalog, record['commit'])[1])
        from zest.cognition_repair import repair_context
        self.assertTrue(repair_context(CognitionStore(self.state_dir), record['id'])['leads'])
        before = len(self.model.calls)
        self.store.run(self.project['id'], model=self.model, check_updates=False)
        self.assertEqual(len(self.model.calls), before)
        (self.repo / 'modules/zz_late.py').write_text('def late_feature():\n    return 43\n')
        target = self.commit('change late feature')
        from zest.cognition_refresh import prepare_refresh
        refreshed = prepare_refresh(self.state_dir, self.project['id'])
        envelope = CognitionStore(self.state_dir).show(refreshed['id'], include_envelope=True)['envelope']
        focus = next(item for item in envelope['refresh_context']['focus'] if item['path'] == 'modules/zz_late.py')
        self.assertEqual(focus['availability'], 'in_package')
        self.assertTrue(any(item['classification'] == 'directly_affected'
                            for item in envelope['refresh_context']['historical_leads']))
        proof = PackageTools(envelope, root=self.repo).read(focus['source_id'])
        self.assertIn('return 43', proof['text'])
        self.assertEqual(proof['evidence']['commit'], target)
        self.assertEqual(len(self.model.calls), before)

    def test_unrecognised_source_contract_is_refused_before_reading(self):
        with self.assertRaises(FileReadError) as caught:
            PackageTools({'files': [], 'source_scope': {**SOURCE_SCOPE, 'version': 999}}, root=self.repo)
        self.assertEqual(caught.exception.code, 'cognition_contract_changed')


if __name__ == '__main__':
    unittest.main()
