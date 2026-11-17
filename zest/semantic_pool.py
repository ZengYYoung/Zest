"""Content-addressed Qdrant pool and two atomically published version maps."""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .files import FileReadError


def point_id(content_key):
    if not re.fullmatch(r'[0-9a-f]{64}', content_key):
        raise FileReadError('semantic_manifest_corrupted', '向量内容身份无效。')
    return str(uuid.UUID(hex=content_key[:32]))


def collection_name(scope):
    if not re.fullmatch(r'[0-9a-f]{64}', scope):
        raise FileReadError('semantic_manifest_corrupted', '向量空间身份无效。')
    return 'zest_pool_' + scope[:32]


def _path(state_dir, project_id):
    try:
        if str(uuid.UUID(project_id)) != project_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise FileReadError('semantic_manifest_corrupted', '项目身份无效。') from exc
    parent = Path(state_dir) / 'semantic-pools'
    project = parent / project_id
    if parent.is_symlink() or project.is_symlink():
        raise FileReadError('semantic_manifest_corrupted', '语义版本目录不能是符号链接。')
    return project / 'versions.json'


def read_versions(state_dir, project_id):
    path = _path(state_dir, project_id)
    if path.is_symlink():
        raise FileReadError('semantic_manifest_corrupted', '语义版本清单不能是符号链接。')
    if not path.exists():
        return []
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['format'] != 'content-pool-v1' or not isinstance(data['versions'], list) or len(data['versions']) > 2:
            raise ValueError
        for version in data['versions']:
            if (not re.fullmatch(r'[0-9a-f]{64}', version['identity'])
                    or not re.fullmatch(r'zest_pool_[0-9a-f]{32}', version['collection'])
                    or not re.fullmatch(r'[0-9a-f]{64}', version['scope'])
                    or version['collection'] != collection_name(version['scope'])
                    or not isinstance(version.get('storage', ''), str)
                    or not isinstance(version['items'], list) or not 0 < len(version['items']) <= 2000
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', version['commit'])):
                raise ValueError
            for item in version['items']:
                if (not re.fullmatch(r'[0-9a-f]{64}', item['key']) or not isinstance(item['evidence'], dict)
                        or item['evidence'].get('commit') != version['commit']
                        or not re.fullmatch(r'[0-9a-f]{64}', item['content_sha256'])):
                    raise ValueError
            encoded = json.dumps(version['items'], sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
            if hashlib.sha256(encoded).hexdigest() != version['snapshot_sha256']:
                raise ValueError
        return data['versions']
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise FileReadError('semantic_manifest_corrupted', '语义版本清单损坏，未使用该索引。') from exc


def find_version(state_dir, project_id, identity):
    return next((v for v in read_versions(state_dir, project_id) if v['identity'] == identity), None)


def publish(state_dir, project_id, snapshot):
    versions = read_versions(state_dir, project_id)
    versions = [snapshot] + [v for v in versions if v['identity'] != snapshot['identity']]
    versions = versions[:2]
    path = _path(state_dir, project_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump({'format': 'content-pool-v1', 'versions': versions}, out, ensure_ascii=False, separators=(',', ':'))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return versions
