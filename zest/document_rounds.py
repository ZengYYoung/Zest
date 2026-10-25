"""Explicit new document rounds; never refill or replay an old research run."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from .cognition_store import CognitionStore, MAX_ENVELOPE_BYTES, digest, now, valid_id
from .files import FileReadError
from .resources import record_state


def recovery(value, record):
    """Distinguish resumable persistence from exhausted/known-invalid generation."""
    pages = value.get('pages', [])
    code = (value.get('error') or {}).get('code')
    errors = {p.get('error', {}).get('code') for p in pages}
    result = {'source_run_id': record['id'], 'source_sha256': digest(value),
              'model_calls': record['call_count'], 'max_model_calls': record['policy']['max_model_calls'],
              'max_seconds': record['policy']['max_seconds']}
    if value['status'] == 'completed':
        return {**result, 'action': 'none', 'message': '本轮文档已保存。'}
    if value['status'] == 'unknown' or any(p['status'] == 'unknown' for p in pages):
        return {**result, 'action': 'inspect', 'message': '有文档请求结果未知，须核对原调用；不能新建轮次重发。'}
    if {code, *errors} & {'cognition_prompt_budget', 'document_context_changed', 'document_source_changed',
                          'cognition_contract_changed', 'cognition_request_changed', 'model_target_changed'}:
        return {**result, 'action': 'inspect', 'message': '文档资料容量、来源、版本或模型范围需要先核对；重复续写或增加预算不能解决此问题。'}
    if pages and all(p['status'] in ('completed', 'retained', 'retired', 'blocked', 'skipped') for p in pages):
        return {**result, 'action': 'inspect', 'message': '剩余事项是待复核事实、资料缺口或人工文档冲突，重复续写不会解决，请展开页面记录核对。'}
    needs_model = 'plan' not in value or any(p['status'] not in ('completed', 'retained', 'retired', 'blocked', 'skipped')
                                           and ('content' not in p or 'review' not in p) for p in pages)
    exhausted = record['call_count'] >= record['policy']['max_model_calls']
    elapsed = bool(record.get('started_at') and (datetime.now(timezone.utc) - datetime.fromisoformat(record['started_at'])).total_seconds() >= record['policy']['max_seconds'])
    known_invalid = {code, *errors} & {'invalid_document_output', 'document_review_rejected', 'cognition_response_invalid'}
    from .document_revision import enabled, supported_projection
    if enabled(record) and not exhausted and not elapsed:
        from .document_contracts import document_contract
        remaining = record['policy']['max_model_calls'] - record['call_count']
        revision = any(p.get('error', {}).get('code') == 'document_review_rejected' and 'content' in p and 'revision_payload' not in p for p in pages)
        projection = any('revision_payload' in p and supported_projection(p, document_contract()) is not None for p in pages)
        last_correction = any(p.get('projection_content') and p.get('projection_review_response') and 'last_revision_payload' not in p for p in pages)
        coverage_correction = any(p.get('error',{}).get('code')=='invalid_document_output' and 'last_revision_payload' in p and 'last_content' not in p
                                  and 'coverage_revision_payload' not in p and not p.get('coverage_revision_blocked') for p in pages)
        provenance = any(p.get('last_review_response',{}).get('issues') and p.get('last_review_response',{}).get('checks')
                         and all(c['verdict']=='supported' for c in p['last_review_response']['checks']) and 'provenance_review_response' not in p for p in pages)
        if remaining >= 2 and (revision or last_correction or coverage_correction) or remaining >= 1 and (projection or provenance):
            return {**result, 'action': 'resume', 'message': '已保存正文复核意见，可在本轮剩余预算内修订或去除不受支持的冗余段落，再独立复核全文；不会重置预算。'}
    if needs_model and (exhausted or elapsed or code in ('cognition_call_budget', 'cognition_time_budget') or known_invalid):
        message = (f"本轮模型调用预算已用完（{record['call_count']} / {record['policy']['max_model_calls']} 次），继续原任务不能增加预算。" if exhausted or code == 'cognition_call_budget' else
                   '本轮运行时间预算已结束，继续原任务不能重置计时。' if elapsed or code == 'cognition_time_budget' else
                   '原文档响应未通过结构或语义核对，重复读取同一响应不会产生新结果。')
        return {**result, 'action': 'new_round', 'message': message + ' 可以基于已有核对事实，明确开始新一轮文档生成。'}
    if not pages and 'plan' in value:
        return {**result, 'action': 'inspect', 'message': '本轮没有可发布页面，请核对计划覆盖范围与事实缺口。'}
    return {**result, 'action': 'resume', 'message': '可以继续尚未发送的写作请求，或复用已保存正文完成发布。'}


def source_dossier(store, envelope):
    context = envelope['document_continuation']
    if context.get('version') != 1 or context.get('mode') != 'reuse_reviewed_facts_document_calls_only':
        raise FileReadError('document_source_changed', '文档续作契约无法核对。')
    from .document_evidence import LAYOUT
    if context.get('evidence_layout') not in (None, LAYOUT):
        raise FileReadError('document_source_changed', '文档原文组织方式无法核对。')
    source = store.show(context['source_run_id'], include_envelope=True)
    if (source['project_id'] != envelope['project_id'] or source['commit'] != envelope['commit']
            or source['approval_sha256'] != context['source_envelope_sha256']
            or source['envelope'].get('document_continuation') or source['status'] not in ('completed', 'partial')):
        raise FileReadError('document_source_changed', '原调查身份、版本或授权已变化，未继续生成。')
    dossier = store.dossier(source['id'], legacy=True)
    if digest(dossier) != context['source_dossier_sha256']:
        raise FileReadError('document_source_changed', '原调查事实或核对状态已变化，未继续生成。')
    for section in dossier['sections']:
        for item in section['facts'] + section['pending']:
            item.setdefault('origin_run_id', source['id'])
    return dossier


def create_round(state_dir, project, source_run_id, run_id, source_sha256):
    """Called only by the explicit, hash-bound new-round command under maintenance lock."""
    from .document_pages import DocumentPages
    from .document_contracts import document_contract
    from .models import ModelSettings, model_target
    from .git import inspect_repository, selected_commit
    valid_id(run_id)
    store = CognitionStore(record_state(state_dir, 'cognition', source_run_id))
    source = store.show(source_run_id, include_envelope=True, include_calls=True)
    ledger = DocumentPages(state_dir, source_run_id)
    if not ledger.value or digest(ledger.value) != source_sha256:
        raise FileReadError('document_source_changed', '页面记录已变化，请刷新项目状态后重新确认。')
    if source['worker_active'] or source['project_id'] != project['id'] or source['status'] not in ('completed', 'partial'):
        raise FileReadError('document_round_not_ready', '原调查仍在执行或不属于当前项目。')
    if recovery(ledger.value, source)['action'] != 'new_round':
        raise FileReadError('document_round_not_needed', '当前页面应继续原任务或核对原因，不应新建文档轮次。')
    if any(c['status'] in ('pending', 'uncertain', 'not_sent') for c in source['calls']):
        raise FileReadError('cognition_execution_unknown', '原调查有未确定的调用，禁止通过新轮次重发。')
    envelope = deepcopy(source['envelope'])
    if (envelope.get('document_generation') != document_contract()
            or model_target(ModelSettings.from_env()) != envelope['target']
            or selected_commit(project, inspect_repository(project['root_path'])) != envelope['commit']):
        raise FileReadError('document_source_changed', '模型、版本或文档契约已变化，请先核对当前项目。')
    root_id = envelope.get('document_continuation', {}).get('source_run_id', source_run_id)
    origin = store.show(root_id, include_envelope=True)
    dossier = store.dossier(root_id, legacy=True)
    from .document_evidence import LAYOUT
    envelope['document_continuation'] = {'version': 1, 'source_run_id': root_id, 'evidence_layout': LAYOUT,
        'source_envelope_sha256': origin['approval_sha256'], 'source_dossier_sha256': digest(dossier),
        'previous_run_id': source_run_id, 'previous_pages_sha256': source_sha256,
        'mode': 'reuse_reviewed_facts_document_calls_only'}
    if len(json.dumps(envelope).encode()) > MAX_ENVELOPE_BYTES:
        raise FileReadError('cognition_scope_budget', '文档来源资料包超过保存容量。')
    source_dossier(store, envelope)
    with store.open(write=True) as conn, conn:
        old = conn.execute('SELECT * FROM cognition_runs WHERE id=?', (run_id,)).fetchone()
        if old:
            if store.envelope(old) != envelope:
                raise FileReadError('document_source_changed', '恢复的新轮次与已保存来源不一致。')
        else:
            timestamp = now()
            conn.execute('INSERT INTO cognition_runs(id,project_id,commit_id,created_at,updated_at,status,envelope,envelope_sha256,started_at,config_dir) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (run_id, project['id'], source['commit'], timestamp, timestamp, 'partial', json.dumps(envelope), digest(envelope), timestamp, str(store.state_dir)))
    child = DocumentPages(store.state_dir, run_id)
    if child.value is None:
        child.value = deepcopy(ledger.value)
        child.value.update(run_id=run_id, envelope_sha256=digest(envelope), status='writing' if 'plan' in child.value else 'planning')
        child.value.pop('error', None)
        if 'plan' not in child.value:
            # A failed source plan may have been capped at one page by its
            # exhausted budget. Freeze a NEW payload for this newly authorized
            # round; never carry that obsolete budget into subsequent rounds.
            for key in ('plan_payload', 'update_payload'):
                child.value.pop(key, None)
        for page in child.value['pages']:
            if page['status'] == 'completed':
                page.setdefault('publication_document_only', bool(source['envelope'].get('document_continuation')))
            if page['status'] == 'failed':
                if page.get('error', {}).get('code') in ('invalid_document_output', 'document_review_rejected', 'cognition_response_invalid'):
                    page.pop('content', None); page.pop('review', None)
                    for key in ('revision_payload', 'revision_review_payload', 'revised_content', 'revised_review', 'revision_review_response', 'review_feedback', 'revision_number', 'projection_content', 'projection_reason', 'projection_review_payload', 'projection_review_response', 'last_revision_payload', 'last_content', 'last_review_payload', 'last_review_response', 'last_write_response', 'coverage_revision_payload', 'coverage_revision_blocked', 'provenance_review_payload', 'provenance_review_response'):
                        page.pop(key, None)
                page['status'] = 'pending'
                page.pop('error', None)
        if child.value.get('incremental'):
            child.value['incremental']['snapshot']['run_id'] = run_id
        child.save()
        store.event(run_id, 'coordinator', 'document_round_explicitly_authorized', envelope['document_continuation'])
    return store.show(run_id)
