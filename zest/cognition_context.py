"""Lossless model views of durable original proofs; no source text truncation."""

from .cognition_contracts import POLICY, canonical


def material_catalog(envelope):
    """Show as many approved entries as the metadata budget fits, not 60 IDs."""
    budget = envelope.get("policy", {}).get("catalog_chars", POLICY["catalog_chars"])
    result = []
    priority = {item["source_id"] for item in envelope.get("refresh_context", {}).get("focus", []) if item["source_id"]}
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
