"""Attribute-level review validation; no semantic truth is inferred by this code."""

from .cognition_contracts import items, keys, strings, text
from .files import FileReadError


def validate_judgments(judgments, payload):
    expected = {f["finding_id"]: f for f in payload["findings"]}
    references = set(expected) | {f["finding_id"] for f in payload["other_findings"]}
    references |= {u["reference_id"] for u in payload["report_unknowns"]}
    output = []
    for judgment in items(judgments, len(expected), minimum=len(expected)):
        keys(judgment, ("finding_id", "checks", "coverage_complete", "conflicts", "reason"))
        identity = judgment["finding_id"]
        if not isinstance(identity, str) or identity not in expected:
            raise FileReadError("invalid_cognition_output", "核对发现身份重复或无效。")
        finding = expected.pop(identity)
        if type(judgment["coverage_complete"]) is not bool:
            raise FileReadError("invalid_cognition_output", "核对覆盖标志必须为布尔值。")
        text(judgment["reason"])
        for check in items(judgment["checks"], None, minimum=1):
            keys(check, ("subject", "attribute", "condition", "assertion", "evidence_ids", "verdict", "reason"))
            for field in ("subject", "attribute", "condition", "assertion", "reason"):
                text(check[field])
            if check["verdict"] not in ("supported", "contradicted", "insufficient_evidence"):
                raise FileReadError("invalid_cognition_output", "独立命题判定无效。")
            strings(check["evidence_ids"], None, allowed=set(finding["evidence_ids"]),
                    minimum=1 if check["verdict"] == "supported" else 0, field="check.evidence_ids")
        seen = set()
        for conflict in items(judgment["conflicts"], None):
            keys(conflict, ("reference_id", "reason"))
            ref = conflict["reference_id"]
            if not isinstance(ref, str) or ref == identity or ref not in references or ref in seen:
                raise FileReadError("invalid_cognition_reference", "冲突线索身份无效、重复或引用自身。")
            seen.add(ref)
            text(conflict["reason"])
        output.append({**judgment, "supported": accepts(judgment, finding)})
    return output


def accepts(judgment, finding):
    """Whole-finding gate; semantic completeness remains a model judgment."""
    return (finding["kind"] == "observed" and judgment.get("coverage_complete") is True
            and bool(judgment.get("checks"))
            and all(c["verdict"] == "supported" for c in judgment["checks"])
            and not judgment.get("conflicts") and not judgment.get("conflict_links"))
