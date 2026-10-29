"""Opt-in, version-bound semantic retrieval for ordinary project questions."""
import hashlib
import json
import math
import os
from pathlib import Path
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
        'hybrid_enabled': 'false', 'qdrant_url': ''}.items()}
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


def _identity(project, commit, settings):
    value = [project['id'], str(Path(project['root_path']).resolve()), commit,
             settings['embedding_url'], settings['embedding_model']]
    if settings.get('qdrant_url'):
        value.append(settings['qdrant_url'])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _manifest(state_dir, identity):
    return Path(state_dir) / 'semantic-manifests' / (identity + '.json')


def build(project, state_dir):
    """Explicit remote operation. Publish only after every vector was persisted."""
    from .git import inspect_repository, selected_commit
    from .resources import project_states
    state_dir = project_states(state_dir, project["id"])[0]
    settings = _settings()
    commit = selected_commit(project, inspect_repository(project['root_path']))
    if not commit:
        raise FileReadError('invalid_commit', '需要已提交的版本。')
    inventory = RetrievalIndex(state_dir).inventory(project, commit=commit)
    identity = _identity(project, commit, settings)
    marker = _manifest(state_dir, identity)
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.unlink(missing_ok=True)
    chunks = []
    for file in inventory['files']:
        for window in file['windows']:
            record = read_project_file(project, file['path'], commit=commit, **{'start_line': window['start_line'], 'end_line': window['end_line']})
            if record['content_sha256'] != file['content_sha256'] or record['evidence']['blob_id'] != file['blob_id']:
                raise FileReadError('index_corrupted', '词法索引与 Git 原文不一致。')
            chunks.append({'evidence': record['evidence'], 'content_sha256': record['content_sha256'],
                           'text': '\n'.join(line['text'] for line in record['lines'])})
    if not chunks:
        raise FileReadError('vector_empty_index', '没有可索引的文本片段。')
    client = _client(state_dir, settings)
    from qdrant_client.http import models
    collection = 'zest_' + uuid.uuid4().hex
    try:
        for start in range(0, len(chunks), 10):
            batch = chunks[start:start + 10]
            vectors = _embed([x['text'] for x in batch], settings, 'document')
            if start == 0:
                client.create_collection(collection, vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE))
            client.upsert(collection, points=[models.PointStruct(id=start+i, vector=v, payload={'project_id': project['id'], 'commit': commit, **chunk}) for i, (v, chunk) in enumerate(zip(vectors, batch))], wait=True)
        tmp = marker.with_suffix('.tmp-' + uuid.uuid4().hex)
        tmp.write_text(json.dumps({'collection': collection, 'identity': identity, 'count': len(chunks)}), encoding='utf-8')
        os.replace(tmp, marker)
    except FileReadError:
        raise
    except Exception as exc:
        raise FileReadError('vector_storage_error', '构建向量索引失败；未发布该索引。') from exc
    finally:
        client.close()
    return {'backend': 'qdrant_local', 'commit': commit, 'project_id': project['id'], 'chunk_count': len(chunks)}


def semantic_search(project, question, state_dir, commit, settings):
    from .resources import project_states
    state_dir = project_states(state_dir, project["id"])[0]
    identity = _identity(project, commit, settings)
    try:
        manifest = json.loads(_manifest(state_dir, identity).read_text(encoding='utf-8'))
        collection = manifest['collection']
        if manifest['identity'] != identity or not isinstance(collection, str) or not collection.startswith('zest_') or len(collection) != 37:
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
            text = '\n'.join(line['text'] for line in record['lines'])
            if record['content_sha256'] != p['content_sha256'] or record['evidence']['blob_id'] != e['blob_id'] or text != p['text']:
                raise ValueError
            matches.append({'evidence': record['evidence'], 'content_sha256': record['content_sha256'], 'text': text})
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
            key = (e['path'], e['commit'], e['start_line'], e['end_line'])
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
        text = '\n'.join(line['text'] for line in record['lines'])
        if record['content_sha256'] != item['content_sha256'] or record['evidence']['blob_id'] != e['blob_id']:
            raise FileReadError('index_corrupted', '词法索引与 Git 原文不一致。')
        verified.append({'evidence': record['evidence'], 'content_sha256': record['content_sha256'], 'text': text})
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
