"""Bounded doc/code specialists and coordinator, over approved sources only."""

import asyncio
import json
import operator
from pathlib import Path, PurePosixPath
from typing import Annotated, TypedDict

from .files import FileReadError
from .knowledge import KnowledgeStore
from .models import DeepSeekModel, ModelSettings
from .qa import answer_context


async def verify_sources(context, state_dir):
    from fastmcp import Client
    from .mcp_server import create_server
    async with Client(create_server(context["project_id"], state_dir)) as client:
        for source in context["sources"]:
            e = source["evidence"]
            if e.get("kind") == "planned_new_file":
                from .repairs import assert_new_path
                from .storage import ProjectRegistry
                project = ProjectRegistry(state_dir).get_project(context["project_id"])
                if project is None or e["commit"] != context["commit"] or source["content"] != "" or source["content_sha256"] is not None:
                    raise FileReadError("source_changed", "待创建路径来源不符合批准上下文。")
                assert_new_path(Path(project["root_path"]), e["commit"], e["path"])
                continue
            if e.get("kind") == "confirmed_knowledge":
                note = KnowledgeStore(state_dir).get(e["knowledge_id"])
                if (note["project_id"] != context["project_id"] or note["status"] != "confirmed"
                        or note["body"] != source["body"] or note["title"] != source["title"]
                        or note["confirmed_at"] != e["confirmed_at"]):
                    raise FileReadError("source_changed", "已批准知识已失效或改变。")
                continue
            result = await client.call_tool("read_file", {"path": e["path"], "commit": e["commit"],
                                                          "start_line": e["start_line"], "end_line": e["end_line"]})
            record = result.data.get("data") if result.data.get("ok") else None
            if (record is None or record["evidence"] != e or record["lines"] != source["lines"]
                    or record["content_sha256"] != source["content_sha256"]
                    or ("content" in source and record["content"] != source["content"])):
                raise FileReadError("source_changed", "MCP 核对来源失败，未调用模型。")


class TeamState(TypedDict, total=False):
    context: dict
    reports: Annotated[list, operator.add]
    final: dict


def answer_multi_context(context, *, allow_model, state_dir, model=None):
    if not allow_model:
        raise FileReadError("model_permission_required", "多 Agent 需要批准来源和最多 3 次模型调用。")
    if not context["sources"]:
        return answer_context(context, allow_model=True, model=model)
    if state_dir is None:
        raise FileReadError("mcp_scope_missing", "需要绑定项目状态目录以核对来源。")
    try:
        from langgraph.graph import StateGraph, START, END
        from langsmith import tracing_context
    except ImportError as exc:
        raise FileReadError("ai_dependencies_missing", "请安装 ai 和 mcp 可选依赖。") from exc
    asyncio.run(verify_sources(context, state_dir))
    provider = model or DeepSeekModel(ModelSettings.from_env())

    def is_document(source):
        e = source["evidence"]
        return (e.get("kind") == "confirmed_knowledge" or
                PurePosixPath(e.get("path", "")).suffix.lower() in
                (".md", ".rst", ".txt", ".toml", ".json", ".xml", ".yaml", ".yml", ".ini", ".properties"))

    def specialist(role, docs):
        def run(state):
            selected = [s for s in state["context"]["sources"] if is_document(s) == docs]
            prompt = ("你负责核对需求、文档及项目约束。" if docs else "你负责分析代码实现和可能的修改影响。")
            local = {**state["context"], "sources": selected,
                     "question": prompt + "只调查原问题，不执行或修改项目：" + state["context"]["question"]}
            report = answer_context(local, allow_model=True, model=provider)
            return {"reports": [{"role": role, **report}]}
        return run

    def coordinate(state):
        reports = sorted(state["reports"], key=lambda r: r["role"])
        summaries = [{"role": r["role"], "status": r["status"], "answer": r["answer"][:2000],
                      "citations": r["citations"], "unknowns": [item[:300] for item in r["unknowns"][:5]]} for r in reports]
        local = {**state["context"], "question": state["context"]["question"] +
                 "\n请协调以下专业分析，并回到原始来源核对；专业分析不是新的事实证据：" +
                 json.dumps(summaries, ensure_ascii=False)}
        final = answer_context(local, allow_model=True, model=provider)
        calls = sum(int(r["model_called"]) for r in [*reports, final])
        usage = {key: sum(r.get("model", {}).get("usage", {}).get(key, 0) for r in [*reports, final])
                 for key in ("input_tokens", "output_tokens", "total_tokens")}
        return {"final": {**final, "strategy": "multi", "model_calls": calls,
                          "total_usage": usage, "specialists": summaries}}

    builder = StateGraph(TeamState)
    builder.add_node("docs", specialist("documentation", True))
    builder.add_node("code", specialist("code", False))
    builder.add_node("coordinate", coordinate)
    builder.add_edge(START, "docs")
    builder.add_edge(START, "code")
    builder.add_edge(["docs", "code"], "coordinate")
    builder.add_edge("coordinate", END)
    with tracing_context(enabled=False):
        state = builder.compile().invoke({"context": context, "reports": []})
    return state["final"]
