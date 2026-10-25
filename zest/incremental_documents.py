"""Sparse stable-claim merge and minimal, resumable page scheduling."""
from copy import deepcopy
import hashlib
from pathlib import Path

from .cognition_store import digest
from .files import FileReadError
from .knowledge_snapshots import fingerprint, new_claim


def revision(old, fact, commit):
    value = deepcopy(old)
    def meaning(item):
        return {'section': item['section'], 'statement': item['statement'],
                'evidence': [(p['evidence']['path'], p['text']) for p in item['evidence']]}
    changed = meaning(old['fact']) != meaning(fact)
    value.update(fact=deepcopy(fact), origin=fact['id'], status='verified', last_verified_commit=commit,
                 evidence_checked_commit=commit, revision=old['revision'] + int(changed),
                 previous={'revision': old['revision'], 'sha256': digest(old)} if changed else old.get('previous'))
    value['fact']['id'] = value['id']
    value.pop('authority', None)
    return value


def initial_merge(context, raw, commit):
    baseline = context['baseline']
    claims = deepcopy(baseline['claims'])
    current, problems, assigned = {}, [], set()
    for key, old in baseline['claims'].items():
        if old['status'] == 'retracted': continue
        matches = [f for f in raw.values() if fingerprint(f) == fingerprint(old['fact'])]
        if matches:
            # Coalesce only identical reviewed assertion identities; no fuzzy
            # semantic merge or omission-based removal takes place here.
            chosen = min(matches, key=lambda f: f['id'])
            claims[key] = revision(old, chosen, commit)
            current[key] = claims[key]['fact']; assigned.update(f['id'] for f in matches)
        else:
            check = context['checks'][key]
            claims[key]['evidence_checked_commit'] = commit
            if check['affected']:
                claims[key]['status'] = 'needs_review'
                problems.append(key)
            else:
                claims[key]['status'] = 'inherited'
                claims[key]['fact']['evidence'] = deepcopy(check['evidence'])
                claims[key]['fact']['id'] = key
                claims[key]['fact']['verification'] = {'status': 'evidence_checked_only',
                                                       'last_verified_commit': old['last_verified_commit'],
                                                       'evidence_checked_commit': commit}
    for identity, fact in raw.items():
        if identity in assigned: continue
        claim = new_claim(fact, commit)
        key = claim['id']
        if key in claims and claims[key]['status'] == 'retracted':
            claim = revision(claims[key], fact, commit)
        claim['fact']['id'] = key
        claims[key] = claim; current[key] = claim['fact']
    return claims, current, problems


def required_pages(context, project):
    required = {p['spec']['slug'] for p in context['baseline']['pages']
                if p['status'] not in ('completed', 'retained', 'retired') or
                any(context['checks'].get(key, {}).get('affected') for key in p['spec']['finding_ids'])}
    root = Path(project['root_path'])
    from .documents import _parents
    for p in context['baseline']['pages']:
        path = '.zest/docs/initialization/' + p['spec']['slug'] + '.md'
        _parents(root, path)
        if p['status'] == 'retired': continue
        target = root / path
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != p.get('published_sha256'):
            required.add(p['spec']['slug'])
    # Related pages carry explicit editorial dependencies in both directions.
    while True:
        expanded = required | {p['spec']['slug'] for p in context['baseline']['pages']
                               if set(p['spec']['related_pages']) & required}
        expanded |= {slug for p in context['baseline']['pages'] if p['spec']['slug'] in required for slug in p['spec']['related_pages']}
        if expanded == required: return required
        required = expanded


def apply_changes(value, claims, current, problems, commit, baseline_ids):
    from .document_pages import fail, strings
    rows = value['claim_changes']
    if len(rows) != len(problems) or {r['claim_id'] for r in rows} != set(problems):
        fail('增量决定必须完整且唯一地覆盖有问题的旧事实。')
    aliases = {}
    for row in rows:
        old = claims[row['claim_id']]
        ids = strings(row['finding_ids'], maximum=160)
        if not row['reason'].strip(): fail('事实决定缺少语义理由。')
        if row['decision'] == 'unresolved':
            if ids or row['evidence_statement']: fail('未解决事实不能附带伪确认。')
            old['status'] = 'unresolved'
            continue
        if not ids or not set(ids) <= current.keys(): fail('事实决定必须引用本轮实际核对的事实。')
        if row['evidence_statement'] not in [current[x]['statement'] for x in ids]:
            fail('事实决定的依据不是引用的新事实原文。')
        if row['decision'] == 'retracted':
            old.update(status='retracted', retraction={'commit': commit, 'finding_ids': ids, 'reason': row['reason']})
        else:
            if len(ids) != 1 or ids[0] in aliases: fail('确认或替代关系必须一对一，不能隐式合并旧事实。')
            source = ids[0]
            if source in baseline_ids and source != old['id']:
                fail('替代目标已有独立继承身份。')
            fact = current[source]
            # current facts carry stable IDs for the writer, but origin is the
            # actual immutable research record, not a generated knowledge ID.
            fact = {**fact, 'id': claims[source]['origin']}
            updated = revision(old, fact, commit)
            updated['decision'] = row
            claims[old['id']] = updated
            aliases[source] = old['id']
    for source, target in aliases.items():
        if source != target:
            claims.pop(source, None)
    return aliases


def validate_update(value, context, claims, current, problems, required, contract, commit):
    from .document_pages import fail, strings, typed, validate_plan
    from .model_outputs import DocumentUpdateResponse
    value = typed(DocumentUpdateResponse, value)
    aliases = apply_changes(value, claims, current, problems, commit, set(context['baseline']['claims']))
    active = {key: claim['fact'] for key, claim in claims.items() if claim['status'] in ('verified', 'inherited')}
    old_pages = {p['spec']['slug']: p for p in context['baseline']['pages']}
    blocked = set(strings(value['blocked_pages'], maximum=48, count=6))
    retired = set(strings(value['retired_pages'], maximum=48, count=6))
    if not blocked | retired <= required or blocked & retired: fail('不能阻塞或撤回无关页面。')
    for slug in retired:
        if not all(claims[x]['status'] == 'retracted' for x in old_pages[slug]['spec']['finding_ids']):
            fail('页面存在未撤回事实，不能整体归档。')
    for page in value['pages']:
        page['finding_ids'] = list(dict.fromkeys(aliases.get(x, x) for x in page['finding_ids']))
    plans = {p['slug']: p for p in value['pages']}
    if len(plans) != len(value['pages']): fail('增量页面身份重复。')
    if set(plans) & (blocked | retired): fail('页面状态互相矛盾。')
    if set(plans) & set(old_pages) - required: fail('增量计划试图重写无关页面。')
    if required - set(plans) - blocked - retired: fail('增量计划遗漏了需要处理的页面。')
    final = []
    for slug, page in old_pages.items():
        if slug in plans: continue
        spec = deepcopy(page['spec'])
        if slug not in blocked | retired and not set(spec['finding_ids']) <= active.keys():
            fail('未解决或撤回的事实不能隐式保留为有效页面。')
        final.append(spec)
    final.extend(value['pages'])
    if len(final) > contract['max_pages']: fail('最终页面数量超过容量。')
    # Validate only active page content; blocked/retired rows intentionally keep
    # old IDs as history, so they are not passed to the content writer validator.
    live = [p for p in final if p['slug'] not in blocked | retired]
    covered = set(x for p in live for x in p['finding_ids'])
    preserved_history = {x for slug in blocked for x in old_pages[slug]['spec']['finding_ids']}
    if set(active) - covered - preserved_history: fail('增量计划遗漏已有或新增事实，不能按遗漏删除。')
    if live:
        validate_plan({'pages': [{**p, 'related_pages': [s for s in p['related_pages'] if s in {q['slug'] for q in live}]} for p in live],
                       'coverage_gaps': value['coverage_gaps']}, {k: v for k, v in active.items() if k in covered}, contract)
    for p in value['pages']:
        if not set(p['related_pages']) <= {q['slug'] for q in final} - {p['slug']}: fail('关联页不属于最终清单。')
        if p['slug'] in old_pages:
            mandatory = {x for x in old_pages[p['slug']]['spec']['finding_ids'] if x in active}
            if not mandatory <= set(p['finding_ids']): fail('旧页事实不能因本轮未提及而丢失。')
    return value, active, final


def prepare_incremental(ledger, state_dir, project, dossier, record, *, model=None):
    from .cognition import CognitionEngine
    from .document_pages import facts_for
    from .incremental_contracts import incremental_contract
    envelope = record['envelope']; context = envelope['incremental_context']
    if envelope.get('incremental_contract') != incremental_contract():
        raise FileReadError('cognition_contract_changed', '跨版本知识契约已变化，未重发旧请求。')
    from .knowledge_snapshots import check_context
    check_context(state_dir, envelope)
    raw = facts_for(dossier)
    if ledger.value:
        if ledger.value.get('input_facts_sha256') != digest(raw) or ledger.value['envelope_sha256'] != digest(envelope):
            raise FileReadError('document_context_changed', '增量文档输入或授权发生变化。')
        if 'plan' in ledger.value: return True
    else:
        ledger.value = {'version': 1, 'run_id': ledger.run_id, 'envelope_sha256': digest(envelope),
                        'input_facts_sha256': digest(raw), 'facts_sha256': digest(raw),
                        'status': 'planning', 'pages': [],
                        'incremental': {'facts': raw, 'baseline': context['baseline'], 'snapshot': deepcopy(context['baseline'])}}
        ledger.save()
    claims, current, problems = initial_merge(context, raw, dossier['commit'])
    required = required_pages(context, project)
    old_ids = {x for p in context['baseline']['pages'] for x in p['spec']['finding_ids']}
    new_ids = {key for key,c in claims.items() if c['status'] in ('verified','inherited')} - old_ids
    def planning_fact(fact):
        # This task judges the relationship between complete, already reviewed
        # assertions. Source excerpts remain intact in the private snapshot and
        # are supplied to each actual page writer/reviewer, not copied en masse.
        return {k: fact[k] for k in ('id','section','statement')} | {
            'source_locations': [{k: p['evidence'][k] for k in ('path','commit','start_line','end_line')}
                                 for p in fact['evidence']]}
    payload = {'commit': dossier['commit'], 'max_pages': envelope['document_generation']['max_pages'],
               'existing_pages': [p['spec'] for p in context['baseline']['pages']],
               'required_updates': sorted(required), 'changed_paths': context['changes'],
               'current_findings': [planning_fact(f) for f in current.values()],
               'inherited_findings': [planning_fact(c['fact']) for c in claims.values() if c['status'] == 'inherited'],
               'problems': [{'claim_id': key, 'fact': planning_fact(claims[key]['fact']), 'evidence_status': context['checks'][key]['status']}
                            for key in problems], 'new_claim_ids': sorted(new_ids)}
    payload = ledger.value.setdefault('update_payload', payload)
    pending_snapshot = deepcopy(context['baseline'])
    pending_snapshot.update(claims=deepcopy(claims), graph=context['graph'])
    for page in pending_snapshot['pages']:
        if page['spec']['slug'] in required:
            page['status'] = 'blocked'
    ledger.value['incremental']['snapshot'] = pending_snapshot
    ledger.save()
    try:
        if not required and not new_ids:
            response = {'claim_changes': [], 'pages': [], 'blocked_pages': [], 'retired_pages': [], 'coverage_gaps': []}
        elif not current and not new_ids:
            response = {'claim_changes': [{'claim_id': key, 'decision': 'unresolved', 'finding_ids': [], 'evidence_statement': '',
                                           'reason': '本轮没有足够的核对事实，保留旧断言为待复核。'} for key in problems],
                        'pages': [], 'blocked_pages': sorted(required), 'retired_pages': [], 'coverage_gaps': []}
        else:
            response = CognitionEngine(state_dir, model=model).call(ledger.run_id, 'document-update', 0, 'document_update', payload, envelope)
        value, facts, final = validate_update(response, context, claims, current, problems, required,
                                             envelope['document_generation'], dossier['commit'])
        old = {p['spec']['slug']: p for p in context['baseline']['pages']}
        pages, snapshot_pages = [], []
        for spec in final:
            slug = spec['slug']; before = deepcopy(old.get(slug, {}))
            status = ('blocked' if slug in value['blocked_pages'] else 'retired' if slug in value['retired_pages']
                      else 'pending' if slug in {p['slug'] for p in value['pages']} else 'retained')
            before.update(spec=spec, status=status, evidence_checked_commit=dossier['commit'])
            snapshot_pages.append(deepcopy(before))
            pages.append(before)
        new_paths = [p for p in context.get('coverage_changes', context['changes']) if p not in {p['evidence']['path'] for c in claims.values() for p in c['fact']['evidence']}]
        coverage = list(dict.fromkeys([*value['coverage_gaps'],
                                      *(f'尚无核对事实覆盖新增或变更资料：{p}' for p in new_paths)]))
        snapshot = {'version': 1, 'run_id': ledger.run_id, 'commit': dossier['commit'], 'claims': claims,
                    'pages': snapshot_pages, 'graph': context['graph'], 'coverage_gaps': coverage,
                    'parent_snapshot': {'run_id': context['baseline']['run_id'], 'sha256': context['baseline_sha256']},
                    'parent_manifest_sha256': context['baseline'].get('manifest_sha256')}
        ledger.value.update(plan={'pages': final, 'coverage_gaps': coverage}, pages=pages, facts_sha256=digest(facts), status='writing',
                            incremental={'facts': facts, 'baseline': context['baseline'], 'snapshot': snapshot,
                                         'coverage_pending': bool(coverage), 'claim_changes': value['claim_changes']})
        ledger.value.pop('error', None)
        ledger.save()
        return True
    except FileReadError as exc:
        ledger.value.update(status='unknown' if exc.code == 'cognition_execution_unknown' else 'failed', error={'code': exc.code, 'message': str(exc)[:500]})
        ledger.save()
        return False
