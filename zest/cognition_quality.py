"""Frozen quality rules and conservative isolation of malformed review judgments."""
from .cognition_contracts import keys
from .cognition_review import validate_judgments
from .files import FileReadError


QUALITY_CONTRACT = {
    "version": 1, "review": "isolate_invalid_findings", "review_batch_size": 2,
    "findings": "每条发现只写一个可独立核对的事实、属性或业务规则；不同接口、分支、默认值、限制分条。相关步骤可连贯描述，但不要拼接多个独立功能。建议每条不超过180字，引用实际读取的原文。",
    "gaps": "源码问题先定位并补读相关方法和后续行段；真实运行、部署、团队背景等仓库外信息单独保留未知。未读源码不等于需要开发者人工确认。",
}


def partition_judgments(response, payload):
    """Never repair citation IDs. Isolate known identities, reject ambiguous batches."""
    keys(response, ("judgments",))
    values = response["judgments"]
    expected = {f["finding_id"]: f for f in payload["findings"]}
    ids = [v.get("finding_id") for v in values] if isinstance(values, list) and all(isinstance(v, dict) for v in values) else []
    if (len(ids) != len(expected) or any(not isinstance(i, str) for i in ids)
            or len(set(ids)) != len(ids) or set(ids) != set(expected)):
        raise FileReadError("invalid_cognition_output", "核对发现身份缺失、重复或无效，不能逐条关联结果。")
    accepted, issues = [], []
    for value in values:
        identity = value["finding_id"]
        peers = [{"finding_id": i, "statement": f["statement"], "kind": f["kind"]}
                 for i, f in expected.items() if i != identity]
        single = {**payload, "findings": [expected[identity]], "other_findings": [*payload["other_findings"], *peers]}
        try:
            accepted.extend(validate_judgments([value], single))
        except FileReadError as exc:
            issues.append({"finding_id": identity, "code": exc.code, "reason": str(exc)})
    # Even a malformed judgment may flag a real conflict: do not accept its peer.
    failed = {v["finding_id"] for v in issues}
    linked = {c.get("reference_id") for v in values if v["finding_id"] in failed
              for c in (v.get("conflicts") if isinstance(v.get("conflicts"), list) else [])
              if isinstance(c, dict) and isinstance(c.get("reference_id"), str)}
    for value in list(accepted):
        if value["finding_id"] in linked:
            accepted.remove(value)
            issues.append({"finding_id": value["finding_id"], "code": "review_conflict_unresolved",
                           "reason": "关联结论的核对未通过，冲突需继续调查。"})
    return accepted, issues


def pending_category(item):
    if "finding" in item:
        if item["status"] == "invalidated": return "invalidated", "已失效的历史结论"
        if (item.get("review") or {}).get("validation_error"): return "review_failed", "核对格式或引用待修正"
        if (item.get('review') or {}).get('conflicts') or (item.get('review') or {}).get('conflict_links'):
            return 'contradiction', '结论存在矛盾，暂不采用'
        if item["finding"]["kind"] == "inferred": return "analysis_hint", "分析线索，尚非事实"
        if item["status"] == "pending_review": return "review_pending", "尚未完成自动核对"
        from .cognition_workflow import remaining_checks
        missing = remaining_checks(item)
        if missing and all(c['reason'].startswith('[外部资料]') for c in missing):
            return 'external_material', '需补充的运行或团队资料'
        if missing and all(c['reason'].startswith('[分析推导]') for c in missing):
            return 'analysis_hint', '分析解释，尚非源码事实'
        return "investigation_gap", "证据不足，后续按需补充"
    if item.get("source_gap"): return "source_gap", "需继续读取的项目资料"
    if item.get("scope_expansion"): return "external_material", "需补充的包外资料"
    return "investigation_gap", "需继续调查的问题"


def classify_pending(sections):
    from .cognition_store import digest
    unique = {}
    for section in sections:
        local = {}
        for item in section["pending"]:
            category, title = pending_category(item)
            identity = digest({"finding_id": item["id"]} if "finding" in item else
                              {"category": category, "question": item["question"].strip()})
            item = {**item, "pending_id": identity, "category": category, "category_title": title}
            local[identity] = item
            unique[identity] = item
        section["pending"] = list(local.values())
    counts = {}
    for item in unique.values():
        key = item["category"]
        counts.setdefault(key, {"category": key, "title": item["category_title"], "count": 0})["count"] += 1
    return len(unique), list(counts.values())
