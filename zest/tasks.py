"""Persistent LangGraph question tasks with context-bound approval."""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import TypedDict
from uuid import UUID, uuid4

from .files import FileReadError
from .models import DeepSeekModel, ModelSettings
from .qa import answer_context, model_payload, prepare_context


class TaskState(TypedDict, total=False):
    task_id: str
    purpose: str
    project: dict
    question: str
    keywords: list
    files: list
    new_files: list
    ranges: list
    retrieval: str
    strategy: str
    target: dict
    context: dict
    payload_sha256: str
    status: str
    result: dict
    error: dict
    events: list


def approval_digest(context: dict, target: dict) -> str:
    approved = {"target": target, "payload": model_payload(context)}
    if context.get("purpose") == "repair_proposal":
        approved["authority"] = {key: context[key] for key in ("project_id", "commit", "workspace_head")}
    data = json.dumps(approved,
                      ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


class TaskRunner:
    def __init__(self, state_dir, *, model=None):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "tasks.sqlite3"
        self.model = model

    @staticmethod
    def _id(task_id):
        try:
            if str(UUID(task_id)) != task_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise FileReadError("invalid_task_id", "任务 ID 必须是完整 UUID。") from exc
        return task_id

    def _target(self):
        settings = self.model.settings if self.model is not None else ModelSettings.from_env()
        return {"provider": settings.provider, "model": settings.name, "base_url": settings.base_url}

    @contextmanager
    def _lock(self, task_id):
        self._id(task_id)
        try:
            import fcntl
        except ImportError as exc:
            raise FileReadError("unsupported_platform", "当前任务执行锁支持 macOS/Linux。") from exc
        directory = self.state_dir / "task-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(directory / f"{task_id}.lock", flags, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FileReadError("task_busy", "该任务正在其他进程中执行。") from exc
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _open(self):
        try:
            with self._open_graph() as opened:
                yield opened
        except (sqlite3.Error, OSError) as exc:
            raise FileReadError("task_storage_error", "无法读取或保存任务状态，请检查本地数据库及权限。") from exc

    @contextmanager
    def _open_graph(self):
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
            from langgraph.graph import StateGraph, START, END
            from langgraph.types import interrupt
        except ImportError as exc:
            raise FileReadError("ai_dependencies_missing", "请执行 uv sync --extra ai。") from exc
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(sqlite3.connect(self.db_path, check_same_thread=False)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS task_records (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
            conn.commit()
            saver = SqliteSaver(conn)
            saver.setup()

            def prepared(state):
                try:
                    purpose = state.get("purpose", "question")
                    if purpose == "repair_proposal":
                        from .repair_agent import prepare_repair_context
                        if state.get("strategy", "single") != "single" or state["keywords"] or state.get("retrieval", "keyword") != "keyword":
                            raise FileReadError("invalid_repair_strategy", "修复建议需要明确文件范围和单次专门 Agent 调用。")
                        context = prepare_repair_context(state["project"], state["question"],
                                                         files=state["files"], new_files=state.get("new_files", []),
                                                         ranges=state.get("ranges", []), state_dir=self.state_dir)
                    else:
                        context = prepare_context(state["project"], state["question"],
                                              keywords=state["keywords"], files=state["files"], state_dir=self.state_dir,
                                              retrieval=state.get("retrieval", "keyword"))
                    strategy = state.get("strategy", "single")
                    if strategy not in ("single", "multi"):
                        raise FileReadError("invalid_strategy", "不支持的 Agent 策略。")
                    if purpose == "question":
                        context["execution_policy"] = {"strategy": strategy, "max_model_calls": 3 if strategy == "multi" else 1}
                    digest = approval_digest(context, state["target"])
                    return {"context": context, "payload_sha256": digest, "status": "awaiting_approval",
                            "events": [{"step": "prepared", "payload_sha256": digest}]}
                except FileReadError as exc:
                    return {"status": "failed", "error": {"code": exc.code, "message": str(exc)}}

            def approved(state):
                decision = interrupt({"kind": "model_context_approval", "target": state["target"],
                                      "commit": state["context"]["commit"],
                                      "payload_sha256": state["payload_sha256"],
                                      "source_count": len(state["context"]["sources"])})
                if (not isinstance(decision, dict) or decision.get("approved") is not True):
                    return {"status": "rejected", "events": [*state["events"], {"step": "rejected"}]}
                if decision.get("payload_sha256") != state["payload_sha256"]:
                    raise FileReadError("approval_mismatch", "批准未匹配当前上下文。")
                return {"status": "approved", "events": [*state["events"], {"step": "approved"}]}

            def answered(state):
                try:
                    provider = self.model or DeepSeekModel(ModelSettings.from_env())
                    settings = provider.settings
                    target = {"provider": settings.provider, "model": settings.name, "base_url": settings.base_url}
                    if target != state["target"]:
                        raise FileReadError("model_target_changed", "模型或接口已变化，需要重新创建任务并批准。")
                    from .knowledge import KnowledgeStore
                    for source in state["context"]["sources"]:
                        evidence = source["evidence"]
                        if evidence.get("kind") == "auto_reviewed_project_knowledge":
                            from .cognition_store import CognitionStore
                            note = CognitionStore(self.state_dir).reviewed_finding(evidence["run_id"], evidence["finding_id"])
                            if (note["project_id"] != state["context"]["project_id"] or note["commit"] != state["context"]["commit"]
                                    or note["finding_sha256"] != evidence["finding_sha256"] or note["body"] != source["body"]):
                                raise FileReadError("cognition_knowledge_changed", "项目认知来源已变化，需要重新准备。")
                        if evidence.get("kind") == "confirmed_knowledge":
                            note = KnowledgeStore(self.state_dir).get(evidence["knowledge_id"])
                            if (note["status"] != "confirmed" or note["confirmed_at"] != evidence["confirmed_at"]
                                    or note["body"] != source["body"] or note["title"] != source["title"]):
                                raise FileReadError("knowledge_changed", "已批准来源中的知识已变化或失效，需要重新准备任务。")
                    if state.get("purpose", "question") == "repair_proposal":
                        from .repair_agent import generate_proposal
                        result = generate_proposal(state["context"], state_dir=self.state_dir, model=provider,
                                                   source_task_id=state["task_id"])
                    elif state.get("strategy", "single") == "multi":
                        from .agents import answer_multi_context
                        result = answer_multi_context(state["context"], allow_model=True,
                                                      state_dir=self.state_dir, model=provider)
                    else:
                        result = answer_context(state["context"], allow_model=True, model=provider)
                    return {"result": result, "status": "completed",
                            "events": [*state["events"], {"step": "completed"}]}
                except FileReadError as exc:
                    return {"status": "failed", "error": {"code": exc.code, "message": str(exc)}}
                except Exception:
                    return {"status": "failed", "error": {"code": "task_execution_failed", "message": "任务执行失败，未回显原始异常。"}}

            builder = StateGraph(TaskState)
            builder.add_node("prepare", prepared)
            builder.add_node("approval", approved)
            builder.add_node("answer", answered)
            builder.add_edge(START, "prepare")
            builder.add_conditional_edges("prepare", lambda s: "approval" if s["status"] != "failed" else END)
            builder.add_conditional_edges("approval", lambda s: "answer" if s["status"] == "approved" else END)
            builder.add_edge("answer", END)
            yield conn, builder.compile(checkpointer=saver)

    @staticmethod
    def _config(task_id):
        return {"configurable": {"thread_id": task_id}}

    def _view(self, conn, graph, task_id):
        row = conn.execute("SELECT created_at FROM task_records WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise FileReadError("task_not_found", "未找到该任务。")
        snapshot = graph.get_state(self._config(task_id))
        state = snapshot.values
        status = ("awaiting_approval" if snapshot.interrupts else
                  "pending_execution" if snapshot.next else state.get("status", "interrupted"))
        return {"task_id": task_id, "created_at": row[0], "status": status,
                "project_id": state.get("project", {}).get("id"),
                "purpose": state.get("purpose", "question"),
                "target": state.get("target"), "payload_sha256": state.get("payload_sha256"),
                "context": state.get("context"), "result": state.get("result"),
                "error": state.get("error"), "events": state.get("events", []),
                "next_steps": list(snapshot.next)}

    def create(self, project, question, *, keywords=None, files=None, retrieval="keyword", strategy="single", purpose="question", new_files=None, ranges=None):
        if purpose not in ("question", "repair_proposal"):
            raise FileReadError("invalid_task_purpose", "不支持的任务类型。")
        if (new_files or ranges) and purpose != "repair_proposal":
            raise FileReadError("invalid_task_purpose", "新文件路径只可用于明确的开发/修复建议任务。")
        from langsmith import tracing_context
        task_id = str(uuid4())
        target = self._target()
        with self._lock(task_id), self._open() as (conn, graph), tracing_context(enabled=False):
            conn.execute("INSERT INTO task_records VALUES (?, ?)", (task_id, datetime.now(timezone.utc).isoformat()))
            conn.commit()
            graph.invoke({"task_id": task_id, "purpose": purpose, "project": project, "question": question, "keywords": keywords or [],
                          "files": files or [], "new_files": new_files or [], "ranges": ranges or [], "retrieval": retrieval, "strategy": strategy,
                          "target": target, "status": "created"},
                         self._config(task_id), durability="sync")
            return self._view(conn, graph, task_id)

    def show(self, task_id):
        self._id(task_id)
        if not self.db_path.exists():
            raise FileReadError("task_not_found", "未找到该任务。")
        with self._open() as (conn, graph):
            return self._view(conn, graph, task_id)

    def list_tasks(self):
        if not self.db_path.exists():
            return []
        with self._open() as (conn, graph):
            ids = [row[0] for row in conn.execute("SELECT id FROM task_records ORDER BY created_at DESC LIMIT 100")]
            return [{k: v for k, v in self._view(conn, graph, task_id).items() if k not in ("context", "result")}
                    for task_id in ids]

    def decide(self, task_id, *, approved, payload_sha256=None):
        from langgraph.types import Command
        from langsmith import tracing_context
        self._id(task_id)
        if not self.db_path.exists():
            raise FileReadError("task_not_found", "未找到该任务。")
        with self._lock(task_id), self._open() as (conn, graph), tracing_context(enabled=False):
            current = self._view(conn, graph, task_id)
            if current["status"] != "awaiting_approval":
                raise FileReadError("task_not_pending", "任务当前未等待批准，不能重复执行批准。")
            digest = approval_digest(current["context"], current["target"])
            if approved and (payload_sha256 != current["payload_sha256"] or payload_sha256 != digest):
                raise FileReadError("approval_mismatch", "批准的上下文哈希不匹配，未调用模型。")
            graph.invoke(Command(resume={"approved": approved, "payload_sha256": payload_sha256}),
                         self._config(task_id), durability="sync")
            return self._view(conn, graph, task_id)
