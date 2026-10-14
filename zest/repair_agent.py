"""A bounded repair specialist produces proposals, never applies its own changes."""

import asyncio
import hashlib
import json

from .agents import verify_sources
from .files import CREDENTIAL_PATTERNS, FileReadError, read_project_file
from .git import inspect_repository, selected_commit
from .knowledge import KnowledgeStore, _text
from .models import REPAIR_PROMPT
from .qa import model_payload
from .repairs import MAX_CHANGE_BYTES, RepairStore, _raw_content
from .storage import ProjectRegistry


def repair_policy(source_ids):
    return {"strategy": "repair_specialist", "editable_source_ids": source_ids,
            "max_model_calls": 1, "max_output_tokens": 4096, "apply_changes": False,
            "prompt_sha256": hashlib.sha256(REPAIR_PROMPT.encode()).hexdigest()}


def prepare_repair_context(project, question, *, files, state_dir):
    _text(question, 2000)
    if (not isinstance(files, list) or not 1 <= len(files) <= 5 or not all(isinstance(path, str) for path in files)
            or len(set(files)) != len(files)):
        raise FileReadError("repair_files_required", "请明确指定 1—5 个不重复的既有待修复文件。")
    state = inspect_repository(project["root_path"])
    commit = selected_commit(project, state)
    if commit is None or state.dirty:
        raise FileReadError("repair_clean_base_required", "修复建议需要已提交且干净的工作区。")
    sources = []
    for path in files:
        record = read_project_file(project, path, commit=commit)
        if (not record["lines"] or record["more_after"] or len(record["content"].encode()) > MAX_CHANGE_BYTES):
            raise FileReadError("repair_full_file_required", "首版修复建议仅支持非空、最多 200 行及 64 KiB 的完整文件，不发送截断片段。")
        sources.append({"id": f"E{len(sources) + 1}", "evidence": record["evidence"],
                        "lines": record["lines"], "content": record["content"],
                        "content_sha256": record["content_sha256"]})
    editable_ids = [source["id"] for source in sources]
    for note in KnowledgeStore(state_dir).for_context(project["id"], commit):
        sources.append({"id": f"E{len(sources) + 1}", "evidence": {"kind": "confirmed_knowledge",
                        "knowledge_id": note["id"], "scope": note["scope"], "observed_commit": note["observed_commit"],
                        "confirmed_at": note["confirmed_at"]}, "title": note["title"], "body": note["body"]})
    context = {"project_id": project["id"], "question": question, "commit": commit,
               "workspace_head": state.head_commit, "sources": sources, "warnings": [],
               "purpose": "repair_proposal", "execution_policy": repair_policy(editable_ids)}
    if len(json.dumps(model_payload(context), ensure_ascii=False)) > 24000:
        raise FileReadError("repair_context_limit", "完整修复上下文超过 24000 字符，请缩小明确的文件范围。")
    return context


def validate_proposal(raw, context):
    if not isinstance(raw, str) or not raw or len(raw) > 256 * 1024:
        raise FileReadError("invalid_proposal_output", "模型方案不是受限 JSON 文本。")
    # Scan the complete response before parsing; errors never return secret content.
    import re
    if any(re.search(pattern, raw.encode()) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_proposal_output", "模型方案含已识别凭据，未保存。")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise FileReadError("invalid_proposal_output", "模型未返回有效 JSON 方案。") from exc
    if (not isinstance(data, dict) or set(data) != {"status", "summary", "changes", "unknowns"}
            or data["status"] not in ("proposed", "insufficient_evidence")
            or not isinstance(data["changes"], list) or len(data["changes"]) > 5
            or not isinstance(data["unknowns"], list) or len(data["unknowns"]) > 20):
        raise FileReadError("invalid_proposal_output", "方案字段不符合契约。")
    _text(data["summary"], 2000)
    for missing in data["unknowns"]:
        _text(missing, 1000)
    if data["status"] == "insufficient_evidence":
        if data["changes"] or not data["unknowns"]:
            raise FileReadError("invalid_proposal_output", "证据不足的方案不能包含修改，且需要说明缺口。")
        return data
    if not data["changes"] or data["unknowns"]:
        raise FileReadError("invalid_proposal_output", "具体修复方案必须有修改且无未解决缺口。")
    known = {source["id"]: source for source in context["sources"]}
    editable = set(context["execution_policy"]["editable_source_ids"])
    seen = set()
    for change in data["changes"]:
        if (not isinstance(change, dict) or set(change) != {"source_id", "content"}
                or not isinstance(change["source_id"], str) or change["source_id"] not in editable
                or change["source_id"] in seen or "path" not in known[change["source_id"]]["evidence"]):
            raise FileReadError("proposal_scope_violation", "模型尝试修改未批准来源、知识记录或重复文件。")
        seen.add(change["source_id"])
        _raw_content(change["content"])
    return data


def generate_proposal(context, *, state_dir, model, source_task_id):
    editable = [source["id"] for source in context["sources"] if "path" in source["evidence"]]
    if context["execution_policy"] != repair_policy(editable) or context["purpose"] != "repair_proposal":
        raise FileReadError("repair_policy_changed", "修复建议策略已变化，需要重新准备并批准。")
    project = ProjectRegistry(state_dir).get_project(context["project_id"])
    if project is None:
        raise FileReadError("project_not_found", "未找到该项目。")
    def check_version():
        state = inspect_repository(project["root_path"])
        if (selected_commit(project, state) != context["commit"] or state.head_commit != context["workspace_head"]
                or state.dirty):
            raise FileReadError("repair_base_changed", "修复建议的基础版本或工作区已变化，需要重新准备。")
    check_version()
    asyncio.run(verify_sources(context, state_dir))
    response = model.complete(model_payload(context), purpose="repair_proposal")
    proposal = validate_proposal(response["text"], context)
    metadata = {key: value for key, value in response.items() if key != "text"}
    if proposal["status"] == "insufficient_evidence":
        return {**proposal, "model_called": True, "model": metadata, "commit": context["commit"]}
    check_version()
    known = {source["id"]: source for source in context["sources"]}
    changes = [{"path": known[change["source_id"]]["evidence"]["path"],
                "expected_sha256": known[change["source_id"]]["content_sha256"], "content": change["content"]}
               for change in proposal["changes"]]
    repair = RepairStore(state_dir).create(project, {"summary": proposal["summary"], "changes": changes},
                                         source_task_id=source_task_id)
    return {"status": "proposed", "repair_id": repair["id"], "repair_plan_sha256": repair["plan_sha256"],
            "next_step": "review_and_approve_repair", "summary": proposal["summary"],
            "commit": context["commit"], "model_called": True, "model": metadata}
