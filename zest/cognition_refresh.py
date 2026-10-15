"""Read-only version-change preview; does not refresh or authorize knowledge."""

import json
from pathlib import Path
import re
import time

from .cognition_contracts import POLICY
from .cognition_store import CognitionStore, now, valid_id
from .files import CREDENTIAL_PATTERNS, FileReadError
from .git import compare_commits, inspect_repository, selected_commit, verify_commit
from .storage import ProjectRegistry


def _baseline(store, project_id, target, base_run_id):
    if not store.db_path.exists():
        if base_run_id:
            raise FileReadError("cognition_not_found", "未找到指定认知基线。")
        return None, [], []
    with store.open() as conn:
        conn.execute("BEGIN")  # One read snapshot for runs, envelopes and findings.
        if base_run_id:
            valid_id(base_run_id)
            base = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (base_run_id,)).fetchone()
            if base is None or base["project_id"] != project_id:
                raise FileReadError("cognition_scope_mismatch", "指定基线不存在或不属于当前项目。")
            if base["status"] not in ("completed", "partial"):
                raise FileReadError("cognition_baseline_not_ready", "基线需为已结束的 completed/partial 调查。")
        else:
            base = conn.execute("""SELECT * FROM cognition_runs WHERE project_id=?
                AND status IN ('completed','partial') ORDER BY (commit_id=?) DESC,created_at DESC,id DESC LIMIT 1""",
                                (project_id, target)).fetchone()
        if base is None:
            return None, [], []
        # Include all eligible investigations of this version, not only the most
        # recent run or the history browser's first 100 rows.
        runs = conn.execute("""SELECT * FROM cognition_runs WHERE project_id=? AND commit_id=?
            AND status IN ('completed','partial') ORDER BY created_at,id""", (project_id, base["commit_id"])).fetchall()
        findings, summaries = [], []
        for run in runs:
            envelope = store.envelope(run)
            catalog = {source["evidence"]["path"]: source["evidence"] for source in envelope["files"]}
            summaries.append({"run_id": run["id"], "status": run["status"], "updated_at": run["updated_at"]})
            for row in conn.execute("SELECT * FROM cognition_findings WHERE run_id=? AND status!='invalidated' ORDER BY id", (run["id"],)):
                try:
                    value = json.loads(row["finding"])
                    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in ("section", "kind", "statement")):
                        raise ValueError
                    if not isinstance(value.get("evidence", []), list):
                        raise ValueError
                except (ValueError, TypeError, RecursionError) as exc:
                    raise FileReadError("cognition_record_corrupt", "保存的认知格式损坏，无法生成完整清单。") from exc
                findings.append({"run_id": run["id"], "finding_id": row["id"], "status": row["status"],
                                 "value": value, "catalog": catalog})
        return {"run_id": base["id"], "commit": base["commit_id"],
                "selection": "explicit" if base_run_id else "current_version_or_latest_finished"}, summaries, findings


def _finding(item, base, changes, available, version_changed):
    value = item["value"]
    proofs = []
    unresolved = not value.get("evidence")
    for proof in value.get("evidence", []):
        source = proof.get("evidence", {}) if isinstance(proof, dict) else {}
        source = source if isinstance(source, dict) else {}
        path = source.get("path") if isinstance(source.get("path"), str) else None
        original = item["catalog"].get(path)
        matched = bool(original and isinstance(proof, dict) and proof.get("text") and source.get("commit") == base
                       and type(source.get("start_line")) is int and type(source.get("end_line")) is int
                       and 0 < source["start_line"] <= source["end_line"]
                       and all(source.get(key) == original.get(key) and source.get(key)
                               for key in ("blob_id", "content_sha256")))
        change = changes.get(path)
        if change and change["before_object"] != source.get("blob_id"):
            matched = False
        state = "unresolved" if not available or not matched else "changed" if change else "unchanged"
        unresolved = unresolved or state == "unresolved"
        proofs.append({"path": path, "commit": source.get("commit"), "blob_id": source.get("blob_id"),
                       "content_sha256": source.get("content_sha256"), "start_line": source.get("start_line"),
                       "end_line": source.get("end_line"), "comparison": state,
                       "change": change["change"] if change else None})
    classification = ("unresolved" if unresolved else "directly_affected" if any(p["comparison"] == "changed" for p in proofs)
                      else "evidence_unchanged")
    return {"run_id": item["run_id"], "finding_id": item["finding_id"], "original_status": item["status"],
            "section": value["section"], "kind": value["kind"], "statement": value["statement"],
            "classification": classification, "evidence": proofs,
            "needs_review": version_changed or item["status"] != "auto_reviewed" or value["kind"] != "observed" or unresolved}


def build_refresh_preview(state_dir, project_id, *, base_run_id=None, progress=None):
    started = time.monotonic()

    def emit(phase, **details):
        if progress:
            progress({"phase": phase, "elapsed_seconds": round(time.monotonic() - started, 3), **details})

    emit("snapshot")
    project = ProjectRegistry(state_dir).get_project_readonly(project_id)
    if project is None:
        raise FileReadError("project_not_found", "未找到该项目。")
    state = inspect_repository(project["root_path"])
    target = selected_commit(project, state)
    if target is not None and not verify_commit(Path(state.root_path), target):
        raise FileReadError("invalid_commit", "选定版本不是有效的本地 commit。")
    result = {"schema_version": 1, "kind": "cognition_refresh_preview", "project_id": project_id,
              "created_at": now(), "target_commit": target, "head_commit": state.head_commit,
              "working_tree_excluded": True, "working_tree_dirty": state.dirty,
              "baseline": None, "baseline_runs": [], "changes": [], "findings": [], "uncovered_changes": [],
              "requires_discussion": False, "warnings": [], "model_calls": 0,
              "limitations": ["file_level_comparison_only", "no_cross_version_knowledge_reuse",
                              "no_semantic_dependency_analysis", "manual_notes_not_mapped",
                              "preview_is_not_investigation_authorization"]}
    emit("baseline")
    base, runs, findings = _baseline(CognitionStore(state_dir), project_id, target, base_run_id)
    result.update(baseline=base, baseline_runs=runs)
    if target is None:
        result["status"] = "no_committed_target"
    elif base is None:
        result["status"] = "no_baseline"
    else:
        available = verify_commit(Path(state.root_path), base["commit"])
        emit("compare")
        result["changes"] = compare_commits(Path(state.root_path), base["commit"], target) if available else []
        result["status"] = ("baseline_unavailable" if not available else
                            "same_version" if base["commit"] == target else "refresh_needed")
        changes = {item["path"]: item for item in result["changes"]}
        emit("evidence", changed_files=len(changes))
        result["findings"] = [_finding(item, base["commit"], changes, available, base["commit"] != target) for item in findings]
        covered = {proof["path"] for item in result["findings"] for proof in item["evidence"]
                   if proof["comparison"] == "changed"}
        result["uncovered_changes"] = [item for item in result["changes"] if item["path"] not in covered]
        if any(run["status"] == "partial" for run in runs):
            result["warnings"].append("baseline_contains_partial_investigations")
        if base["commit"] != target:
            result["warnings"].append("unchanged_direct_evidence_does_not_prove_new_version_claims")
    result["requires_discussion"] = len(result["changes"]) >= POLICY["large_scope_files"]
    if result["requires_discussion"]:
        result["warnings"].append("large_change_set_review_scope_before_investigation")
    result["counts"] = {"changed_files": len(result["changes"]), "findings": len(result["findings"]),
                        "uncovered_changes": len(result["uncovered_changes"]),
                        **{key: sum(f["classification"] == key for f in result["findings"])
                           for key in ("directly_affected", "evidence_unchanged", "unresolved")}}
    # The preview is a snapshot, not permission to run against a moving ref.
    if selected_commit(project, inspect_repository(project["root_path"])) != target:
        raise FileReadError("cognition_refresh_target_changed", "对比期间选定版本发生变化，请重新生成清单。")
    encoded = json.dumps(result, ensure_ascii=False).encode()
    if any(re.search(pattern, encoded) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_refresh_preview", "清单包含已识别凭据，未输出。")
    emit("completed", **result["counts"])
    return result


def format_refresh_preview(record):
    from .terminal import display_text
    labels = {"no_committed_target": "项目尚无提交", "no_baseline": "尚无已结束的认知基线",
              "baseline_unavailable": "基线提交已无法在本地读取，不能判断影响",
              "same_version": "选定版本与认知基线相同", "refresh_needed": "版本已变化，待准备刷新调查"}
    kinds = {"directly_affected": "证据文件变化，待刷新", "evidence_unchanged": "直接证据未变",
             "unresolved": "证据不完整或无法核对，待调查"}
    base = record["baseline"]
    lines = ["Zest · 增量认知预览（只读）", labels[record["status"]],
             f"基线：{base['commit'] if base else '无'}", f"选定版本：{record['target_commit'] or '无'}",
             f"变更文件 {record['counts']['changed_files']} 个 · 关联认知 {record['counts']['findings']} 项"]
    if record["working_tree_dirty"]:
        lines.append("工作区有未提交修改；本次仅比较固定 commit，不包含工作区内容。")
    if record["requires_discussion"]:
        lines.append("变更较多，调查前请先商量按哪些模块分批；下面清单未截断。")
    if "baseline_contains_partial_investigations" in record["warnings"]:
        lines.append("基线含部分完成的调查，已有认知覆盖并不完整。")
    for change in record["changes"]:
        lines.append(f"  {change['change']} · {change['path']}")
    for finding in record["findings"]:
        lines.append(f"  {finding['run_id']} / {finding['finding_id']} · {kinds[finding['classification']]} · {finding['statement']}")
    if record["uncovered_changes"]:
        lines.append(f"另有 {len(record['uncovered_changes'])} 个变更文件未关联既有直接证据，需在后续调查中确认。")
    lines.append("本清单为文件级影响线索，不含完整语义依赖；直接证据未变不表示可跨版本复用结论。")
    lines.append("未调用模型、更新知识或授予调查权限。")
    return "\n".join(display_text(line) for line in lines)
