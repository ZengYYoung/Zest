"""Grounded project questions, with bounded context and checked citation IDs."""

import json
import os
from pathlib import Path
import re

from .files import CREDENTIAL_PATTERNS, FileReadError, read_project_file
from .git import inspect_repository, selected_commit
from .models import DeepSeekModel, ModelSettings, response_metadata
from .search import search_project


MAX_SOURCES = 8
MAX_CONTEXT_CHARS = 128000


def context_budget():
    """Application source budget, independent of a provider's token window."""
    value = os.environ.get("ZEST_QA_CONTEXT_CHARS")
    if value is None:
        try:
            from dotenv import dotenv_values
        except ImportError:
            pass
        else:
            value = dotenv_values(Path.cwd() / ".env", interpolate=False).get("ZEST_QA_CONTEXT_CHARS")
    if value is None:
        return MAX_CONTEXT_CHARS
    try:
        budget = int(value)
    except (TypeError, ValueError) as exc:
        raise FileReadError("invalid_qa_context_budget", "ZEST_QA_CONTEXT_CHARS 须为 1—1000000 的整数字符数。") from exc
    if not 1 <= budget <= 1000000:
        raise FileReadError("invalid_qa_context_budget", "ZEST_QA_CONTEXT_CHARS 须为 1—1000000 的整数字符数。")
    return budget


def compact_provenance(proofs):
    """Keep every distinct original window once, with no duplicated text form."""
    result, seen = [], set()
    for proof in proofs:
        item = {key: proof[key] for key in ("source_id", "evidence", "more_after") if key in proof}
        if "lines" in proof:
            item["lines"] = proof["lines"]
        else:
            item["text"] = proof["text"]
        identity = json.dumps({k: v for k, v in item.items() if k != "source_id"}, sort_keys=True, ensure_ascii=False)
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def _symbol_ranges(project, question, commit, warnings):
    # Exact declaration names locate implementations before TF-IDF examples.
    names = list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", question)))
    if not names:
        return []
    from .structure import build_structure
    structure = build_structure(project, commit=commit)
    warnings.extend(structure["warnings"])
    declarations = [symbol for module in structure["modules"] for symbol in module["symbols"]]
    declarations.sort(key=lambda s: any(p == "tests" or p.startswith("test_") for p in Path(s["evidence"]["path"]).parts))
    ranges = []
    for name in names:
        for symbol in declarations:
            if name.casefold() not in (symbol["name"].casefold(), symbol["lexical_name"].casefold()):
                continue
            e = symbol["evidence"]
            end = min(e["end_line"], e["start_line"] + 199)
            if end < e["end_line"]:
                warnings.append({"code": "symbol_context_partial", "path": e["path"]})
            ranges.append((e["path"], e["start_line"], end))
            if len(ranges) >= MAX_SOURCES:
                return ranges
    return ranges


def _merge_ranges(ranges):
    """Merge overlapping/adjacent windows while preserving first priority."""
    result = []
    for path, start, end in ranges:
        first = len(result)
        changed = True
        while changed:
            changed = False
            for index, (previous, left, right) in enumerate(result):
                if (previous == path and max(start, left) <= min(end, right) + 1
                        and max(end, right) - min(start, left) < 200):
                    first = min(first, index)
                    start, end = min(start, left), max(end, right)
                    result.pop(index)
                    changed = True
                    break
        result.insert(first, (path, start, end))
    return result


def prepare_context(project: dict, question: str, *, keywords=None, files=None, state_dir=None, retrieval="keyword") -> dict:
    if not question.strip() or len(question) > 2000:
        raise FileReadError("invalid_question", "问题须为 1—2000 字符的非空文本。")
    if any(re.search(pattern, question.encode("utf-8")) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_question", "问题中包含已识别凭据，未发送。")
    keywords = keywords or []
    files = files or []
    if len(keywords) > 3 or len(files) > 5:
        raise FileReadError("context_input_limit", "最多指定 3 个关键词和 5 个文件。")
    budget = context_budget()
    state = inspect_repository(project["root_path"])
    commit = selected_commit(project, state)
    result = {"project_id": project["id"], "question": question, "commit": commit,
              "sources": [], "warnings": []}
    seen = set()
    size = 0
    if commit != state.head_commit:
        result["warnings"].append({"code": "managed_version_selected"})

    if state_dir is not None:
        from .knowledge import KnowledgeStore
        for note in KnowledgeStore(state_dir).for_context(project["id"], commit):
            source = {"id": f"E{len(result['sources']) + 1}",
                      "evidence": {"kind": "confirmed_knowledge", "knowledge_id": note["id"],
                                   "scope": note["scope"], "observed_commit": note["observed_commit"],
                                   "confirmed_at": note["confirmed_at"]},
                      "title": note["title"], "body": note["body"]}
            added = len(json.dumps(source, ensure_ascii=False))
            if len(result["sources"]) < MAX_SOURCES and size + added <= budget:
                result["sources"].append(source)
                size += added
            else:
                result["warnings"].append({"code": "knowledge_context_budget"})

        from .cognition_store import CognitionStore
        for note in CognitionStore(state_dir).for_context(project["id"], commit, query=question):
            source = {"id": f"E{len(result['sources']) + 1}",
                      "evidence": {"kind": "auto_reviewed_project_knowledge", "run_id": note["run_id"],
                                   "finding_id": note["id"], "observed_commit": note["commit"],
                                   "finding_sha256": note["finding_sha256"]},
                      "title": "项目认知 · 自动核对", "body": note["body"],
                      "provenance": compact_provenance(note["evidence"])}
            added = len(json.dumps(source, ensure_ascii=False))
            if len(result["sources"]) < MAX_SOURCES and size + added <= budget:
                result["sources"].append(source)
                size += added
            else:
                result["warnings"].append({"code": "cognition_context_budget"})

    deferred_notes = []
    if retrieval == "hybrid":
        deferred_notes, result["sources"], size = result["sources"], [], 0

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
        from .document_layout import shared_document, legacy_document
        if shared_document(path):
            source["document_role"] = "shared_project_document_not_independent_runtime_proof"
        elif legacy_document(path):
            source["document_role"] = "legacy_reference_not_maintained_by_zest_check_shared_documents_and_code"
        added = len(json.dumps(source, ensure_ascii=False))
        if size + added > budget:
            result["warnings"].append({"code": "context_budget", "path": path})
            return
        result["sources"].append(source)
        size += added

    if retrieval not in ("keyword", "lexical", "hybrid"):
        raise FileReadError("invalid_retrieval", "不支持的检索方式。")
    ranges = [(path, 1, 60) for path in files]
    if retrieval in ("lexical", "hybrid"):
        if state_dir is None:
            raise FileReadError("index_missing", "词法检索需要指定索引状态目录。")
        from .index import RetrievalIndex
        if retrieval == "hybrid":
            from .hybrid import search as hybrid_search
            symbols = _symbol_ranges(project, question, commit, result["warnings"])
            matches, result["retrieval_path"] = hybrid_search(project, question, state_dir, commit, result["warnings"], symbols=symbols)
        else:
            ranked = RetrievalIndex(state_dir).search(project, question, commit=commit, limit=5)
            matches = ranked["matches"]
        if retrieval == "lexical":
            ranges.extend(_symbol_ranges(project, question, commit, result["warnings"]))
        for item in matches:
            e = item["evidence"]
            ranges.append((e["path"], e["start_line"], e["end_line"]))
        if retrieval == "lexical" and ranked["metadata"]["skipped_counts"]:
            result["warnings"].append({"code": "index_skipped_sources"})
    for path, start, end in _merge_ranges(ranges):
        add(path, start, end)
    for source in deferred_notes:
        source["id"] = f"E{len(result['sources']) + 1}"
        added = len(json.dumps(source, ensure_ascii=False))
        if len(result["sources"]) < MAX_SOURCES and size + added <= budget:
            result["sources"].append(source)
            size += added
        else:
            result["warnings"].append({"code": "knowledge_context_budget"})
    from .documents import document_ranges
    for path, start, end in document_ranges(project, question, commit):
        add(path, start, end)
    for path in ["README.md", "pyproject.toml", "package.json"]:
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
    try:
        answer = validate_answer(response["text"], context["sources"])
    except FileReadError as exc:
        exc.model_response = response_metadata(response)
        raise
    known = {source["id"]: source for source in context["sources"]}
    return {**answer, "commit": context["commit"], "model_called": True,
            "evidence": [known[item] for item in dict.fromkeys(answer["citations"])],
            "model": response_metadata(response),
            "warnings": context["warnings"],
            **({"retrieval_path": context["retrieval_path"]} if "retrieval_path" in context else {})}


def model_payload(context: dict) -> dict:
    payload = {"question": context["question"], "sources": context["sources"],
               "limitations": sorted({item["code"] for item in context["warnings"]})}
    if "execution_policy" in context:
        payload["execution_policy"] = context["execution_policy"]
    if "purpose" in context:
        payload["purpose"] = context["purpose"]
    if "conversation_context" in context:
        payload["conversation_context"] = context["conversation_context"]
    return payload
