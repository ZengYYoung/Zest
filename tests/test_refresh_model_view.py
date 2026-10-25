"""Historical hints are bounded without rewriting approvals or original proof."""
from copy import deepcopy
from unittest import TestCase
from zest.cognition_context import refresh_model_view
from zest.cognition_contracts import canonical


class RefreshModelViewTest(TestCase):
    def context(self):
        return {'schema_version': 1, 'model_view_version': 1, 'project_id': 'p', 'target_commit': 'new',
            'baseline': {'run_id': 'old-run', 'commit': 'old'}, 'semantics': 'historical_hints_only',
            'requires_discussion': False, 'focus': [{'source_id': 'S1', 'path': 'app.py', 'availability': 'in_package', 'change': 'modified'}],
            'changes': [{'path': 'app.py', 'change': 'modified', 'before_object': 'old-blob', 'after_object': 'new-blob'}],
            'uncovered_changes': [], 'warnings': [],
            'historical_leads': [{'run_id': 'old-run', 'finding_id': f'T1:F{i}', 'finding_sha256': str(i),
                'original_status': 'needs_investigation', 'section': 'flows', 'kind': 'inferred',
                'statement': '这是一条很长的旧调查线索，尚未确认。' * 10, 'classification': 'unresolved',
                'evidence': [{'path': 'app.py', 'commit': 'old', 'blob_id': 'old-blob'}]} for i in range(210)]}

    def test_large_backlog_is_not_repeated_wholesale_in_each_model_request(self):
        context = self.context()
        before = deepcopy(context)
        view = refresh_model_view(context)
        self.assertLess(len(canonical(view)), 9000)
        self.assertEqual(context, before)
        self.assertEqual(view['coverage']['historical_leads_total'], 210)
        self.assertLess(view['coverage']['historical_leads_included'], 210)
        self.assertTrue(view['coverage']['omitted_hints_are_not_resolved_or_verified'])
        for item in view['historical_leads']:
            self.assertEqual(item['statement'], next(old['statement'] for old in context['historical_leads'] if old['finding_id'] == item['finding_id']))
        self.assertEqual(view['changes'], [{'path': 'app.py', 'change': 'modified'}])

    def test_legacy_requests_remain_exact_and_task_hints_do_not_leak_other_paths(self):
        context = self.context()
        context.pop('model_view_version')
        self.assertIs(refresh_model_view(context), context)
        context['model_view_version'] = 1
        view = refresh_model_view(context, {'S2'})
        self.assertEqual(view['historical_leads'], [])
        self.assertEqual(view['focus'], [])
        self.assertEqual(view['changes'], [])
