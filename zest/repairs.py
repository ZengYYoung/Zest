"""Approved exact file replacements built through an isolated Git index.

This worker creates candidate objects/refs, never checks out or executes project code.
"""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from uuid import UUID, uuid4

from .files import (CREDENTIAL_PATTERNS, FileReadError, _find_file,
                    _validate_path, load_text_blob)
from .git import git_environment, inspect_repository, selected_commit, list_tree
from .knowledge import _text


MAX_PLAN_BYTES = 320 * 1024
MAX_CHANGE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024


def digest(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _diff(old, new, path):
    rows = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                               fromfile=f"a/{path}", tofile=f"b/{path}")
    return "".join(row if row.endswith("\n") else row + "\n\\ No newline at end of file\n" for row in rows)


def validate_change_paths(paths):
    if not all(isinstance(path, str) for path in paths):
        raise FileReadError("invalid_repair_change", "路径必须是字符串。")
    prefixes, folded = {}, set()
    for path in paths:
        parts = _validate_path(path)
        if path.casefold() in folded:
            raise FileReadError("duplicate_repair_path", "方案存在重复或大小写冲突的文件。")
        folded.add(path.casefold())
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            previous = prefixes.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise FileReadError("repair_path_collision", "方案中的目录存在大小写冲突。")
    for path in folded:
        if any(other.startswith(path + "/") for other in folded):
            raise FileReadError("repair_path_collision", "方案不能同时创建文件及该文件下的子路径。")


def assert_new_path(root, commit, path):
    parts = _validate_path(path)
    tree = commit
    for index, part in enumerate(parts):
        entries = list_tree(root, tree)
        entry = next((item for item in entries if item.path == part), None)
        if any(item.path.casefold() == part.casefold() and item.path != part for item in entries):
            raise FileReadError("repair_path_collision", "新路径与既有 Git 路径存在大小写冲突。")
        if entry is None:
            return
        if index == len(parts) - 1:
            raise FileReadError("repair_path_exists", "创建路径在该版本中已经存在，不能覆盖。")
        if entry.object_type != "tree":
            raise FileReadError("unsupported_file_type", "新路径经过文件、链接或子模块，不能创建。")
        tree = entry.object_id


def _raw_content(content):
    if not isinstance(content, str):
        raise FileReadError("invalid_repair_content", "替换内容必须是 UTF-8 文本。")
    try:
        raw = content.encode("utf-8")
    except UnicodeError as exc:
        raise FileReadError("invalid_repair_content", "替换内容不能编码为 UTF-8。") from exc
    if len(raw) > MAX_CHANGE_BYTES or b"\0" in raw:
        raise FileReadError("repair_content_limit", "每个替换文件最多 64 KiB，且不能含 NUL。")
    if any(re.search(pattern, raw) for pattern in CREDENTIAL_PATTERNS):
        raise FileReadError("sensitive_repair", "替换内容含已识别凭据，未保存。")
    return raw


def anchored_content(raw, start, end, old_text, new_text):
    if (type(start) is not int or type(end) is not int or start < 1 or end < start or end - start + 1 > 200):
        raise FileReadError("invalid_edit_range", "锚点修改必须指定 1—200 行的有效范围。")
    old_anchor, replacement = _raw_content(old_text), _raw_content(new_text)
    if not old_anchor:
        raise FileReadError("invalid_edit_anchor", "旧锚点不能为空。")
    # Split only physical LF lines, preserving CRLF and every unseen byte.
    pieces = raw.split(b"\n")
    lines = [piece + b"\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    if start > len(lines) or end > len(lines):
        raise FileReadError("invalid_edit_range", "修改范围超出原文件。")
    segment = b"".join(lines[start - 1:end])
    if segment.count(old_anchor) != 1:
        raise FileReadError("edit_anchor_mismatch", "旧锚点在批准行段内必须精确匹配一次，未修改。")
    result = b"".join(lines[:start - 1]) + segment.replace(old_anchor, replacement, 1) + b"".join(lines[end:])
    _raw_content(result.decode("utf-8"))
    return result.decode("utf-8")


def prepare_plan(project, proposal):
    if (not isinstance(proposal, dict) or set(proposal) != {"summary", "changes"}
            or not isinstance(proposal["changes"], list) or not 1 <= len(proposal["changes"]) <= 5):
        raise FileReadError("invalid_repair_plan", "方案需要 summary 和 1—5 项 changes。")
    _text(proposal["summary"], 2000)
    if not all(isinstance(change, dict) for change in proposal["changes"]):
        raise FileReadError("invalid_repair_change", "每项变更必须是对象。")
    validate_change_paths([change.get("path") for change in proposal["changes"]])
    state = inspect_repository(project["root_path"])
    if state.unborn or state.dirty:
        raise FileReadError("repair_clean_base_required", "修复方案需要有提交且干净的基础工作区。")
    root = Path(state.root_path)
    base = selected_commit(project, state)
    changes, total = [], 0
    for change in proposal["changes"]:
        if (set(change) not in ({"path", "expected_sha256", "content"}, {"path", "expected_sha256", "content", "operation"},
                               {"path", "expected_sha256", "operation", "start_line", "end_line", "old_text", "new_text"})
                or not isinstance(change["path"], str)):
            raise FileReadError("invalid_repair_change", "变更需要 path、expected_sha256 和完整替换 content。")
        parts = _validate_path(change["path"])
        operation = change.get("operation", "replace")
        if operation not in ("replace", "create", "edit"):
            raise FileReadError("invalid_repair_operation", "仅支持替换、创建和行段锚点修改。")
        if operation == "edit" and "old_text" not in change:
            raise FileReadError("invalid_repair_change", "锚点操作需要行段、old_text 与 new_text。")
        if operation != "edit" and "content" not in change:
            raise FileReadError("invalid_repair_change", "创建与替换操作需要完整正文。")
        if operation == "create":
            if change["expected_sha256"] is not None:
                raise FileReadError("invalid_repair_change", "创建文件的 expected_sha256 必须为 null。")
            assert_new_path(root, base, change["path"])
            old, entry = b"", None
        else:
            entry = _find_file(root, base, parts)
            old = load_text_blob(root, entry)
        if len(old) > MAX_CHANGE_BYTES:
            raise FileReadError("repair_content_limit", "首版修复仅支持不超过 64 KiB 的既有文本文件。")
        if operation != "create" and hashlib.sha256(old).hexdigest() != change["expected_sha256"]:
            raise FileReadError("repair_source_mismatch", "原文件哈希不匹配，需要重新读取来源并准备方案。")
        content = (anchored_content(old, change["start_line"], change["end_line"], change["old_text"], change["new_text"])
                   if operation == "edit" else change["content"])
        new = _raw_content(content)
        if operation != "create" and new == old:
            raise FileReadError("repair_no_change", "替换内容与原文件相同。")
        total += len(old) + len(new)
        if total > MAX_TOTAL_BYTES:
            raise FileReadError("repair_plan_limit", "方案原文与替换正文合计超过 256 KiB。")
        diff = _diff(old.decode(), content, change["path"])
        if operation == "create":
            diff = "new file mode 100644\n" + (diff.replace(f"--- a/{change['path']}\n", "--- /dev/null\n", 1)
                                                or f"--- /dev/null\n+++ b/{change['path']}\n")
        changes.append({**change, "content": content, "mode": entry.mode if entry else "100644", "old_blob": entry.object_id if entry else None,
                        "new_sha256": hashlib.sha256(new).hexdigest(),
                        "diff": diff})
    return {"project_id": project["id"], "root_path": state.root_path,
            "base_commit": base, "workspace_head": state.head_commit,
            "summary": proposal["summary"], "changes": changes,
            "policy": {"operation": "replace_or_create_text" if any(change.get("operation") == "create" for change in changes)
                       else "bounded_anchor_edit" if any(change.get("operation") == "edit" for change in changes)
                       else "replace_existing_text", "execute_code": False,
                       "switch_version": False, "max_files": 5}}


def load_proposal(path):
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_PLAN_BYTES + 1)
        if len(raw) > MAX_PLAN_BYTES:
            raise FileReadError("repair_plan_limit", "方案 JSON 超过 320 KiB。")
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise FileReadError("invalid_repair_plan", "无法读取 UTF-8 JSON 修复方案。") from exc


def _git_write(root, index, *args, input_bytes=None):
    env = git_environment()
    env.update(GIT_INDEX_FILE=str(index), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Zest Maintenance", GIT_AUTHOR_EMAIL="zest@localhost",
               GIT_COMMITTER_NAME="Zest Maintenance", GIT_COMMITTER_EMAIL="zest@localhost")
    try:
        completed = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
             "-c", "commit.gpgSign=false", "-c", "core.splitIndex=false", "-C", str(root), *args],
            input=input_bytes, capture_output=True, env=env, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FileReadError("repair_git_failed", "候选 Git 操作失败或超时；未切换原项目版本。") from exc
    if completed.returncode:
        raise FileReadError("repair_git_failed", "候选 Git 操作失败；未切换原项目版本。")
    return completed.stdout.strip()


class RepairStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "repairs.sqlite3"

    @contextmanager
    def _open(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                    raise FileReadError("repair_version_error", "修复数据库版本不兼容。")
                with conn:
                    conn.execute("""CREATE TABLE IF NOT EXISTS repairs (
                        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL,
                        status TEXT NOT NULL, plan TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
                        candidate_commit TEXT, candidate_ref TEXT, error TEXT, events TEXT NOT NULL)""")
                    conn.execute("PRAGMA user_version = 1")
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise FileReadError("repair_storage_error", "无法访问修复数据库，请检查权限和完整性。") from exc

    @contextmanager
    def _lock(self, repair_id):
        try:
            if str(UUID(repair_id)) != repair_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise FileReadError("invalid_repair_id", "修复 ID 必须是完整 UUID。") from exc
        try:
            import fcntl
        except ImportError as exc:
            raise FileReadError("unsupported_platform", "修复执行锁需要 macOS/Linux。") from exc
        directory = self.state_dir / "repair-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / f"{repair_id}.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FileReadError("repair_busy", "修复正在其他进程中执行。") from exc
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _record(conn, repair_id):
        row = conn.execute("SELECT * FROM repairs WHERE id=?", (repair_id,)).fetchone()
        if row is None:
            raise FileReadError("repair_not_found", "未找到该修复。")
        record = dict(row)
        try:
            record["plan"] = json.loads(record["plan"])
            record["events"] = json.loads(record["events"])
            record["error"] = json.loads(record["error"]) if record["error"] else None
            if digest(record["plan"]) != record["plan_sha256"]:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise FileReadError("repair_record_corrupt", "修复记录或方案哈希损坏。") from exc
        if record["status"] == "applying":
            record["status"] = "pending_execution"
        return record

    def create(self, project, proposal, *, source_task_id=None):
        if source_task_id is not None:
            try:
                if str(UUID(source_task_id)) != source_task_id:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as exc:
                raise FileReadError("invalid_task_id", "方案来源任务 ID 必须是完整 UUID。") from exc
        plan = prepare_plan(project, proposal)
        if source_task_id is not None:
            plan["source_task_id"] = source_task_id
        repair_id, now = source_task_id or str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self._open() as conn, conn:
            conn.execute("INSERT INTO repairs VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                         (repair_id, project["id"], now, "awaiting_approval", json.dumps(plan), digest(plan),
                          None, None, None, json.dumps([{"step": "prepared", "at": now}])))
            record = self._record(conn, repair_id)
            if record["plan_sha256"] != digest(plan):
                raise FileReadError("repair_task_conflict", "同一来源任务已经产生不同方案，不覆盖原记录。")
            return record

    def show(self, repair_id):
        if not self.db_path.exists():
            raise FileReadError("repair_not_found", "未找到该修复。")
        with self._open() as conn:
            return self._record(conn, repair_id)

    def list_repairs(self, project_id):
        if not self.db_path.exists():
            return []
        with self._open() as conn:
            rows = [dict(row) for row in conn.execute("""SELECT id,created_at,status,plan_sha256,
                candidate_commit,candidate_ref FROM repairs WHERE project_id=? ORDER BY created_at DESC LIMIT 100""",
                                                      (project_id,))]
            for row in rows:
                if row["status"] == "applying":
                    row["status"] = "pending_execution"
            return rows

    def decide(self, repair_id, *, approved, plan_sha256=None):
        with self._lock(repair_id), self._open() as conn:
            current = self._record(conn, repair_id)
            if current["status"] != "awaiting_approval":
                raise FileReadError("repair_not_pending", "修复未等待批准，不能重复执行。")
            if approved and plan_sha256 != current["plan_sha256"]:
                raise FileReadError("repair_approval_mismatch", "批准哈希不匹配，未生成候选版本。")
            if not approved:
                self._finish(conn, current, "rejected")
                return self._record(conn, repair_id)
            plan = current["plan"]
            # Rebuild the approved data from its original objects before any writes.
            refreshed = prepare_plan({"id": plan["project_id"], "root_path": plan["root_path"]},
                                     {"summary": plan["summary"], "changes": [
                                         {k: change[k] for k in (("path", "expected_sha256", "operation", "start_line", "end_line", "old_text", "new_text")
                                                                 if change.get("operation") == "edit" else ("path", "expected_sha256", "content", "operation")) if k in change}
                                         for change in plan["changes"]]})
            if "source_task_id" in plan:
                refreshed["source_task_id"] = plan["source_task_id"]
            if digest(refreshed) != current["plan_sha256"]:
                raise FileReadError("repair_base_changed", "基础版本或方案已改变，需重新准备和批准。")
            current["events"].append({"step": "approved", "at": datetime.now(timezone.utc).isoformat(),
                                      "plan_sha256": plan_sha256})
            with conn:
                conn.execute("UPDATE repairs SET status='applying',events=? WHERE id=?",
                             (json.dumps(current["events"]), repair_id))
            try:
                candidate, ref = self._apply(repair_id, plan)
                current["candidate_commit"], current["candidate_ref"] = candidate, ref
                self._finish(conn, current, "candidate_ready")
            except FileReadError as exc:
                self._finish(conn, current, "failed", error={"code": exc.code, "message": str(exc)})
            return self._record(conn, repair_id)

    def _finish(self, conn, current, status, *, error=None):
        events = [*current["events"], {"step": status, "at": datetime.now(timezone.utc).isoformat()}]
        with conn:
            conn.execute("""UPDATE repairs SET status=?,candidate_commit=?,candidate_ref=?,error=?,events=? WHERE id=?""",
                         (status, current["candidate_commit"], current["candidate_ref"],
                          json.dumps(error) if error else None, json.dumps(events), current["id"]))

    def _apply(self, repair_id, plan):
        directory = self.state_dir / "repair-artifacts" / repair_id
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        index = directory / "index"
        root = Path(plan["root_path"])
        _git_write(root, index, "read-tree", plan["base_commit"])
        for change in plan["changes"]:
            blob = _git_write(root, index, "hash-object", "-w", "--stdin", input_bytes=change["content"].encode()).decode()
            row = f"{change['mode']} {blob}\t{change['path']}\0".encode()
            _git_write(root, index, "update-index", "-z", "--index-info", input_bytes=row)
        tree = _git_write(root, index, "write-tree").decode()
        candidate = _git_write(root, index, "commit-tree", tree, "-p", plan["base_commit"],
                               "-m", f"Zest approved repair {repair_id}").decode()
        state = inspect_repository(root)
        selected = selected_commit({"id": plan["project_id"]}, state)
        if (selected != plan["base_commit"] or state.head_commit != plan["workspace_head"] or state.dirty):
            raise FileReadError("repair_base_changed", "执行期间基础工作区已改变；候选未发布，原项目未修改。")
        ref = f"refs/zest/candidates/{repair_id}"
        _git_write(root, index, "update-ref", ref, candidate, "0" * len(candidate))
        return candidate, ref
