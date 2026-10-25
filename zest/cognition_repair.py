"""A fresh, explicitly approved supplement over an ended run's fixed sources."""
from .cognition_store import CognitionStore, digest
from .files import FileReadError
from .git import inspect_repository, selected_commit
from .resources import record_state
from .storage import ProjectRegistry


REPAIR_GOAL = ("本轮补全已有项目理解：优先处理未核对候选、失败任务和包内未读/需续读资料，"
               "按独立事项制定至多四个具体任务，不重新泛泛介绍项目。旧发现及旧引用只作线索；"
               "新结论必须实际读取本轮批准的非空原文并核对。仓库外运行、部署和团队背景单独保留未知。"
               "保留尚未解决的旧问题，不用少量新结论宣称整个项目已理解。")


def repair_context(store, run_id, *, version=1):
    record = store.show(run_id, include_envelope=True, include_calls=True)
    if record['status'] not in ('failed', 'partial', 'completed') or record['worker_active']:
        raise FileReadError('cognition_repair_not_ready', '只能补全已经结束的调查。')
    bound = CognitionStore(record_state(store.state_dir, 'cognition', run_id))
    with bound.open() as conn:
        if bound._blocking_calls(conn, run_id, record['policy']):
            raise FileReadError('cognition_execution_unknown', '原调查仍有结果不明的调用，请先核对原记录，不能通过补全绕过。')
    from .cognition_followup import _lead
    from .project_sources import source_root
    root = source_root(store.state_dir, record) if record['envelope'].get('source_scope') else None
    leads = []
    for item in record['findings']:
        lead = _lead(record, item, root=root)
        lead['review'] = {'reason': (item.get('review') or {}).get('reason', '')}
        leads.append(lead)
    dossier = store.dossier(run_id, legacy=True)
    questions = {item['pending_id']: {'category': item['category'], 'question': item['question']}
                 for section in dossier['sections'] for item in section['pending'] if 'question' in item}
    source = {key: record[key] for key in ('id', 'project_id', 'commit', 'status', 'approval_sha256', 'call_count', 'error', 'plan', 'jobs', 'findings')}
    source['calls_sha256'] = digest(record['calls'])
    result = {'version': 1, 'source_run_id': run_id, 'project_id': record['project_id'], 'commit': record['commit'],
            'source_sha256': digest(source), 'goal': REPAIR_GOAL, 'leads': leads,
            'questions': list(questions.values()), 'plan_gaps': dossier['plan_gaps'],
            'failed_tasks': [{'id': j['id'], 'goal': j['spec']['goal']} for j in record['jobs'] if j['status'] == 'failed'],
            'semantics': 'historical_leads_only_not_current_evidence_or_task_replay'}
    if version == 1:
        return result
    current = store.dossier(run_id)
    selected = [i for i in current['backlog']['issues'] if i['state'] == 'actionable'][:4]
    scoped_leads = []
    for issue in selected:
        for ref in issue['references']:
            if not ref.get('finding_id'):
                continue
            origin = store.show(ref['run_id'], include_envelope=True)
            item = next(f for f in origin['findings'] if f['id'] == ref['finding_id'])
            origin_root = source_root(store.state_dir, origin) if origin['envelope'].get('source_scope') else None
            lead = _lead(origin, item, root=origin_root)
            scoped_leads.append({**lead, 'finding_id': ref['run_id'] + ':' + ref['finding_id'],
                                 'issue_id': issue['issue_id'], 'origin_run_id': ref['run_id']})
            break  # Exact duplicate claims share one investigation, not N repeats.
    result.update(version=2, goal='只处理 selected_issues 中的明确事项，已核对事实不重复调查；不为六部分齐全新建泛化问题。',
                  backlog_sha256=digest(current['backlog']), selected_issues=selected,
                  leads=scoped_leads, questions=[{'issue_id': i['issue_id'], 'category': i['category'], 'question': i['question']}
                                                for i in selected if not i.get('evidence_locations')],
                  plan_gaps=[], failed_tasks=[])
    return result


def check_repair_context(store, envelope, context=None):
    context = envelope.get('repair_context') if context is None else context
    if context is None: return
    if (context != repair_context(store, context['source_run_id'], version=context.get('version', 1)) or context['project_id'] != envelope['project_id']
            or context['commit'] != envelope['commit']):
        raise FileReadError('cognition_repair_source_changed', '补全依据的原调查已变化，请重新准备资料。')


def repair_view(context, files, sections=None, *, planning=False, store=None):
    """Keep complete claims/questions, compact redundant historical identities."""
    sources = {item['evidence']['path']: item['id'] for item in files}
    if context.get('version') == 2:
        chosen = [i for i in context['selected_issues'] if sections is None or set(sections) & set(i['sections'])]
        ids = {i['issue_id'] for i in chosen}
        return {**context, 'selected_issues': chosen, 'leads': [l for l in context['leads'] if l['issue_id'] in ids],
                'questions': [q for q in context['questions'] if q['issue_id'] in ids],
                'instruction': '任务 goal 已指定唯一 issue_id；只处理该项。原线索不是本轮证据，必须读取本轮原文。不得重新介绍项目或为未涉及模块新列缺口。'}
    leads = []
    for item in context['leads']:
        if sections is not None and item['section'] not in sections:
            continue
        fields = ('finding_id', 'kind', 'section', 'status') if planning else ('finding_id', 'kind', 'section', 'statement', 'status', 'review')
        lead = {key: item[key] for key in fields}
        if not planning:
            lead['read_locations'] = [{'source_id': sources.get(e['path'], ''), 'path': e['path'],
                                      'start_line': e['start_line'], 'end_line': e['end_line']} for e in item['evidence_locations']]
        leads.append(lead)
    questions = context['questions']
    if planning:
        questions = [q for q in questions if q['category'] == 'source_gap']
    elif sections is not None and store is not None:
        # The source hash is checked again immediately before every request.
        # Recover section membership from the frozen source, not text heuristics.
        relevant = {item['question'] for section in store.dossier(context['source_run_id'], legacy=True)['sections']
                    if section['id'] in sections for item in section['pending'] if 'question' in item}
        questions = [q for q in questions if q['question'] in relevant]
    return {key: context[key] for key in ('version', 'source_run_id', 'commit', 'source_sha256', 'goal', 'plan_gaps', 'failed_tasks', 'semantics')} | {
        'questions': questions, 'total_question_count': len(context['questions']),
        'question_delivery': '规划优先列出包内阅读需求；各部分的其余完整问题将在对应调查任务中提供。' if planning else '本任务涉及的完整问题。',
        'focus_source_ids': sorted({sources[e['path']] for item in context['leads'] for e in item['evidence_locations'] if e['path'] in sources}),
        'leads': leads, 'lead_statements_omitted_for_planning': planning,
        'lead_delivery': '规划阶段仅显示待核对位置及问题，完整旧断言将在对应调查任务中提供；原文需重新读取。' if planning else '相关部分的完整旧断言，仅作线索。'}


def prepare_repair(state_dir, run_id, *, target=None, new_run_id=None):
    store = CognitionStore(state_dir)
    context = repair_context(store, run_id, version=2)
    if not context['selected_issues']:
        raise FileReadError('cognition_repair_no_work', '当前没有可自动补查的新事项；已尝试、外部资料和需明确提问的问题保留，不重复调查。')
    project = ProjectRegistry(state_dir).get_project_readonly(context['project_id'])
    if project is None or selected_commit(project, inspect_repository(project['root_path'])) != context['commit']:
        raise FileReadError('cognition_repair_version_changed', '项目版本已变化，请先检查更新，再补全对应版本的理解。')
    source = store.show(run_id, include_envelope=True)
    # Reuse the exact saved inventory, not a new crawl or a replay of old calls.
    return store.create(project['id'], survey_id=source['envelope']['survey_id'], target=target,
                        repair_context=context, run_id=new_run_id)


def targeted_repair_plan(context, files):
    """Deterministic scoped tasks, without another generic planning model call."""
    from .cognition_contracts import validate_plan
    sources = {f['evidence']['path']: f['id'] for f in files}
    tasks, sections = [], set()
    for index, issue in enumerate(context['selected_issues'], 1):
        section = issue['sections'][0]
        paths = [e['path'] for e in issue.get('evidence_locations', [])]
        if issue.get('material_request'):
            paths.append(issue['material_request']['path'])
        ids = list(dict.fromkeys(sources[p] for p in paths if p in sources))
        sections.add(section)
        tasks.append({'id': f'T{index}', 'title': '处理明确待办：' + issue['category'],
            'goal': '仅处理 repair_context.selected_issues 中 issue_id=' + issue['issue_id'] + ' 的原问题；不重复调查其他已知内容。',
            'reason': '从当前跨轮待办队列选择，复用来源定位并重新读取原文。', 'source_ids': ids,
            'sections': [section], 'deliverable': '该项可独立核对的结论；证据不足时仅列该项剩余缺口。',
            'acceptance': ['实际读取关联原文。若原断言正确，原样作为独立发现提交；不能仅因任务结束宣称问题解决。',
                           '若原断言不正确，提交有证据的更正，不把原错误断言当成事实。'],
            'depends_on': [], 'priority': 1})
    return validate_plan({'overview': [], 'tasks': tasks, 'gaps': []}, set(sources.values()), required_sections=sections)
