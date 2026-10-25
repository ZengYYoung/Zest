"""Repository-wide metadata inventory with lazy, fixed-commit source reads.

The survey is a set of starting points, not a permission boundary. No worktree
bytes or host path are included in the portable source descriptor.
"""
from collections import deque
import hashlib
from pathlib import Path

from .files import FileReadError, load_text_blob, _find_file
from .git import list_tree, verify_commit
from .survey import _path, _roles


SOURCE_SCOPE = {
    'version': 1, 'kind': 'project_commit',
    'access': 'safe_project_text_read_only',
    'context': 'automatic_windows_v1',
}


def project_policy(policy):
    return {**policy, 'max_turns_per_task': 8, 'max_tool_actions_per_task': 32}


def inventory(root, commit, seeds):
    """Freeze allowed path/blob identities without eagerly loading the repository."""
    root = Path(root)
    if not verify_commit(root, commit):
        raise FileReadError('invalid_commit', '项目资料需要本地有效的固定提交。')
    by_path = {s['evidence']['path']: s for s in seeds}
    output, queue = [], deque([(commit, '')])
    while queue:
        tree, prefix = queue.popleft()
        for entry in list_tree(root, tree):
            path = prefix + entry.path
            try:
                _path(path, directory=entry.object_type == 'tree')
            except FileReadError:
                continue
            if entry.object_type == 'tree':
                queue.append((entry.object_id, path + '/'))
            elif entry.regular_file:
                seed = by_path.get(path)
                if seed and seed['evidence']['blob_id'] == entry.object_id:
                    output.append(seed)
                else:
                    output.append({'id': 'P' + hashlib.sha256(path.encode()).hexdigest()[:24],
                                   'roles': _roles(path),
                                   'evidence': {'path': path, 'commit': commit, 'blob_id': entry.object_id,
                                                'content_identity': 'git_blob_v1'}})
    return output


def load_source(root, source):
    """Resolve the manifest's exact blob; credential and text gates run on read."""
    evidence = source['evidence']
    _path(evidence['path'])
    entry = _find_file(Path(root), evidence['commit'], evidence['path'].split('/'))
    if entry.object_id != evidence['blob_id']:
        raise FileReadError('cognition_source_changed', '原文与固定项目目录身份不一致。')
    raw = load_text_blob(Path(root), entry)
    return {**source, 'body': raw.decode(),
            'evidence': {**evidence, 'content_sha256': hashlib.sha256(raw).hexdigest()}}


def evidence_matches(source, approved):
    """Check saved evidence identities for both old packages and lazy manifests."""
    if any(not source.get(k) or source[k] != approved.get(k) for k in ('path', 'commit', 'blob_id')):
        return False
    if approved.get('content_identity') == 'git_blob_v1':
        value = source.get('content_sha256', '')
        return (source.get('content_identity') == 'git_blob_v1' and len(value) == 64
                and all(c in '0123456789abcdef' for c in value))
    return bool(source.get('content_sha256') and source['content_sha256'] == approved.get('content_sha256'))


def source_root(state_dir, record):
    from .storage import ProjectRegistry
    project = ProjectRegistry(state_dir).get_project_readonly(record['project_id'])
    if project is None:
        raise FileReadError('project_not_found', '未找到固定资料所属项目。')
    return project['root_path']
