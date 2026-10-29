"""Local, durable multi-turn conversations linked to existing approved tasks."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

from .files import CREDENTIAL_PATTERNS, FileReadError
from .resources import route_store
from .storage import ProjectRegistry
from .tasks import TaskRunner


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise FileReadError("invalid_conversation_id", "对话和消息身份必须是完整 UUID。") from exc
    return value


@route_store(projects=("create",), lists=("list_for_project",),
             records={"show": "conversation", "send": "conversation", "answer": "conversation"})
class ConversationStore:
    def __init__(self, state_dir, *, model=None):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "conversations.sqlite3"
        self.model = model

    @contextmanager
    def _open(self, *, write=False):
        if not write and not self.db_path.exists():
            raise FileReadError("conversation_not_found", "未找到该对话。")
        if self.db_path.is_symlink():
            raise FileReadError("conversation_storage_error", "对话数据库不能是符号链接。")
        if write:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        with closing(sqlite3.connect(self.db_path if write else self.db_path.as_uri() + "?mode=ro", uri=not write, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            if write:
                with conn:
                    conn.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
                    conn.execute("CREATE TABLE IF NOT EXISTS conversation_turns (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sequence INTEGER NOT NULL, question TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, UNIQUE(conversation_id,sequence))")
            yield conn

    @contextmanager
    def _lock(self, conversation_id):
        folder = self.state_dir / "conversation-locks"
        if folder.is_symlink():
            raise FileReadError("conversation_storage_error", "对话锁目录不能是符号链接。")
        folder.mkdir(exist_ok=True, mode=0o700)
        fd = os.open(folder / (identity(conversation_id) + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FileReadError("conversation_busy", "当前对话正在准备一轮消息，请稍后核对原记录。") from exc
            yield

    def create(self, project):
        timestamp = now()
        value = {"id": str(uuid4()), "project_id": project["id"], "title": "新对话", "created_at": timestamp, "updated_at": timestamp}
        with self._open(write=True) as conn, conn:
            conn.execute("INSERT INTO conversations VALUES (:id,:project_id,:title,:created_at,:updated_at)", value)
        return {**value, "turns": [], "has_earlier": False}

    def list_for_project(self, project_id):
        if not self.db_path.exists():
            return []
        with self._open() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC,id DESC LIMIT 100", (project_id,))]

    def _conversation(self, conn, conversation_id):
        row = conn.execute("SELECT * FROM conversations WHERE id=?", (identity(conversation_id),)).fetchone()
        if row is None:
            raise FileReadError("conversation_not_found", "未找到该对话。")
        return dict(row)

    def _turn(self, row):
        turn = dict(row)
        turn["error"] = json.loads(turn["error"]) if turn["error"] else None
        try:
            task = TaskRunner(self.state_dir, model=self.model).show(turn["task_id"])
            if not task.get("project_id"):
                turn["status"] = "unknown"
            else:
                turn["task"] = task
                turn["status"] = task["status"]
        except FileReadError as exc:
            if exc.code != "task_not_found":
                raise
            if turn["status"] == "preparing":
                turn["status"] = "unknown"
        return turn

    def show(self, conversation_id, *, before=None):
        with self._open() as conn:
            result = self._conversation(conn, conversation_id)
            rows = conn.execute("SELECT * FROM conversation_turns WHERE conversation_id=? AND sequence<? ORDER BY sequence DESC LIMIT 51", (conversation_id, before if before is not None else 2**63 - 1)).fetchall()
        result["has_earlier"] = len(rows) > 50
        result["turns"] = [self._turn(row) for row in reversed(rows[:50])]
        return result

    def _history(self, conversation_id):
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM conversation_turns WHERE conversation_id=? ORDER BY sequence DESC LIMIT 13", (conversation_id,)).fetchall()
        history, size, truncated = [], 0, len(rows) > 12
        for row in rows[:12]:
            turn = self._turn(row)
            task = turn.get("task", {})
            entry = {"user": turn["question"], "assistant": (task.get("result") or {}).get("answer", ""),
                     "status": turn["status"], "observed_commit": (task.get("context") or {}).get("commit"),
                     "source_paths": [source["evidence"]["path"] for source in (task.get("context") or {}).get("sources", [])
                                      if source.get("evidence", {}).get("path")][:5]}
            count = len(json.dumps(entry, ensure_ascii=False))
            if size + count > 20000:
                truncated = True
                break
            size += count
            history.append(entry)
        return {"history": list(reversed(history)), "truncated": truncated,
                "role": "conversation_history_not_project_evidence_or_execution_authorization",
                "interaction": "结合前文理解指代与开发者需求。需求含糊时先提出具体业务问题，逐轮澄清，不能自行补全关键业务规则。项目事实仅依据本轮 sources；历史版本的回答不能冒充当前事实。此轮只交流，不执行代码、提交、推送或部署。"}

    def send(self, conversation_id, question, *, message_id, automatic_answer=False):
        identity(message_id)
        if type(automatic_answer) is not bool:
            raise FileReadError('invalid_conversation_mode', '自动回答选项须为布尔值。')
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise FileReadError("invalid_question", "消息须为 1—2000 字符。")
        if any(re.search(pattern, question.encode()) for pattern in CREDENTIAL_PATTERNS):
            raise FileReadError("sensitive_question", "消息包含已识别凭据，未保存或发送。")
        with self._lock(conversation_id):
            with self._open() as conn:
                conversation = self._conversation(conn, conversation_id)
                old = conn.execute("SELECT * FROM conversation_turns WHERE id=?", (message_id,)).fetchone()
                if old:
                    if old["conversation_id"] != conversation_id or old["question"] != question:
                        raise FileReadError("conversation_message_conflict", "消息身份已绑定其他内容。")
                    return self._turn(old)
                previous = conn.execute("SELECT * FROM conversation_turns WHERE conversation_id=? ORDER BY sequence DESC LIMIT 1", (conversation_id,)).fetchone()
            if previous and self._turn(previous)["status"] in ("awaiting_approval", "approved", "pending_execution", "preparing", "unknown"):
                raise FileReadError("conversation_turn_pending", "请先处理或核对上一轮消息，再继续交流。")
            history = self._history(conversation_id)
            # Carry navigation hints forward, then re-read them at the new version.
            mentioned = re.findall(r"(?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go|rs|md|rst|json|yaml|yml)\b", question)
            paths = list(dict.fromkeys([*mentioned, *(path for entry in reversed(history["history"]) for path in entry["source_paths"])]))[:5]
            project = ProjectRegistry(self.state_dir).get_project_readonly(conversation["project_id"])
            if project is None:
                raise FileReadError("project_not_found", "对话所属项目未登记。")
            task_id = str(uuid4())
            sequence = previous["sequence"] + 1 if previous else 1
            with self._open(write=True) as conn, conn:
                conn.execute("INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)", (message_id, conversation_id, sequence, question, task_id, "preparing", None, now()))
                conn.execute("UPDATE conversations SET updated_at=?,title=CASE WHEN title='新对话' THEN ? ELSE title END WHERE id=?", (now(), question[:60], conversation_id))
            error = None
            try:
                from .web_operations import report_progress
                report_progress('sources', '正在查找本轮问题相关的项目资料')
                # An initialization index is useful immediately, before shared
                # docs have been committed. Never silently use another version.
                retrieval = "keyword"
                from .index import RetrievalIndex
                from .git import inspect_repository, selected_commit
                commit = selected_commit(project, inspect_repository(project["root_path"]))
                if commit is not None:
                    try:
                        RetrievalIndex(self.state_dir).inventory(project, commit=commit)
                        retrieval = "lexical"
                        from .hybrid import enabled as hybrid_enabled
                        if automatic_answer and hybrid_enabled():
                            retrieval = "hybrid"
                    except FileReadError as exc:
                        if exc.code not in ("index_missing", "index_stale"):
                            raise
                TaskRunner(self.state_dir, model=self.model).create(project, question, files=paths,
                                                                  task_id=task_id, conversation_context=history, retrieval=retrieval)
                status = "prepared"
            except FileReadError as exc:
                status, error = "failed", {"code": exc.code, "message": str(exc)}
            with self._open(write=True) as conn, conn:
                conn.execute("UPDATE conversation_turns SET status=?,error=? WHERE id=?", (status, json.dumps(error) if error else None, message_id))
                row = conn.execute("SELECT * FROM conversation_turns WHERE id=?", (message_id,)).fetchone()
            if automatic_answer and not error:
                return self._answer_prepared(conversation, row)
            return self._turn(row)

    def _answer_prepared(self, conversation, row):
        """The submitted message authorizes its exact read-only answer, once.

        Use the existing hash-bound decision and checkpoint; never bypass it or
        resume a started/failed/unknown execution when receiving a duplicate.
        """
        turn = self._turn(row)
        task = turn.get('task') or {}
        if task.get('status') != 'awaiting_approval':
            return turn
        if (task.get('project_id') != conversation['project_id'] or task.get('purpose') != 'question'
                or task.get('question') != row['question']
                or (task.get('context') or {}).get('project_id') != conversation['project_id']):
            raise FileReadError('conversation_task_mismatch', '消息与只读问答任务不匹配，未自动执行。')
        from .web_operations import report_progress
        report_progress('answer', '正在结合项目资料生成回答', sources=len(task['context'].get('sources', [])))
        TaskRunner(self.state_dir, model=self.model).decide(task['task_id'], approved=True,
                                                         payload_sha256=task['payload_sha256'])
        return self._turn(row)

    def answer(self, conversation_id, *, message_id):
        """Explicit continuation for an older unsent conversation message."""
        identity(message_id)
        with self._lock(conversation_id):
            with self._open() as conn:
                conversation = self._conversation(conn, conversation_id)
                row = conn.execute('SELECT * FROM conversation_turns WHERE id=? AND conversation_id=?',
                                   (message_id, conversation_id)).fetchone()
            if row is None:
                raise FileReadError('conversation_message_not_found', '未找到这条对话消息。')
            return self._answer_prepared(conversation, row)
