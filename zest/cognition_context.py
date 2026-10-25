"""Lossless model views of durable original proofs; no source text truncation."""

from .cognition_contracts import POLICY, canonical


def refresh_model_view(context, source_ids=None):
    """Bound historical hints, preserving the full frozen context for validation.

    Legacy requests keep their original body. No original-text proof is shortened
    or promoted to knowledge; omitted hints have explicit coverage counts.
    """
    if context.get('model_view_version') != 1:
        return context
    focus = context.get('focus', [])
    if source_ids is not None:
        focus = [item for item in focus if item.get('source_id') in source_ids]
    paths = {item['path'] for item in focus}
    slim_changes = lambda items: [{'path': item['path'], 'change': item['change']} for item in items]
    leads = sorted(context.get('historical_leads', []), key=lambda item: (
        not any(proof.get('path') in paths for proof in item.get('evidence', [])),
        item.get('original_status') != 'auto_reviewed', item.get('classification') != 'directly_affected'))
    selected = []
    for item in leads:
        if source_ids is not None and not any(proof.get('path') in paths for proof in item.get('evidence', [])):
            continue
        hint = {key: item[key] for key in ('run_id', 'finding_id', 'finding_sha256', 'original_status', 'section', 'kind', 'statement', 'classification')}
        hint['source_paths'] = list(dict.fromkeys(proof.get('path') for proof in item.get('evidence', []) if proof.get('path')))
        if len(canonical([*selected, hint])) <= (4000 if source_ids is not None else 6000):
            selected.append(hint)
    changes = context.get('changes', [])
    if source_ids is not None:
        changes = [item for item in changes if item['path'] in paths]
    return {key: context[key] for key in ('schema_version', 'model_view_version', 'project_id', 'target_commit', 'baseline', 'semantics', 'requires_discussion')} | {
        'focus': focus, 'changes': slim_changes(changes),
        'uncovered_changes': slim_changes([item for item in context.get('uncovered_changes', []) if item['path'] in paths]),
        'historical_leads': selected, 'warnings': context.get('warnings', []),
        'coverage': {'historical_leads_included': len(selected), 'historical_leads_total': len(leads),
                     'change_files_included': len(changes), 'change_files_total': len(context.get('changes', [])),
                     'full_context_retained_in_approved_envelope': True,
                     'omitted_hints_are_not_resolved_or_verified': True}}


def describe_context_request(authorization, envelope, spec=None):
    """Readable metadata derived from the exact saved request, never model claims."""
    import json
    request = authorization['request']
    purpose = request.get('purpose', '')
    titles = {'project_plan': '规划项目调查', 'project_followup_plan': '规划补充调查',
              'project_research': '阅读与分析项目资料', 'project_report': '整理模块调查结论',
              'project_review': '核对结论与源码证据'}
    sources = {item['id']: item['evidence']['path'] for item in envelope['files']}
    paths = [sources[identity] for identity in (spec or {}).get('source_ids', []) if identity in sources]
    topics = []
    try:
        body = json.loads(request['body_text'])
        for message in body.get('messages', []):
            if message.get('role') != 'user' or not isinstance(message.get('content'), str):
                continue
            payload = json.loads(message['content'])
            for item in payload.get('findings', []):
                if isinstance(item, dict) and isinstance(item.get('statement'), str):
                    topics.append(item['statement'])
            for proof in payload.get('evidence', {}).values():
                path = proof.get('evidence', {}).get('path') if isinstance(proof, dict) else None
                if isinstance(path, str):
                    paths.append(path)
    except (ValueError, TypeError, AttributeError):
        pass
    paths = list(dict.fromkeys(paths))
    return {'title': (spec or {}).get('title') or titles.get(purpose, '继续项目调查'),
            'purpose': titles.get(purpose, '继续项目调查'),
            'goal': (spec or {}).get('goal') or ('对照已读取的源码，检查这些结论是否可靠。' if purpose == 'project_review' else '整理本项目的目标、模块关系和待确认事项。'),
            'reason': '完整请求包含已有资料与调查记录，超过单次默认长度；本次只增加请求长度。',
            'source_paths': paths[:30], 'source_count': len(paths), 'topics': topics[:4],
            'model': request.get('target', {}).get('model', envelope['target'].get('model')),
            'additional_calls': 1, 'new_files_authorized': False}


def material_catalog(envelope):
    """Show as many approved entries as the metadata budget fits, not 60 IDs."""
    budget = envelope.get("policy", {}).get("catalog_chars", POLICY["catalog_chars"])
    result = []
    priority = {item["source_id"] for item in envelope.get("refresh_context", {}).get("focus", []) if item["source_id"]}
    priority.update(item["source_id"] for item in envelope.get("followup_context", {}).get("focus", []) if item["source_id"])
    sources = sorted(envelope["files"], key=lambda source: source["id"] not in priority)
    for source in sources:
        item = {"id": source["id"], "path": source["evidence"]["path"], "roles": source["roles"]}
        if len(canonical([*result, item])) > budget:
            break
        result.append(item)
    return result


def scope_review(envelope):
    count = len(envelope["files"])
    shown = len(material_catalog(envelope))
    complete = envelope.get("survey_coverage", {}).get("inventory_scan_finished")
    if envelope.get('source_scope'):
        return {'requires_discussion': False, 'source_count': count, 'catalog_count': shown,
                'survey_complete': complete, 'reasons': [],
                'message': '按项目固定版本自主检索与补读；目录分页和请求分批自动处理。'}
    reasons = []
    if count >= envelope["policy"]["large_scope_files"]:
        reasons.append("large_material_package")
    if shown < count:
        reasons.append("planning_catalog_partial")
    if complete is False:
        reasons.append("survey_incomplete")
    refresh = envelope.get("refresh_context", {})
    if refresh.get("requires_discussion"):
        reasons.append("large_change_set")
    if any(item["availability"] == "outside_package" for item in refresh.get("focus", [])):
        reasons.append("changed_material_outside_package")
    if any(item["availability"] == "outside_package" for item in envelope.get("followup_context", {}).get("focus", [])):
        reasons.append("followup_material_outside_package")
    return {"requires_discussion": bool(reasons), "source_count": count, "catalog_count": shown,
            "survey_complete": complete, "reasons": reasons,
            "message": "资料较多或本轮摸底/目录不完整，请先确认按模块调查，或只批准当前已展示的固定资料包。" if reasons else "当前资料范围可直接审阅。"}


def approved_materials(envelope):
    """Complete, minimal authorization inventory, distinct from read evidence."""
    return [{"id": source["id"], "path": source["evidence"]["path"]} for source in envelope["files"]]


def read_source_ids(results):
    """A successful window/empty observation is a read, never a full-file claim."""
    return sorted({item["result"]["data"]["source_id"] for item in results
                   if item["tool"] == "read" and item["result"].get("ok")})


def material_request_text(request):
    """Format only stored data; legacy free text is never reclassified."""
    if isinstance(request, str):
        return "历史资料需求（未分类）：" + request
    if request["location"] == "outside_package":
        prefix = "包外资料需另行批准："
    elif request["location"] == "approved_read":
        prefix = "包内待补读（已有读取窗口）："
    else:
        prefix = "包内待补读（尚未读取）："
    return prefix + request["path"] + " · " + request["reason"]


def compact_proof(proof):
    """Text and its physical range replace the redundant per-line text copy."""
    return {key: value for key, value in proof.items() if key != "lines"}


def compact_results(results):
    output, seen = [], set()
    for result in results:
        if result["tool"] == "read" and result["result"].get("ok"):
            data = compact_proof(result["result"]["data"])
            ref = result.get("citation_id") or ("observation", canonical(data))
            if ref in seen:
                continue
            seen.add(ref)
            result = {**result, "result": {**result["result"], "data": data}}
        output.append(result)
    # Locations are hints, not evidence. Keep every original-text proof visible.
    while len(canonical(output)) > POLICY["evidence_warning_chars"]:
        index = next((i for i, result in enumerate(output) if result["tool"] != "read"), None)
        if index is None:
            break
        output.pop(index)
    return output


def investigation_view(results):
    """Separate saved metadata from text/tool evidence without dropping either."""
    tools, metadata = [], []
    for item in compact_results(results):
        if item["tool"] == "read" and item["result"].get("ok") and not item["result"]["data"].get("text"):
            metadata.append(item["result"]["data"])
        else:
            tools.append(item)
    return {"tool_results": tools, "metadata_observations": metadata}


def review_payload(commit, batch, other, unknowns=()):
    proofs, identities, findings = {}, {}, []
    for item in batch:
        refs = []
        for proof in item["finding"]["evidence"]:
            key = canonical(proof)
            if key not in identities:
                ref = f"P{len(proofs) + 1}"
                identities[key] = ref
                proofs[ref] = compact_proof(proof)
            refs.append(identities[key])
        findings.append({"finding_id": item["id"],
                         **{key: value for key, value in item["finding"].items() if key != "evidence"},
                         "evidence_ids": refs})
    return {"commit": commit, "findings": findings, "evidence": proofs, "other_findings": other,
            "report_unknowns": list(unknowns)}


def review_unknowns(jobs):
    """Located hints, never additional original evidence or resolved facts."""
    return [{"reference_id": f"{job['id']}:U{index + 1}", "statement": value}
            for job in jobs if job.get("result")
            for index, value in enumerate(job["result"]["report"]["unknowns"])]
