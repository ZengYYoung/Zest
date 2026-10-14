"""Approved managed-source promotion/rollback using atomic Git ref transactions.

This selects Zest's source version. Application deployments and user worktrees stay separate.
"""

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .files import FileReadError
from .git import inspect_repository, managed_ref, read_direct_ref, verify_commit
from .repairs import RepairStore, _git_write, digest
from .storage import ProjectRegistry
from .verification import VerificationStore, _candidate, collect_snapshot, COMMAND, POLICY, baseline_strategy


SOURCE_POLICY = {"scope": "zest_managed_source", "worktree_checkout": False, "deploy_runtime": False,
                 "ref_transaction": True, "requires_explicit_approval": True}


def _validated_verification_data(state_dir, verification_id):
    verification = VerificationStore(state_dir).show(verification_id)
    plan, results = verification["plan"], verification["result"]
    if (verification["status"] != "passed" or plan.get("backend") != "docker"
            or plan.get("command") != ["python", *COMMAND] or plan.get("policy") != POLICY
            or not isinstance(results, dict)):
        raise FileReadError("release_verification_required", "切换需要真实 Docker 后端通过的兼容验证，离线夹具不能用于切换。")
    candidate, baseline = results.get("candidate", {}), results.get("baseline", {})
    if (not isinstance(candidate, dict) or not isinstance(baseline, dict)
            or candidate.get("status") != "passed" or type(candidate.get("exit_code")) is not int
            or candidate.get("exit_code") != 0
            or type(candidate.get("tests_run")) is not int or candidate["tests_run"] < 1
            or candidate.get("oom_killed") is not False or candidate.get("output_truncated") is not False
            or candidate.get("policy_verified") is not True or candidate.get("cleanup_ok") is not True
            or baseline.get("status") not in ("passed", "failed", "not_applicable") or baseline.get("cleanup_ok") is not True):
        raise FileReadError("release_verification_required", "验证结果不满足版本切换条件。")
    if not any(event.get("step") == "approved" and event.get("plan_sha256") == verification["plan_sha256"]
               for event in verification["events"]):
        raise FileReadError("release_verification_required", "验证缺少匹配的批准事件。")
    repair = RepairStore(state_dir).show(plan["repair_id"])
    root, base, target = _candidate(repair)
    if (plan["root_path"] != str(root) or plan["base_commit"] != base or plan["candidate_commit"] != target
            or plan["repair_plan_sha256"] != repair["plan_sha256"]):
        raise FileReadError("release_evidence_changed", "验证与批准候选版本不匹配。")
    for commit, key in ((base, "base_manifest"), (target, "candidate_manifest")):
        manifest, _ = collect_snapshot(root, commit)
        if manifest != plan[key]:
            raise FileReadError("release_evidence_changed", "验证来源清单与 Git 对象不匹配。")
    strategy = baseline_strategy(repair, plan["base_manifest"])
    if plan.get("baseline_strategy", "run") != strategy:
        raise FileReadError("release_verification_required", "基线策略与批准修改不匹配。")
    if ((strategy == "run" and baseline["status"] not in ("passed", "failed"))
            or (strategy != "run" and (baseline["status"] != "not_applicable"
                or baseline.get("container_started") is not False or baseline.get("tests_run") != 0))):
        raise FileReadError("release_verification_required", "基线缺少真实结果或明确的初始创建声明。")
    return verification, repair


def _validated_verification(state_dir, verification_id):
    try:
        return _validated_verification_data(state_dir, verification_id)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise FileReadError("release_verification_required", "验证证据结构损坏，未切换版本。") from exc


class ReleaseStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "releases.sqlite3"

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
                if conn.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                    raise FileReadError("release_version_error", "版本操作数据库不兼容。")
                with conn:
                    conn.execute("""CREATE TABLE IF NOT EXISTS operations (
                        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL, record TEXT NOT NULL)""")
                    conn.execute("PRAGMA user_version = 1")
                yield conn
        except (OSError, sqlite3.Error) as exc:
            raise FileReadError("release_storage_error", "无法访问版本操作状态；Git 引用可能已更新，请使用 recover 核对。") from exc

    @staticmethod
    def _save(conn, record):
        with conn:
            conn.execute("INSERT OR REPLACE INTO operations VALUES (?,?,?,?)",
                         (record["id"], record["plan"]["project_id"], record["created_at"], json.dumps(record)))

    @staticmethod
    def _record(conn, operation_id):
        row = conn.execute("SELECT record FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise FileReadError("release_not_found", "未找到版本操作。")
        try:
            record = json.loads(row[0])
            if (record["id"] != operation_id or record["status"] not in (
                    "awaiting_approval", "applying", "applied", "rejected", "failed")
                    or digest(record["plan"]) != record["plan_sha256"]):
                raise ValueError
        except (ValueError, KeyError, TypeError) as exc:
            raise FileReadError("release_record_corrupt", "版本操作记录损坏。") from exc
        if record["status"] == "applying":
            record["status"] = "pending_execution"
        return record

    def _project(self, project_id):
        project = ProjectRegistry(self.state_dir).get_project(project_id)
        if project is None:
            raise FileReadError("project_not_found", "未找到该项目。")
        return project

    def active(self, project_id):
        project = self._project(project_id)
        state = inspect_repository(project["root_path"])
        ref = managed_ref(project_id)
        current = read_direct_ref(Path(state.root_path), ref)
        return {"project_id": project_id, "root_path": state.root_path, "ref": ref,
                "managed_commit": current, "selected_commit": current or state.head_commit,
                "workspace_head": state.head_commit, "workspace_dirty": state.dirty,
                "scope": "zest_managed_source", "runtime_deployed": False}

    def _plan(self, project, target, operation):
        state = inspect_repository(project["root_path"])
        root = Path(state.root_path)
        current = read_direct_ref(root, managed_ref(project["id"]))
        previous = current or state.head_commit
        if previous is None or not verify_commit(root, target):
            raise FileReadError("release_commit_required", "版本选择需要有效的当前与目标提交。")
        if previous == target:
            raise FileReadError("release_no_change", "目标已经是当前管理版本。")
        return {"project_id": project["id"], "root_path": str(root), "operation": operation,
                "active_ref": managed_ref(project["id"]), "expected_ref_commit": current,
                "previous_commit": previous, "target_commit": target, "workspace_head": state.head_commit,
                "policy": SOURCE_POLICY}

    def _create(self, plan):
        record = {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                  "status": "awaiting_approval", "plan": plan, "plan_sha256": digest(plan),
                  "error": None, "events": [{"step": "prepared"}]}
        with self._open() as conn:
            self._save(conn, record)
        return record

    def promote(self, verification_id):
        verification, repair = _validated_verification(self.state_dir, verification_id)
        project = self._project(repair["project_id"])
        if project["root_path"] != repair["plan"]["root_path"]:
            raise FileReadError("release_project_changed", "登记项目位置与验证来源不一致。")
        plan = self._plan(project, repair["candidate_commit"], "promote")
        if plan["previous_commit"] != repair["plan"]["base_commit"]:
            raise FileReadError("release_base_changed", "候选不是从当前管理版本产生，需要重新准备修复与验证。")
        plan.update(verification_id=verification_id, verification_sha256=digest(verification),
                    repair_id=repair["id"], candidate_ref=repair["candidate_ref"])
        return self._create(plan)

    def rollback(self, applied_operation_id):
        source = self.show(applied_operation_id)
        if source["status"] != "applied":
            raise FileReadError("release_history_required", "回退需要已经完成的历史版本操作。")
        project = self._project(source["plan"]["project_id"])
        target = read_direct_ref(Path(project["root_path"]), self._history_ref(source["id"], "before"))
        if target != source["plan"]["previous_commit"] or project["root_path"] != source["plan"]["root_path"]:
            raise FileReadError("release_history_changed", "回退历史引用或项目位置已变化。")
        plan = self._plan(project, target, "rollback")
        plan.update(history_operation_id=source["id"], history_ref=self._history_ref(source["id"], "before"),
                    history_sha256=source["plan_sha256"], warning="restores_previous_source_not_runtime_health")
        return self._create(plan)

    def show(self, operation_id):
        if not self.db_path.exists():
            raise FileReadError("release_not_found", "未找到版本操作。")
        with self._open() as conn:
            return self._record(conn, operation_id)

    def list_operations(self, project_id):
        if not self.db_path.exists():
            return []
        with self._open() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM operations WHERE project_id=? ORDER BY created_at DESC LIMIT 100",
                                                (project_id,))]
            return [{"id": record["id"], "status": record["status"], "created_at": record["created_at"],
                     "operation": record["plan"]["operation"], "previous_commit": record["plan"]["previous_commit"],
                     "target_commit": record["plan"]["target_commit"], "plan_sha256": record["plan_sha256"]}
                    for record in (self._record(conn, operation_id) for operation_id in ids)]

    @staticmethod
    def _history_ref(operation_id, side):
        managed_ref(operation_id)  # Enforce UUID before constructing any ref.
        return f"refs/zest/releases/{operation_id}/{side}"

    def _check(self, plan):
        project = self._project(plan["project_id"])
        if project["root_path"] != plan["root_path"] or plan["policy"] != SOURCE_POLICY:
            raise FileReadError("release_project_changed", "版本计划与当前项目或策略不匹配。")
        state = inspect_repository(plan["root_path"])
        current = read_direct_ref(Path(state.root_path), plan["active_ref"])
        if current != plan["expected_ref_commit"] or state.head_commit != plan["workspace_head"]:
            raise FileReadError("release_base_changed", "批准后当前管理版本或工作区 HEAD 已变化。")
        if plan["operation"] == "promote":
            verification, repair = _validated_verification(self.state_dir, plan["verification_id"])
            if (digest(verification) != plan["verification_sha256"] or repair["id"] != plan["repair_id"]
                    or repair["candidate_commit"] != plan["target_commit"]
                    or repair["plan"]["base_commit"] != plan["previous_commit"]):
                raise FileReadError("release_evidence_changed", "批准后的验证证据已变化。")
        elif plan["operation"] == "rollback":
            history = self.show(plan["history_operation_id"])
            target = read_direct_ref(Path(state.root_path), plan["history_ref"])
            if (history["status"] != "applied" or history["plan_sha256"] != plan["history_sha256"]
                    or target != plan["target_commit"]):
                raise FileReadError("release_history_changed", "批准后的回退来源已变化。")
        else:
            raise FileReadError("release_record_corrupt", "未知版本操作。")

    def decide(self, operation_id, *, approved, plan_sha256=None):
        initial = self.show(operation_id)
        locks = RepairStore(self.state_dir)
        with locks._lock(initial["plan"]["project_id"]), locks._lock(operation_id), self._open() as conn:
            record = self._record(conn, operation_id)
            if record["status"] != "awaiting_approval":
                raise FileReadError("release_not_pending", "版本操作未等待批准，不能重复执行。")
            if approved and plan_sha256 != record["plan_sha256"]:
                raise FileReadError("release_approval_mismatch", "批准哈希不匹配，管理版本未改变。")
            if not approved:
                record["status"] = "rejected"
                record["events"].append({"step": "rejected"})
                self._save(conn, record)
                return record
            self._check(record["plan"])
            record["status"] = "applying"
            record["events"].append({"step": "approved", "plan_sha256": plan_sha256})
            self._save(conn, record)
            try:
                self._apply(record)
            except FileReadError as exc:
                record["error"] = {"code": exc.code, "message": str(exc)}
            # Git and SQLite cannot share a transaction: prove the Git outcome before recording it.
            self._reconcile(record)
            self._save(conn, record)
            return self._record(conn, operation_id)

    def _apply(self, record):
        plan = record["plan"]
        root = Path(plan["root_path"])
        expected = plan["expected_ref_commit"] or "0" * len(plan["target_commit"])
        directory = self.state_dir / "release-artifacts" / record["id"]
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        verify_ref = plan["candidate_ref"] if plan["operation"] == "promote" else plan["history_ref"]
        commands = (f"start\nverify {verify_ref} {plan['target_commit']}\n"
                    f"create {self._history_ref(record['id'], 'before')} {plan['previous_commit']}\n"
                    f"create {self._history_ref(record['id'], 'after')} {plan['target_commit']}\n"
                    f"update {plan['active_ref']} {plan['target_commit']} {expected}\nprepare\ncommit\n")
        _git_write(root, directory / "unused-index", "update-ref", "--no-deref", "--create-reflog",
                   "-m", f"Zest {plan['operation']} {record['id']}", "--stdin", input_bytes=commands.encode())

    def _reconcile(self, record):
        root, plan = Path(record["plan"]["root_path"]), record["plan"]
        before = read_direct_ref(root, self._history_ref(record["id"], "before"))
        after = read_direct_ref(root, self._history_ref(record["id"], "after"))
        current = read_direct_ref(root, plan["active_ref"])
        if before == plan["previous_commit"] and after == plan["target_commit"]:
            record["status"] = "applied"
            record["error"] = None
            record["events"].append({"step": "applied", "at": datetime.now(timezone.utc).isoformat(),
                                      "observed_active_commit": current})
        elif before is None and after is None and current == plan["expected_ref_commit"]:
            record["status"] = "failed"
            record["error"] = record["error"] or {"code": "release_not_applied", "message": "Git 事务未完成，未重放。"}
        else:
            record["status"] = "applying"
            record["error"] = {"code": "release_outcome_unknown", "message": "引用状态无法核对，请保留记录并检查；未自动重放。"}

    def recover(self, operation_id):
        initial = self.show(operation_id)
        locks = RepairStore(self.state_dir)
        with locks._lock(initial["plan"]["project_id"]), locks._lock(operation_id), self._open() as conn:
            record = self._record(conn, operation_id)
            if record["status"] != "pending_execution":
                raise FileReadError("release_not_recoverable", "仅未确定执行结果的操作需要核对恢复。")
            self._reconcile(record)
            self._save(conn, record)
            return self._record(conn, operation_id)
