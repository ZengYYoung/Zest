"""Stable, evidence-scoped gaps; historical receipts are never rewritten."""
from .cognition_store import digest
from .cognition_quality import pending_category


def fact_key(finding):
    return {'statement': finding['statement'].strip(), 'kind': finding['kind'],
            'section': finding['section'],
            'sources': sorted({(p['evidence']['path'], p['evidence']['blob_id']) for p in finding['evidence']})}


def attempted_issues(store, record):
    attempted, seen = set(), set()
    while record['id'] not in seen:
        seen.add(record['id'])
        full = store.show(record['id'], include_envelope=True)
        context = full['envelope'].get('repair_context')
        if not context:
            break
        if context.get('version') == 2 and full['status'] in ('completed', 'partial', 'failed') and full['call_count']:
            attempted.update(i['issue_id'] for i in context['selected_issues'])
        record = store.show(context['source_run_id'])
    return attempted


def normalize_backlog(store, record, dossier):
    """Coalesce exact duplicates, close exact verified matches, retain unknowns.

    Similar wording, successful job completion and a file being read never prove
    that an old business question has been answered.
    """
    attempted = attempted_issues(store, record)
    known = {digest(fact_key(item['finding'])) for s in dossier['sections'] for item in s['facts']
             if item['finding']['kind'] == 'observed'}
    sections = {s['id']: s for s in dossier['sections']}
    for gap in dossier['plan_gaps']:
        if isinstance(gap, dict):
            target = sections.get(gap.get('section'))
            if target:
                target['pending'].append({'question': gap['question'], 'plan_gap': True})
    groups, closed = {}, set()
    for section in dossier['sections']:
        local = {}
        for original in section['pending']:
            item = dict(original)
            category, title = pending_category(item)
            tracked = item.get('tracked_issue')
            if 'finding' in item:
                key = {'commit': dossier['commit'], 'finding': fact_key(item['finding'])}
                review = item.get('review') or {}
                if (item['status'] != 'invalidated' and not review.get('conflicts') and not review.get('conflict_links')
                        and digest({**fact_key(item['finding']), 'kind': 'observed'}) in known):
                    closed.add(digest(key))
                    continue
                if (review.get('coverage_complete') is True and not review.get('conflicts') and not review.get('conflict_links')
                        and any(c['verdict'] == 'contradicted' for c in review.get('checks', []))):
                    closed.add(digest(key))  # Rejected assertion, never promoted to a fact.
                    continue
            else:
                key = {'commit': dossier['commit'], 'question': item['question'].strip()}
            identity = tracked['issue_id'] if tracked else digest(key)
            if tracked and 'finding' not in item:
                category, title = tracked['category'], tracked['title']
            origin = item.get('origin_run_id', record['id'])
            finding_id = item.get('origin_finding_id') or item.get('id')
            if finding_id and finding_id.startswith(origin + ':'):
                finding_id = finding_id[len(origin) + 1:]
            ref = {'run_id': origin, 'finding_id': finding_id} if 'finding' in item else {'run_id': origin, 'task_id': item.get('task_id')}
            if tracked:
                state = 'attempted' if identity in attempted else 'actionable'
            elif category in ('analysis_hint', 'external_material', 'invalidated'):
                state = 'external' if category == 'external_material' else 'deferred'
            elif 'finding' in item or item.get('material_request'):
                state = 'attempted' if identity in attempted else 'actionable'
            else:
                state = 'needs_question'
            entry = groups.setdefault(identity, {'issue_id': identity, 'category': category, 'title': title,
                'question': tracked['question'] if tracked else item.get('question') or item['finding']['statement'], 'state': state,
                'sections': [], 'references': []})
            if section['id'] not in entry['sections']:
                entry['sections'].append(section['id'])
            if ref not in entry['references']:
                entry['references'].append(ref)
            if 'finding' in item:
                entry.setdefault('evidence_locations', [p['evidence'] for p in item['finding']['evidence']])
            if item.get('material_request'):
                entry['material_request'] = item['material_request']
            item.update(pending_id=identity, category=category, category_title=title, gap_state=entry['state'])
            local.setdefault(identity, item)
        section['pending'] = list(local.values())
    values = list(groups.values())
    # Contradictions take precedence; no semantic similarity merging is used.
    values.sort(key=lambda i: (i['category'] != 'contradiction',
                              'flows' not in i['sections'], i['issue_id']))
    counts = {}
    for issue in values:
        cat = issue['category']
        counts.setdefault(cat, {'category': cat, 'title': issue['title'], 'count': 0})['count'] += 1
    dossier.update(pending_count=len(values), pending_summary=list(counts.values()),
        backlog={'version': 1, 'total': len(values), 'actionable': sum(i['state'] == 'actionable' for i in values),
                 'attempted': sum(i['state'] == 'attempted' for i in values),
                 'external': sum(i['state'] == 'external' for i in values),
                 'deferred': sum(i['state'] in ('deferred', 'needs_question') for i in values),
                 'closed_exact_matches': len(closed), 'issues': values})
    if record.get('repair', {}).get('version') == 2:
        selected = store.show(record['id'], include_envelope=True)['envelope']['repair_context']['selected_issues']
        open_ids = set(groups)
        resolved = sum(i['issue_id'] not in open_ids for i in selected)
        dossier['repair_progress'] = {'processed': len(selected), 'resolved': resolved, 'remaining': len(selected) - resolved}
