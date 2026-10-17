"""Durable, at-most-once dispatch for local Web operations.

A receipt committed before dispatch prevents HTTP retries from repeating side effects.
Interrupted running receipts are unknown, never automatically replayed.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from uuid import UUID

from .files import FileReadError
from .git import RepositoryError
from .storage import StorageError


def now():
    return datetime.now(timezone.utc).isoformat()


class WebOperations:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.path = self.state_dir / "web-operations.sqlite3"

    @contextmanager
    def open(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("""CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, action TEXT NOT NULL,
                parameters TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, result TEXT, error TEXT)""")
            conn.commit()
            yield conn

    def submit(self, request_id, action, parameters):
        from .web_commands import validate_command
        request_id = str(UUID(str(request_id)))
        parameters = validate_command(action, parameters)
        encoded = json.dumps(parameters, ensure_ascii=True, sort_keys=True)
        digest = hashlib.sha256((action + "\n" + encoded).encode()).hexdigest()
        with self.open() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM operations WHERE id=?", (request_id,)).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    raise FileReadError("request_identity_conflict", "该请求身份已用于其他内容，未重复执行。")
                return self.view(row), False
            conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)",
                         (request_id, digest, action, encoded, "queued", now(), now(), None, None))
        return self.show(request_id), True

    def view(self, row):
        result = dict(row)
        for key in ("parameters", "result", "error"):
            result[key] = json.loads(result[key]) if result[key] else None
        if result["status"] == "running":
            with self.lock(result["id"], required=False) as acquired:
                if acquired:
                    result["status"] = "unknown"
                    result["error"] = {"code": "web_execution_unknown", "message": "执行已中断，结果待核对；请读取对应业务记录，不要重新提交操作。"}
        return result

    def show(self, operation_id):
        operation_id = str(UUID(str(operation_id)))
        if not self.path.exists():
            raise FileReadError("web_operation_not_found", "未找到该网页操作。")
        with self.open() as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise FileReadError("web_operation_not_found", "未找到该网页操作。")
        return self.view(row)

    def list(self):
        if not self.path.exists():
            return []
        with self.open() as conn:
            rows = conn.execute("""SELECT id,digest,action,NULL AS parameters,status,created_at,
                                 updated_at,NULL AS result,error FROM operations
                                 ORDER BY created_at DESC LIMIT 100""").fetchall()
        return [self.view(row) for row in rows]

    @contextmanager
    def lock(self, operation_id, *, required=True):
        directory = self.state_dir / "web-operation-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / (operation_id + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if required:
                    raise FileReadError("web_operation_busy", "该网页操作正在执行。")
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def launch(self, operation_id):
        if self.show(operation_id)["status"] != "queued":
            return
        try:
            process = subprocess.Popen([sys.executable, "-m", "zest.web_operations", str(self.state_dir), operation_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, cwd=Path(__file__).resolve().parent.parent)
        except OSError as exc:
            raise FileReadError("web_worker_start_failed", "操作已保存但进程未启动；可使用同一操作身份启动排队项。") from exc
        threading.Thread(target=process.wait, daemon=True).start()

    def run(self, operation_id):
        from .web_commands import execute
        with self.lock(operation_id, required=False) as acquired:
            if not acquired:
                return
            with self.open() as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
                if row is None or row["status"] != "queued":
                    return
                conn.execute("UPDATE operations SET status='running',updated_at=? WHERE id=?", (now(), operation_id))
            try:
                result = execute(self.state_dir, row["action"], json.loads(row["parameters"]))
            except (FileReadError, RepositoryError, StorageError) as exc:
                self.finish(operation_id, "failed", error={"code": getattr(exc, "code", "state_error"), "message": str(exc)})
            except Exception:
                # An unexpected failure may follow a side effect: retain uncertainty.
                self.finish(operation_id, "unknown", error={"code": "web_execution_unknown", "message": "执行异常，结果待核对；未自动重试，请查看对应业务记录。"})
            else:
                self.finish(operation_id, "completed", result=result)

    def finish(self, operation_id, status, *, result=None, error=None):
        with self.open() as conn, conn:
            conn.execute("UPDATE operations SET status=?,updated_at=?,result=?,error=? WHERE id=?",
                         (status, now(), json.dumps(result, ensure_ascii=True), json.dumps(error, ensure_ascii=True), operation_id))


if __name__ == "__main__":
    WebOperations(sys.argv[1]).run(str(UUID(sys.argv[2])))
