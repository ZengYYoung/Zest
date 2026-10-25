"""Bounded onboarding workflow and evidence-backed, independently usable parts.

The contract is frozen in new project-understanding packages. Historical runs keep
their original graph and whole-finding semantics.
"""
from .cognition_contracts import canonical


WORKFLOW_CONTRACT = {
    'version': 1,
    'stages': ['survey', 'structure', 'research', 'review', 'publish'],
    'max_review_repairs': 1,
    'max_source_repair_rounds': 1,
    'max_source_repair_tasks': 2,
    'planning': '先依据目录和入口建立模块职责与依赖骨架，再按模块调查代表性核心流程；收集启动方式与约束。不以穷尽全部细节作为初始化结束条件。',
    'research': '每项发现聚焦独立事实，先交付产品用途、模块协作与核心操作。对源码可回答的缺口定向补读；部署和团队背景等包外未知单列。',
    'review': '格式或引用问题只重新核对现有原文，不重新调查业务。checks 的 subject、attribute、condition、assertion 须完整表达本项原断言和限定条件，supported 子项可单独发布；不得把推测改为事实。',
    'gap_routing': 'insufficient_evidence 的 reason 以 [补读源码]、[外部资料] 或 [分析推导] 开头，随后说明具体缺口。部署、实际运行与团队背景缺口用 [外部资料]；只依据框架常识的解释用 [分析推导]，不能当作原文事实。外部资料和分析推导不自动重新读源码。',
    'finish': '最多一轮定向补读，随后发布可用内容与具体缺口。完成初始化不等于穷尽项目，不因外部未知继续循环。',
}


def issue_kind(item):
    review = item.get('review') or {}
    if review.get('validation_error'):
        return 'review_format'
    if review.get('conflicts') or review.get('conflict_links'):
        return 'contradiction'
    if item['finding']['kind'] == 'inferred':
        return 'analysis'
    checks = review.get('checks', [])
    if any(c['verdict'] == 'contradicted' for c in checks):
        return 'contradiction'
    return 'source_evidence'


def remaining_checks(item):
    """Keep the full original in the ledger; target only the unresolved checks."""
    review = item.get('review') or {}
    checks = [c for c in review.get('checks', []) if c['verdict'] != 'supported']
    return checks


def source_checks(item):
    return [c for c in remaining_checks(item)
            if not c['reason'].startswith(('[外部资料]', '[分析推导]'))]


def check_statement(check):
    # Include every scope-bearing field, never promote an assertion by dropping
    # its subject, attribute, condition or negation.
    return f"{check['subject']} · {check['attribute']}：{check['assertion']}（条件：{check['condition']}）"


def supported_parts(finding, judgment, payload):
    """Project only structurally validated model checks, never repair citations."""
    if (finding['kind'] != 'observed' or judgment['supported']
            or judgment.get('conflicts') or judgment.get('conflict_links')):
        return []
    from .cognition_context import compact_proof
    output = []
    for index, check in enumerate(judgment['checks'], 1):
        if check['verdict'] != 'supported':
            continue
        if len(check_statement(check)) > 800:
            continue
        originals = [p for p in finding['evidence']
                     if any(canonical(compact_proof(p)) == canonical(payload['evidence'][ref])
                            for ref in check['evidence_ids'])]
        # A mismatch is not a cue to invent an ID or to borrow a peer's proof.
        if not originals or any(not any(canonical(compact_proof(p)) == canonical(payload['evidence'][ref])
                                       for p in originals) for ref in check['evidence_ids']):
            continue
        output.append((index, {**finding, 'statement': check_statement(check),
                               'evidence': originals, 'citations': list(check['evidence_ids'])}))
    return output


def workflow_progress(record):
    status = record['status']
    stage = {'awaiting_approval': 'survey', 'queued': 'survey', 'planning': 'structure',
             'investigating': 'research', 'reviewing': 'review'}.get(status, 'publish')
    return {'version': 1, 'stage': stage, 'stages': WORKFLOW_CONTRACT['stages'],
            'source_repair_rounds': int(any(j['spec'].get('followup_finding') for j in record['jobs'])),
            'max_source_repair_rounds': WORKFLOW_CONTRACT['max_source_repair_rounds']}
