"""Route a Web message through bounded, compiled LangGraph subgraphs."""

import re
from typing import TypedDict

from .files import CREDENTIAL_PATTERNS, FileReadError
from .models import DeepSeekModel, ModelSettings


class RouterState(TypedDict, total=False):
    question: str
    history: dict
    route: str
    optimized_prompt: str
    strategy: str
    purpose: str
    files: list[str]


def route_message(question, history, *, model=None, dispatch=None):
    """Call the router once; downstream agents retain the original request.

    No generated path or authorization is accepted from the model. An invalid
    response fails closed, rather than guessing a potentially mutating route.
    """
    from langgraph.graph import END, START, StateGraph
    from langsmith import tracing_context
    from .model_outputs import RouterResponse

    provider = model or DeepSeekModel(ModelSettings.from_env())

    def classify(state):
        response = provider.complete({"question": state["question"], "conversation_context": state["history"]}, purpose="router")
        if response.get("structured_status") != "valid":
            raise FileReadError("invalid_router_output", "路由模型未返回有效结构化结果，本轮未继续执行。")
        try:
            result = RouterResponse.model_validate_json(response.get("text", ""))
        except ValueError as exc:
            raise FileReadError("invalid_router_output", "路由模型结果不符合契约，本轮未继续执行。") from exc
        prompt = result.optimized_prompt.strip()
        if not prompt or len(prompt) > 2000 or any(re.search(p, prompt.encode()) for p in CREDENTIAL_PATTERNS):
            raise FileReadError("invalid_router_output", "路由模型生成的需求为空、过长或包含敏感内容，本轮未继续执行。")
        return {"route": result.route, "optimized_prompt": prompt}

    def make_subgraph(node):
        graph = StateGraph(RouterState)
        graph.add_node("specialist", node)
        graph.add_edge(START, "specialist")
        graph.add_edge("specialist", END)
        return graph.compile()

    def handoff(state, purpose, strategy, files=None):
        options = {"purpose": purpose, "strategy": strategy, "files": files or []}
        if dispatch is not None:
            dispatch({**options, "route": state["route"], "optimized_prompt": state["optimized_prompt"]})
        return options

    def question_agent(state):
        return handoff(state, "question", "single")

    def investigation_agent(state):
        return handoff(state, "question", "multi")

    def modification_agent(state):
        # Only explicit paths in the current user message can delimit edits.
        paths = list(dict.fromkeys(re.findall(r"(?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go|rs|md|rst|json|yaml|yml)\b", state["question"])))
        if not paths or len(paths) > 5:
            raise FileReadError("repair_files_required", "请明确指定 1—5 个需要修改的已有文件；目前仅生成候选方案，需单独审批。")
        return handoff(state, "repair_proposal", "single", paths)

    graph = StateGraph(RouterState)
    graph.add_node("router", classify)
    graph.add_node("qa", make_subgraph(question_agent))
    graph.add_node("investigation", make_subgraph(investigation_agent))
    graph.add_node("modification", make_subgraph(modification_agent))
    graph.add_edge(START, "router")
    graph.add_conditional_edges("router", lambda state: state["route"],
                                {name: name for name in ("qa", "investigation", "modification")})
    for name in ("qa", "investigation", "modification"):
        graph.add_edge(name, END)
    with tracing_context(enabled=False):
        state = graph.compile().invoke({"question": question, "history": history})
    return {key: state[key] for key in ("route", "optimized_prompt", "purpose", "strategy") } | {"files": state.get("files", [])}
