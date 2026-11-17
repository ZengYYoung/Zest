"""Project-local, content-addressed cache for document embedding vectors."""
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from .files import FileReadError


def namespace(project, settings, chunking):
    # A project boundary prevents incidental reuse across unrelated repositories.
    values = ['document-embedding-cache-v1', project['id'], str(Path(project['root_path']).resolve()),
              settings['embedding_url'], settings['embedding_model'], settings.get('embedding_revision', ''), chunking]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def content_key(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _encode(vector):
    return json.dumps(vector, separators=(',', ':'), allow_nan=False)


def _validate_vector(vector):
    return (isinstance(vector, list) and 0 < len(vector) <= 4096
            and all(type(x) in (int, float) and math.isfinite(x) for x in vector))


@contextmanager
def open_cache(state_dir):
    directory = Path(state_dir)
    path = directory / 'embedding-cache.sqlite3'
    if path.is_symlink():
        raise FileReadError('embedding_cache_error', '向量缓存不能是符号链接。')
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(sqlite3.connect(path)) as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise FileReadError('embedding_cache_error', '向量缓存版本不兼容。')
            with conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS vectors (
                    namespace TEXT NOT NULL, content_key TEXT NOT NULL, vector TEXT NOT NULL,
                    vector_sha256 TEXT NOT NULL, PRIMARY KEY (namespace, content_key))''')
                if version == 0:
                    conn.execute('PRAGMA user_version=1')
            yield conn
    except (sqlite3.Error, OSError) as exc:
        raise FileReadError('embedding_cache_error', '无法访问向量缓存。') from exc


def load_vectors(conn, scope, keys):
    result = {}
    for key in dict.fromkeys(keys):
        row = conn.execute('SELECT vector,vector_sha256 FROM vectors WHERE namespace=? AND content_key=?', (scope, key)).fetchone()
        if row is None:
            continue
        try:
            raw, checksum = row
            if not isinstance(raw, str) or len(raw) > 120000 or hashlib.sha256(raw.encode()).hexdigest() != checksum:
                raise ValueError
            vector = json.loads(raw)
            if not _validate_vector(vector):
                raise ValueError
        except (ValueError, TypeError, RecursionError) as exc:
            raise FileReadError('embedding_cache_corrupted', '向量缓存损坏，未使用缓存。') from exc
        result[key] = vector
    return result


def save_vectors(conn, scope, values):
    rows = []
    for key, vector in values.items():
        if not _validate_vector(vector):
            raise FileReadError('embedding_invalid_response', 'Embedding 响应格式无效。')
        raw = _encode(vector)
        rows.append((scope, key, raw, hashlib.sha256(raw.encode()).hexdigest()))
    with conn:
        conn.executemany('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)', rows)


def load_cached_readonly(state_dir, scope, keys):
    path = Path(state_dir) / 'embedding-cache.sqlite3'
    if path.is_symlink():
        raise FileReadError('embedding_cache_error', '向量缓存不能是符号链接。')
    if not path.exists():
        return {}
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            if conn.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise FileReadError('embedding_cache_error', '向量缓存版本不兼容。')
            return load_vectors(conn, scope, keys)
    except (sqlite3.Error, OSError) as exc:
        raise FileReadError('embedding_cache_error', '无法读取向量缓存。') from exc
