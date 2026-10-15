"""Lossless model views of durable original proofs; no source text truncation."""

from .cognition_contracts import canonical


def compact_proof(proof):
    """Text and its physical range replace the redundant per-line text copy."""
    return {key: value for key, value in proof.items() if key != "lines"}


def compact_results(results):
    output, seen = [], set()
    for result in results:
        if result["tool"] == "read" and result["result"].get("ok"):
            ref = result["citation_id"]
            if ref in seen:
                continue
            seen.add(ref)
            result = {**result, "result": {**result["result"], "data": compact_proof(result["result"]["data"])}}
        output.append(result)
    # Locations are hints, not evidence. Keep every original-text proof visible.
    while len(canonical(output)) > 18000:
        index = next((i for i, result in enumerate(output) if result["tool"] != "read"), None)
        if index is None:
            break
        output.pop(index)
    return output


def review_payload(commit, batch, other):
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
    return {"commit": commit, "findings": findings, "evidence": proofs, "other_findings": other}
