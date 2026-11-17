import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.chunking import chunks, token_count, MAX_TOKENS, CHUNKING_VERSION
from zest.git import inspect_repository
from zest.hybrid import _identity, _manifest, build, semantic_search, search
from zest.files import FileReadError
from zest.index import RetrievalIndex
from zest.qa import prepare_context
from zest.storage import ProjectRegistry


class ChunkingTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def project(self, content, name='app.py'):
        (self.repo / name).write_text(content)
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        RetrievalIndex(self.state_dir).build(project)
        return project

    def test_python_function_and_markdown_section_boundaries(self):
        code = 'import os\n\n@dec\ndef alpha():\n    return 1\n\ndef beta():\n    return 2\n'
        parts = list(chunks('x.py', code))
        self.assertIn((3, 5), [(e['start_line'], e['end_line']) for e, _ in parts])
        self.assertIn((7, 8), [(e['start_line'], e['end_line']) for e, _ in parts])
        md = list(chunks('x.md', '# Start\nfirst\n## Next\nsecond\n'))
        self.assertEqual([(e['start_line'], e['end_line']) for e, _ in md], [(1, 2), (3, 4)])

    def test_long_unicode_line_fully_covered_under_budget(self):
        line = ('你好abc !' * 1500)
        parts = list(chunks('x.txt', line))
        self.assertEqual(''.join(text for _, text in parts), line)
        self.assertTrue(all(token_count(text) <= MAX_TOKENS and len(text) <= 6000 for _, text in parts))
        self.assertEqual(parts[0][0]['start_col'], 0)
        self.assertEqual(parts[-1][0]['end_col'], len(line))
        self.assertTrue(all(a[0]['end_col'] == b[0]['start_col'] for a, b in zip(parts, parts[1:])))

    def test_invalid_python_fallback_and_bounded_long_function(self):
        text = 'def broken(:\n' + 'value = 1\n' * 100
        parts = list(chunks('broken.py', text))
        self.assertTrue(parts)
        self.assertTrue(all(e['end_line'] - e['start_line'] < 40 and token_count(t) <= MAX_TOKENS for e, t in parts))
        self.assertEqual(parts[-1][0]['end_line'], 101)

    def test_long_line_retrieval_is_source_bound_through_qdrant(self):
        try:
            import qdrant_client
        except ImportError:
            self.skipTest('hybrid extra not installed')
        text = ' '.join(['filler'] * 1800 + ['uniquetarget'] + ['filler'] * 500)
        p = self.project(text + '\n', 'data.txt')
        commit = p['git']['head_commit']
        index = RetrievalIndex(self.state_dir)
        results = index.search(p, 'uniquetarget')['matches']
        self.assertTrue(results)
        e = results[0]['evidence']
        self.assertEqual(text[e['start_col']:e['end_col']], results[0]['text'])
        ctx = prepare_context(p, 'uniquetarget', state_dir=self.state_dir, retrieval='lexical')
        source = next(s for s in ctx['sources'] if s['evidence']['path'] == 'data.txt')
        self.assertIn('uniquetarget', source['lines'][0]['text'])
        self.assertEqual(source['lines'][0]['text'], text[source['evidence']['start_col']:source['evidence']['end_col']])
        import asyncio
        from zest.agents import verify_sources
        asyncio.run(verify_sources(ctx, self.state_dir))
        settings = {'embedding_url': 'https://example.test', 'embedding_key': 'test', 'embedding_model': 'fake',
                    'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        def embed(texts, _settings, kind):
            return [[1., 0.] if 'uniquetarget' in t else [0., 1.] for t in texts]
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=embed):
            build(p, self.state_dir)
            matches = semantic_search(p, 'uniquetarget', self.state_dir, commit, settings)
            self.assertIn('uniquetarget', matches[0]['text'])
            self.assertIn('start_col', matches[0]['evidence'])
            found, _ = search(p, 'uniquetarget', self.state_dir, commit, [])
            self.assertIn('uniquetarget', found[0]['text'])

    def test_old_index_readable_and_new_index_invalidates_semantic_identity(self):
        p = self.project('def compute(): return 1\n')
        index = RetrievalIndex(self.state_dir)
        commit = p['git']['head_commit']
        with closing(sqlite3.connect(index.db_path)) as conn, conn:
            meta = json.loads(conn.execute('SELECT metadata FROM indexes').fetchone()[0])
            self.assertEqual(meta.pop('chunking'), CHUNKING_VERSION)
            conn.execute('UPDATE indexes SET metadata=?', (json.dumps(meta),))
        self.assertTrue(index.search(p, 'compute')['matches'])
        self.assertTrue(index.inventory(p, commit=commit)['files'])
        settings = {'embedding_url': 'https://example.test', 'embedding_model': 'fake'}
        self.assertNotEqual(_identity(p, commit, settings), _identity(p, commit, settings, CHUNKING_VERSION))

    def test_previous_semantic_manifest_is_not_reused_after_rechunking(self):
        p = self.project('def compute(): return 1\n')
        commit = p['git']['head_commit']
        settings = {'embedding_url': 'https://example.test', 'embedding_model': 'fake'}
        old_identity = _identity(p, commit, settings)
        marker = _manifest(self.state_dir, old_identity)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({'identity': old_identity, 'collection': 'zest_' + 'a' * 32}))
        with patch('zest.hybrid._embed', side_effect=AssertionError('stale index must not call embedding')):
            with self.assertRaises(FileReadError) as exc:
                semantic_search(p, 'compute', self.state_dir, commit, settings)
        self.assertEqual(exc.exception.code, 'semantic_index_missing')

    def test_semantic_source_reconstruction_handles_crlf_and_partial_lines(self):
        from zest.hybrid import _source_chunks
        long_line = ' '.join(['piece'] * 1600 + ['uniquetarget'] + ['piece'] * 300)
        raw = ('first\r\n' + long_line + '\r\nlast\r\n').encode()
        (self.repo / 'file.txt').write_bytes(raw)
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        commit = project['git']['head_commit']
        index = RetrievalIndex(self.state_dir)
        index.build(project)
        sources = _source_chunks(project, index.inventory(project, commit=commit), commit)
        target = next(item for item in sources if 'uniquetarget' in item['text'])
        e = target['evidence']
        self.assertEqual(long_line[e['start_col']:e['end_col']], target['text'])
        self.assertEqual(e['start_line'], 2)

    def test_unchanged_and_renamed_blobs_reuse_lexical_chunks(self):
        from zest.chunking import chunks as real_chunks
        (self.repo / 'a.py').write_text('def alpha(): return 1\n')
        (self.repo / 'b.py').write_text('def beta(): return 2\n')
        self.commit('first')
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        (self.repo / 'b.py').write_text('def beta(): return 3\n')
        self.commit('changed')
        with patch('zest.index.make_chunks', wraps=real_chunks) as chunker:
            metadata = index.build(p)
        self.assertEqual(metadata['files_reused'], 1)
        self.assertEqual(metadata['files_rechunked'], 1)
        self.assertEqual([call.args[0] for call in chunker.call_args_list], ['b.py'])
        self.git('mv', 'a.py', 'moved.py')
        self.commit('renamed')
        with patch('zest.index.make_chunks', wraps=real_chunks) as chunker:
            metadata = index.build(p)
        self.assertEqual(metadata['files_reused'], 2)
        self.assertEqual(chunker.call_count, 0)
        self.assertEqual(index.search(p, 'alpha')['matches'][0]['evidence']['path'], 'moved.py')
        self.git('mv', 'moved.py', 'moved.md')
        self.commit('kind changed')
        with patch('zest.index.make_chunks', wraps=real_chunks) as chunker:
            metadata = index.build(p)
        self.assertEqual(metadata['files_rechunked'], 1)
        self.assertEqual([call.args[0] for call in chunker.call_args_list], ['moved.md'])

    def test_corrupted_cached_chunks_are_rebuilt_from_git(self):
        p = self.project('def alpha(): return 1\n')
        index = RetrievalIndex(self.state_dir)
        with closing(sqlite3.connect(index.db_path)) as conn, conn:
            chunks_data = json.loads(conn.execute('SELECT chunks FROM indexes').fetchone()[0])
            chunks_data[0]['text'] = 'fake'
            conn.execute('UPDATE indexes SET chunks=?', (json.dumps(chunks_data),))
        metadata = index.build(p)
        self.assertEqual(metadata['files_rechunked'], 1)
        self.assertEqual(index.search(p, 'alpha')['matches'][0]['text'], 'def alpha(): return 1')
