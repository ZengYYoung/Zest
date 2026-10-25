"""Local, bounded update receipts; never ask a model to explain a Git check."""
from pathlib import Path
import re

from .files import CREDENTIAL_PATTERNS, FileReadError, read_project_file
from .git import _git, RepositoryError


def meaningful_changes(project, commit, changes, owned_hashes):
    from .survey import _path
    meaningful, excluded = [], []
    for change in changes:
        path = change['path']
        try:
            _path(path)
        except FileReadError:
            continue
        if any(re.search(pattern, path.encode()) for pattern in CREDENTIAL_PATTERNS):
            continue
        owned = owned_hashes.get(path, [])
        if owned and change['change'] != 'deleted':
            try:
                record = read_project_file(project, path, commit=commit, end_line=1)
                if record['content_sha256'] in owned:
                    excluded.append(path)
                    continue
            except FileReadError:
                pass
        if path.startswith('.zest/') and not path.startswith('.zest/docs/'):
            excluded.append(path)
        else:
            meaningful.append(change)
    return meaningful, excluded


def summarize_changes(project, before, after, changes, excluded):
    counts = {kind: sum(item['change'] == kind for item in changes)
              for kind in ('added', 'modified', 'deleted', 'type_changed')}
    result = {'base_commit': before, 'target_commit': after, 'file_count': len(changes),
              'counts': counts, 'excluded_count': len(excluded),
              'files': [{'path': item['path'], 'change': item['change']} for item in changes[:30]],
              'more_files': max(0, len(changes) - 30), 'commit_subjects': []}
    if before and after and before != after and changes:
        # compare_commits has already validated these exact object IDs. Only
        # local metadata is read, with no fetch, model request or code execution.
        try:
            raw = _git(Path(project['root_path']), 'log', '--no-show-signature', '-3',
                       '--format=%s', f'{before}..{after}', '--').stdout
            for subject in raw.decode('utf-8', errors='replace').splitlines()[:3]:
                if any(re.search(pattern, subject.encode()) for pattern in CREDENTIAL_PATTERNS):
                    subject = '此提交说明包含疑似凭据，已隐藏。'
                result['commit_subjects'].append(''.join(c for c in subject if c.isprintable())[:180])
        except RepositoryError:
            result['subjects_unavailable'] = True
    return result
