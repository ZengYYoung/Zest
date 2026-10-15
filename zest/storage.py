"""Persist project registrations in local SQLite state owned by Zest."""

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .git import GitState


class StorageError(Exception):
    """Local state is inaccessible or incompatible."""


class ProjectRegistry:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.db_path = self.state_dir / "state.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        conn = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            # New state files contain local paths: use restrictive permissions.
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise StorageError("本地状态版本不兼容，请使用匹配的 Zest 版本。")
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS projects (
                        id TEXT PRIMARY KEY,
                        root_path TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        git_state TEXT NOT NULL,
                        mode TEXT NOT NULL DEFAULT 'import'
                    )
                """)
                # Additive v1 extension: old named INSERT/UPDATE statements remain valid.
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
                if "mode" not in columns:
                    conn.execute("ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'import'")
                conn.execute("PRAGMA user_version = 1")
            return conn
        except (OSError, sqlite3.Error, StorageError) as exc:
            if conn is not None:
                conn.close()
            if isinstance(exc, StorageError):
                raise
            raise StorageError("无法打开本地项目状态，请检查状态目录权限及数据库完整性。") from exc

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        try:
            state = json.loads(row["git_state"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageError("本地项目记录损坏。") from exc
        if row["mode"] not in ("import", "development"):
            raise StorageError("本地项目模式损坏。")
        return {
            "id": row["id"], "mode": row["mode"], "root_path": row["root_path"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "git": state,
        }

    def register(self, state: GitState, *, mode: str = "import") -> dict:
        if mode not in ("import", "development"):
            raise StorageError("项目模式不受支持。")
        now = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("""
                    INSERT INTO projects (id, root_path, created_at, updated_at, git_state, mode)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(root_path) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        git_state = excluded.git_state
                """, (str(uuid4()), state.root_path, now, now,
                      json.dumps(asdict(state), ensure_ascii=True), mode))
                row = conn.execute("SELECT * FROM projects WHERE root_path = ?", (state.root_path,)).fetchone()
                return self._record(row)
        except sqlite3.Error as exc:
            raise StorageError("保存项目状态失败，请检查数据库权限或稍后重试。") from exc

    def list_projects(self) -> list[dict]:
        if not self.db_path.exists():
            return []
        try:
            with closing(self._connect()) as conn:
                return [self._record(row) for row in conn.execute("SELECT * FROM projects ORDER BY created_at, id")]
        except sqlite3.Error as exc:
            raise StorageError("读取项目状态失败。") from exc

    def get_project(self, project_id: str) -> dict | None:
        if not self.db_path.exists():
            return None
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                return self._record(row) if row is not None else None
        except sqlite3.Error as exc:
            raise StorageError("读取项目状态失败。") from exc

    def get_project_readonly(self, project_id: str) -> dict | None:
        """Read a registration without initialization, migration, or timestamps."""
        if not self.db_path.exists():
            return None
        if self.db_path.is_symlink():
            raise StorageError("只读项目状态不能是符号链接。")
        try:
            with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise StorageError("本地项目状态版本不兼容。")
                row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                if row is None:
                    return None
                # Legacy v1 registrations predate the additive mode column.
                return self._record({"mode": "import", **dict(row)})
        except (sqlite3.Error, OSError) as exc:
            raise StorageError("只读项目状态失败。") from exc
