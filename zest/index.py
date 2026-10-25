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

from .files import CREDENTIAL_PATTERNS, FileReadError, _validate_path, load_text_blob, validate_directory
from .git import inspect_repository, list_tree, verify_commit, selected_commit
from .resources import route_store


MAX_INDEX_FILES = 200
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_INDEX_CHUNKS = 2000


def terms(text):
    found = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.casefold())
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        found.extend([phrase] if len(phrase) == 1 else [phrase[i:i + 2] for i in range(len(phrase) - 1)])
    return Counter(found)


def vector(counts, idf):
    values = {term: (1 + math.log(count)) * idf[term] for term, count in counts.items() if term in idf}
    norm = math.sqrt(sum(v * v for v in values.values()))
    return {term: value / norm for term, value in values.items()} if norm else {}


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
                try:
                    raw = load_text_blob(root, entry)
                except FileReadError as exc:
                    skips[exc.code] += 1
                    continue
                size += len(raw)
                if size > MAX_INDEX_BYTES:
                    raise FileReadError("index_budget_exceeded", "文本字节达到索引预算，未替换已有索引。")
                lines = raw.decode("utf-8").split("\n")
                if lines[-1] == "":
                    lines.pop()
                for start in range(0, len(lines), 30):
                    end = min(start + 40, len(lines))
                    text = "\n".join(lines[start:end])
                    if len(text) > 6000:
                        skips["chunk_too_large"] += 1
                        continue
                    counts = terms(text)
                    if not counts:
                        continue
                    chunks.append({"evidence": {"path": path, "commit": commit, "blob_id": entry.object_id,
                                                "start_line": start + 1, "end_line": end},
                                   "text": text, "terms": dict(counts),
                                   "content_sha256": hashlib.sha256(raw).hexdigest()})
                    if len(chunks) > MAX_INDEX_CHUNKS:
                        raise FileReadError("index_budget_exceeded", "分块数量达到预算，未替换已有索引。")
        metadata = {"backend": "lexical_tfidf", "project_id": project["id"], "commit": commit,
                    "created_at": datetime.now(timezone.utc).isoformat(), "chunk_count": len(chunks),
                    "files_considered": considered, "bytes_indexed": size, "skipped_counts": dict(skips),
                    "working_tree_changes_included": False}
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
                files = {}
                for chunk in chunks:
                    source = chunk["evidence"]
                    _validate_path(source["path"])
                    if (source["commit"] != commit or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["blob_id"])
                            or not re.fullmatch(r"[0-9a-f]{64}", chunk["content_sha256"])
                            or type(source["start_line"]) is not int or type(source["end_line"]) is not int
                            or not 0 < source["start_line"] <= source["end_line"] <= source["start_line"] + 39):
                        raise ValueError
                    reference = {"path": source["path"], "commit": commit, "blob_id": source["blob_id"],
                                 "content_sha256": chunk["content_sha256"]}
                    previous = files.setdefault(source["path"], {**reference, "windows": []})
                    if any(previous[key] != value for key, value in reference.items()):
                        raise ValueError
                    previous["windows"].append({"start_line": source["start_line"], "end_line": source["end_line"]})
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
