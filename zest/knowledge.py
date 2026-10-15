"""Reviewed project notes, distinct from conversation and graph checkpoints."""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

from .files import CREDENTIAL_PATTERNS, FileReadError
from .git import inspect_repository, selected_commit, verify_commit
from .resources import route_store


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FileReadError("invalid_knowledge_text", "知识标题或正文为空或超出长度限制。")
    if any(re.search(pattern, value.encode("utf-8")) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_knowledge", "知识内容包含已识别凭据，未保存或使用。")
    return value


@route_store(projects=("add",), lists=("list_notes", "management_records"), contexts=("for_context",),
             records={name: "knowledge" for name in ("get", "confirm", "invalidate")},
             stable={"add": ("knowledge", "note_id")})
class KnowledgeStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "knowledge.sqlite3"

    @contextmanager
    def _open(self, *, write=True):
        try:
            if not write:
                if self.db_path.is_symlink():
                    raise FileReadError("knowledge_storage_error", "知识数据库不能是符号链接。")
                with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                    conn.row_factory = sqlite3.Row
                    if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                        raise FileReadError("knowledge_version_error", "知识数据库版本不兼容。")
                    yield conn
                return
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise FileReadError("knowledge_version_error", "知识数据库版本不兼容。")
                conn.execute("PRAGMA foreign_keys = ON")
                with conn:
                    conn.execute("""CREATE TABLE IF NOT EXISTS notes (
                        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                        kind TEXT NOT NULL, scope TEXT NOT NULL, observed_commit TEXT,
                        status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        confirmed_at TEXT, invalidation_reason TEXT)""")
                    conn.execute("""CREATE TABLE IF NOT EXISTS note_events (
                        id INTEGER PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(id),
                        action TEXT NOT NULL, created_at TEXT NOT NULL)""")
                    if version == 0:
                        conn.execute("PRAGMA user_version = 1")
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise FileReadError("knowledge_storage_error", "无法访问知识数据库，请检查权限及完整性。") from exc

    @staticmethod
    def _record(conn, note_id):
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise FileReadError("knowledge_not_found", "未找到该知识记录。")
        record = dict(row)
        record["events"] = [dict(event) for event in conn.execute(
            "SELECT action,created_at FROM note_events WHERE note_id=? ORDER BY id", (note_id,))]
        return record

    def add(self, project, title, body, *, kind="decision", scope="snapshot", note_id=None, observed_commit=None):
        _text(title, 200)
        _text(body, 8000)
        if kind not in ("decision", "constraint", "maintenance") or scope not in ("snapshot", "project"):
            raise FileReadError("invalid_knowledge_scope", "知识类型或适用范围不受支持。")
        state = inspect_repository(project["root_path"])
        commit = selected_commit(project, state)
        if observed_commit is not None:
            if not isinstance(observed_commit, str) or not verify_commit(Path(state.root_path), observed_commit):
                raise FileReadError("invalid_commit", "历史知识版本必须是本项目的完整 commit。")
            commit = observed_commit
        if scope == "snapshot" and commit is None:
            raise FileReadError("knowledge_snapshot_required", "版本知识需要至少一个已提交版本。")
        if note_id is not None:
            try:
                if str(UUID(note_id)) != note_id:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as exc:
                raise FileReadError("invalid_knowledge_id", "固定知识 ID 必须是完整 UUID。") from exc
        note_id, now = note_id or str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self._open() as conn, conn:
            old = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            if old is not None:
                if (old["project_id"], old["title"], old["body"], old["kind"], old["scope"], old["observed_commit"]) != (
                        project["id"], title, body, kind, scope, commit):
                    raise FileReadError("knowledge_id_conflict", "同一来源 ID 已有不同知识，不覆盖原记录。")
                return self._record(conn, note_id)
            conn.execute("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (note_id, project["id"], title, body, kind, scope, commit, "draft", now, now, None, None))
            conn.execute("INSERT INTO note_events(note_id,action,created_at) VALUES (?,?,?)", (note_id, "created", now))
            return self._record(conn, note_id)

    def get(self, note_id):
        if not self.db_path.exists():
            raise FileReadError("knowledge_not_found", "未找到该知识记录。")
        with self._open(write=False) as conn:
            return self._record(conn, note_id)

    def list_notes(self, project_id):
        if not self.db_path.exists():
            return []
        with self._open(write=False) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM notes WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,))]

    def management_records(self, project_id):
        """Full read-only inventory; context ranking/history limits do not apply."""
        if not self.db_path.exists():
            return []
        with self._open(write=False) as conn:
            records = [dict(row) for row in conn.execute(
                "SELECT * FROM notes WHERE project_id=? ORDER BY created_at DESC,id", (project_id,))]
        for record in records:
            _text(record["title"], 200)
            _text(record["body"], 8000)
            if record["status"] not in ("draft", "confirmed", "invalidated") or record["scope"] not in ("snapshot", "project"):
                raise FileReadError("knowledge_record_corrupt", "项目记忆状态或范围损坏，未生成总览。")
        return records

    def confirm(self, note_id):
        now = datetime.now(timezone.utc).isoformat()
        with self._open() as conn, conn:
            changed = conn.execute("UPDATE notes SET status='confirmed',confirmed_at=?,updated_at=? WHERE id=? AND status='draft'",
                                   (now, now, note_id)).rowcount
            if not changed:
                raise FileReadError("knowledge_not_draft", "只有存在的草稿可以确认。")
            conn.execute("INSERT INTO note_events(note_id,action,created_at) VALUES (?,?,?)", (note_id, "confirmed", now))
            return self._record(conn, note_id)

    def invalidate(self, note_id, reason):
        _text(reason, 500)
        now = datetime.now(timezone.utc).isoformat()
        with self._open() as conn, conn:
            changed = conn.execute("UPDATE notes SET status='invalidated',invalidation_reason=?,updated_at=? WHERE id=? AND status!='invalidated'",
                                   (reason, now, note_id)).rowcount
            if not changed:
                raise FileReadError("knowledge_not_active", "记录不存在或已经失效。")
            conn.execute("INSERT INTO note_events(note_id,action,created_at) VALUES (?,?,?)", (note_id, "invalidated", now))
            return self._record(conn, note_id)

    def for_context(self, project_id, commit):
        if not self.db_path.exists():
            return []
        with self._open(write=False) as conn:
            records = [dict(row) for row in conn.execute("""SELECT * FROM notes
                WHERE project_id=? AND status='confirmed' AND (scope='project' OR observed_commit=?)
                ORDER BY updated_at DESC LIMIT 3""", (project_id, commit))]
            for note in records:
                _text(note["title"], 200)
                _text(note["body"], 8000)
            return records
