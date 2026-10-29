import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.hybrid import _embed, fuse, rerank, search
from zest.index import RetrievalIndex
from zest.qa import prepare_context
from zest.storage import ProjectRegistry


class HybridTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def project(self):
        (self.repo / 'login.py').write_text('def login():\n    return "token"\n')
        (self.repo / 'issue.py').write_text('def issue_credentials():\n    return "credential"\n')
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        RetrievalIndex(self.state_dir).build(project)
        return project

    def test_embedding_request_and_response(self):
        settings = {'embedding_url': 'https://example.test/embedding', 'embedding_key': 'test', 'embedding_model': 'text-embedding-v4'}
        with patch('zest.hybrid._post', return_value={'output': {'embeddings': [{'text_index': 0, 'embedding': [1., 2.]}]}}) as post:
            self.assertEqual(_embed(['hello'], settings, 'query'), [[1., 2.]])
            self.assertEqual(post.call_args.args[2]['parameters']['text_type'], 'query')
        with patch('zest.hybrid._post', return_value={'output': {'embeddings': [{'text_index': 1, 'embedding': [1]}]}}):
            with self.assertRaises(FileReadError):
                _embed(['hello'], settings, 'query')

    def test_fusion_and_rerank_validation(self):
        def item(path):
            return {'evidence': {'path': path, 'commit': 'abc', 'start_line': 1, 'end_line': 2}, 'text': path}
        a, b, c = map(item, ['a', 'b', 'c'])
        self.assertEqual(fuse([a, b], [c, b])[0], b)
        settings = {'rerank_url': 'https://example.test', 'rerank_key': 'x', 'rerank_model': 'qwen3-rerank'}
        with patch('zest.hybrid._post', return_value={'output': {'results': [{'index': 1}, {'index': 0}]}}):
            self.assertEqual(rerank('q', [a, b], settings), [b, a])
        with patch('zest.hybrid._post', return_value={'output': {'results': [{'index': 1}, {'index': 1}]}}):
            with self.assertRaises(FileReadError):
                rerank('q', [a, b], settings)

    def test_fallback_and_source_budget_priority(self):
        p = self.project()
        warnings = []
        with patch('zest.hybrid.semantic_search', side_effect=FileReadError('semantic_index_missing', 'missing')):
            matches, path = search(p, 'login token', self.state_dir, p['git']['head_commit'], warnings)
            self.assertEqual(path, 'lexical')
            self.assertEqual(matches[0]['evidence']['path'], 'login.py')
            self.assertEqual(warnings[0]['code'], 'semantic_index_missing')
            context = prepare_context(p, 'login token', state_dir=self.state_dir, retrieval='hybrid')
            self.assertEqual(context['retrieval_path'], 'lexical')
            self.assertEqual(context['sources'][0]['evidence']['path'], 'login.py')

    def test_rerank_failure_retains_semantic_candidate_and_commit(self):
        p = self.project()
        commit = p['git']['head_commit']
        semantic = RetrievalIndex(self.state_dir).search(p, 'credential', commit=commit)['matches']
        with patch('zest.hybrid.semantic_search', return_value=semantic), patch('zest.hybrid.rerank', side_effect=FileReadError('retrieval_service_error', 'down')):
            warnings = []
            matches, path = search(p, 'login token', self.state_dir, commit, warnings)
            self.assertEqual(path, 'hybrid_rrf')
            self.assertEqual({m['evidence']['path'] for m in matches}, {'login.py', 'issue.py'})
            self.assertTrue(all(m['evidence']['commit'] == commit for m in matches))
            self.assertEqual(warnings[0]['retrieval_path'], 'hybrid_rrf')

    def test_stale_lexical_index_is_not_bypassed(self):
        p = self.project()
        (self.repo / 'login.py').write_text('new')
        self.commit('new')
        with self.assertRaises(FileReadError) as exc:
            prepare_context(p, 'login', state_dir=self.state_dir, retrieval='hybrid')
        self.assertEqual(exc.exception.code, 'index_stale')

    def test_local_qdrant_build_search_and_failed_rebuild(self):
        try:
            import qdrant_client  # noqa: F401
        except ImportError:
            self.skipTest('hybrid extra not installed')
        from zest.hybrid import build, semantic_search
        p = self.project()
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test',
                    'embedding_model': 'text-embedding-v4', 'rerank_url': '', 'rerank_key': '',
                    'rerank_model': 'qwen3-rerank', 'hybrid_enabled': 'true'}
        def embed(texts, _settings, kind):
            return [[1., 0.] if 'credential' in t else [0., 1.] for t in texts]
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=embed):
            built = build(p, self.state_dir)
            self.assertEqual(built['commit'], p['git']['head_commit'])
            matches = semantic_search(p, 'credential', self.state_dir, built['commit'], settings)
            self.assertEqual(matches[0]['evidence']['path'], 'issue.py')
            self.assertEqual(matches[0]['evidence']['commit'], built['commit'])
            with patch('zest.hybrid._embed', side_effect=FileReadError('retrieval_service_error', 'failed')):
                with self.assertRaises(FileReadError):
                    build(p, self.state_dir)
            with self.assertRaises(FileReadError) as exc:
                semantic_search(p, 'credential', self.state_dir, built['commit'], settings)
            self.assertEqual(exc.exception.code, 'semantic_index_missing')

    def test_dashscope_environment_defaults_and_overrides(self):
        from zest.hybrid import _settings
        env = {'DASHSCOPE_API_KEY': 'shared', 'DASHSCOPE_BASE_URL': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
               'ZEST_RERANK_KEY': 'separate', 'ZEST_RERANK_URL': 'https://example.test/rerank'}
        with patch.dict('os.environ', env, clear=True), patch('dotenv.dotenv_values', return_value={}):
            settings = _settings()
        self.assertEqual(settings['embedding_key'], 'shared')
        self.assertEqual(settings['rerank_key'], 'separate')
        self.assertEqual(settings['embedding_url'], 'https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding')
        self.assertEqual(settings['rerank_url'], 'https://example.test/rerank')

    def test_qdrant_server_identity_and_local_address_validation(self):
        from zest.hybrid import _client, _identity
        p = self.project()
        settings = {'embedding_url': 'https://example.test', 'embedding_model': 'text-embedding-v4'}
        local = _identity(p, p['git']['head_commit'], settings)
        server = _identity(p, p['git']['head_commit'], {**settings, 'qdrant_url': 'http://127.0.0.1:6333'})
        self.assertNotEqual(local, server)
        with self.assertRaises(FileReadError) as exc:
            _client(self.state_dir, {'qdrant_url': 'http://example.com:6333'})
        self.assertEqual(exc.exception.code, 'qdrant_config_invalid')
