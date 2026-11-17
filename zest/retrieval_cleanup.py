"""Preview and explicitly clean only unreferenced derived retrieval data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .files import FileReadError
from .git import inspect_repository, selected_commit
from .index import RetrievalIndex
from .resources import project_states
from .embedding_cache import namespace
from .semantic_pool import read_versions, collection_name, point_id


def _scope(state_dir, project):
    from .hybrid import _settings
    destination = project_states(state_dir, project['id'])[0]
    commit = selected_commit(project, inspect_repository(project['root_path']))
    if not commit:
        raise FileReadError('invalid_commit', '需要已提交版本。')
    inventory = RetrievalIndex(destination).inventory(project, commit=commit)
    settings = _settings()
    scope = namespace(project, settings, inventory['metadata'].get('chunking'))
    return destination, commit, settings, scope


def plan(project, state_dir):
    from .hybrid import _client
    try:
        from qdrant_client.http import models
    except ImportError as exc:
        raise FileReadError('vector_dependencies_missing', '请安装 hybrid 可选依赖。') from exc
    destination, commit, settings, scope = _scope(state_dir, project)
    versions = read_versions(destination, project['id'])
    storage = settings.get('qdrant_url', '')
    pool = collection_name(scope)
    retained = [v for v in versions if v.get('scope') == scope and v.get('storage', '') == storage and v['collection'] == pool]
    referenced = {item['key'] for v in retained for item in v['items']}
    cache_referenced = {item['key'] for v in versions if v.get('scope') == scope for item in v['items']}
    candidates = []
    client = _client(destination, settings)
    try:
        if client.collection_exists(pool):
            offset = None
            while True:
                points, offset = client.scroll(collection_name=pool, offset=offset, limit=500,
                    with_payload=True, with_vectors=False,
                    scroll_filter=models.Filter(must=[models.FieldCondition(key='scope', match=models.MatchValue(value=scope))]))
                for point in points:
                    key = (point.payload or {}).get('content_key')
                    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key) or str(point.id) != point_id(key):
                        raise FileReadError('semantic_pool_corrupted', '向量池记录异常，未提供清理计划。')
                    if key not in referenced:
                        candidates.append(key)
                if len(candidates) > 50000:
                    raise FileReadError('cleanup_budget_exceeded', '候选过多，请缩小清理范围。')
                if offset is None:
                    break
    except FileReadError:
        raise
    except Exception as exc:
        raise FileReadError('vector_storage_error', '无法核对本机向量库，未生成清理计划。') from exc
    finally:
        client.close()
    candidates.sort()
    cache_keys = []
    cache_path = destination / 'embedding-cache.sqlite3'
    if cache_path.is_symlink():
        raise FileReadError('embedding_cache_error', '向量缓存不能是符号链接。')
    if cache_path.exists():
        try:
            with closing(sqlite3.connect(cache_path.as_uri() + '?mode=ro', uri=True)) as conn:
                if conn.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise ValueError
                cache_keys = [row[0] for row in conn.execute('SELECT content_key FROM vectors WHERE namespace=? ORDER BY content_key', (scope,))]
                if len(cache_keys) > 50000 or any(not re.fullmatch(r'[0-9a-f]{64}', x) for x in cache_keys):
                    raise ValueError
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise FileReadError('embedding_cache_error', '无法核对向量缓存。') from exc
    obsolete_cache = [key for key in cache_keys if key not in cache_referenced]
    # Legacy manifests do not record enough metadata to prove which old
    # lexical version they need. Preserve all lexical rows until a dedicated
    # migration can verify those links.
    legacy_dir = destination / 'semantic-manifests'
    if legacy_dir.is_symlink():
        raise FileReadError('semantic_manifest_corrupted', '旧语义索引目录不能是符号链接。')
    legacy_protected = legacy_dir.exists() and any(legacy_dir.glob('*.json'))
    index = RetrievalIndex(destination)
    with index._open(write=False) as conn:
        index_rows = conn.execute('SELECT commit_id,created_at,metadata,chunks FROM indexes WHERE project_id=? ORDER BY created_at DESC', (project['id'],)).fetchall()
        kept = {commit, *(v['commit'] for v in versions), *(row['commit_id'] for row in index_rows[:2])}
        old_indexes = []
        for row in index_rows:
            if not legacy_protected and row['commit_id'] not in kept:
                digest = hashlib.sha256((row['metadata'] + '\0' + row['chunks']).encode()).hexdigest()
                old_indexes.append({'commit': row['commit_id'], 'sha256': digest})
    body = {'project_id': project['id'], 'selected_commit': commit, 'scope': scope, 'collection': pool,
            'qdrant_url': storage or 'embedded_local', 'retained_commits': sorted(kept),
            'pool_remove_keys': candidates, 'cache_remove_keys': obsolete_cache, 'lexical_indexes': old_indexes,
            'retained_versions': [{'identity': v['identity'], 'snapshot_sha256': v.get('snapshot_sha256', '')} for v in versions],
            'legacy_collections_included': False, 'legacy_lexical_indexes_protected': legacy_protected, 'model_called': False}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return {**body, 'plan_sha256': hashlib.sha256(raw).hexdigest()}


def execute(project, state_dir, *, expected_plan_sha256):
    from .hybrid import _build_lock, _client, _settings
    from .embedding_cache import open_cache
    try:
        from qdrant_client.http import models
    except ImportError as exc:
        raise FileReadError('vector_dependencies_missing', '请安装 hybrid 可选依赖。') from exc
    destination = project_states(state_dir, project['id'])[0]
    with _build_lock(destination):
        current = plan(project, destination)
        if current['plan_sha256'] != expected_plan_sha256:
            raise FileReadError('cleanup_plan_changed', '版本或索引状态已变化，请重新预览清理。')
        # A changed Git target must not cause its local lexical index to be deleted.
        selected = selected_commit(project, inspect_repository(project['root_path']))
        if selected != current['selected_commit']:
            raise FileReadError('cleanup_plan_changed', '项目版本已变化，请重新预览。')
        client = _client(destination, _settings())
        try:
            keys = current['pool_remove_keys']
            for start in range(0, len(keys), 500):
                client.delete(collection_name=current['collection'],
                    points_selector=models.PointIdsList(points=[point_id(k) for k in keys[start:start + 500]]), wait=True)
        except Exception as exc:
            raise FileReadError('vector_storage_error', '清理未全部完成；请重新预览后核对。') from exc
        finally:
            client.close()
        with open_cache(destination) as conn, conn:
            for key in current['cache_remove_keys']:
                conn.execute('DELETE FROM vectors WHERE namespace=? AND content_key=?', (current['scope'], key))
        index = RetrievalIndex(destination)
        removed = 0
        with index._open() as conn, conn:
            for row in current['lexical_indexes']:
                actual = conn.execute('SELECT metadata,chunks FROM indexes WHERE project_id=? AND commit_id=?', (project['id'], row['commit'])).fetchone()
                if actual is None or hashlib.sha256((actual['metadata'] + '\0' + actual['chunks']).encode()).hexdigest() != row['sha256']:
                    raise FileReadError('cleanup_plan_changed', '词法索引已变化，请重新预览清理。')
                removed += conn.execute('DELETE FROM indexes WHERE project_id=? AND commit_id=?', (project['id'], row['commit'])).rowcount
        return {'project_id': project['id'], 'pool_points_removed': len(current['pool_remove_keys']),
                'cached_vectors_removed': len(current['cache_remove_keys']), 'lexical_indexes_removed': removed,
                'legacy_collections_removed': 0, 'git_and_documents_unchanged': True}
