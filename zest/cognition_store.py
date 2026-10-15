"""Durable approved scopes, independent jobs, request ledger and reviewed findings."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from .cognition_contracts import canonical, COGNITION_PROMPTS, POLICY, text
from .files import FileReadError, _find_file, load_text_blob
from .git import inspect_repository, verify_commit
from .storage import ProjectRegistry
from .survey import SurveyStore, _path

MAX_ENVELOPE_BYTES = 16 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def valid_id(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise FileReadError("invalid_cognition_id", "认知工作流 ID 必须是完整 UUID。") from exc
    return value


def prepare_envelope(project, survey, target):
    from .models import output_contract, structured_base_url
    target = {**target, "base_url": structured_base_url(target["base_url"])}
    if survey["project_id"] != project["id"]:
        raise FileReadError("cognition_scope_mismatch", "摸底记录不属于当前项目。")
    state = inspect_repository(project["root_path"])
    root = Path(state.root_path)
    commit = survey["source"]["commit"]
    if commit is None or not verify_commit(root, commit):
        raise FileReadError("invalid_commit", "认知工作流需要本地有效的固定提交。")
    files = []
    for source in survey["sources"]:
        evidence = source["evidence"]
        path = evidence["path"]
        _path(path)
        entry = _find_file(root, commit, path.split("/"))
        raw = load_text_blob(root, entry)
        if (evidence["commit"] != commit or entry.object_id != evidence["blob_id"]
                or hashlib.sha256(raw).hexdigest() != evidence["content_sha256"]):
            raise FileReadError("cognition_source_changed", "资料来源身份不匹配，未创建认知请求。")
        files.append({"id": source["id"], "roles": source["roles"], "evidence": evidence, "body": raw.decode()})
    if not files:
        raise FileReadError("cognition_empty_scope", "摸底记录没有可用于调查的安全文本。")
    envelope = {"schema_version": 1, "project_id": project["id"], "survey_id": survey["id"],
                "survey_sha256": survey["record_sha256"], "commit": commit, "target": target,
                "policy": dict(POLICY), "instructions": dict(COGNITION_PROMPTS), "files": files,
                "structured_outputs": {purpose: output_contract(purpose) for purpose in COGNITION_PROMPTS},
                "survey_gaps": survey["gaps"], "survey_coverage": survey["coverage"],
                "authorization": "model_requests_over_this_fixed_material_package_only"}
    from .cognition_context import scope_review
    envelope["scope_review"] = scope_review(envelope)
    if len(canonical(envelope).encode()) > MAX_ENVELOPE_BYTES:
        raise FileReadError("cognition_scope_budget", "完整可审阅资料包超过保存预算。")
    return envelope


class CognitionStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "cognition.sqlite3"

    @contextmanager
    def open(self, *, write=False):
        try:
            if self.db_path.is_symlink():
                raise FileReadError("cognition_storage_error", "认知数据库不能是符号链接。")
            if write:
                self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
            with closing(sqlite3.connect(self.db_path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=30)) as conn:
                conn.row_factory = sqlite3.Row
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1) or (not write and version != 1):
                    raise FileReadError("cognition_version_error", "认知数据库版本不兼容。")
                if write:
                    conn.executescript("""
                    CREATE TABLE IF NOT EXISTS cognition_runs (
                        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, commit_id TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
                        envelope TEXT NOT NULL, envelope_sha256 TEXT NOT NULL,
                        plan TEXT, error TEXT, call_count INTEGER NOT NULL DEFAULT 0,
                        started_at TEXT, config_dir TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS cognition_project_time ON cognition_runs(project_id,created_at DESC);
                    CREATE TABLE IF NOT EXISTS cognition_jobs (
                        run_id TEXT NOT NULL, id TEXT NOT NULL, spec TEXT NOT NULL,
                        status TEXT NOT NULL, result TEXT, updated_at TEXT NOT NULL,
                        PRIMARY KEY(run_id,id));
                    CREATE TABLE IF NOT EXISTS cognition_calls (
                        run_id TEXT NOT NULL, actor TEXT NOT NULL, sequence INTEGER NOT NULL,
                        purpose TEXT NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL, response TEXT, error TEXT, created_at TEXT NOT NULL,
                        PRIMARY KEY(run_id,actor,sequence));
                    CREATE TABLE IF NOT EXISTS cognition_findings (
                        run_id TEXT NOT NULL, id TEXT NOT NULL, job_id TEXT NOT NULL,
                        finding TEXT NOT NULL, status TEXT NOT NULL, review TEXT,
                        PRIMARY KEY(run_id,id));
                    CREATE TABLE IF NOT EXISTS cognition_events (
                        seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL, actor TEXT NOT NULL,
                        action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS cognition_call_receipts (
                        run_id TEXT NOT NULL, actor TEXT NOT NULL, sequence INTEGER NOT NULL,
                        receipt TEXT NOT NULL, PRIMARY KEY(run_id,actor,sequence));
                    CREATE TABLE IF NOT EXISTS cognition_context_requests (
                        run_id TEXT NOT NULL, actor TEXT NOT NULL, sequence INTEGER NOT NULL,
                        request_sha256 TEXT NOT NULL, authorization TEXT NOT NULL,
                        status TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT,
                        PRIMARY KEY(run_id,actor,sequence));
                    CREATE TABLE IF NOT EXISTS cognition_request_bodies (
                        run_id TEXT NOT NULL, actor TEXT NOT NULL, sequence INTEGER NOT NULL,
                        request TEXT NOT NULL, PRIMARY KEY(run_id,actor,sequence));
                    PRAGMA user_version=1;
                    """)
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise FileReadError("cognition_storage_error", "无法读取或保存认知状态。") from exc

    def create(self, project_id, *, survey_id=None, target=None):
        project = ProjectRegistry(self.state_dir).get_project(project_id)
        if project is None:
            raise FileReadError("project_not_found", "未找到该项目。")
        survey = SurveyStore(self.state_dir).show(survey_id) if survey_id else SurveyStore(self.state_dir).build(project)
        if target is None:
            from .models import ModelSettings
            settings = ModelSettings.from_env()
            target = {"provider": settings.provider, "model": settings.name, "base_url": settings.base_url}
        envelope = prepare_envelope(project, survey, target)
        envelope["shared_knowledge"] = self.for_context(project_id, envelope["commit"])
        if len(canonical(envelope).encode()) > MAX_ENVELOPE_BYTES:
            raise FileReadError("cognition_scope_budget", "完整资料包和共享认知超过保存预算。")
        run_id, timestamp = str(uuid4()), now()
        with self.open(write=True) as conn, conn:
            conn.execute("INSERT INTO cognition_runs(id,project_id,commit_id,created_at,updated_at,status,envelope,envelope_sha256,config_dir) VALUES (?,?,?,?,?,?,?,?,?)",
                         (run_id, project_id, envelope["commit"], timestamp, timestamp, "awaiting_approval",
                          canonical(envelope), digest(envelope), str(Path.cwd())))
        self.event(run_id, "coordinator", "scope_prepared", {"files": len(envelope["files"])})
        return self.show(run_id)

    @staticmethod
    def envelope(row):
        try:
            value = json.loads(row["envelope"])
            if digest(value) != row["envelope_sha256"] or value["project_id"] != row["project_id"] or value["commit"] != row["commit_id"]:
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise FileReadError("cognition_record_corrupt", "认知授权资料包损坏或身份不一致。") from exc

    def show(self, run_id, *, include_envelope=False, include_calls=False):
        valid_id(run_id)
        if not self.db_path.exists():
            raise FileReadError("cognition_not_found", "未找到该认知工作流。")
        with self.open() as conn:
            row = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise FileReadError("cognition_not_found", "未找到该认知工作流。")
            envelope = self.envelope(row)
            result = {key: row[key] for key in ("id", "project_id", "created_at", "updated_at", "status", "call_count", "started_at")}
            result.update(commit=row["commit_id"], approval_sha256=row["envelope_sha256"], target=envelope["target"], policy=envelope["policy"],
                          file_count=len(envelope["files"]), plan=json.loads(row["plan"]) if row["plan"] else None,
                          error=json.loads(row["error"]) if row["error"] else None)
            result["scope_review"] = envelope.get("scope_review")
            result["jobs"] = [{"id": j["id"], "spec": json.loads(j["spec"]), "status": j["status"],
                               "result": json.loads(j["result"]) if j["result"] else None}
                              for j in conn.execute("SELECT * FROM cognition_jobs WHERE run_id=? ORDER BY id", (run_id,))]
            result["findings"] = [{"id": f["id"], "job_id": f["job_id"], "finding": json.loads(f["finding"]),
                                   "status": f["status"], "review": json.loads(f["review"]) if f["review"] else None}
                                  for f in conn.execute("SELECT * FROM cognition_findings WHERE run_id=? ORDER BY id", (run_id,))]
            result["events"] = [dict(event) for event in conn.execute("SELECT actor,action,detail,created_at FROM cognition_events WHERE run_id=? ORDER BY seq DESC LIMIT 100", (run_id,))][::-1]
            result["context_requests"] = self._context_records(conn, run_id)
            if include_envelope:
                result["envelope"] = envelope
            if include_calls:
                result["calls"] = [dict(call) for call in conn.execute("SELECT actor,sequence,purpose,payload,payload_sha256,status,response,error,created_at FROM cognition_calls WHERE run_id=? ORDER BY created_at,actor,sequence", (run_id,))]
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognition_call_receipts'").fetchone():
                    receipts = {(row["actor"], row["sequence"]): json.loads(row["receipt"]) for row in
                                conn.execute("SELECT * FROM cognition_call_receipts WHERE run_id=?", (run_id,))}
                    for call in result["calls"]:
                        call["receipt"] = receipts.get((call["actor"], call["sequence"]))
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognition_request_bodies'").fetchone():
                    requests = {(row["actor"], row["sequence"]): json.loads(row["request"]) for row in
                                conn.execute("SELECT * FROM cognition_request_bodies WHERE run_id=?", (run_id,))}
                    for call in result["calls"]:
                        call["request"] = requests.get((call["actor"], call["sequence"]))
        result["worker_active"] = self.worker_active(run_id)
        return result

    @staticmethod
    def _context_records(conn, run_id, *, full=False):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognition_context_requests'").fetchone():
            return []
        records = []
        for row in conn.execute("SELECT * FROM cognition_context_requests WHERE run_id=? ORDER BY actor,sequence", (run_id,)):
            try:
                value = json.loads(row["authorization"])
                request = value["request"]
                if (digest(value) != row["request_sha256"] or value["run_id"] != run_id
                        or value["actor"] != row["actor"] or value["sequence"] != row["sequence"]
                        or len(request["body_text"]) != request["chars"]
                        or hashlib.sha256(request["body_text"].encode()).hexdigest() != request["body_sha256"]
                        or not 0 < value["default_chars"] < request["chars"] <= value["max_chars"]):
                    raise ValueError
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                raise FileReadError("cognition_record_corrupt", "扩展请求身份损坏，不能审阅或执行。") from exc
            record = {key: row[key] for key in ("run_id", "actor", "sequence", "request_sha256", "status", "created_at", "decided_at")}
            chars, default = value["request"]["chars"], value["default_chars"]
            record.update(chars=chars, default_chars=default, max_chars=value["max_chars"],
                          over_percent=round((chars / default - 1) * 100, 2))
            if full:
                record["authorization"] = value
            records.append(record)
        return records

    def context_requests(self, run_id):
        self.show(run_id)
        with self.open() as conn:
            return self._context_records(conn, run_id, full=True)

    def require_context(self, run_id, actor, sequence, envelope, request):
        """Persist an unsent exact request before asking; never reserve a call."""
        authorization = {"run_id": run_id, "actor": actor, "sequence": sequence,
                         "envelope_sha256": digest(envelope), "request": request,
                         "default_chars": envelope["policy"]["max_prompt_chars"],
                         "max_chars": envelope["policy"]["max_extended_prompt_chars"]}
        identity = digest(authorization)
        with self.open(write=True) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ("planning", "investigating", "reviewing") or row["envelope_sha256"] != digest(envelope):
                raise FileReadError("approval_mismatch", "请求未匹配正在执行的固定授权包。")
            old = conn.execute("SELECT * FROM cognition_context_requests WHERE run_id=? AND actor=? AND sequence=?", (run_id, actor, sequence)).fetchone()
            if old and (old["request_sha256"] != identity or digest(json.loads(old["authorization"])) != identity):
                raise FileReadError("cognition_request_changed", "待扩展请求已变化，需要重新准备；旧批准不能放行。")
            if old is None:
                conn.execute("INSERT INTO cognition_context_requests VALUES (?,?,?,?,?,?,?,NULL)",
                             (run_id, actor, sequence, identity, canonical(authorization), "awaiting_approval", now()))
            status = old["status"] if old else "awaiting_approval"
        if status == "approved":
            return identity
        if status == "rejected":
            raise FileReadError("cognition_context_rejected", "本次完整请求的字符扩展已拒绝，未外发；保留已有证据与缺口。")
        raise FileReadError("cognition_context_approval_required", "完整请求适度超限，已保存并暂停；请审阅完整请求后批准本次扩展。")

    def decide_context(self, run_id, actor, sequence, *, approved, request_sha256):
        from .cognition import execution_lock
        valid_id(run_id)
        with execution_lock(self.state_dir, f"run-{run_id}"):
            with self.open(write=True) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                run = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (run_id,)).fetchone()
                if run is None or run["status"] not in ("awaiting_context_approval", "planning", "investigating", "reviewing"):
                    raise FileReadError("cognition_not_pending", "工作流未等待请求扩展，不能重复决定。")
                records = self._context_records(conn, run_id, full=True)
                record = next((item for item in records if item["actor"] == actor and item["sequence"] == sequence), None)
                if record is None or record["status"] != "awaiting_approval":
                    raise FileReadError("cognition_not_pending", "该请求未等待扩展批准。")
                if request_sha256 != record["request_sha256"] or record["authorization"]["envelope_sha256"] != run["envelope_sha256"]:
                    raise FileReadError("approval_mismatch", "批准未匹配固定资料包与本次完整请求。")
                self.envelope(run)
                if conn.execute("SELECT 1 FROM cognition_calls WHERE run_id=? AND status!='completed' LIMIT 1", (run_id,)).fetchone():
                    raise FileReadError("cognition_execution_unknown", "存在无有效结果的旧调用，扩展批准不能允许重发。")
                conn.execute("UPDATE cognition_context_requests SET status=?,decided_at=? WHERE run_id=? AND actor=? AND sequence=?",
                             ("approved" if approved else "rejected", now(), run_id, actor, sequence))
                pending = conn.execute("SELECT 1 FROM cognition_context_requests WHERE run_id=? AND status='awaiting_approval' LIMIT 1", (run_id,)).fetchone()
                conn.execute("UPDATE cognition_runs SET status=?,error=NULL,updated_at=? WHERE id=?",
                             ("awaiting_context_approval" if pending else "queued", now(), run_id))
                if not pending:
                    conn.execute("UPDATE cognition_jobs SET status='queued' WHERE run_id=? AND status='awaiting_context_approval'", (run_id,))
            self.event(run_id, actor, "context_approved" if approved else "context_rejected", {"sequence": sequence, "request_sha256": request_sha256})
        return self.show(run_id)

    def worker_active(self, run_id):
        """Read an existing advisory lock without creating state or changing a run."""
        valid_id(run_id)
        path = self.state_dir / "cognition-locks" / f"run-{run_id}.lock"
        if not path.exists() or path.is_symlink():
            return False
        import fcntl
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    def list_runs(self, project_id):
        if not self.db_path.exists():
            return []
        with self.open() as conn:
            return [dict(row) for row in conn.execute('SELECT id,project_id,commit_id AS "commit",status,created_at,updated_at,call_count FROM cognition_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 100', (project_id,))]

    def event(self, run_id, actor, action, detail=None):
        with self.open(write=True) as conn, conn:
            conn.execute("INSERT INTO cognition_events(run_id,actor,action,detail,created_at) VALUES (?,?,?,?,?)",
                         (run_id, actor, action, canonical(detail or {}), now()))

    def decide(self, run_id, *, approved, approval_sha256=None):
        valid_id(run_id)
        with self.open(write=True) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise FileReadError("cognition_not_found", "未找到该认知工作流。")
            self.envelope(row)
            if row["status"] != "awaiting_approval":
                raise FileReadError("cognition_not_pending", "该工作流未等待批准，不能重复批准。")
            if approved and approval_sha256 != row["envelope_sha256"]:
                raise FileReadError("approval_mismatch", "批准未匹配完整资料包与预算。")
            conn.execute("UPDATE cognition_runs SET status=?,updated_at=? WHERE id=?", ("queued" if approved else "rejected", now(), run_id))
        self.event(run_id, "coordinator", "approved" if approved else "rejected")
        return self.show(run_id)

    def set_status(self, run_id, status, *, error=None):
        with self.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_runs SET status=?,error=?,updated_at=? WHERE id=?",
                         (status, canonical(error) if error else None, now(), run_id))

    def save_plan(self, run_id, plan):
        raw = canonical(plan)
        with self.open(write=True) as conn, conn:
            old = conn.execute("SELECT plan FROM cognition_runs WHERE id=?", (run_id,)).fetchone()[0]
            if old is not None and old != raw:
                raise FileReadError("cognition_plan_changed", "恢复时计划身份不一致。")
            conn.execute("UPDATE cognition_runs SET plan=?,updated_at=? WHERE id=?", (raw, now(), run_id))
            for spec in plan["tasks"]:
                conn.execute("INSERT OR IGNORE INTO cognition_jobs VALUES (?,?,?,?,?,?)", (run_id, spec["id"], canonical(spec), "queued", None, now()))

    def job_status(self, run_id, job_id, status, result=None):
        with self.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_jobs SET status=?,result=?,updated_at=? WHERE run_id=? AND id=?",
                         (status, canonical(result) if result is not None else None, now(), run_id, job_id))

    def publish_job(self, run_id, job_id, result):
        self.job_status(run_id, job_id, result["report"]["status"], result)
        with self.open(write=True) as conn, conn:
            audit = result.get("report_validation") or {}
            indices = audit.get("accepted_indices", list(range(len(result["report"]["findings"]))))
            for index, finding in zip(indices, result["report"]["findings"], strict=True):
                proofs = [result["evidence"][ref] for ref in finding["citations"]]
                value = {**finding, "evidence": proofs}
                finding_id = f"{job_id}:F{index + 1}"
                conn.execute("INSERT OR IGNORE INTO cognition_findings VALUES (?,?,?,?,?,?)",
                             (run_id, finding_id, job_id, canonical(value), "pending_review", None))

    def save_review(self, run_id, judgments):
        with self.open(write=True) as conn, conn:
            for judgment in judgments:
                row = conn.execute("SELECT finding FROM cognition_findings WHERE run_id=? AND id=?", (run_id, judgment["finding_id"])).fetchone()
                if row is None:
                    raise FileReadError("invalid_cognition_reference", "核对引用未知发现。")
                finding = json.loads(row[0])
                supported = judgment["supported"] and finding["kind"] == "observed"
                conn.execute("UPDATE cognition_findings SET status=?,review=? WHERE run_id=? AND id=? AND status!='invalidated'",
                             ("auto_reviewed" if supported else "needs_investigation", canonical(judgment), run_id, judgment["finding_id"]))

    def invalidate(self, run_id, finding_id, reason):
        text(reason)
        with self.open(write=True) as conn, conn:
            cursor = conn.execute("UPDATE cognition_findings SET status='invalidated',review=? WHERE run_id=? AND id=?",
                                  (canonical({"reason": reason, "invalidated_at": now()}), run_id, finding_id))
            if cursor.rowcount != 1:
                raise FileReadError("cognition_finding_not_found", "未找到该认知发现。")
        return self.show(run_id)

    def for_context(self, project_id, commit, *, query=""):
        if not self.db_path.exists() or commit is None:
            return []
        from .index import terms
        with self.open() as conn:
            rows = conn.execute("""SELECT f.id,f.run_id,f.finding,f.review,r.commit_id,r.updated_at
                FROM cognition_findings f JOIN cognition_runs r ON r.id=f.run_id
                WHERE r.project_id=? AND r.commit_id=? AND f.status='auto_reviewed'
                AND r.status IN ('completed','partial') ORDER BY r.updated_at DESC LIMIT 60""", (project_id, commit))
            records = []
            query_terms = set(terms(query))
            for row in rows:
                finding = json.loads(row["finding"])
                text(finding["statement"])
                records.append({"id": row["id"], "run_id": row["run_id"], "body": finding["statement"],
                                "section": finding["section"], "commit": row["commit_id"], "evidence": finding["evidence"],
                                "review": json.loads(row["review"]), "updated_at": row["updated_at"],
                                "finding_sha256": digest({"finding": finding, "review": json.loads(row["review"])}),
                                "score": len(query_terms & set(terms(finding["statement"])))})
        records.sort(key=lambda value: value["score"], reverse=True)
        return records[:3]

    def reviewed_finding(self, run_id, finding_id):
        record = self.show(run_id)
        item = next((item for item in record["findings"] if item["id"] == finding_id), None)
        if item is None or item["status"] != "auto_reviewed" or record["status"] not in ("completed", "partial"):
            raise FileReadError("cognition_knowledge_changed", "认知已失效或尚未核对，需重新准备上下文。")
        return {"project_id": record["project_id"], "commit": record["commit"],
                "finding_sha256": digest({"finding": item["finding"], "review": item["review"]}),
                "body": item["finding"]["statement"]}

    def pending_for_paths(self, project_id, commit, paths):
        """Conservative file overlap, not a complete semantic dependency analysis."""
        if not self.db_path.exists():
            return []
        pending = []
        for run in self.list_runs(project_id):
            if run["commit"] != commit or run["status"] not in ("queued", "planning", "investigating", "reviewing", "pending_execution"):
                continue
            record = self.show(run["id"], include_envelope=True)
            catalog = {source["id"]: source["evidence"]["path"] for source in record["envelope"]["files"]}
            if not record["plan"]:
                if set(paths) & set(catalog.values()):
                    pending.append({"run_id": run["id"], "task_id": None, "reason": "调查计划尚未形成。"})
                continue
            for job in record["jobs"]:
                findings = [item for item in record["findings"] if item["job_id"] == job["id"]]
                if job["status"] == "completed" and findings and all(item["status"] == "auto_reviewed" for item in findings):
                    continue
                touched = {catalog[source_id] for source_id in job["spec"]["source_ids"]}
                if job["result"]:
                    touched.update(proof["evidence"]["path"] for proof in job["result"].get("evidence", {}).values())
                if set(paths) & touched:
                    pending.append({"run_id": run["id"], "task_id": job["id"], "reason": "关联文件调查尚未完成核对。"})
        return pending

    def recover(self, run_id):
        from .cognition import execution_lock
        valid_id(run_id)
        with execution_lock(self.state_dir), execution_lock(self.state_dir, f"run-{run_id}"):
            return self._recover(run_id)

    def _recover(self, run_id):
        current = self.show(run_id)
        if current["status"] not in ("planning", "investigating", "reviewing", "pending_execution"):
            raise FileReadError("cognition_not_recoverable", "该状态不需要恢复。")
        with self.open(write=True) as conn, conn:
            if any(item["status"] == "awaiting_approval" for item in self._context_records(conn, run_id)):
                raise FileReadError("cognition_context_approval_required", "待扩展请求尚未批准，不能通过恢复绕过。")
            unknown = conn.execute("SELECT 1 FROM cognition_calls WHERE run_id=? AND status!='completed' LIMIT 1", (run_id,)).fetchone()
            if unknown:
                raise FileReadError("cognition_execution_unknown", "存在结果未知的模型请求，仅读取核对，不重复调用。")
            conn.execute("UPDATE cognition_runs SET status='queued',error=NULL,updated_at=? WHERE id=?", (now(), run_id))
        self.event(run_id, "coordinator", "recovery_queued", {"mode": "saved_response_reuse"})
        return self.show(run_id)

    def dossier(self, run_id):
        from .survey import SECTIONS
        record = self.show(run_id)
        parts = [{"id": key, "title": title, "facts": [], "pending": []} for key, title, _ in SECTIONS]
        lookup = {part["id"]: part for part in parts}
        for item in record["findings"]:
            target = lookup[item["finding"]["section"]]
            target["facts" if item["status"] == "auto_reviewed" else "pending"].append(item)
        for job in record["jobs"]:
            if job["result"]:
                for section in job["spec"]["sections"]:
                    for rejected in (job["result"].get("report_validation") or {}).get("rejected_findings", []):
                        lookup[section]["pending"].append({"question": f"报告候选未接收：{rejected['field']} · {rejected['code']} · {rejected['message']}",
                                                           "report_rejection": True, "task_id": job["id"]})
                    lookup[section]["pending"].extend({"question": value} for value in job["result"]["report"]["unknowns"])
                    lookup[section]["pending"].extend({"question": value, "scope_expansion": True} for value in job["result"]["report"].get("scope_requests", []))
        return {"run_id": run_id, "project_id": record["project_id"], "commit": record["commit"], "status": record["status"],
                "sections": parts, "plan_gaps": record["plan"]["gaps"] if record["plan"] else [],
                "limitations": ["automatic_review_not_human_confirmation", "static_snapshot_not_runtime_verification"]}
