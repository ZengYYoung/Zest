import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.hybrid import build, plan_build, semantic_search
from zest.index import RetrievalIndex
from zest.storage import ProjectRegistry


class IncrementalEmbeddingTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def test_commit_change_reuses_unchanged_chunks_and_preserves_versions(self):
        try:
            import qdrant_client
        except ImportError:
            self.skipTest('hybrid extra not installed')
        (self.repo / 'a.py').write_text('def alpha():\n    return "unchanged"\n')
        (self.repo / 'b.py').write_text('def beta():\n    return "oldvalue"\n')
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        index = RetrievalIndex(self.state_dir)
        first_commit = project['git']['head_commit']
        index.build(project)
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test',
                    'embedding_model': 'fake', 'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake',
                    'hybrid_enabled': 'true'}
        calls = []
        def embed(texts, _settings, kind):
            if kind == 'document':
                calls.extend(texts)
            return [[1., 0.] if 'unchanged' in text else [0., 1.] for text in texts]
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=embed):
            preview = plan_build(project, self.state_dir)
            self.assertFalse(preview['model_called'])
            self.assertEqual(preview['new_vectors'], 2)
            self.assertFalse((self.state_dir / 'embedding-cache.sqlite3').exists())
            first = build(project, self.state_dir)
            self.assertEqual(first['new_vectors'], 2)
            self.assertEqual(first['reused_chunks'], 0)
            calls.clear()
            (self.repo / 'b.py').write_text('def beta():\n    return "newvalue"\n')
            self.commit('updated')
            second_commit = inspect_repository(self.repo).head_commit
            index.build(project)
            preview = plan_build(project, self.state_dir)
            self.assertEqual(preview['new_vectors'], 1)
            self.assertEqual(preview['reused_chunks'], 1)
            second = build(project, self.state_dir)
            self.assertEqual(second['new_vectors'], 1)
            self.assertEqual(second['reused_chunks'], 1)
            self.assertEqual(len(calls), 1)
            self.assertIn('newvalue', calls[0])
            self.assertTrue(all('unchanged' not in text for text in calls))
            old = semantic_search(project, 'unchanged', self.state_dir, first_commit, settings)
            new = semantic_search(project, 'unchanged', self.state_dir, second_commit, settings)
            self.assertTrue(all(m['evidence']['commit'] == first_commit for m in old))
            self.assertTrue(all(m['evidence']['commit'] == second_commit for m in new))
            self.assertTrue(any('oldvalue' in m['text'] for m in old))
            self.assertTrue(any('newvalue' in m['text'] for m in new))
            calls.clear()
            again = build(project, self.state_dir)
            self.assertEqual(again['new_vectors'], 0)
            self.assertTrue(again['collection_reused'])
            self.assertEqual(len(calls), 0)
            self.git('mv', 'a.py', 'renamed.py')
            (self.repo / 'b.py').unlink()
            self.commit('rename and delete')
            third_commit = inspect_repository(self.repo).head_commit
            index.build(project)
            plan = plan_build(project, self.state_dir)
            self.assertEqual(plan['new_vectors'], 0)
            self.assertEqual(plan['reused_chunks'], 1)
            third = build(project, self.state_dir, expected_plan_sha256=plan['plan_sha256'])
            self.assertEqual(third['new_vectors'], 0)
            current = semantic_search(project, 'unchanged', self.state_dir, third_commit, settings)
            self.assertEqual({m['evidence']['path'] for m in current}, {'renamed.py'})
            self.assertTrue(all(m['evidence']['commit'] == third_commit for m in current))

    def test_corrupt_cache_fails_closed(self):
        from zest.embedding_cache import open_cache, save_vectors, load_vectors
        with open_cache(self.state_dir) as conn:
            save_vectors(conn, 'scope', {'key': [1.0, 2.0]})
            with conn:
                conn.execute('UPDATE vectors SET vector=?', (json.dumps([9.0, 2.0]),))
            with self.assertRaises(FileReadError) as exc:
                load_vectors(conn, 'scope', ['key'])
            self.assertEqual(exc.exception.code, 'embedding_cache_corrupted')

    def test_model_and_chunking_separate_cache_namespaces(self):
        from zest.embedding_cache import namespace
        p = {'id': 'project', 'root_path': str(self.repo)}
        s = {'embedding_url': 'https://example.test', 'embedding_model': 'v1'}
        initial = namespace(p, s, 'c1')
        self.assertNotEqual(initial, namespace(p, {**s, 'embedding_model': 'v2'}, 'c1'))
        self.assertNotEqual(initial, namespace(p, {**s, 'embedding_revision': 'new'}, 'c1'))
        self.assertNotEqual(initial, namespace(p, s, 'c2'))

    def test_failed_batch_does_not_publish_and_reuses_completed_batches(self):
        try:
            import qdrant_client
        except ImportError:
            self.skipTest('hybrid extra not installed')
        content = ''.join(f'def method_{i}():\n    return {i}\n\n' for i in range(12))
        (self.repo / 'many.py').write_text(content)
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        RetrievalIndex(self.state_dir).build(p)
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test', 'embedding_model': 'fake',
                    'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        calls = []
        def fail_second(texts, settings, kind):
            calls.append(len(texts))
            if len(calls) == 2:
                raise FileReadError('retrieval_service_error', 'failed')
            return [[1., 0.] for _ in texts]
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=fail_second):
            with self.assertRaises(FileReadError):
                build(p, self.state_dir)
            plan = plan_build(p, self.state_dir)
            self.assertEqual(plan['reused_chunks'], 10)
            self.assertGreater(plan['new_vectors'], 0)
            with self.assertRaises(FileReadError) as exc:
                semantic_search(p, 'method', self.state_dir, p['git']['head_commit'], settings)
            self.assertEqual(exc.exception.code, 'semantic_index_missing')
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=lambda t, s, k: [[1., 0.] for _ in t]):
            result = build(p, self.state_dir)
            self.assertEqual(result['reused_chunks'], 10)

    def test_approved_plan_rejected_when_configuration_changes(self):
        (self.repo / 'a.py').write_text('def alpha(): return 1\n')
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        RetrievalIndex(self.state_dir).build(p)
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'oldkey',
                    'embedding_model': 'fake', 'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        with patch('zest.hybrid._settings', return_value=settings):
            plan = plan_build(p, self.state_dir)
        changed = {**settings, 'embedding_key': 'newkey'}
        with patch('zest.hybrid._settings', return_value=changed), patch('zest.hybrid._embed', side_effect=AssertionError('must not call model')):
            with self.assertRaises(FileReadError) as exc:
                build(p, self.state_dir, expected_plan_sha256=plan['plan_sha256'])
            self.assertEqual(exc.exception.code, 'semantic_plan_changed')

    def test_approved_plan_rejected_when_commit_changes(self):
        (self.repo / 'a.py').write_text('def alpha(): return 1\n')
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        index = RetrievalIndex(self.state_dir)
        index.build(p)
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test',
                    'embedding_model': 'fake', 'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        with patch('zest.hybrid._settings', return_value=settings):
            plan = plan_build(p, self.state_dir)
            (self.repo / 'a.py').write_text('def alpha(): return 2\n')
            self.commit('changed')
            index.build(p)
            with patch('zest.hybrid._embed', side_effect=AssertionError('must not call model')):
                with self.assertRaises(FileReadError) as exc:
                    build(p, self.state_dir, expected_plan_sha256=plan['plan_sha256'])
                self.assertEqual(exc.exception.code, 'semantic_plan_changed')

    def test_project_workspace_cache_and_index_use_same_resource_directory(self):
        try:
            import qdrant_client
        except ImportError:
            self.skipTest('hybrid extra not installed')
        (self.repo / 'a.py').write_text('def alpha(): return 1\n')
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        RetrievalIndex(self.state_dir).build(p)
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test', 'embedding_model': 'fake',
                    'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        from pathlib import Path
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=lambda t, s, k: [[1., 0.] for _ in t]):
            plan = plan_build(p, self.state_dir)
            build(p, self.state_dir, expected_plan_sha256=plan['plan_sha256'])
            self.assertTrue((Path(p['resource_dir']) / 'embedding-cache.sqlite3').exists())
            self.assertEqual(plan_build(p, self.state_dir)['new_vectors'], 0)
            matches = semantic_search(p, 'alpha', self.state_dir, p['git']['head_commit'], settings)
            self.assertTrue(matches)

    def test_pool_shares_points_and_retains_two_version_maps(self):
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            self.skipTest('hybrid extra not installed')
        from zest.semantic_pool import read_versions
        from zest.hybrid import _client
        (self.repo / 'stable.py').write_text('def stable():\n    return "constant"\n')
        (self.repo / 'changing.py').write_text('def changing():\n    return "version0"\n')
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        index = RetrievalIndex(self.state_dir)
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test', 'embedding_model': 'fake',
                    'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        commits = []
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=lambda ts, s, k: [[1., 0.] if 'constant' in t else [0., 1.] for t in ts]):
            for i in range(3):
                if i:
                    (self.repo / 'changing.py').write_text(f'def changing():\n    return "version{i}"\n')
                    self.commit(f'change {i}')
                commits.append(inspect_repository(self.repo).head_commit)
                index.build(p)
                result = build(p, self.state_dir)
                self.assertEqual(result['new_points'], 2 if i == 0 else 1)
            versions = read_versions(self.state_dir, p['id'])
            self.assertEqual([v['commit'] for v in versions], [commits[2], commits[1]])
            self.assertEqual(versions[0]['collection'], versions[1]['collection'])
            client = _client(self.state_dir, settings)
            try:
                self.assertEqual(client.count(versions[0]['collection'], exact=True).count, 4)
            finally:
                client.close()
            for commit in commits[1:]:
                hits = semantic_search(p, 'constant', self.state_dir, commit, settings)
                self.assertTrue(hits)
                self.assertTrue(all(x['evidence']['commit'] == commit for x in hits))
            with self.assertRaises(FileReadError) as exc:
                semantic_search(p, 'constant', self.state_dir, commits[0], settings)
            self.assertEqual(exc.exception.code, 'semantic_index_missing')
            from zest.retrieval_cleanup import plan as cleanup_plan, execute as cleanup
            preview = cleanup_plan(p, self.state_dir)
            self.assertEqual(len(preview['pool_remove_keys']), 1)
            self.assertEqual(len(preview['cache_remove_keys']), 1)
            self.assertEqual([x['commit'] for x in preview['lexical_indexes']], [commits[0]])
            from zest.hybrid import _manifest, _identity
            legacy = _manifest(self.state_dir, _identity(p, commits[0], settings))
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text('{}')
            protected = cleanup_plan(p, self.state_dir)
            self.assertTrue(protected['legacy_lexical_indexes_protected'])
            self.assertEqual(protected['lexical_indexes'], [])
            legacy.unlink()
            with self.assertRaises(FileReadError) as denied:
                cleanup(p, self.state_dir, expected_plan_sha256='0' * 64)
            self.assertEqual(denied.exception.code, 'cleanup_plan_changed')
            cleaned = cleanup(p, self.state_dir, expected_plan_sha256=preview['plan_sha256'])
            self.assertEqual(cleaned['pool_points_removed'], 1)
            self.assertEqual(cleaned['cached_vectors_removed'], 1)
            self.assertEqual(cleaned['lexical_indexes_removed'], 1)
            self.assertEqual(cleaned['legacy_collections_removed'], 0)
            self.assertTrue(all(semantic_search(p, 'constant', self.state_dir, c, settings) for c in commits[1:]))
            self.assertEqual(cleanup_plan(p, self.state_dir)['pool_remove_keys'], [])

    def test_version_maps_are_isolated_between_projects_sharing_state(self):
        import hashlib
        from uuid import uuid4
        from zest.semantic_pool import publish, read_versions, collection_name
        first, second = str(uuid4()), str(uuid4())
        scope = 'a' * 64
        def snapshot(commit, identity):
            items = [{'key': 'c' * 64, 'content_sha256': 'd' * 64, 'evidence': {'commit': commit}}]
            encoded = json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
            return {'identity': identity, 'commit': commit, 'scope': scope, 'collection': collection_name(scope),
                    'storage': '', 'items': items, 'snapshot_sha256': hashlib.sha256(encoded).hexdigest()}
        publish(self.state_dir, first, snapshot('1' * 40, '1' * 64))
        publish(self.state_dir, second, snapshot('2' * 40, '2' * 64))
        publish(self.state_dir, first, snapshot('3' * 40, '3' * 64))
        publish(self.state_dir, first, snapshot('4' * 40, '4' * 64))
        self.assertEqual([x['commit'] for x in read_versions(self.state_dir, first)], ['4' * 40, '3' * 40])
        self.assertEqual([x['commit'] for x in read_versions(self.state_dir, second)], ['2' * 40])

    def test_failed_new_commit_keeps_old_snapshot_without_claiming_new_commit(self):
        (self.repo / 'a.py').write_text('def alpha(): return 1\n')
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        index = RetrievalIndex(self.state_dir)
        old = p['git']['head_commit']
        index.build(p)
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test', 'embedding_model': 'fake',
                    'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        with patch('zest.hybrid._settings', return_value=settings), patch('zest.hybrid._embed', side_effect=lambda t, s, k: [[1., 0.] for _ in t]):
            build(p, self.state_dir)
            (self.repo / 'a.py').write_text('def alpha(): return 2\n')
            self.commit('changed')
            new = inspect_repository(self.repo).head_commit
            index.build(p)
            with patch('zest.hybrid._embed', side_effect=FileReadError('retrieval_service_error', 'failed')):
                with self.assertRaises(FileReadError):
                    build(p, self.state_dir)
            self.assertTrue(semantic_search(p, 'alpha', self.state_dir, old, settings))
            with self.assertRaises(FileReadError) as exc:
                semantic_search(p, 'alpha', self.state_dir, new, settings)
            self.assertEqual(exc.exception.code, 'semantic_index_missing')

    def test_project_index_lock_prevents_cleanup_or_build_during_active_read(self):
        from zest.hybrid import _build_lock
        with _build_lock(self.state_dir, shared=True):
            with self.assertRaises(FileReadError) as exc:
                with _build_lock(self.state_dir):
                    pass
            self.assertEqual(exc.exception.code, 'semantic_build_busy')

    def test_corrupted_version_map_is_rejected_before_use(self):
        from uuid import uuid4
        from zest.semantic_pool import read_versions
        project_id = str(uuid4())
        folder = self.state_dir / 'semantic-pools' / project_id
        folder.mkdir(parents=True)
        (folder / 'versions.json').write_text('{"format":"content-pool-v1","versions":[{}]}')
        with self.assertRaises(FileReadError) as exc:
            read_versions(self.state_dir, project_id)
        self.assertEqual(exc.exception.code, 'semantic_manifest_corrupted')

    def test_legacy_collection_remains_readable_without_migration(self):
        from uuid import uuid4
        from qdrant_client.http import models
        from zest.files import read_project_file
        from zest.hybrid import _identity, _manifest, _client
        (self.repo / 'a.py').write_text('def alpha(): return 1\n')
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        index = RetrievalIndex(self.state_dir)
        metadata = index.build(p)
        commit = p['git']['head_commit']
        settings = {'embedding_url': 'https://example.test/embed', 'embedding_key': 'test', 'embedding_model': 'fake',
                    'rerank_url': '', 'rerank_key': '', 'rerank_model': 'fake', 'hybrid_enabled': 'true'}
        record = read_project_file(p, 'a.py', commit=commit, start_line=1, end_line=1)
        client = _client(self.state_dir, settings)
        collection = 'zest_' + uuid4().hex
        try:
            client.create_collection(collection, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
            client.upsert(collection, points=[models.PointStruct(id=0, vector=[1., 0.], payload={
                'project_id': p['id'], 'commit': commit, 'evidence': record['evidence'],
                'content_sha256': record['content_sha256'], 'text': record['lines'][0]['text']})], wait=True)
        finally:
            client.close()
        identity = _identity(p, commit, settings, metadata.get('chunking'))
        marker = _manifest(self.state_dir, identity)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({'identity': identity, 'collection': collection, 'count': 1}))
        with patch('zest.hybrid._embed', return_value=[[1., 0.]]):
            hits = semantic_search(p, 'alpha', self.state_dir, commit, settings)
        self.assertEqual(hits[0]['evidence']['path'], 'a.py')
        self.assertTrue(marker.exists())
