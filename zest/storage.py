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
            if self.db_path.is_symlink():
                raise StorageError("项目登记数据库不能是符号链接。")
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
                for column in ("resource_dir", "legacy_state_dir", "removed_at"):
                    if column not in columns:
                        conn.execute(f"ALTER TABLE projects ADD COLUMN {column} TEXT")
                if version == 0:
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
        result = {
            "id": row["id"], "mode": row["mode"], "root_path": row["root_path"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "git": state,
        }
        if "resource_dir" in row.keys() and row["resource_dir"]:
            result.update(resource_dir=row["resource_dir"], legacy_state_dir=row["legacy_state_dir"])
        if "removed_at" in row.keys() and row["removed_at"]:
            result["removed_at"] = row["removed_at"]
        return result

    def register(self, state: GitState, *, mode: str = "import", workspace: bool = False) -> dict:
        if mode not in ("import", "development"):
            raise StorageError("项目模式不受支持。")
        from .resources import preflight_workspace
        identity = preflight_workspace(Path(state.root_path)) if workspace else None
        if identity:
            return self._reuse_workspace(Path(state.root_path), identity)
        if workspace:
            # Recognized pre-workspace catalogs are reused, never assigned a new
            # identity merely because the caller selected a different catalog.
            legacy = ProjectRegistry(Path(state.root_path) / ".zest").list_projects_readonly()
            previous = next((p for p in legacy if p["root_path"] == state.root_path), None)
            if previous and self.state_dir != Path(state.root_path) / ".zest":
                raise StorageError("此项目仍使用根 .zest 历史登记，请从原登记入口接入，未改写其管理来源。")
        registration_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("""
                    INSERT INTO projects (id, root_path, created_at, updated_at, git_state, mode)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(root_path) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        git_state = excluded.git_state,
                        removed_at = NULL
                """, (registration_id, state.root_path, now, now,
                      json.dumps(asdict(state), ensure_ascii=True), mode))
                row = conn.execute("SELECT * FROM projects WHERE root_path = ?", (state.root_path,)).fetchone()
                result = self._record(row)
            return self.attach_workspace(result) if workspace else result
        except sqlite3.Error as exc:
            raise StorageError("保存项目状态失败，请检查数据库权限或稍后重试。") from exc

    def _reuse_workspace(self, root, identity):
        """Validate existing ownership read-only; only attach a missing catalog."""
        from .files import FileReadError
        destination = root / ".zest" / "state"
        previous = ProjectRegistry(destination).get_project_readonly(identity["project_id"])
        if (previous is None or previous["root_path"] != str(root)
                or previous.get("resource_dir") != str(destination)
                or previous.get("legacy_state_dir") != identity["legacy_state_dir"]):
            raise FileReadError("resource_identity_mismatch", "已有 .zest 登记不完整或身份冲突，未重新初始化；请核对原管理状态。")
        registrations = self.list_projects_readonly(include_removed=True)
        for registered in registrations:
            if registered["id"] == previous["id"] or registered["root_path"] == str(root):
                if (registered["id"] != previous["id"] or registered["root_path"] != str(root)
                        or (registered.get("resource_dir"), registered.get("legacy_state_dir"))
                        not in ((None, None), (str(destination), identity["legacy_state_dir"]))):
                    raise StorageError("登记入口与已有项目身份冲突，未覆盖任何管理状态。")
                if registered.get("resource_dir"):
                    if registered.get("removed_at"):
                        with closing(self._connect()) as conn, conn:
                            conn.execute("UPDATE projects SET removed_at=NULL WHERE id=?", (previous["id"],))
                    return {k: v for k, v in previous.items() if k != "removed_at"}
                break
        # A newly selected catalog gets a pointer/mirror of the original identity;
        # the authoritative project-local registration is never opened writable.
        try:
            with closing(self._connect()) as conn, conn:
                saved = conn.execute("""INSERT INTO projects(id,root_path,created_at,updated_at,git_state,mode,resource_dir,legacy_state_dir)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(root_path) DO UPDATE SET
                    resource_dir=excluded.resource_dir,legacy_state_dir=excluded.legacy_state_dir,removed_at=NULL
                    WHERE projects.id=excluded.id
                    AND (projects.resource_dir IS NULL OR projects.resource_dir=excluded.resource_dir)
                    AND (projects.legacy_state_dir IS NULL OR projects.legacy_state_dir=excluded.legacy_state_dir)""",
                    (previous["id"], str(root), previous["created_at"], previous["updated_at"],
                     json.dumps(previous["git"]), previous["mode"], str(destination), identity["legacy_state_dir"]))
                if saved.rowcount != 1:
                    raise StorageError("登记入口在连接期间发生身份冲突，未覆盖记录。")
        except sqlite3.Error as exc:
            raise StorageError("连接已有项目登记失败，原项目资源未修改。") from exc
        return {k: v for k, v in previous.items() if k != "removed_at"}

    def attach_workspace(self, project):
        from .resources import ensure_workspace, preflight_workspace
        from .git import inspect_repository
        identity = preflight_workspace(Path(project["root_path"]))
        if identity:
            if identity["project_id"] != project["id"]:
                raise StorageError("已有 .zest 属于另一项目身份，未覆盖。")
            return self._reuse_workspace(Path(project["root_path"]), identity)
        destination, legacy = ensure_workspace(project, self.state_dir)
        project = {**project, "resource_dir": str(destination), "legacy_state_dir": str(legacy),
                   "git": asdict(inspect_repository(project["root_path"]))}
        local = ProjectRegistry(destination)
        with closing(local._connect()) as conn, conn:
            existing = conn.execute("SELECT id FROM projects WHERE root_path=?", (project["root_path"],)).fetchone()
            if existing and existing["id"] != project["id"]:
                raise StorageError("项目资源登记身份冲突，未覆盖既有记录。")
            conn.execute("""INSERT INTO projects(id,root_path,created_at,updated_at,git_state,mode,resource_dir,legacy_state_dir)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(root_path) DO UPDATE SET
                updated_at=excluded.updated_at,git_state=excluded.git_state,
                resource_dir=excluded.resource_dir,legacy_state_dir=excluded.legacy_state_dir""",
                (project["id"], project["root_path"], project["created_at"], project["updated_at"], json.dumps(project["git"]),
                 project["mode"], str(destination), str(legacy)))
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE projects SET resource_dir=?,legacy_state_dir=?,git_state=? WHERE id=?",
                         (str(destination), str(legacy), json.dumps(project["git"]), project["id"]))
        return project

    def remove_project(self, project_id: str) -> dict:
        """Hide only this catalog entry; keep identity, resources and running work."""
        from .files import FileReadError
        if self.get_project_readonly(project_id) is None:
            raise FileReadError("project_not_found", "未找到该项目。")
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("UPDATE projects SET removed_at=? WHERE id=? AND removed_at IS NULL",
                             (datetime.now(timezone.utc).isoformat(), project_id))
        except sqlite3.Error as exc:
            raise StorageError("移除项目失败，原项目文件和记录均已保留。") from exc
        return {"project_id": project_id, "removed": True, "files_deleted": False, "records_preserved": True}

    def list_projects_readonly(self, *, include_removed: bool = False) -> list[dict]:
        if not self.db_path.exists():
            return []
        if self.db_path.is_symlink():
            raise StorageError("只读项目状态不能是符号链接。")
        try:
            with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise StorageError("本地项目状态版本不兼容。")
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
                visible = " WHERE removed_at IS NULL" if "removed_at" in columns and not include_removed else ""
                return [self._record({"mode": "import", **dict(row)}) for row in
                        conn.execute("SELECT * FROM projects" + visible + " ORDER BY created_at,id")]
        except sqlite3.Error as exc:
            raise StorageError("只读项目状态失败。") from exc

    def list_projects(self) -> list[dict]:
        if not self.db_path.exists():
            return []
        try:
            with closing(self._connect()) as conn:
                return [self._record(row) for row in conn.execute("SELECT * FROM projects WHERE removed_at IS NULL ORDER BY created_at, id")]
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
