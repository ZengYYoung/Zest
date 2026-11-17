"""Prepare bounded, fixed-commit Git history evidence for explicit history questions."""
import hashlib
import json
import re

from .files import FileReadError
from .git_history import changes, diff, history

HISTORY_INTENT = re.compile(r'(?:提交历史|历史提交|提交记录|历史记录|历史版本|哪(?:一|次).*提交|哪(?:一|次).*修改|什么时候(?:修改|更改|改|提交)|何时(?:修改|更改|改|提交)|git\s+(?:log|history|diff)|\bcommit\b|提交\s*[0-9a-f]{40,64})', re.I)
COMMIT = re.compile(r'(?<![0-9a-f])(?:[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])', re.I)
PATH = re.compile(r'(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go|rs|md|rst|json|yaml|yml|sql|toml)\b', re.I)


def _body(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _source(kind, params, value, title):
    body = _body(value)
    return {'evidence': {'kind': kind, 'parameters': params,
                         'content_sha256': hashlib.sha256(body.encode()).hexdigest()},
            'title': title, 'body': body}


def collect(project, question, selected, warnings):
    if not HISTORY_INTENT.search(question):
        return []
    match = COMMIT.search(question)
    target = match.group(0).lower() if match else selected
    if not target:
        return []
    paths = PATH.findall(question)
    path = paths[0] if paths else None
    try:
        # History describes commits; summaries alone are not code evidence.
        params = {'commit': target, 'limit': 20, 'path': path}
        data = history(project, **params)
        results = [_source('git_history', params, data, 'Git 提交历史 · 摘要不是代码变更证据')]
        warnings.append({'code': 'git_history_summary_not_diff'})
        if match:
            exact = history(project, commit=target, limit=1)
            entry = next((item for item in exact['commits'] if item['commit'] == target), None)
            if entry and entry['parents']:
                params = {'before': entry['parents'][0], 'after': target}
                data = changes(project, **params)
                results.insert(0, _source('git_changes', params, data, 'Git 变更文件 · 相对第一父提交'))
                if data['truncated']:
                    warnings.append({'code': 'git_changes_truncated'})
                if path:
                    params = {**params, 'path': path}
                    data = diff(project, **params)
                    results.insert(0, _source('git_diff', params, data, 'Git 历史差异 · 相对第一父提交'))
                    if data['truncated']:
                        warnings.append({'code': 'git_diff_truncated'})
            else:
                warnings.append({'code': 'git_diff_parent_unavailable'})
        return results
    except FileReadError as exc:
        warnings.append({'code': exc.code, 'message': '历史资料不可用；未将其他版本冒充请求的证据。'})
        return []


def recheck(project, source):
    evidence = source['evidence']
    params = evidence['parameters']
    readers = {'git_history': history, 'git_changes': changes, 'git_diff': diff}
    value = readers[evidence['kind']](project, **params)
    body = _body(value)
    return body == source.get('body') and hashlib.sha256(body.encode()).hexdigest() == evidence.get('content_sha256')
