"""Same-version, separately approved investigation of one unresolved finding."""
import hashlib
import time

from .cognition_contracts import SECTION_IDS, canonical, keys, strings, text, validate_plan
from .cognition_context import review_unknowns
from .cognition_store import CognitionStore, digest, valid_id
from .files import FileReadError
from .git import inspect_repository, selected_commit
from .storage import ProjectRegistry
from .survey import SurveyStore


def _lead(record, item):
    finding = item['finding']
    if finding['section'] not in SECTION_IDS or finding['kind'] not in ('observed', 'inferred'):
        raise FileReadError('cognition_followup_source_invalid', '原发现的类别或档案部分无效。')
    text(finding['statement'])
    sources = {s['id']: s for s in record['envelope']['files']}
    locations = []
    for proof in finding['evidence']:
        source = sources.get(proof['source_id'])
        ev = proof['evidence']; start, end = ev['start_line'], ev['end_line']
        if source is None or type(start) is not int or type(end) is not int:
            raise FileReadError('cognition_followup_source_invalid', '原发现的来源身份或行段无效，请先检查保存记录。')
        approved = source['evidence']
        lines = source['body'].split('\n')
        if lines[-1] == '': lines.pop()
        if (not 1 <= start <= end <= len(lines) or ev['commit'] != record['commit']
                or any(ev.get(k) != approved.get(k) for k in ('path', 'commit', 'blob_id', 'content_sha256'))
                or hashlib.sha256(source['body'].encode()).hexdigest() != ev['content_sha256']
                or proof['text'] != '\n'.join(line.removesuffix('\r') for line in lines[start-1:end])
                or not proof['text']):
            raise FileReadError('cognition_followup_source_invalid', '原发现保存的原文与固定包不匹配，请先检查来源。')
        locations.append(dict(ev))
    if not locations:
        raise FileReadError('cognition_followup_source_invalid', '原发现缺少可定位原文，请先检查来源。')
    return {'finding_id': item['id'], 'job_id': item['job_id'], 'status': item['status'],
            'kind': finding['kind'], 'section': finding['section'], 'statement': finding['statement'],
            'review': item['review'], 'evidence_locations': locations,
            'identity_sha256': digest({'finding': finding, 'review': item['review'], 'status': item['status'], 'job_id': item['job_id']})}


def source_snapshot(store, run_id, finding_id):
    record = store.show(valid_id(run_id), include_envelope=True)
    if record['status'] not in ('completed', 'partial'):
        raise FileReadError('cognition_followup_not_pending', '定向调查来源必须是已结束的 completed/partial 调查。')
    try:
        selected = next((f for f in record['findings'] if f['id'] == finding_id), None)
        if selected is None or selected['finding']['kind'] != 'observed' or selected['status'] not in ('pending_review', 'needs_investigation'):
            raise FileReadError('cognition_followup_not_pending', '只能选择尚未核对或仍需调查的 observed 发现。')
        lead = _lead(record, selected)
        unknowns = {u['reference_id']: u for u in review_unknowns(record['jobs'])}
        findings = {f['id']: f for f in record['findings']}
        leads, questions, queue = {finding_id: lead}, {}, [lead]
        while queue:
            current = queue.pop(0)
            refs = set()
            review = current['review'] or {}
            for key in ('conflicts', 'conflict_links'):
                for conflict in review.get(key, []): refs.add(conflict['reference_id'])
            for ref in sorted(refs):
                if ref == current['finding_id']: raise ValueError
                if ref in leads: continue
                if ref in findings:
                    leads[ref] = _lead(record, findings[ref]); queue.append(leads[ref])
                elif ref in unknowns:
                    questions[ref] = unknowns[ref]
                else:
                    raise ValueError
        value = {'run_id': run_id, 'project_id': record['project_id'], 'commit': record['commit'],
                 'approval_sha256': record['approval_sha256'], 'status': record['status'], 'error': record['error'],
                 'selected': lead, 'related_findings': [leads[ref] for ref in sorted(leads) if ref != finding_id],
                 'related_unknowns': [questions[ref] for ref in sorted(questions)]}
        text(canonical(value), 256000)
        return value
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise FileReadError('cognition_followup_source_invalid', '原发现或关联冲突记录损坏，未准备定向调查。') from exc


def followup_context(source, files):
    paths = {s['evidence']['path']: s['id'] for s in files}
    needed = sorted({e['path'] for lead in [source['selected'], *source['related_findings']] for e in lead['evidence_locations']})
    return {'schema_version': 1, 'project_id': source['project_id'], 'commit': source['commit'],
            'source': source, 'source_sha256': digest(source), 'sections': [source['selected']['section']],
            'goal': '逐项重新调查 followup_context.source.selected 中的原断言、限定条件、核对意见与关联冲突；只回答选定问题。',
            'acceptance': ['重新读取本次批准包内非空原文，按独立属性核对选定问题。',
                           '明确回应原核对意见与关联冲突；无法回答的部分保留未知，不重复全项目结论。'],
            'focus': [{'path': path, 'source_id': paths.get(path, ''), 'availability': 'in_package' if path in paths else 'outside_package'} for path in needed],
            'semantics': 'unverified_source_leads_only_no_old_citations_or_task_inheritance'}


def check_followup_context(store, envelope, context=None):
    context = envelope.get('followup_context') if context is None else context
    if context is None: return
    try:
        source = source_snapshot(store, context['source']['run_id'], context['source']['selected']['finding_id'])
        expected = followup_context(source, envelope['files'])
        if (source['project_id'] != envelope['project_id'] or source['commit'] != envelope['commit']
                or expected != context):
            raise ValueError
    except (FileReadError, KeyError, TypeError, ValueError) as exc:
        raise FileReadError('cognition_followup_source_changed', '定向调查的原发现、核对意见、关联冲突或来源身份已变化，请重新准备。') from exc


def prepare_followup(state_dir, run_id, finding_id, *, target=None, progress=None):
    started = time.monotonic()
    def emit(phase):
        if progress: progress({'phase': phase, 'elapsed_seconds': round(time.monotonic()-started, 3)})
    emit('source')
    store = CognitionStore(state_dir)
    source = source_snapshot(store, run_id, finding_id)
    from .cognition_resolution import effective_resolutions
    effect = effective_resolutions(state_dir, source['project_id'], source['commit']).get((run_id, finding_id))
    if effect and effect['active']:
        raise FileReadError('resolution_already_closed', '原问题已通过处理决定关闭，无需重复补查。')
    project = ProjectRegistry(state_dir).get_project_readonly(source['project_id'])
    if project is None:
        raise FileReadError('project_not_found', '未找到原调查所属项目。')
    if selected_commit(project, inspect_repository(project['root_path'])) != source['commit']:
        raise FileReadError('cognition_followup_version_changed', '原发现不是当前选定版本，请先核对版本变化。')
    emit('materials')
    survey = SurveyStore(state_dir).build(project, commit=source['commit'], progress=lambda e: emit('survey_'+e['phase']))
    context = followup_context(source, survey['sources'])
    emit('preparing')
    result = store.create(source['project_id'], survey_id=survey['id'], target=target, followup_context=context)
    emit('prepared')
    return result


def focused_plan(value, context, source_ids):
    keys(value, ('overview', 'source_ids', 'gaps'))
    chosen = strings(value['source_ids'], None, allowed=source_ids, minimum=1, field='source_ids')
    questions = strings(value['gaps'], 12, field='gaps')
    section = context['sections'][0]
    plan = {'overview': value['overview'], 'tasks': [
        {'id': 'T1', 'title': '定向调查：'+context['source']['selected']['finding_id'],
         'goal': context['goal'], 'reason': '处理已结束调查中选定的一项未解决发现。',
         'source_ids': chosen, 'sections': context['sections'],
         'deliverable': '围绕选定问题提交独立原文支持的结论、核对与冲突处理说明及缺口。',
         'acceptance': context['acceptance'], 'depends_on': [], 'priority': 1}],
        'gaps': [{'section': section, 'question': q} for q in questions]}
    return validate_plan(plan, source_ids, required_sections=context['sections'])
