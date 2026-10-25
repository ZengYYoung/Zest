"""Bounded model views of complete, durable project research records."""
from copy import deepcopy

from .cognition_contracts import canonical
from .cognition_context import compact_results
from .files import FileReadError


def research_view(results, *, budget=14000):
    values = compact_results(results)
    selected, used = [], 0
    for item in reversed(values):
        size = len(canonical(item))
        if used + size <= budget:
            selected.append(item)
            used += size
    selected.reverse()
    tools, metadata = [], []
    for item in selected:
        if item['tool'] == 'read' and item['result'].get('ok') and not item['result']['data'].get('text'):
            metadata.append(item['result']['data'])
        else:
            tools.append(item)
    catalog = []
    for item in values:
        if item.get('citation_id'):
            data = item['result']['data']
            ev = data['evidence']
            catalog.append({'citation_id': item['citation_id'], 'source_id': data['source_id'],
                            'path': ev['path'], 'start_line': ev['start_line'], 'end_line': ev['end_line']})
    return {'tool_results': tools, 'metadata_observations': metadata, 'evidence_catalog': catalog,
            'context_window': {'shown_results': len(selected), 'saved_results': len(values),
                               'originals_retained': True,
                               'continuation': '较早原文仍保存在调查记录中；需要再次核对时按来源与行段 read，R 编号复用。'}}


def fit_payload(payload, compile_request, maximum):
    """Split optional context lists before dispatch; never truncate source proofs.

    Task goals, submitted findings and their review evidence are required. A
    required item too large to fit is reported honestly rather than auto-approved.
    """
    value = deepcopy(payload)
    locations = [('starting_documents',), ('shared_knowledge',),
                 ('material_catalog',), ('refresh_context', 'historical_leads'),
                 ('refresh_context', 'uncovered_changes'), ('refresh_context', 'changes'),
                 ('refresh_context', 'focus'), ('gaps',), ('tool_results',)]
    counts = {}
    for path in locations:
        parent = value
        for key in path[:-1]:
            parent = parent.get(key, {})
        items = parent.get(path[-1])
        if isinstance(items, list):
            counts[path] = len(items)
    while True:
        request = compile_request(value)
        if request['chars'] <= maximum:
            return value
        changed = False
        for path in locations:
            parent = value
            for key in path[:-1]:
                parent = parent.get(key, {})
            items = parent.get(path[-1])
            minimum = 1 if path[-1] in ('material_catalog', 'tool_results') else 0
            if not isinstance(items, list) or len(items) <= minimum:
                continue
            keep = max(minimum, len(items) // 2)
            parent[path[-1]] = items[-keep:] if path[-1] == 'tool_results' else items[:keep]
            if path[-1] == 'material_catalog':
                value['catalog_coverage'] = {**value.get('catalog_coverage', {}),
                                             'shown': keep, 'truncated': True}
            if path[-1] == 'starting_documents':
                value['starting_documents_coverage'] = {**value.get('starting_documents_coverage', {}), 'included': keep}
            value.setdefault('automatic_context_windows', {})['.'.join(path)] = {
                'shown': keep, 'total': counts[path], 'omitted_is_not_completed': True}
            changed = True
            break
        if not changed:
            raise FileReadError('cognition_prompt_budget', '必要原文或任务超过单次请求容量，已保留记录；本次未外发。')
