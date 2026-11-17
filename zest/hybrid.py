"""Opt-in, version-bound semantic retrieval for ordinary project questions."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .files import FileReadError, read_project_file
from .index import RetrievalIndex


def _settings():
    values = {}
    try:
        from dotenv import dotenv_values
        values = dotenv_values(Path.cwd() / '.env', interpolate=False)
    except ImportError:
        pass
    def get(name, default=''):
        return (os.environ.get(name, values.get(name) or default) or '').strip()
    settings = {k: get('ZEST_' + k.upper(), default) for k, default in {
        'embedding_url': '', 'embedding_key': '', 'embedding_model': 'text-embedding-v4',
        'rerank_url': '', 'rerank_key': '', 'rerank_model': 'qwen3-rerank',
        'hybrid_enabled': 'false', 'qdrant_url': '', 'embedding_revision': ''}.items()}
    shared_key = get('DASHSCOPE_API_KEY')
    settings['embedding_key'] = settings['embedding_key'] or shared_key
    settings['rerank_key'] = settings['rerank_key'] or shared_key
    # Only derive native endpoints from a valid, exact OpenAI-compatible base.
    base = get('DASHSCOPE_BASE_URL').rstrip('/')
    try:
        parsed = urlsplit(base)
        if (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and parsed.path == '/compatible-mode/v1'):
            native = base[:-len('/compatible-mode/v1')] + '/api/v1/services/'
            settings['embedding_url'] = settings['embedding_url'] or native + 'embeddings/text-embedding/text-embedding'
            settings['rerank_url'] = settings['rerank_url'] or native + 'rerank/text-rerank/text-rerank'
    except ValueError:
        pass
    return settings


def enabled():
    return _settings()['hybrid_enabled'].lower() == 'true'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, key, body):
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise FileReadError('retrieval_config_invalid', '检索服务 URL 格式无效。') from exc
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FileReadError('retrieval_config_invalid', '检索服务 URL 须为不含凭据或查询参数的 HTTPS 地址。')
    if not key:
        raise FileReadError('retrieval_not_configured', '检索服务尚未配置。')
    try:
        request = Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('response too large')
        return json.loads(raw)
    except Exception as exc:
        raise FileReadError('retrieval_service_error', '检索服务请求失败或响应无效。') from exc


def _embed(texts, settings, kind):
    if not settings['embedding_url'] or not settings['embedding_key']:
        raise FileReadError('embedding_not_configured', 'Embedding 服务尚未配置。')
    data = _post(settings['embedding_url'], settings['embedding_key'],
                 {'model': settings['embedding_model'], 'input': {'texts': texts},
                  'parameters': {'text_type': kind}})
    try:
        items = sorted(data['output']['embeddings'], key=lambda x: x.get('text_index', x.get('index')))
        if [x.get('text_index', x.get('index')) for x in items] != list(range(len(texts))):
            raise ValueError
        vectors = [x['embedding'] for x in items]
        if not vectors or not all(isinstance(v, list) and 0 < len(v) <= 4096 and len(v) == len(vectors[0])
                                  and all(type(x) in (int, float) and math.isfinite(x) for x in v) for v in vectors):
            raise ValueError
        return vectors
    except (KeyError, TypeError, ValueError):
        raise FileReadError('embedding_invalid_response', 'Embedding 响应格式无效。') from None


def _client(state_dir, settings):
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:
        raise FileReadError('vector_dependencies_missing', '请安装 hybrid 可选依赖。') from exc
    try:
        url = settings.get('qdrant_url', '')
        if url:
            parsed = urlsplit(url)
            if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in ('', '/') or parsed.port is None):
                raise FileReadError('qdrant_config_invalid', 'Qdrant 地址须为带端口的本地 HTTP 地址。')
            return QdrantClient(url=url, prefer_grpc=False, timeout=10)
        return QdrantClient(path=str(Path(state_dir) / 'qdrant'))
    except FileReadError:
        raise
    except Exception as exc:
        raise FileReadError('vector_storage_error', '无法打开本地 Qdrant 索引。') from exc


def _identity(project, commit, settings, chunking=None):
    value = [project['id'], str(Path(project['root_path']).resolve()), commit,
             settings['embedding_url'], settings['embedding_model']]
    if settings.get('qdrant_url'):
        value.append(settings['qdrant_url'])
    if chunking:
        value.append(chunking)
    if settings.get('embedding_revision'):
        value.append({'revision': settings['embedding_revision']})
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _manifest(state_dir, identity):
    return Path(state_dir) / 'semantic-manifests' / (identity + '.json')


def _source_chunks(project, inventory, commit):
    """Reread each fixed Git blob once, then verify every stored window."""
    from .files import _find_file, load_text_blob
    from .chunking import slice_record
    root = Path(project['root_path'])
    chunks = []
    for file in inventory['files']:
        entry = _find_file(root, commit, file['path'].split('/'))
        raw = load_text_blob(root, entry)
        if hashlib.sha256(raw).hexdigest() != file['content_sha256'] or entry.object_id != file['blob_id']:
            raise FileReadError('index_corrupted', '词法索引与 Git 原文不一致。')
        physical = raw.split(b'\n')
        if physical[-1] == b'':
            physical.pop()
        decoded = []
        for i, line in enumerate(physical):
            if line.endswith(b'\r') and (i < len(physical) - 1 or raw.endswith(b'\n')):
                line = line[:-1]
            decoded.append(line.decode('utf-8'))
        for window in file['windows']:
            start, end = window['start_line'], window['end_line']
            if not 1 <= start <= end <= len(decoded) or end - start >= 40:
                raise FileReadError('index_corrupted', '索引片段范围无效。')
            source = {'path': file['path'], 'commit': commit, 'blob_id': entry.object_id, **window}
            record = {'lines': [{'number': i + 1, 'text': decoded[i]} for i in range(start - 1, end)]}
            try:
                lines = slice_record(record, source)
            except ValueError as exc:
                raise FileReadError('index_corrupted', '索引片段范围无效。') from exc
            chunks.append({'evidence': source, 'content_sha256': file['content_sha256'],
                           'text': '\n'.join(line['text'] for line in lines)})
    if not chunks:
        raise FileReadError('vector_empty_index', '没有可索引的文本片段。')
    return chunks


def _build_plan(project, commit, settings, inventory, chunks, cached):
    from .embedding_cache import content_key
    keys = [content_key(chunk['text']) for chunk in chunks]
    unique = {}
    for key, chunk in zip(keys, chunks):
        if key not in cached:
            unique.setdefault(key, {'text_sha256': key, 'evidence': chunk['evidence'],
                                    'content_sha256': chunk['content_sha256'], 'characters': len(chunk['text']), 'text': chunk['text']})
    try:
        target = urlsplit(settings['embedding_url'])
        if (target.scheme != 'https' or not target.hostname or target.username or target.password
                or target.query or target.fragment):
            raise ValueError
    except ValueError as exc:
        raise FileReadError('retrieval_config_invalid', '请先配置有效的 HTTPS 向量化接口。') from exc
    qdrant_url = settings.get('qdrant_url', '')
    if qdrant_url:
        try:
            q = urlsplit(qdrant_url)
            if (q.scheme != 'http' or q.hostname not in ('127.0.0.1', 'localhost', '::1')
                    or q.username or q.password or q.query or q.fragment or q.path not in ('', '/') or q.port is None):
                raise ValueError
        except ValueError as exc:
            raise FileReadError('qdrant_config_invalid', 'Qdrant 地址须为带端口的本地 HTTP 地址。') from exc
    plan = {'project_id': project['id'], 'commit': commit, 'chunk_count': len(chunks),
            'reused_chunks': sum(key in cached for key in keys), 'new_vectors': len(unique),
            'unique_chunks': len(set(keys)), 'pending_characters': sum(x['characters'] for x in unique.values()),
            'embedding_configured': bool(settings['embedding_url'] and settings['embedding_key']),
            'embedding_url': settings['embedding_url'], 'embedding_model': settings['embedding_model'],
            'embedding_revision': settings.get('embedding_revision', ''),
            'qdrant_url': settings.get('qdrant_url', '') or 'embedded_local',
            'chunking': inventory['metadata'].get('chunking'),
            'pending_sources': list(unique.values()), 'model_called': False}
    # Bind the key without exposing it or its fingerprint in the preview.
    raw = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    secret_digest = hashlib.sha256(settings['embedding_key'].encode()).digest()
    return {**plan, 'plan_sha256': hashlib.sha256(raw + secret_digest).hexdigest()}


def plan_build(project, state_dir):
    """Inspect reuse and outbound volume without network or writes."""
    from .git import inspect_repository, selected_commit
    from .resources import project_states
    from .embedding_cache import content_key, namespace, load_cached_readonly
    destination = project_states(state_dir, project['id'])[0]
    settings = _settings()
    commit = selected_commit(project, inspect_repository(project['root_path']))
    if not commit:
        raise FileReadError('invalid_commit', '需要已提交的版本。')
    inventory = RetrievalIndex(destination).inventory(project, commit=commit)
    chunks = _source_chunks(project, inventory, commit)
    keys = [content_key(chunk['text']) for chunk in chunks]
    scope = namespace(project, settings, inventory['metadata'].get('chunking'))
    cached = load_cached_readonly(destination, scope, keys)
    return _build_plan(project, commit, settings, inventory, chunks, cached)

def index_status(project, state_dir):
    """Report local publication markers without contacting Qdrant or any model."""
    from .git import inspect_repository, selected_commit
    from .resources import project_states
    destination = project_states(state_dir, project['id'])[0]
    repository = inspect_repository(project['root_path'])
    commit = selected_commit(project, repository)
    status = {'commit': commit, 'working_tree_dirty': repository.dirty,
              'lexical': 'missing', 'semantic': 'unavailable', 'hybrid_enabled': enabled(),
              'qdrant_checked': False}
    if not commit:
        return status
    try:
        inventory = RetrievalIndex(destination).inventory(project, commit=commit)
    except FileReadError as exc:
        if exc.code in ('index_missing', 'index_stale'):
            return status
        raise
    status['lexical'] = 'ready'
    settings = _settings()
    identity = _identity(project, commit, settings, inventory['metadata'].get('chunking'))
    from .semantic_pool import find_version
    if find_version(destination, project['id'], identity) is not None:
        status['semantic'] = 'published_unverified'
        return status
    marker = _manifest(destination, identity)
    if not marker.exists():
        status['semantic'] = 'missing'
        return status
    try:
        data = json.loads(marker.read_text(encoding='utf-8'))
        if data['identity'] != identity or not re.fullmatch(r'zest_[0-9a-f]{32}', data['collection']):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        status['semantic'] = 'invalid'
        return status
    status['semantic'] = 'published_unverified'
    return status


@contextmanager
def _build_lock(state_dir, *, shared=False):
    import fcntl
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(directory / 'semantic-build.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise FileReadError('semantic_build_busy', '该项目的语义索引正在构建。') from exc
        try:
            yield
        finally:
            os.close(fd)
    except OSError as exc:
        raise FileReadError('vector_storage_error', '无法锁定语义索引。') from exc


def build(project, state_dir, *, expected_plan_sha256=None):
    from .resources import project_states
    destination = project_states(state_dir, project['id'])[0]
    with _build_lock(destination):
        return _build(project, destination, expected_plan_sha256=expected_plan_sha256)


def _build(project, state_dir, *, expected_plan_sha256=None):
    """Publish the current version after all vectors have been persisted."""
    from .git import inspect_repository, selected_commit
    from .resources import project_states
    state_dir = project_states(state_dir, project["id"])[0]
    settings = _settings()
    commit = selected_commit(project, inspect_repository(project['root_path']))
    if not commit:
        raise FileReadError('invalid_commit', '需要已提交的版本。')
    inventory = RetrievalIndex(state_dir).inventory(project, commit=commit)
    identity = _identity(project, commit, settings, inventory["metadata"].get("chunking"))
    marker = _manifest(state_dir, identity)
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    chunks = _source_chunks(project, inventory, commit)
    from .embedding_cache import content_key, namespace, open_cache, load_vectors, save_vectors
    scope = namespace(project, settings, inventory["metadata"].get("chunking"))
    keys = [content_key(chunk['text']) for chunk in chunks]
    by_key = {key: chunk['text'] for key, chunk in zip(keys, chunks)}
    from .semantic_pool import collection_name, point_id, find_version, read_versions, publish
    collection = collection_name(scope)
    client = _client(state_dir, settings)
    from qdrant_client.http import models
    try:
        with open_cache(state_dir) as cache:
            vectors = load_vectors(cache, scope, keys)
            if expected_plan_sha256 is not None:
                actual = _build_plan(project, commit, settings, inventory, chunks, vectors)
                if actual['plan_sha256'] != expected_plan_sha256:
                    raise FileReadError('semantic_plan_changed', '项目版本、索引、缓存或检索配置已变化，请重新预览。')
            reused = sum(key in vectors for key in keys)
            missing = [key for key in by_key if key not in vectors]
            for start in range(0, len(missing), 10):
                batch = missing[start:start + 10]
                embedded = _embed([by_key[key] for key in batch], settings, 'document')
                if len(embedded) != len(batch):
                    raise FileReadError('embedding_invalid_response', 'Embedding 响应数量不一致。')
                produced = dict(zip(batch, embedded))
                save_vectors(cache, scope, produced)
                vectors.update(produced)
            dimensions = {len(vectors[key]) for key in keys}
            if len(dimensions) != 1:
                raise FileReadError('embedding_dimension_mismatch', '向量维度不一致，请更换模型版本标识后重建。')
            dimension = dimensions.pop()
            existed = client.collection_exists(collection)
            if not existed:
                client.create_collection(collection, vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE))
            existing = {}
            unique_keys = list(by_key)
            for start in range(0, len(unique_keys), 100):
                subset = unique_keys[start:start + 100]
                records = client.retrieve(collection_name=collection, ids=[point_id(k) for k in subset], with_payload=True)
                existing.update({str(p.id): p.payload for p in records})
            pending = []
            for key in unique_keys:
                pid = point_id(key)
                payload = existing.get(pid)
                if payload is not None:
                    if payload.get('content_key') != key or payload.get('scope') != scope:
                        raise FileReadError('semantic_pool_corrupted', '向量内容身份冲突，未发布索引。')
                else:
                    pending.append(key)
            for start in range(0, len(pending), 100):
                batch = pending[start:start + 100]
                client.upsert(collection, points=[models.PointStruct(id=point_id(k), vector=vectors[k],
                    payload={'content_key': k, 'scope': scope}) for k in batch], wait=True)
        items = [{'key': key, 'evidence': chunk['evidence'], 'content_sha256': chunk['content_sha256']}
                 for key, chunk in zip(keys, chunks)]
        snapshot = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        previous = find_version(state_dir, project['id'], identity)
        unchanged = bool(previous and previous['collection'] == collection and previous.get('snapshot_sha256') == snapshot and not pending)
        versions = read_versions(state_dir, project['id'])
        if not unchanged or not versions or versions[0]['identity'] != identity:
            publish(state_dir, project['id'], {'identity': identity, 'commit': commit, 'collection': collection,
                                'scope': scope, 'storage': settings.get('qdrant_url', ''),
                                'snapshot_sha256': snapshot, 'items': items})
    except FileReadError:
        raise
    except Exception as exc:
        raise FileReadError('vector_storage_error', '构建向量索引失败；未发布该索引。') from exc
    finally:
        client.close()
    return {'backend': 'qdrant_content_pool', 'commit': commit, 'project_id': project['id'], 'chunk_count': len(chunks),
            'reused_chunks': reused, 'new_vectors': len(missing), 'unique_chunks': len(by_key),
            'new_points': len(pending), 'collection_reused': unchanged}


def _search_pool(project, question, state_dir, commit, settings, snapshot):
    try:
        from qdrant_client.http import models
    except ImportError as exc:
        raise FileReadError('vector_dependencies_missing', '请安装 hybrid 可选依赖。') from exc
    from .chunking import slice_record
    from .embedding_cache import content_key, namespace
    from .semantic_pool import collection_name, point_id
    inventory = RetrievalIndex(state_dir).inventory(project, commit=commit)
    scope = namespace(project, settings, inventory['metadata'].get('chunking'))
    if snapshot['commit'] != commit or snapshot.get('scope') != scope or snapshot['collection'] != collection_name(scope):
        raise FileReadError('semantic_manifest_corrupted', '语义版本清单身份不匹配。')
    locations = {}
    for item in snapshot['items']:
        pid = point_id(item['key'])
        locations.setdefault(pid, []).append(item)
    if not locations:
        return []
    vector = _embed([question], settings, 'query')[0]
    client = _client(state_dir, settings)
    try:
        results = client.query_points(collection_name=snapshot['collection'], query=vector, limit=20,
            with_payload=True, query_filter=models.Filter(must=[models.HasIdCondition(has_id=list(locations))])).points
        matches = []
        for point in results:
            pid = str(point.id)
            payload = point.payload or {}
            for item in locations.get(pid, []):
                e = item['evidence']
                if payload.get('scope') != scope or payload.get('content_key') != item['key'] or e.get('commit') != commit:
                    raise ValueError
                record = read_project_file(project, e['path'], commit=commit, start_line=e['start_line'], end_line=e['end_line'])
                text = '\n'.join(line['text'] for line in slice_record(record, e))
                if record['content_sha256'] != item['content_sha256'] or record['evidence']['blob_id'] != e['blob_id'] or content_key(text) != item['key']:
                    raise ValueError
                matches.append({'evidence': {**record['evidence'], **{k: e[k] for k in ('start_col', 'end_col') if k in e}},
                                'content_sha256': record['content_sha256'], 'text': text})
                if len(matches) >= 20:
                    return matches
        return matches
    except FileReadError:
        raise
    except Exception as exc:
        raise FileReadError('vector_storage_error', '向量索引不可用或来源校验失败。') from exc
    finally:
        client.close()


def semantic_search(project, question, state_dir, commit, settings):
    from .resources import project_states
    state_dir = project_states(state_dir, project["id"])[0]
    inventory = RetrievalIndex(state_dir).inventory(project, commit=commit)
    identity = _identity(project, commit, settings, inventory["metadata"].get("chunking"))
    from .semantic_pool import find_version
    snapshot = find_version(state_dir, project['id'], identity)
    if snapshot is not None:
        with _build_lock(state_dir, shared=True):
            snapshot = find_version(state_dir, project['id'], identity)
            if snapshot is None:
                raise FileReadError('semantic_index_missing', '该历史版本的语义索引已不再保留。')
            return _search_pool(project, question, state_dir, commit, settings, snapshot)
    try:
        manifest = json.loads(_manifest(state_dir, identity).read_text(encoding='utf-8'))
        collection = manifest['collection']
        if manifest['identity'] != identity or not isinstance(collection, str) or not re.fullmatch(r'zest_[0-9a-f]{32}', collection):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise FileReadError('semantic_index_missing', '当前项目、版本和模型没有可用向量索引。') from None
    vector = _embed([question], settings, 'query')[0]
    client = _client(state_dir, settings)
    try:
        from qdrant_client.http import models
        results = client.query_points(collection_name=collection, query=vector, limit=20, with_payload=True,
            query_filter=models.Filter(must=[models.FieldCondition(key='project_id', match=models.MatchValue(value=project['id'])),
                                            models.FieldCondition(key='commit', match=models.MatchValue(value=commit))])).points
        matches = []
        for point in results:
            p = point.payload
            e = p['evidence']
            if p['project_id'] != project['id'] or p['commit'] != commit or e['commit'] != commit:
                raise ValueError
            record = read_project_file(project, e['path'], commit=commit, start_line=e['start_line'], end_line=e['end_line'])
            from .chunking import slice_record
            text = '\n'.join(line['text'] for line in slice_record(record, e))
            if record['content_sha256'] != p['content_sha256'] or record['evidence']['blob_id'] != e['blob_id'] or text != p['text']:
                raise ValueError
            matches.append({'evidence': {**record['evidence'], **{k: e[k] for k in ('start_col', 'end_col') if k in e}}, 'content_sha256': record['content_sha256'], 'text': text})
        return matches
    except FileReadError:
        raise
    except Exception as exc:
        raise FileReadError('vector_storage_error', '向量索引不可用或来源校验失败。') from exc
    finally:
        client.close()


def fuse(*rankings):
    scores, items = {}, {}
    for ranking in rankings:
        seen = set()
        for rank, item in enumerate(ranking, 1):
            e = item['evidence']
            key = (e['path'], e['commit'], e['start_line'], e['end_line'], e.get('start_col', -1), e.get('end_col', -1))
            if key in seen:
                continue
            seen.add(key)
            items[key] = item
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    return [items[k] for k in sorted(items, key=lambda k: (-scores[k], k))]


def rerank(question, matches, settings):
    if not settings['rerank_url'] or not settings['rerank_key']:
        raise FileReadError('rerank_not_configured', 'Rerank 服务尚未配置。')
    data = _post(settings['rerank_url'], settings['rerank_key'], {'model': settings['rerank_model'],
        'input': {'query': question, 'documents': [m['text'] for m in matches]},
        'parameters': {'top_n': len(matches), 'return_documents': False}})
    try:
        rows = data['output']['results']
        indices = [row['index'] for row in rows]
        if len(indices) != len(matches) or any(type(i) is not int for i in indices) or set(indices) != set(range(len(matches))):
            raise ValueError
        return [matches[i] for i in indices]
    except (KeyError, TypeError, ValueError):
        raise FileReadError('rerank_invalid_response', 'Rerank 响应格式无效。') from None


def search(project, question, state_dir, commit, warnings, *, symbols=()):
    lexical = RetrievalIndex(state_dir).search(project, question, commit=commit, limit=20)
    verified = []
    for item in lexical['matches']:
        e = item['evidence']
        record = read_project_file(project, e['path'], commit=commit, start_line=e['start_line'], end_line=e['end_line'])
        from .chunking import slice_record
        try:
            text = '\n'.join(line['text'] for line in slice_record(record, e))
        except ValueError as exc:
            raise FileReadError('index_corrupted', '索引片段范围无效。') from exc
        if record['content_sha256'] != item['content_sha256'] or record['evidence']['blob_id'] != e['blob_id']:
            raise FileReadError('index_corrupted', '词法索引与 Git 原文不一致。')
        verified.append({'evidence': {**record['evidence'], **{k: e[k] for k in ('start_col', 'end_col') if k in e}}, 'content_sha256': record['content_sha256'], 'text': text})
    lexical_matches = []
    for path, start, end in symbols:
        record = read_project_file(project, path, commit=commit, start_line=start, end_line=end)
        lexical_matches.append({'evidence': record['evidence'], 'content_sha256': record['content_sha256'],
                                'text': '\n'.join(line['text'] for line in record['lines'])})
    lexical_matches = fuse(lexical_matches, verified)[:20]
    if lexical['metadata']['skipped_counts']:
        warnings.append({'code': 'index_skipped_sources'})
    settings = _settings()
    try:
        semantic = semantic_search(project, question, state_dir, commit, settings)
    except FileReadError as exc:
        warnings.append({'code': exc.code, 'retrieval_path': 'lexical'})
        return lexical_matches, 'lexical'
    combined = fuse(lexical_matches, semantic)
    if not combined:
        return [], 'hybrid'
    try:
        combined = rerank(question, combined, settings)
        return combined, 'hybrid_rerank'
    except FileReadError as exc:
        warnings.append({'code': exc.code, 'retrieval_path': 'hybrid_rrf'})
        return combined, 'hybrid_rrf'
