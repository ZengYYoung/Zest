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
from .models import ModelSettings
from .qa import answer_context, model_payload, prepare_context


class TaskState(TypedDict, total=False):
    project: dict
    question: str
    keywords: list
    files: list
    target: dict
    context: dict
    payload_sha256: str
    status: str
    result: dict
    error: dict
    events: list


def approval_digest(context: dict, target: dict) -> str:
    data = json.dumps({"target": target, "payload": model_payload(context)},
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
                    context = prepare_context(state["project"], state["question"],
                                              keywords=state["keywords"], files=state["files"], state_dir=self.state_dir)
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
                    if self._target() != state["target"]:
                        raise FileReadError("model_target_changed", "模型或接口已变化，需要重新创建任务并批准。")
                    from .knowledge import KnowledgeStore
                    for source in state["context"]["sources"]:
                        evidence = source["evidence"]
                        if evidence.get("kind") == "confirmed_knowledge":
                            note = KnowledgeStore(self.state_dir).get(evidence["knowledge_id"])
                            if (note["status"] != "confirmed" or note["confirmed_at"] != evidence["confirmed_at"]
                                    or note["body"] != source["body"] or note["title"] != source["title"]):
                                raise FileReadError("knowledge_changed", "已批准来源中的知识已变化或失效，需要重新准备任务。")
                    result = answer_context(state["context"], allow_model=True, model=self.model)
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
                "target": state.get("target"), "payload_sha256": state.get("payload_sha256"),
                "context": state.get("context"), "result": state.get("result"),
                "error": state.get("error"), "events": state.get("events", []),
                "next_steps": list(snapshot.next)}

    def create(self, project, question, *, keywords=None, files=None):
        from langsmith import tracing_context
        task_id = str(uuid4())
        target = self._target()
        with self._lock(task_id), self._open() as (conn, graph), tracing_context(enabled=False):
            conn.execute("INSERT INTO task_records VALUES (?, ?)", (task_id, datetime.now(timezone.utc).isoformat()))
            conn.commit()
            graph.invoke({"project": project, "question": question, "keywords": keywords or [],
                          "files": files or [], "target": target, "status": "created"},
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
