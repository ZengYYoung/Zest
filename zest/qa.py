"""Grounded project questions, with bounded context and checked citation IDs."""

import json
from pathlib import Path
import re

from .files import CREDENTIAL_PATTERNS, FileReadError, read_project_file
from .git import inspect_repository
from .models import DeepSeekModel, ModelSettings
from .search import search_project


MAX_SOURCES = 8
MAX_CONTEXT_CHARS = 24000


def prepare_context(project: dict, question: str, *, keywords=None, files=None, state_dir=None) -> dict:
    if not question.strip() or len(question) > 2000:
        raise FileReadError("invalid_question", "问题须为 1—2000 字符的非空文本。")
    if any(re.search(pattern, question.encode("utf-8")) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_question", "问题中包含已识别凭据，未发送。")
    keywords = keywords or []
    files = files or []
    if len(keywords) > 3 or len(files) > 5:
        raise FileReadError("context_input_limit", "最多指定 3 个关键词和 5 个文件。")
    state = inspect_repository(project["root_path"])
    commit = state.head_commit
    result = {"project_id": project["id"], "question": question, "commit": commit,
              "sources": [], "warnings": []}
    seen = set()
    size = 0

    if state_dir is not None:
        from .knowledge import KnowledgeStore
        for note in KnowledgeStore(state_dir).for_context(project["id"], commit):
            source = {"id": f"E{len(result['sources']) + 1}",
                      "evidence": {"kind": "confirmed_knowledge", "knowledge_id": note["id"],
                                   "scope": note["scope"], "observed_commit": note["observed_commit"],
                                   "confirmed_at": note["confirmed_at"]},
                      "title": note["title"], "body": note["body"]}
            added = len(json.dumps(source, ensure_ascii=False))
            if size + added <= MAX_CONTEXT_CHARS:
                result["sources"].append(source)
                size += added
            else:
                result["warnings"].append({"code": "knowledge_context_budget"})

    if not commit:
        result["warnings"].append({"code": "unborn_repository"})
        return result

    def add(path, start=1, end=60):
        nonlocal size
        if len(result["sources"]) >= MAX_SOURCES:
            return
        key = (path, start, end)
        if key in seen:
            return
        seen.add(key)
        try:
            record = read_project_file(project, path, commit=commit, start_line=start, end_line=end)
        except FileReadError as exc:
            result["warnings"].append({"code": exc.code, "path": path})
            return
        if not record["lines"]:
            return
        source = {"id": f"E{len(result['sources']) + 1}", "evidence": record["evidence"],
                  "lines": record["lines"], "content_sha256": record["content_sha256"]}
        added = len(json.dumps(source, ensure_ascii=False))
        if size + added > MAX_CONTEXT_CHARS:
            result["warnings"].append({"code": "context_budget", "path": path})
            return
        result["sources"].append(source)
        size += added

    for path in [*files, "README.md", "pyproject.toml", "package.json"]:
        add(path)
    for keyword in keywords:
        matches = search_project(project, keyword, commit=commit, limit=8)
        result["warnings"].extend(matches["warnings"])
        for match in matches["matches"]:
            evidence = match["evidence"]
            add(evidence["path"], max(1, evidence["start_line"] - 3), evidence["end_line"] + 3)
    return result


def validate_answer(raw: str, sources: list[dict]) -> dict:
    if len(raw) > 64000:
        raise FileReadError("invalid_model_output", "模型输出超出大小限制。")
    if any(re.search(pattern, raw.encode("utf-8")) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_model_output", "模型输出包含已识别凭据，未返回正文。")
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise FileReadError("invalid_model_output", "模型未返回有效 JSON。") from exc
    if (not isinstance(answer, dict) or set(answer) != {"status", "answer", "citations", "unknowns"}
            or answer["status"] not in ("answered", "insufficient_evidence")
            or not isinstance(answer["answer"], str) or not 1 <= len(answer["answer"]) <= 12000
            or not isinstance(answer["citations"], list) or len(answer["citations"]) > MAX_SOURCES
            or not all(isinstance(item, str) for item in answer["citations"])
            or not isinstance(answer["unknowns"], list) or len(answer["unknowns"]) > 20
            or not all(isinstance(item, str) and len(item) <= 2000 for item in answer["unknowns"])):
        raise FileReadError("invalid_model_output", "模型回答字段不符合契约。")
    known = {source["id"]: source for source in sources}
    if any(item not in known for item in answer["citations"]):
        raise FileReadError("invalid_citation", "模型引用了未提供的来源。")
    if answer["status"] == "answered" and not answer["citations"]:
        raise FileReadError("missing_citation", "模型给出答案但未引用证据。")
    if any(item not in known for item in re.findall(r"\[(E\d+)\]", answer["answer"])):
        raise FileReadError("invalid_citation", "回答正文包含未知来源。")
    return answer


def answer_context(context: dict, *, allow_model: bool, model=None) -> dict:
    if not allow_model:
        raise FileReadError("model_permission_required", "调用模型会发送问题和选定片段；请先 preview，再明确使用 --allow-model。")
    if not context["sources"]:
        return {"status": "insufficient_evidence", "answer": "没有可用证据，无法回答。",
                "citations": [], "unknowns": ["需要已提交的允许文件"], "model_called": False,
                "commit": context["commit"], "warnings": context["warnings"]}
    provider = model or DeepSeekModel(ModelSettings.from_env())
    # Only the bounded sources and question are sent; local absolute paths and keys are not prompt data.
    response = provider.complete(model_payload(context))
    answer = validate_answer(response["text"], context["sources"])
    known = {source["id"]: source for source in context["sources"]}
    return {**answer, "commit": context["commit"], "model_called": True,
            "evidence": [known[item] for item in dict.fromkeys(answer["citations"])],
            "model": {key: value for key, value in response.items() if key != "text"},
            "warnings": context["warnings"]}


def model_payload(context: dict) -> dict:
    return {"question": context["question"], "sources": context["sources"],
            "limitations": sorted({item["code"] for item in context["warnings"]})}
