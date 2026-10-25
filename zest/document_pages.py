"""Durable page planning, prose writing, semantic review and independent publication.

The private ledger lives beside cognition state. Provider attempts reuse the
existing call/receipt ledger and the same total budget; page completion means a
reviewed artifact was persisted, never that all project knowledge is complete.
"""
import hashlib
import json
from pathlib import Path
import re

from .cognition_store import CognitionStore, digest, valid_id
from .document_contracts import document_contract
from .files import CREDENTIAL_PATTERNS, FileReadError
from .resources import _directory, _regular, record_state


def fail(message):
    raise FileReadError('invalid_document_output', message)


def strings(value, *, maximum=500, count=50):
    if not isinstance(value, list) or len(value) > count:
        fail('页面字段不是有界列表。')
    if any(not isinstance(s, str) or not s.strip() or len(s) > maximum for s in value):
        fail('页面字段包含空值或超长文本。')
    if len(set(value)) != len(value):
        fail('页面字段存在重复值。')
    return value


def facts_for(dossier):
    result = {}
    for section in dossier['sections']:
        for item in section['facts']:
            identity = item.get('origin_run_id', dossier['run_id']) + ':' + item['id']
            result[identity] = {'id': identity, 'section': section['id'],
                                'statement': item['finding']['statement'],
                                'evidence': item['finding']['evidence'],
                                **({'partial_origin': item['finding']['partial_origin']}
                                   if item['finding'].get('partial_origin') else {})}
    return result


def validate_plan(value, facts, contract):
    from .model_outputs import DocumentPlanResponse
    value = typed(DocumentPlanResponse, value)
    pages = value['pages']
    if not 1 <= len(pages) <= contract['max_pages']:
        fail('页面数量超出计划容量。')
    names, covered = set(), set()
    for page in pages:
        slug = page['slug']
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', slug) or slug == 'index' or slug in names:
            fail('页面身份无效或重复。')
        names.add(slug)
        for key, limit in [('title', 100), ('description', 300), ('goal', 800)]:
            strings([page[key]], maximum=limit)
            if '\n' in page[key] or '\r' in page[key]:
                fail('页面元信息不能换行。')
        ids = strings(page['finding_ids'], maximum=160)
        if not ids or not set(ids) <= facts.keys():
            fail('页面引用了未经核对的事实。')
        covered.update(ids)
    for page in pages:
        related = strings(page['related_pages'], maximum=48, count=contract['max_pages'])
        if not set(related) <= names - {page['slug']}:
            fail('关联页面不属于本次计划。')
    if covered != facts.keys():
        fail('文档计划遗漏了已有核对事实。')
    strings(value['coverage_gaps'], count=12)
    return value


def typed(model, value):
    try:
        return model.model_validate(value).model_dump(mode='json')
    except (ValueError, TypeError) as exc:
        raise FileReadError('invalid_document_output', '页面响应不符合已批准结构。') from exc


def validate_page(value, spec, contract):
    from .model_outputs import DocumentWriteResponse
    value = typed(DocumentWriteResponse, value)
    blocks = value['blocks']
    if not 1 <= len(blocks) <= contract['max_blocks']:
        fail('页面正文块数量无效。')
    covered, headings = set(), set()
    for block in blocks:
        ids = strings(block['finding_ids'], maximum=160)
        if not ids or not set(ids) <= set(spec['finding_ids']):
            fail('正文引用了页面范围外的事实。')
        covered.update(ids)
        strings([block['markdown']], maximum=contract['max_page_chars'])
        prose = re.sub(r'```[\s\S]*?```', '', block['markdown'])
        for heading in re.findall(r'^#{1,6}\s+(.+)$', prose, re.M):
            key = heading.strip().casefold()
            if key in headings:
                fail('页面有重复小节。')
            headings.add(key)
        if re.search(r'^#\s', prose, re.M) or block['markdown'].startswith('---'):
            fail('页面标题和元信息由程序维护。')
        for link in re.findall(r'\]\(([^\s)]+)\)', prose):
            if link not in {slug + '.md' for slug in spec['related_pages']}:
                fail('正文链接不属于关联文档。')
    body = '\n\n'.join(b['markdown'] for b in blocks)
    plain = re.sub(r'```[\s\S]*?```', '', body)
    if not any(len(line.strip()) >= 15 and not re.match(r'^\s*(?:#|[-*|>]|\d+\.)', line)
               for line in plain.splitlines()):
        fail('正文缺少解释性段落。')
    if len(body) > contract['max_page_chars'] or covered != set(spec['finding_ids']):
        fail('页面过长或遗漏了计划事实。')
    strings(value['limits'], count=12)
    safe_text(body + '\n' + '\n'.join(value['limits']))
    return value


def validate_review(value, page):
    from .model_outputs import DocumentReviewResponse
    value = typed(DocumentReviewResponse, value)
    expected = {f'B{i}' for i in range(1, len(page['blocks']) + 1)}
    checks = value['checks']
    if len(checks) != len(expected) or {c['block_id'] for c in checks} != expected:
        fail('页面语义核对没有完整覆盖正文。')
    if value['issues'] or any(c['verdict'] != 'supported' or not c['reason'].strip() for c in checks):
        raise FileReadError('document_review_rejected', '页面正文未通过完整语义核对，保留原文。')
    return value


def safe_text(body):
    raw = body.encode()
    if '\0' in body or any(re.search(pattern, raw) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError('sensitive_initialization_document', '页面包含已识别敏感内容，未发布。')
    return raw


class DocumentPages:
    def __init__(self, state_dir, run_id):
        self.run_id = valid_id(run_id)
        self.directory = record_state(state_dir, 'cognition', self.run_id)
        self.path = self.directory / 'document-pages' / (self.run_id + '.json')
        _directory(self.path.parent)
        _regular(self.path)
        self.value = None
        if self.path.exists():
            try:
                if self.path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError
                record = json.loads(self.path.read_bytes())
                payload = record['payload']
                if (record['sha256'] != digest(payload) or payload['version'] != 1
                        or payload['run_id'] != self.run_id):
                    raise ValueError
                self.value = payload
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise FileReadError('document_ledger_error', '页面账本无法校验，未重发模型请求。') from exc

    def save(self):
        from .initialization import atomic
        raw = json.dumps({'sha256': digest(self.value), 'payload': self.value}, ensure_ascii=False).encode()
        if len(raw) > 2 * 1024 * 1024:
            raise FileReadError('document_ledger_budget', '页面账本超过保存容量。')
        atomic(self.path, raw)

    def summary(self):
        value = self.value
        from .document_rounds import recovery
        continuation = recovery(value, CognitionStore(self.directory).show(self.run_id))
        pages = value.get('pages', [])
        counts = {state: sum(p['status'] == state for p in pages)
                  for state in ('pending', 'writing', 'reviewing', 'ready', 'completed', 'skipped', 'failed', 'unknown')}
        return {'version': 1, 'run_id': self.run_id, 'status': value['status'],
                'recovery': continuation,
                'total': len(pages), **counts,
                'pages': [{'slug': p['spec']['slug'], 'title': p['spec']['title'], 'status': p['status'],
                           **({'error': p['error']} if p.get('error') else {}),
                           **({'candidate': p['candidate']} if p.get('candidate') else {}),
                           **({'last_verified_commit': p['last_verified_commit'],
                               'evidence_checked_commit': p.get('evidence_checked_commit')} if 'last_verified_commit' in p else {})} for p in pages],
                'retained': sum(p['status'] == 'retained' for p in pages),
                'retired': sum(p['status'] == 'retired' for p in pages),
                **({'knowledge': value['knowledge_summary']} if value.get('knowledge_summary') else {}),
                **({'manifest_candidate': value['manifest_candidate']} if value.get('manifest_candidate') else {}),
                **({'index_candidate': value['index_candidate']} if value.get('index_candidate') else {}),
                **({'error': value['error']} if value.get('error') else {})}


def generate_documents(state_dir, project, dossier, *, model=None):
    """Called only by explicit/background execution, never by status or republish."""
    from .cognition import CognitionEngine, execution_lock
    store = CognitionStore(state_dir)
    record = store.show(dossier['run_id'], include_envelope=True)
    envelope = record['envelope']
    contract = envelope.get('document_generation')
    facts = facts_for(dossier)
    if not contract or (not facts and not envelope.get('incremental_context')) or dossier['status'] not in ('completed', 'partial'):
        return None  # Historical runs retain their original, entirely local publisher.
    if contract != document_contract():
        raise FileReadError('cognition_contract_changed', '页面生成契约已变化，旧任务不自动升级。')
    ledger = DocumentPages(state_dir, dossier['run_id'])
    with execution_lock(ledger.directory, 'documents-' + ledger.run_id):
        context_hash = digest(facts)
        if envelope.get('incremental_context'):
            from .incremental_documents import prepare_incremental
            if not prepare_incremental(ledger, state_dir, project, dossier, record, model=model):
                return ledger
            facts = ledger.value['incremental']['facts']
            context_hash = digest(facts)
        if ledger.value is None:
            ledger.value = {'version': 1, 'run_id': ledger.run_id, 'envelope_sha256': digest(envelope),
                            'facts_sha256': context_hash, 'status': 'planning', 'pages': []}
            ledger.save()
        elif ledger.value['facts_sha256'] != context_hash or ledger.value['envelope_sha256'] != digest(envelope):
            raise FileReadError('document_context_changed', '页面事实或授权已变化，未覆盖旧页或重发请求。')
        engine = CognitionEngine(state_dir, model=model)
        if 'plan' not in ledger.value:
            # The plan includes all facts; if it cannot fit, stop honestly rather
            # than silently call a partial catalog a complete document plan.
            remaining = envelope['policy']['max_model_calls'] - record['call_count']
            max_pages = min(contract['max_pages'], max(1, (remaining - 1) // 2))
            payload = ledger.value.setdefault('plan_payload', {'commit': dossier['commit'], 'max_pages': max_pages,
                       'findings': [{k: v for k, v in fact.items() if k != 'evidence'} for fact in facts.values()],
                       'coverage_sections': [{'id': s['id'], 'title': s['title'], 'facts': len(s['facts'])}
                                             for s in dossier['sections']]})
            ledger.save()
            try:
                plan = validate_plan(engine.call(ledger.run_id, 'document-plan', 0, 'document_plan', payload, envelope), facts,
                                     {**contract, 'max_pages': payload['max_pages']})
                ledger.value.update(plan=plan, pages=[{'spec': p, 'status': 'pending'} for p in plan['pages']], status='writing')
                ledger.value.pop('error', None)
                ledger.save()
            except FileReadError as exc:
                ledger.value.update(status='unknown' if exc.code == 'cognition_execution_unknown' else 'failed', error={'code': exc.code, 'message': str(exc)[:500]})
                ledger.save()
                return ledger
        for page in ledger.value['pages']:
            if page['status'] in ('completed', 'skipped', 'retained', 'retired', 'blocked'):
                continue
            spec = page['spec']
            try:
                check_current_facts(ledger)
                from .cognition_context import compact_proof
                selected = [{**facts[identity], 'evidence': [compact_proof(p) for p in facts[identity]['evidence']]}
                            for identity in spec['finding_ids']]
                payload = {'commit': dossier['commit'], 'page': spec, 'findings': selected,
                           'related_documents': [{'slug': p['slug'], 'title': p['title'], 'path': p['slug'] + '.md'}
                                                 for p in ledger.value['plan']['pages'] if p['slug'] in spec['related_pages']]}
                if ledger.value.get('incremental'):
                    claims = ledger.value['incremental']['snapshot']['claims']
                    payload['verification_history'] = {key: {'status': claims[key]['status'],
                                                             'last_verified_commit': claims[key]['last_verified_commit'],
                                                             'evidence_checked_commit': claims[key]['evidence_checked_commit']}
                                                       for key in spec['finding_ids']}
                    payload['inheritance_notice'] = 'inherited 事实仅检查原文仍可定位，保留其实际核验版本；不能写成当前版本已完整重查。'
                from .document_evidence import LAYOUT, shared_proofs
                if envelope.get('document_continuation', {}).get('evidence_layout') == LAYOUT:
                    payload = shared_proofs(payload)
                from .document_revision import write_and_review
                write_and_review(engine, ledger, page, payload, contract, envelope)
                page.update(status='ready')
                page.pop('error', None)
                ledger.save()
            except FileReadError as exc:
                page.update(status='unknown' if exc.code == 'cognition_execution_unknown' else 'failed', error={'code': exc.code, 'message': str(exc)[:500]})
                ledger.save()
        ledger.value['status'] = 'ready' if all(p['status'] in ('ready', 'completed', 'skipped', 'retained', 'retired') for p in ledger.value['pages']) else 'partial'
        ledger.save()
    return ledger


def check_current_facts(ledger):
    current = CognitionStore(ledger.directory).dossier(ledger.run_id)
    expected = ledger.value.get('input_facts_sha256', ledger.value['facts_sha256'])
    if digest(facts_for(current)) != expected:
        raise FileReadError('document_context_changed', '页面依据已有失效或变更，停止发布旧正文。')
    if ledger.value.get('incremental'):
        from .knowledge_snapshots import validate_local
        validate_local(ledger.directory, ledger.value['incremental']['baseline'])


def render_page(spec, content, facts, dossier, plan):
    # Metadata/provenance are program owned; no model-supplied path reaches I/O.
    frontmatter = '\n'.join([f'title: {json.dumps(spec["title"], ensure_ascii=False)}',
                             f'description: {json.dumps(spec["description"], ensure_ascii=False)}',
                             f'observed_commit: "{dossier["commit"]}"'])
    body = f'---\n{frontmatter}\n---\n\n# {spec["title"]}\n\n'
    if dossier.get('document_only'):
        from .document_evidence import SOURCE_NOTICE
        body += '> ' + SOURCE_NOTICE + '\n\n'
    body += '\n\n'.join(block['markdown'] for block in content['blocks'])
    if content['limits']:
        body += '\n\n## 资料限制\n\n' + '\n\n'.join(content['limits'])
    related = [p for p in plan['pages'] if p['slug'] in spec['related_pages']]
    body += '\n\n## 继续阅读\n\n[文档总览](index.md)'
    body += ''.join(f' · [{p["title"]}]({p["slug"]}.md)' for p in related)
    body += '\n\n## 来源与核对范围\n\n'
    inherited = sorted({facts[key]['verification']['last_verified_commit'] for key in spec['finding_ids']
                        if facts[key].get('verification', {}).get('status') == 'evidence_checked_only'})
    if inherited:
        body += ('部分事实沿用实际核验版本 ' + '、'.join('`' + version + '`' for version in inherited) +
                 '；本轮仅检查这些事实的原文，未重新调查其全部依赖。\n\n')
    if any(facts[identity].get('partial_origin') for identity in spec['finding_ids']):
        body += '部分说明来自原结论中独立核对通过的部分；原结论未整体通过，其余部分待补充，未经支持的属性仍保留为缺口。\n\n'
    paths = set()
    for identity in spec['finding_ids']:
        for proof in facts[identity]['evidence']:
            ev = proof['evidence']
            entry = f'{ev["path"]}:{ev["start_line"]}–{ev["end_line"]}'
            if entry not in paths:
                body += f'- `{entry}`\n'
                paths.add(entry)
    body += (f'\n观察版本：`{dossier["commit"]}`。基于已核对静态资料生成并复核正文；尚非人工确认。'
             '未运行项目、测试或部署；合并和线上状态未知。\n')
    return body


def publish_pages(project, dossier, value, ledger):
    """Persist each reviewed page before marking it complete, independently."""
    from .documents import _lock, _parents, _create
    from .initialization import atomic, gap_summary
    root = Path(project['root_path'])
    prefix = '.zest/docs/initialization/'
    facts = ledger.value.get('incremental', {}).get('facts', facts_for(dossier))
    if ledger.value['facts_sha256'] != digest(facts):
        raise FileReadError('document_context_changed', '页面依据已变化，不能重新发布旧正文。')
    check_current_facts(ledger)
    hashes = dict(value.get('document_hashes', {}))
    conflicts = [item['destination'] for item in value.get('migration', {}).get('conflicts', [])]
    created = []

    def write(path, body, *, previous=None):
        raw = safe_text(body)
        _parents(root, path)
        target = root / path
        expected = hashlib.sha256(raw).hexdigest()
        if target.exists():
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected and actual not in (hashes.get(path), previous):
                return None
            if actual != expected:
                atomic(target, raw)
        else:
            _create(root, path, raw)
        # A successful write alone is insufficient if the persisted artifact differs.
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise FileReadError('document_write_failed', '页面落盘校验未通过。')
        hashes[path] = expected
        created.append(path)
        return expected

    with _lock(root):
        for page in ledger.value['pages']:
            if page['status'] in ('retained', 'retired', 'blocked'):
                path = prefix + page['spec']['slug'] + '.md'
                _parents(root, path)
                target = root / path
                if page['status'] == 'retained':
                    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != page.get('published_sha256'):
                        page.update(status='blocked', error={'code': 'document_artifact_changed'})
                        conflicts.append(path)
                    else:
                        created.append(path)
                        hashes[path] = page['published_sha256']
                continue
            if 'review' not in page:
                continue
            check_current_facts(ledger)
            path = prefix + page['spec']['slug'] + '.md'
            try:
                validate_review(page['review'], page['content'])
                body = render_page(page['spec'], page['content'], facts, dossier, ledger.value['plan'])
                result = write(path, body, previous=page.get('published_sha256'))
                if result is None:
                    conflicts.append(path)
                    candidate = prefix + 'candidates/' + ledger.run_id + '/' + page['spec']['slug'] + '.md'
                    # A candidate is a complete Markdown artifact, linked from the
                    # program index. Never replace an edited candidate either.
                    parts = re.split(r'(```[\s\S]*?```)', body)
                    candidate_body = ''.join(part if i % 2 else re.sub(r'\]\(([^\s)]+\.md)\)', r'](../../\1)', part)
                                             for i, part in enumerate(parts))
                    if write(candidate, candidate_body) is None:
                        conflicts.append(candidate)
                    page.update(status='skipped', candidate=candidate)
                else:
                    page.update(status='completed', published_sha256=result,
                                last_verified_commit=dossier['commit'], evidence_checked_commit=dossier['commit'])
                    page.pop('candidate', None)
                page.pop('error', None)
                ledger.save()
            except (FileReadError, OSError) as exc:
                page.update(status='failed', error={'code': getattr(exc, 'code', 'document_write_failed')})
                ledger.save()
        ledger.value['status'] = 'completed' if ledger.value['pages'] and all(p['status'] in ('completed', 'retained', 'retired') for p in ledger.value['pages']) else 'partial'
        if ledger.value.get('incremental', {}).get('coverage_pending'):
            ledger.value['status'] = 'partial'
        if not ledger.value['pages'] and (ledger.value.get('error') or {}).get('code') == 'cognition_execution_unknown':
            ledger.value['status'] = 'unknown'
        ledger.save()
        entries = []
        for page in ledger.value['pages']:
            spec = page['spec']
            if page['status'] in ('completed', 'retained'):
                entries.append(f'- [{spec["title"]}]({spec["slug"]}.md)：{spec["description"]}')
                if page['status'] == 'retained':
                    entries.append(f'  沿用核验版本 `{page.get("last_verified_commit")}`；证据检查至 `{dossier["commit"]}`，未重新语义核验。')
            elif page['status'] == 'retired':
                entries.append(f'- {spec["title"]}：内容已明确撤回，历史正文保留。')
            elif page.get('candidate'):
                relative = page['candidate'].removeprefix(prefix)
                entries.append(f'- {spec["title"]}：保留团队原文，可查看[生成候选]({relative})。')
            elif (root / (prefix + spec['slug'] + '.md')).is_file():
                _parents(root, prefix + spec['slug'] + '.md')
                entries.append(f'- [{spec["title"]}]({spec["slug"]}.md)：保留已有版本；本轮页面尚未更新。')
        body = '# 项目工程文档\n\n按项目主题组织的阅读入口。\n\n' + '\n'.join(entries)
        if ledger.value['status'] != 'completed':
            body += '\n\n部分页面尚未完成，已发布页面可继续阅读；详情见项目状态中的页面记录。'
        gaps = list(dict.fromkeys([*ledger.value.get('plan', {}).get('coverage_gaps', []),
                                  *(g['question'] if isinstance(g, dict) else g for g in dossier['plan_gaps'])]))
        if gaps:
            body += '\n\n## 资料限制\n\n' + '\n'.join('- ' + g for g in gaps)
        body += f'\n\n观察版本：`{dossier["commit"]}`。未运行项目、测试或部署；合并和线上状态未知。\n'
        index_path = prefix + 'index.md'
        if not ledger.value['pages'] and (root / index_path).exists():
            # A failed new plan cannot erase the previous working navigation.
            _parents(root, index_path)
            created.append(index_path)
        elif write(index_path, body, previous=ledger.value.get('index_sha256')) is None:
            conflicts.append(index_path)
            candidate = prefix + 'candidates/' + dossier['run_id'] + '/index.md'
            candidate_body = re.sub(r'\]\(([^\s)]+\.md)\)', r'](../../\1)', body)
            candidate_sha = write(candidate, candidate_body, previous=ledger.value.get('index_candidate_sha256'))
            if candidate_sha is not None:
                ledger.value.update(index_candidate=candidate, index_candidate_sha256=candidate_sha)
                created.append(candidate)
                ledger.save()
            else:
                conflicts.append(candidate)
        else:
            ledger.value['index_sha256'] = hashes[index_path]
            ledger.save()
        record = CognitionStore(ledger.directory).show(ledger.run_id, include_envelope=True)
        if record['envelope'].get('incremental_contract'):
            from .knowledge_snapshots import publish_snapshot
            conflict = publish_snapshot(ledger.directory, project, dossier, ledger, hashes)
            if conflict:
                conflicts.append(conflict)
    covered = [s['id'] for s in dossier['sections'] if s['facts']]
    readiness = 'ready' if {'purpose', 'architecture', 'flows'} <= set(covered) else 'limited' if facts else 'unavailable'
    return {'documents': created, 'document_hashes': hashes, 'conflicts': list(dict.fromkeys(conflicts)),
            'document_generation': ledger.summary(),
            'summary': {'documents': len(created), 'reviewed_findings': len(facts_for(dossier)),
                        'inherited_findings': sum(c['status'] == 'inherited' for c in ledger.value.get('incremental', {}).get('snapshot', {}).get('claims', {}).values()),
                        'pending_items': dossier['pending_count'], 'pending_categories': dossier['pending_summary'],
                        'covered_sections': covered, 'readiness': readiness,
                        'knowledge_status': 'has_gaps' if dossier['pending_count'] or gaps else 'initial_scope_covered',
                        'unresolved_gaps': len(gaps), **gap_summary(dossier)}}
