"""Versioned, local lexical TF-IDF chunks: a transparent RAG baseline."""

from collections import Counter, deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from .chunking import CHUNKING_VERSION, chunks as make_chunks, slice_record
from .files import CREDENTIAL_PATTERNS, MAX_FILE_BYTES, FileReadError, _validate_path, load_text_blob, validate_directory
from .git import RepositoryError, compare_commits, inspect_repository, list_tree, verify_commit, selected_commit
from .resources import route_store


MAX_INDEX_FILES = 200
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_INDEX_CHUNKS = 2000


def _reuse_policy():
    return hashlib.sha256(b'index-file-reuse-v1\0' + str(MAX_FILE_BYTES).encode() + b'\0' + b'\0'.join(CREDENTIAL_PATTERNS)).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _diff_baseline(index, project_id, root, commit):
    """Use only a complete, integrity-checked local snapshot as a diff baseline."""
    if not index.db_path.exists():
        return None
    try:
        with index._open(write=False) as conn:
            row = conn.execute('SELECT commit_id,metadata,chunks FROM indexes WHERE project_id=? ORDER BY created_at DESC LIMIT 1', (project_id,)).fetchone()
        if row is None:
            return None
        meta, items = json.loads(row['metadata']), json.loads(row['chunks'])
        files = meta.get('indexed_files')
        if (meta.get('project_id') != project_id or meta.get('commit') != row['commit_id']
                or meta.get('backend') != 'lexical_tfidf' or meta.get('chunking') != CHUNKING_VERSION
                or meta.get('reuse_policy') != _reuse_policy() or not isinstance(files, dict)
                or meta.get('indexed_files_sha256') != _digest(files)
                or not isinstance(items, list) or len(items) != meta.get('chunk_count')
                or len(items) > MAX_INDEX_CHUNKS or meta.get('chunks_sha256') != _digest(items)):
            return None
        groups = {}
        for item in items:
            e = item['evidence']
            _validate_path(e['path'])
            file = files[e['path']]
            if (e['commit'] != row['commit_id'] or e['blob_id'] != file['blob_id']
                    or item['content_sha256'] != file['content_sha256']):
                return None
            groups.setdefault(e['path'], []).append(item)
        changed = {entry['path'] for entry in compare_commits(root, row['commit_id'], commit)}
        return {'commit': row['commit_id'], 'files': files, 'groups': groups, 'changed': changed}
    except (FileReadError, RepositoryError, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return None


def terms(text):
    found = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.casefold())
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        found.extend([phrase] if len(phrase) == 1 else [phrase[i:i + 2] for i in range(len(phrase) - 1)])
    return Counter(found)


def vector(counts, idf):
    values = {term: (1 + math.log(count)) * idf[term] for term, count in counts.items() if term in idf}
    norm = math.sqrt(sum(v * v for v in values.values()))
    return {term: value / norm for term, value in values.items()} if norm else {}


def _chunk_kind(path):
    if path.endswith(('.py', '.pyi')):
        return 'python'
    if path.lower().endswith(('.md', '.rst')):
        return 'markdown'
    return 'text'


def _reusable_chunks(index, project_id):
    """Read the latest matching local index; invalid caches are ignored."""
    if not index.db_path.exists():
        return {}
    try:
        with index._open(write=False) as conn:
            row = conn.execute('SELECT commit_id,metadata,chunks FROM indexes WHERE project_id=? ORDER BY created_at DESC LIMIT 1', (project_id,)).fetchone()
        if row is None:
            return {}
        metadata, items = json.loads(row['metadata']), json.loads(row['chunks'])
        if (metadata.get('project_id') != project_id or metadata.get('commit') != row['commit_id']
                or metadata.get('backend') != 'lexical_tfidf' or metadata.get('chunking') != CHUNKING_VERSION
                or not isinstance(items, list) or len(items) != metadata.get('chunk_count') or len(items) > MAX_INDEX_CHUNKS):
            return {}
        encoded = json.dumps(items, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        if metadata.get('chunks_sha256') != hashlib.sha256(encoded).hexdigest():
            return {}
        groups, paths = {}, {}
        for item in items:
            evidence = item['evidence']
            path = evidence['path']
            _validate_path(path)
            if evidence['commit'] != row['commit_id']:
                return {}
            identity = (evidence['blob_id'], _chunk_kind(path))
            origin = paths.setdefault(identity, path)
            if origin == path:
                groups.setdefault(identity, []).append(item)
        return groups
    except (FileReadError, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return {}


def _reuse_file(items, raw, path, commit, blob_id):
    """Verify cached chunks against the immutable blob before reuse."""
    if not items:
        return None
    sha = hashlib.sha256(raw).hexdigest()
    lines = raw.decode('utf-8').replace('\r\n', '\n').split('\n')
    if lines[-1] == '':
        lines.pop()
    result = []
    try:
        for item in items:
            old = item['evidence']
            start, end = old['start_line'], old['end_line']
            if (old['blob_id'] != blob_id or item['content_sha256'] != sha or type(start) is not int or type(end) is not int
                    or not 1 <= start <= end <= len(lines) or end - start >= 40):
                return None
            record = {'lines': [{'number': i + 1, 'text': lines[i]} for i in range(start - 1, end)]}
            text = '\n'.join(line['text'] for line in slice_record(record, old))
            if text != item['text'] or len(text) > 6000 or dict(terms(text)) != item['terms']:
                return None
            evidence = {'path': path, 'commit': commit, 'blob_id': blob_id, 'start_line': start, 'end_line': end}
            evidence.update({key: old[key] for key in ('start_col', 'end_col') if key in old})
            result.append({'evidence': evidence, 'text': text, 'terms': item['terms'], 'content_sha256': sha})
        return result
    except (ValueError, KeyError, TypeError, IndexError):
        return None


@route_store(projects=("build",), fallback={name: ("index_missing", "index_stale") for name in ("search", "inventory")})
class RetrievalIndex:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "index.sqlite3"

    @contextmanager
    def _open(self, *, write=True):
        try:
            if not write:
                if self.db_path.is_symlink():
                    raise FileReadError("index_storage_error", "索引数据库不能是符号链接。")
                with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                    conn.row_factory = sqlite3.Row
                    if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                        raise FileReadError("index_version_error", "检索索引版本不兼容。")
                    yield conn
                return
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise FileReadError("index_version_error", "检索索引版本不兼容。")
                with conn:
                    conn.execute("""CREATE TABLE IF NOT EXISTS indexes (
                        project_id TEXT NOT NULL, commit_id TEXT NOT NULL, created_at TEXT NOT NULL,
                        metadata TEXT NOT NULL, chunks TEXT NOT NULL, PRIMARY KEY(project_id,commit_id))""")
                    if version == 0:
                        conn.execute("PRAGMA user_version=1")
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise FileReadError("index_storage_error", "无法访问检索索引，请检查权限及完整性。") from exc

    def build(self, project):
        state = inspect_repository(project["root_path"])
        commit = selected_commit(project, state)
        if commit is None:
            raise FileReadError("unborn_repository", "需要已提交文件才能建立索引。")
        root = Path(state.root_path)
        queue = deque([(commit, "")])
        chunks, skips = [], Counter()
        considered = size = 0
        reused_files = rechunked_files = diff_reused_files = 0
        indexed_files = {}
        reusable = _reusable_chunks(self, project["id"])
        baseline = _diff_baseline(self, project['id'], root, commit)
        started = time.monotonic()
        while queue:
            if time.monotonic() - started > 30:
                raise FileReadError("index_budget_exceeded", "构建达到时长预算，未替换已有索引。")
            tree, prefix = queue.popleft()
            for entry in list_tree(root, tree):
                if time.monotonic() - started > 30:
                    raise FileReadError("index_budget_exceeded", "构建达到时长预算，未替换已有索引。")
                path = prefix + entry.path
                if entry.object_type == "tree":
                    try:
                        validate_directory(path)
                    except FileReadError:
                        skips["blocked_directory"] += 1
                        continue
                    if len(path.split("/")) >= 32:
                        skips["blocked_directory"] += 1
                        continue
                    queue.append((entry.object_id, path + "/"))
                    continue
                try:
                    _validate_path(path)
                except FileReadError as exc:
                    skips[exc.code] += 1
                    continue
                considered += 1
                if considered > MAX_INDEX_FILES:
                    raise FileReadError("index_budget_exceeded", "文件数量达到索引预算，未替换已有索引。")
                previous_file = baseline['files'].get(path) if baseline and path not in baseline['changed'] else None
                previous_chunks = baseline['groups'].get(path) if previous_file else None
                if (previous_chunks and entry.regular_file and previous_file.get('blob_id') == entry.object_id
                        and type(previous_file.get('bytes')) is int and 0 <= previous_file['bytes'] <= MAX_FILE_BYTES):
                    size += previous_file['bytes']
                    if size > MAX_INDEX_BYTES:
                        raise FileReadError('index_budget_exceeded', '文本字节达到索引预算，未替换已有索引。')
                    for item in previous_chunks:
                        chunks.append({**item, 'evidence': {**item['evidence'], 'path': path, 'commit': commit}})
                    indexed_files[path] = previous_file
                    reused_files += 1
                    diff_reused_files += 1
                    if len(chunks) > MAX_INDEX_CHUNKS:
                        raise FileReadError('index_budget_exceeded', '分块数量达到预算，未替换已有索引。')
                    continue
                try:
                    raw = load_text_blob(root, entry)
                except FileReadError as exc:
                    skips[exc.code] += 1
                    continue
                size += len(raw)
                if size > MAX_INDEX_BYTES:
                    raise FileReadError("index_budget_exceeded", "文本字节达到索引预算，未替换已有索引。")
                indexed_files[path] = {'blob_id': entry.object_id, 'bytes': len(raw),
                                       'content_sha256': hashlib.sha256(raw).hexdigest()}
                previous = reusable.get((entry.object_id, _chunk_kind(path)))
                reused = _reuse_file(previous, raw, path, commit, entry.object_id)
                if reused is not None:
                    chunks.extend(reused)
                    reused_files += 1
                    if len(chunks) > MAX_INDEX_CHUNKS:
                        raise FileReadError("index_budget_exceeded", "分块数量达到预算，未替换已有索引。")
                    continue
                rechunked_files += 1
                # Match read_project_file's CRLF-normalized source lines.
                source_text = raw.decode("utf-8").replace("\r\n", "\n")
                for window, text in make_chunks(path, source_text):
                    if time.monotonic() - started > 30:
                        raise FileReadError("index_budget_exceeded", "构建达到时长预算，未替换已有索引。")
                    counts = terms(text)
                    if not counts:
                        continue
                    chunks.append({"evidence": {"path": path, "commit": commit, "blob_id": entry.object_id,
                                                **window},
                                   "text": text, "terms": dict(counts),
                                   "content_sha256": hashlib.sha256(raw).hexdigest()})
                    if len(chunks) > MAX_INDEX_CHUNKS:
                        raise FileReadError("index_budget_exceeded", "分块数量达到预算，未替换已有索引。")
        chunks_digest = hashlib.sha256(json.dumps(chunks, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        metadata = {"backend": "lexical_tfidf", "project_id": project["id"], "commit": commit,
                    "created_at": datetime.now(timezone.utc).isoformat(), "chunk_count": len(chunks),
                    "files_considered": considered, "bytes_indexed": size, "skipped_counts": dict(skips),
                    "files_reused": reused_files, "files_rechunked": rechunked_files, "chunks_sha256": chunks_digest,
                    "working_tree_changes_included": False, "chunking": CHUNKING_VERSION,
                    "diff_base_commit": baseline['commit'] if baseline else None, "files_diff_reused": diff_reused_files,
                    "indexed_files": indexed_files, "indexed_files_sha256": _digest(indexed_files),
                    "reuse_policy": _reuse_policy()}
        with self._open() as conn, conn:
            conn.execute("INSERT OR REPLACE INTO indexes VALUES (?,?,?,?,?)",
                         (project["id"], commit, metadata["created_at"], json.dumps(metadata), json.dumps(chunks)))
        return metadata

    def inventory(self, project, *, commit):
        """List stored source references without returning cached original text."""
        root = Path(project["root_path"])
        if not verify_commit(root, commit):
            raise FileReadError("invalid_commit", "索引总览需要有效的固定 commit。")
        if not self.db_path.exists():
            raise FileReadError("index_missing", "尚未建立索引。")
        with self._open(write=False) as conn:
            row = conn.execute("SELECT metadata,chunks FROM indexes WHERE project_id=? AND commit_id=?",
                               (project["id"], commit)).fetchone()
            if row is None:
                raise FileReadError("index_stale", "当前版本没有索引，未使用历史版本索引。")
            try:
                metadata, chunks = json.loads(row[0]), json.loads(row[1])
                if (metadata["backend"] != "lexical_tfidf" or metadata["project_id"] != project["id"]
                        or metadata["commit"] != commit or not isinstance(chunks, list)
                        or len(chunks) != metadata["chunk_count"] or len(chunks) > MAX_INDEX_CHUNKS):
                    raise ValueError
                if "chunks_sha256" in metadata:
                    encoded = json.dumps(chunks, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
                    if hashlib.sha256(encoded).hexdigest() != metadata["chunks_sha256"]:
                        raise ValueError
                files = {}
                for chunk in chunks:
                    source = chunk["evidence"]
                    _validate_path(source["path"])
                    if (source["commit"] != commit or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["blob_id"])
                            or not re.fullmatch(r"[0-9a-f]{64}", chunk["content_sha256"])
                            or type(source["start_line"]) is not int or type(source["end_line"]) is not int
                            or not 0 < source["start_line"] <= source["end_line"] <= source["start_line"] + 39):
                        raise ValueError
                    if "start_col" in source or "end_col" in source:
                        if (metadata.get("chunking") != CHUNKING_VERSION or source["start_line"] != source["end_line"]
                                or type(source.get("start_col")) is not int or type(source.get("end_col")) is not int
                                or not 0 <= source["start_col"] < source["end_col"] <= 262144
                                or source["end_col"] - source["start_col"] > 6000):
                            raise ValueError
                    reference = {"path": source["path"], "commit": commit, "blob_id": source["blob_id"],
                                 "content_sha256": chunk["content_sha256"]}
                    previous = files.setdefault(source["path"], {**reference, "windows": []})
                    if any(previous[key] != value for key, value in reference.items()):
                        raise ValueError
                    previous["windows"].append({key: source[key] for key in ("start_line", "end_line", "start_col", "end_col") if key in source})
            except (FileReadError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                raise FileReadError("index_corrupted", "索引来源记录损坏，未生成总览。") from exc
        return {"status": "available", "metadata": metadata,
                "files": [files[path] for path in sorted(files)],
                "content_reverified": False, "cached_text_included": False}

    def search(self, project, query, *, commit=None, limit=5):
        if not query.strip() or len(query) > 2000 or not 1 <= limit <= 20:
            raise FileReadError("invalid_index_query", "问题须为非空且不超过 2000 字符，数量为 1—20。")
        state = inspect_repository(project["root_path"])
        selected = selected_commit(project, state, commit)
        if selected is None or not verify_commit(Path(state.root_path), selected):
            raise FileReadError("invalid_commit", "需要本地存在的完整 commit。")
        if not self.db_path.exists():
            raise FileReadError("index_missing", "没有索引，请先执行 index build。")
        with self._open(write=False) as conn:
            row = conn.execute("SELECT metadata,chunks FROM indexes WHERE project_id=? AND commit_id=?",
                               (project["id"], selected)).fetchone()
            if row is None:
                raise FileReadError("index_stale", "当前 commit 没有索引，请重建或明确查询已索引的历史版本。")
            try:
                metadata, chunks = json.loads(row[0]), json.loads(row[1])
                if (metadata["backend"] != "lexical_tfidf" or metadata["project_id"] != project["id"]
                        or metadata["commit"] != selected or not isinstance(chunks, list)
                        or len(chunks) != metadata["chunk_count"] or len(chunks) > MAX_INDEX_CHUNKS):
                    raise ValueError
                if "chunks_sha256" in metadata:
                    encoded = json.dumps(chunks, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
                    if hashlib.sha256(encoded).hexdigest() != metadata["chunks_sha256"]:
                        raise ValueError("Invalid index metadata")
                df = Counter()
                for chunk in chunks:
                    e = chunk["evidence"]
                    _validate_path(e["path"])
                    if (e["commit"] != selected or not isinstance(chunk["text"], str)
                            or len(chunk["text"]) > 6000 or e["start_line"] < 1
                            or not e["start_line"] <= e["end_line"] <= e["start_line"] + 39
                            or not all(type(count) is int and count > 0 for count in chunk["terms"].values())
                            or any(re.search(p, chunk["text"].encode()) for p in CREDENTIAL_PATTERNS)):
                        raise ValueError("Invalid index chunk")
                    if "start_col" in e or "end_col" in e:
                        if (metadata.get("chunking") != CHUNKING_VERSION or e["start_line"] != e["end_line"]
                                or type(e.get("start_col")) is not int or type(e.get("end_col")) is not int
                                or not 0 <= e["start_col"] < e["end_col"] <= 262144
                                or e["end_col"] - e["start_col"] != len(chunk["text"])):
                            raise ValueError
                    df.update(chunk["terms"].keys())
                idf = {term: math.log((1 + len(chunks)) / (1 + count)) + 1 for term, count in df.items()}
                query_vector = vector(terms(query), idf)
                matches = []
                for chunk in chunks:
                    document_vector = vector(chunk["terms"], idf)
                    score = sum(value * document_vector.get(term, 0) for term, value in query_vector.items())
                    if score > 0:
                        matches.append({key: value for key, value in chunk.items() if key != "terms"} | {"score": score})
            except (FileReadError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
                raise FileReadError("index_corrupted", "索引记录损坏，请重新构建。") from exc
        matches.sort(key=lambda item: (-item["score"], item["evidence"]["path"], item["evidence"]["start_line"]))
        return {"backend": "lexical_tfidf", "commit": selected, "query": query,
                "matches": matches[:limit], "metadata": metadata,
                "truncated": len(matches) > limit}
