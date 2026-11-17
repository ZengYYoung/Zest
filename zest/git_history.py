"""Bounded, read-only Git history metadata for the selected local repository."""
from pathlib import Path
import re

from .files import CREDENTIAL_PATTERNS, FileReadError, _validate_path
from .git import _git, compare_commits, inspect_repository, selected_commit, verify_commit


def history(project, *, commit=None, limit=20, path=None):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise FileReadError('invalid_history_limit', '历史数量须为 1—50。')
    state = inspect_repository(project['root_path'])
    root = Path(state.root_path)
    target = selected_commit(project, state, commit)
    if not target or not verify_commit(root, target):
        raise FileReadError('invalid_commit', '历史查询需要本地有效的完整 commit。')
    if path is not None:
        if not isinstance(path, str):
            raise FileReadError('invalid_path', '路径须为项目相对路径。')
        _validate_path(path)
    arguments = ['log', '-z', '--no-decorate', '--no-show-signature', f'--max-count={limit + 1}',
                 '--format=%H%x00%P%x00%cI%x00%s', target, '--']
    if path:
        arguments.append(':(literal)' + path)
    raw = _git(root, *arguments).stdout
    if len(raw) > 1024 * 1024:
        raise FileReadError('history_budget_exceeded', '历史摘要过大，未返回不完整结果。')
    parts = raw.split(b'\0')
    if parts and parts[-1] == b'':
        parts.pop()
    if len(parts) % 4:
        raise FileReadError('history_invalid', '无法解析 Git 历史。')
    entries = []
    try:
        for offset in range(0, len(parts), 4):
            identity, parents, date, subject = parts[offset:offset + 4]
            identity = identity.decode('ascii')
            parent_ids = parents.decode('ascii').split()
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', identity) or any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', p) for p in parent_ids):
                raise ValueError
            if any(re.search(pattern, subject) for pattern in CREDENTIAL_PATTERNS):
                summary = '[提交说明包含敏感内容，已隐藏]'
            else:
                summary = subject.decode('utf-8', errors='replace')[:500]
            entries.append({'commit': identity, 'parents': parent_ids, 'committed_at': date.decode('ascii'), 'subject': summary})
    except (UnicodeError, ValueError) as exc:
        raise FileReadError('history_invalid', '无法解析 Git 历史。') from exc
    shallow = _git(root, 'rev-parse', '--is-shallow-repository').stdout.strip() == b'true'
    return {'selected_commit': target, 'path': path, 'commits': entries[:limit],
            'truncated': len(entries) > limit, 'shallow_repository': shallow,
            'limitation': 'commit_subject_is_untrusted_summary_not_change_evidence'}


def changes(project, *, before, after):
    state = inspect_repository(project['root_path'])
    root = Path(state.root_path)
    if not isinstance(before, str) or not isinstance(after, str) or not verify_commit(root, before) or not verify_commit(root, after):
        raise FileReadError('invalid_commit', '版本对比需要本地有效的完整 commit。')
    changes = compare_commits(root, before, after)
    visible, omitted = [], 0
    for change in changes:
        try:
            _validate_path(change['path'])
        except FileReadError:
            omitted += 1
            continue
        visible.append(change)
    return {'before': before, 'after': after, 'changes': visible[:200], 'truncated': len(visible) > 200,
            'excluded_paths': omitted, 'limitation': 'paths_are_metadata_read_historical_files_for_evidence'}


def diff(project, *, before, after, path):
    """Bounded unified diff of allowed blobs; never execute Git diff drivers."""
    from difflib import unified_diff
    from .files import _find_file, load_text_blob
    import hashlib
    if not isinstance(path, str):
        raise FileReadError('invalid_path', '路径须为项目相对路径。')
    _validate_path(path)
    state = inspect_repository(project['root_path'])
    root = Path(state.root_path)
    if not isinstance(before, str) or not isinstance(after, str) or not verify_commit(root, before) or not verify_commit(root, after):
        raise FileReadError('invalid_commit', '版本对比需要本地有效的完整 commit。')
    def get(commit):
        try:
            entry = _find_file(root, commit, path.split('/'))
        except FileReadError as exc:
            if exc.code == 'file_not_found':
                return None
            raise
        raw = load_text_blob(root, entry)
        text = raw.decode('utf-8')
        lines = text.splitlines(keepends=True)
        if len(lines) > 10000:
            raise FileReadError('history_diff_budget', '文件行数超过差异工具预算，请缩小范围。')
        return {'lines': lines, 'blob_id': entry.object_id, 'content_sha256': hashlib.sha256(raw).hexdigest()}
    old, new = get(before), get(after)
    if old is None and new is None:
        raise FileReadError('file_not_found', '两个版本都没有该文件。')
    output, size, truncated = [], 0, False
    for line in unified_diff(old['lines'] if old else [], new['lines'] if new else [],
                             fromfile='a/' + path, tofile='b/' + path, n=3):
        if line and not line.endswith('\n'):
            line += '\n\\ No newline at end of file\n'
        if len(output) >= 400 or size + len(line) > 30000:
            truncated = True
            break
        output.append(line)
        size += len(line)
    def reference(value, commit):
        return {'commit': commit, 'path': path, 'blob_id': value['blob_id'], 'content_sha256': value['content_sha256']} if value else None
    return {'before': reference(old, before), 'after': reference(new, after), 'diff': ''.join(output),
            'truncated': truncated, 'limitation': 'untrusted_diff_not_execution_authority'}
