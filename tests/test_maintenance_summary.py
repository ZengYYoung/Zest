"""Metadata-only summaries, safe paths, and bounded public receipts."""
from unittest import TestCase
from unittest.mock import patch
from tests import test_projects as fixtures
from zest.git import compare_commits
from zest.maintenance_summary import meaningful_changes, summarize_changes


class SummaryTest(TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def test_receipt_counts_changes_and_limits_details_without_model_or_source_bodies(self):
        (self.repo / 'before.py').write_text('old\n')
        (self.repo / 'edit.py').write_text('before\n')
        before = self.commit()
        (self.repo / 'before.py').unlink()
        (self.repo / 'edit.py').write_text('after\n')
        for i in range(35):
            (self.repo / f'added{i}.py').write_text('new\n')
        after = self.commit('新增计算接口并调整入口')
        project = {'root_path': str(self.repo)}
        changes, excluded = meaningful_changes(project, after, compare_commits(self.repo, before, after), {})
        with patch('zest.maintenance_summary.read_project_file', side_effect=AssertionError('not source analysis')):
            summary = summarize_changes(project, before, after, changes, excluded)
        self.assertEqual(summary['file_count'], 37)
        self.assertEqual(summary['counts'], {'added': 35, 'modified': 1, 'deleted': 1, 'type_changed': 0})
        self.assertEqual(len(summary['files']), 30)
        self.assertEqual(summary['more_files'], 7)
        self.assertEqual(summary['commit_subjects'], ['新增计算接口并调整入口'])
        self.assertNotIn('body', str(summary))

    def test_private_paths_and_credential_subjects_are_not_exposed(self):
        (self.repo / 'app.py').write_text('before\n')
        before = self.commit()
        (self.repo / '.env').write_text('NOT_A_REAL_SECRET=fixture\n')
        (self.repo / 'app.py').write_text('after\n')
        fake_key = 'sk-' + 'x' * 30
        after = self.commit('accidental fixture ' + fake_key)
        project = {'root_path': str(self.repo)}
        changes, excluded = meaningful_changes(project, after, compare_commits(self.repo, before, after), {})
        summary = summarize_changes(project, before, after, changes, excluded)
        self.assertEqual(summary['files'], [{'path': 'app.py', 'change': 'modified'}])
        self.assertNotIn('.env', str(summary))
        self.assertNotIn(fake_key, str(summary))
        self.assertIn('已隐藏', summary['commit_subjects'][0])
