import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.git_history import history, changes, diff
from zest.storage import ProjectRegistry


class GitHistoryToolsTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def test_history_and_changed_paths_are_read_only_and_version_bound(self):
        (self.repo / 'app.py').write_text('x = 1\n')
        self.commit('first commit')
        first = inspect_repository(self.repo).head_commit
        (self.repo / 'app.py').write_text('x = 2\n')
        (self.repo / 'new.py').write_text('y = 3\n')
        self.commit('second commit')
        second = inspect_repository(self.repo).head_commit
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        import json
        code, output, error = self.cli('project', 'history', project['id'], '--limit', '1')
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(json.loads(output)['commits'][0]['commit'], second)
        code, output, error = self.cli('project', 'changes', project['id'], first, second)
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(len(json.loads(output)['changes']), 2)
        result = history(project, limit=1)
        self.assertEqual(result['commits'][0]['commit'], second)
        self.assertTrue(result['truncated'])
        self.assertEqual(history(project, commit=first)['commits'][0]['subject'], 'first commit')
        self.assertEqual({x['path'] for x in changes(project, before=first, after=second)['changes']}, {'app.py', 'new.py'})
        difference = diff(project, before=first, after=second, path='app.py')
        self.assertIn('-x = 1', difference['diff'])
        self.assertIn('+x = 2', difference['diff'])
        self.assertEqual(difference['before']['commit'], first)
        self.assertIsNone(diff(project, before=first, after=second, path='new.py')['before'])
        self.assertEqual(history(project, path='new.py')['commits'][0]['commit'], second)
        with self.assertRaises(FileReadError):
            history(project, commit='HEAD')
        with self.assertRaises(FileReadError):
            history(project, path='../other')
        with self.assertRaises(FileReadError):
            history(project, limit=51)

    def test_ordinary_questions_can_prepare_specific_history_and_diff(self):
        from zest.qa import prepare_context
        from zest.qa_history import recheck
        (self.repo / 'app.py').write_text('x = 1\n')
        self.commit('first')
        (self.repo / 'app.py').write_text('x = 2\n')
        self.commit('second')
        after = inspect_repository(self.repo).head_commit
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        context = prepare_context(project, f'git diff {after} app.py 这次修改了什么？', state_dir=self.state_dir)
        history_sources = [s for s in context['sources'] if s['evidence'].get('kind') in ('git_history', 'git_changes', 'git_diff')]
        self.assertEqual([s['evidence']['kind'] for s in history_sources], ['git_diff', 'git_changes', 'git_history'])
        self.assertIn('+x = 2', history_sources[0]['body'])
        self.assertTrue(all(recheck(project, source) for source in history_sources))
        import asyncio
        from zest.agents import verify_sources
        asyncio.run(verify_sources(context, self.state_dir))
        history_sources[0]['body'] += 'tampered'
        self.assertFalse(recheck(project, history_sources[0]))

    def test_ordinary_question_does_not_collect_git_history(self):
        from zest.qa import prepare_context
        (self.repo / 'app.py').write_text('x = 1\n')
        self.commit('first')
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        with patch('zest.qa_history.history', side_effect=AssertionError('ordinary questions should not scan history')):
            context = prepare_context(project, '项目使用什么语言？', state_dir=self.state_dir)
        self.assertNotIn('git_history', {s['evidence'].get('kind') for s in context['sources']})
