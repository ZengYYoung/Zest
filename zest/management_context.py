"""Read-only project management inventory, separate from model request context."""

import json
from pathlib import Path
import re
import time

from .cognition_context import material_request_text
from .cognition_contracts import text
from .cognition_store import CognitionStore, now
from .files import CREDENTIAL_PATTERNS, FileReadError, _validate_path
from .git import inspect_repository, selected_commit, verify_commit
from .index import RetrievalIndex
from .knowledge import KnowledgeStore
from .resources import project_states
from .storage import ProjectRegistry
from .survey import SECTIONS


FINISHED = {"completed", "partial"}
INACTIVE = FINISHED | {"rejected"}


def _followup(record, finding):
    """Derive navigation from saved state, without creating an executable task."""
    review = finding["review"]
    if review is not None and not isinstance(review, dict):
        raise ValueError("invalid saved review")
    review = review or {}
    reason = review.get("reason") or ("尚未完成原文核对。" if finding["status"] == "pending_review"
                                     else "原文核对尚未通过，需要补充调查。")
    text(reason)
    conflicts = []
    for key in ("conflicts", "conflict_links"):
        values = review.get(key, [])
        if not isinstance(values, list):
            raise ValueError("invalid saved conflicts")
        for value in values:
            text(value["reference_id"], 160)
            text(value["reason"])
            conflicts.append({"run_id": record["id"], "reference_id": value["reference_id"],
                              "reason": value["reason"], "relation": key})
    action = ("inspect_saved_evidence" if not finding["saved_evidence_identity_valid"] else
              "review_finding" if finding["status"] == "pending_review" else "investigate_finding")
    return {**finding, "id": f"{record['id']}/{finding['finding_id']}", "commit": record["commit"],
            "run_status": record["status"], "run_error": record.get("error"),
            "action": action, "reason": reason, "conflicts": conflicts}


def _proofs(finding, catalog, commit):
    """Only check saved identities; this does not re-read Git or review meaning."""
    references = []
    for proof in finding.get("evidence", []):
        source = proof["evidence"]
        _validate_path(source["path"])
        approved = catalog.get(proof.get("source_id"), {})
        if (not proof.get("text") or source.get("commit") != commit
                or any(not source.get(key) or source[key] != approved.get(key)
                       for key in ("path", "commit", "blob_id", "content_sha256"))
                or type(source.get("start_line")) is not int or type(source.get("end_line")) is not int
                or not 0 < source["start_line"] <= source["end_line"]):
            return [], False
        references.append({key: source[key] for key in
                           ("path", "commit", "blob_id", "content_sha256", "start_line", "end_line")})
    return references, bool(references)


def _cognition(records, target):
    sections = [{"id": key, "title": title, "facts": [], "pending": []} for key, title, _ in SECTIONS]
    by_section = {section["id"]: section for section in sections}
    runs, history, todos, gaps, followups = [], [], [], [], []
    try:
        for record in records:
            summary = {key: record[key] for key in ("id", "commit", "status", "created_at", "updated_at", "call_count")}
            current = target is not None and record["commit"] == target
            (runs if current else history).append(summary)
            if record["status"] not in INACTIVE:
                todos.append({**summary, "current_version": current,
                              "action": "inspect_saved_requests_before_explicit_recovery" if record["status"] in ("pending_execution", "failed")
                              else "review_context_request" if record["status"] == "awaiting_context_approval"
                              else "review_material_package" if record["status"] == "awaiting_approval" else "inspect_progress"})
            if not current:
                continue
            for item in record["findings"]:
                finding = item["finding"]
                text(finding["statement"])
                if finding["kind"] not in ("observed", "inferred"):
                    raise ValueError
                section = by_section[finding["section"]]
                proofs, valid = _proofs(finding, record["catalog"], target)
                fact = (record["status"] in FINISHED and item["status"] == "auto_reviewed"
                        and finding["kind"] == "observed" and valid)
                entry = {
                    "run_id": record["id"], "finding_id": item["id"], "job_id": item["job_id"],
                    "section": finding["section"],
                    "statement": finding["statement"], "kind": finding["kind"], "status": item["status"],
                    "evidence": proofs, "saved_evidence_identity_valid": valid,
                    "review": item["review"], "invalidated": item["status"] == "invalidated"}
                section["facts" if fact else "pending"].append(entry)
                if (record["status"] in FINISHED and finding["kind"] == "observed"
                        and item["status"] in ("pending_review", "needs_investigation")):
                    followups.append(_followup(record, entry))
            for gap in (record["plan"] or {}).get("gaps", []):
                text(gap["question"])
                gaps.append({"run_id": record["id"], "section": gap["section"], "question": gap["question"], "kind": "plan_gap"})
            for job in record["jobs"]:
                if not job["result"]:
                    continue
                report = job["result"]["report"]
                questions = [("unknown", value) for value in report["unknowns"]]
                for key in ("in_scope_requests", "scope_requests"):
                    questions.extend((key, material_request_text(value)) for value in report.get(key, []))
                for key in ("rejected_findings", "rejected_material_requests"):
                    questions.extend((key, f"未接收：{issue['field']} · {issue['code']} · {issue['message']}")
                                     for issue in (job["result"].get("report_validation") or {}).get(key, []))
                for kind, question in questions:
                    text(question, 4000)
                    gaps.append({"run_id": record["id"], "job_id": job["id"],
                                 "sections": job["spec"]["sections"], "kind": kind, "question": question})
        return {"current_runs": runs, "historical_runs": history, "sections": sections,
                "gaps": gaps, "todos": todos, "followup_actions": followups,
                "coverage": {"sections_with_facts": sum(bool(s["facts"]) for s in sections), "total_sections": len(sections)},
                "evidence_check": "saved_package_identity_only_not_new_source_or_semantic_review"}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise FileReadError("cognition_record_corrupt", "认知内容或来源结构损坏，未生成管理总览。") from exc


def build_management_context(state_dir, project_id, *, progress=None):
    """One deterministic inventory; never creates stores, refreshes or calls AI."""
    started = time.monotonic()
    def emit(phase):
        if progress:
            progress({"phase": phase, "elapsed_seconds": round(time.monotonic() - started, 3)})
    emit("snapshot")
    project = ProjectRegistry(state_dir).get_project_readonly(project_id)
    if project is None:
        raise FileReadError("project_not_found", "未找到该项目。")
    directories = project_states(state_dir, project_id)
    state = inspect_repository(project["root_path"])
    target = selected_commit(project, state)
    if target is not None and not verify_commit(Path(state.root_path), target):
        raise FileReadError("invalid_commit", "管理总览需要有效的本地选定 commit。")
    result = {"schema_version": 1, "kind": "project_management_context", "project_id": project_id,
              "created_at": now(), "target_commit": target, "head_commit": state.head_commit,
              "working_tree_dirty": state.dirty, "working_tree_excluded": True,
              "resource_dir": project.get("resource_dir"), "record_sources": [str(d) for d in directories],
              "model_calls": 0, "records_truncated": False,
              "limitations": ["read_only_inventory_not_model_request_authorization", "static_records_not_runtime_verification",
                              "automatic_review_not_human_confirmation", "per_store_read_snapshots_not_global_transaction",
                              "investigation_todos_only_not_all_maintenance_tasks", "management_skills_not_automatically_loaded"]}
    emit("memory")
    applicable, other = [], []
    for note in KnowledgeStore(state_dir).management_records(project_id):
        if note["status"] == "confirmed" and (note["scope"] == "project" or
                                               (target is not None and note["observed_commit"] == target)):
            applicable.append(note)
        else:
            reason = note["status"] if note["status"] != "confirmed" else "different_version"
            other.append({key: note[key] for key in ("id", "title", "scope", "observed_commit", "status")} | {"reason": reason})
    result["memory"] = {"applicable": applicable, "other": other}
    emit("cognition")
    result["cognition"] = _cognition(CognitionStore(state_dir).management_records(project_id), target)
    for action in result["cognition"]["followup_actions"]:
        action["inspect_command"] = ["zest", "--state-dir", str(Path(state_dir).resolve()), "cognition", "show",
                                     action["run_id"], "--format", "json"]
        if action["saved_evidence_identity_valid"]:
            action["prepare_command"] = ["zest", "--state-dir", str(Path(state_dir).resolve()), "cognition", "followup-create",
                                         action["run_id"], action["finding_id"]]
    emit("index")
    result["index"] = {"status": "no_committed_target", "files": []}
    if target is not None:
        try:
            result["index"] = RetrievalIndex(state_dir).inventory(project, commit=target)
        except FileReadError as exc:
            if exc.code not in ("index_missing", "index_stale"):
                raise
            result["index"] = {"status": "no_current_version_index", "files": []}
    if selected_commit(project, inspect_repository(project["root_path"])) != target:
        raise FileReadError("management_target_changed", "读取期间选定版本变化，请重新获取管理总览。")
    if any(re.search(pattern, json.dumps(result, ensure_ascii=False).encode()) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_management_context", "管理记录包含已识别凭据，未输出总览。")
    emit("completed")
    return result


def format_management_context(record):
    from shlex import join
    from .terminal import display_text
    from .cognition_views import LABELS
    actions = {"inspect_saved_requests_before_explicit_recovery": "核对保存的请求，再决定是否显式恢复",
               "review_context_request": "审阅待扩展请求", "review_material_package": "审阅待批准资料包",
               "inspect_progress": "查看当前调查进度"}
    index_states = {"available": "当前版本已有索引", "no_current_version_index": "当前版本尚无索引",
                    "no_committed_target": "项目尚无提交"}
    lines = ["Zest · 项目管理上下文（只读）", f"项目：{record['project_id']}",
             f"选定版本：{record['target_commit'] or '尚无提交'}", f"HEAD：{record['head_commit'] or '尚无提交'}",
             "未提交内容不纳入事实；总览不启动任务或授予执行权限。", "", "适用的人工确认记忆："]
    lines.extend(f"- {display_text(n['title'])} [{n['id']}]\n  {display_text(n['body'])}" for n in record["memory"]["applicable"])
    if not record["memory"]["applicable"]:
        lines.append("尚无适用记录。")
    lines.append(f"草稿、失效或其他版本记忆：{len(record['memory']['other'])} 项（JSON 可查看身份与状态）")
    for section in record["cognition"]["sections"]:
        lines.extend(["", section["title"] + "："])
        for item in section["facts"]:
            lines.append(f"- {display_text(item['statement'])} [{item['run_id']} / {item['finding_id']}]")
            lines.extend(f"  来源：{display_text(p['path'])}:{p['start_line']}–{p['end_line']} · {p['commit']}" for p in item["evidence"])
        if not section["facts"]:
            lines.append("尚无同版本自动核对事实。")
        for item in section["pending"]:
            lines.append(f"  待核实/失效（{LABELS.get(item['status'], item['status'])}）：{display_text(item['statement'])}")
    lines.extend(["", f"索引：{index_states[record['index']['status']]}；{len(record['index']['files'])} 个已索引文件"])
    lines.extend(f"- {display_text(p['path'])} · {p['blob_id']}" for p in record["index"]["files"])
    lines.extend(["", "认知缺口与资料需求："])
    lines.extend("- " + display_text(g["question"]) for g in record["cognition"]["gaps"])
    if not record["cognition"]["gaps"]:
        lines.append("尚无已记录缺口，不代表没有缺口。")
    lines.extend(["", "调查待办："])
    lines.extend(f"- {r['id']} · {LABELS.get(r['status'], r['status'])} · {'当前版本' if r['current_version'] else '历史版本，先核对'} · {actions[r['action']]}"
                 for r in record["cognition"]["todos"])
    if not record["cognition"]["todos"]:
        lines.append("没有登记中的调查待办。")
    lines.extend(["", "后续行动（已结束调查）："])
    followups = record["cognition"].get("followup_actions", [])
    action_names = {"review_finding": "需要核对", "investigate_finding": "需要补查",
                    "inspect_saved_evidence": "先核对保存的来源身份"}
    for action in followups:
        lines.append("- " + display_text(f"{action_names[action['action']]} · {action['run_id']} / {action['finding_id']} · {action['statement']}"))
        lines.append("  原因：" + display_text(action["reason"]))
        if action["run_error"]:
            lines.append("  调查停止原因：" + display_text(action["run_error"]))
        lines.extend("  来源：" + display_text(f"{p['path']}:{p['start_line']}–{p['end_line']} · {p['commit']}")
                     for p in action["evidence"])
        if not action["saved_evidence_identity_valid"]:
            lines.append("  保存的来源身份不完整或不匹配，需先检查。")
        lines.extend("  未解决冲突：" + display_text(f"{c['run_id']} / {c['reference_id']} · {c['reason']}")
                     for c in action["conflicts"])
        lines.append("  查看：" + display_text(join(action["inspect_command"])))
        if action.get("prepare_command"):
            lines.append("  准备新待批准调查包：" + display_text(join(action["prepare_command"])))
    if not followups:
        lines.append("没有符合当前版本与状态条件的后续行动；未知和推测仍见原栏目。")
    else:
        lines.append("以上为只读建议；继续核对或补查须另行准备和批准，不恢复旧调查或重发请求。")
    lines.append(f"历史版本调查：{len(record['cognition']['historical_runs'])} 次；没有移入当前事实。")
    lines.append("自动核对仍可能出错；此处仅检查保存的来源身份，未重新核对原文或证明实际运行状态。")
    return "\n".join(lines)
