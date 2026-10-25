"""Durable approved scopes, independent jobs, request ledger and reviewed findings."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from .cognition_contracts import canonical, COGNITION_PROMPTS, POLICY, text, known_format_failure
from .files import FileReadError, _find_file, load_text_blob
from .git import inspect_repository, verify_commit
from .storage import ProjectRegistry
from .survey import SurveyStore, _path
from .resources import route_store

MAX_ENVELOPE_BYTES = 16 * 1024 * 1024
_dossier_reads = ContextVar('cognition_dossier_reads', default=None)


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


def prepare_envelope(project, survey, target, *, project_scope=False):
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
    from .cognition_quality import QUALITY_CONTRACT
    envelope["quality_contract"] = dict(QUALITY_CONTRACT)
    if project_scope:
        from .project_sources import SOURCE_SCOPE, inventory, project_policy
        envelope['source_scope'] = dict(SOURCE_SCOPE)
        envelope['files'] = inventory(root, commit, files)
        envelope['policy'] = project_policy(POLICY)
        envelope['authorization'] = 'model_requests_over_safe_project_commit_sources'
        from .document_contracts import document_contract
        envelope['document_generation'] = document_contract()
        from .incremental_contracts import incremental_contract
        envelope['incremental_contract'] = incremental_contract()
    from .resources import management_guidance
    guidance = management_guidance(project)
    if guidance is not None:
        envelope["management_guidance"] = guidance
    from .cognition_context import scope_review
    envelope["scope_review"] = scope_review(envelope)
    if len(canonical(envelope).encode()) > MAX_ENVELOPE_BYTES:
        raise FileReadError("cognition_scope_budget", "完整可审阅资料包超过保存预算。")
    return envelope


@route_store(projects=("create",), lists=("list_runs", "management_records"), contexts=("for_context",),
             records={name: "cognition" for name in ("show", "decide", "context_requests", "decide_context", "decide_contexts",
                       "worker_active", "invalidate", "reviewed_finding", "recover", "dossier")},
             stable={"create": ("cognition", "run_id")})
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
                    CREATE TABLE IF NOT EXISTS cognition_call_attempts (
                        run_id TEXT NOT NULL, actor TEXT NOT NULL, sequence INTEGER NOT NULL,
                        attempt INTEGER NOT NULL, status TEXT NOT NULL, receipt TEXT, error TEXT,
                        created_at TEXT NOT NULL, PRIMARY KEY(run_id,actor,sequence,attempt));
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

    def create(self, project_id, *, survey_id=None, target=None, refresh_context=None, followup_context=None, repair_context=None, run_id=None, initialization=False):
        if initialization and (refresh_context is not None or followup_context is not None or repair_context is not None):
            raise FileReadError("cognition_scope_mismatch", "首次初始化与增量或补查不能混用。")
        if run_id is not None:
            valid_id(run_id)
            try:
                existing = self.show(run_id)
            except FileReadError as exc:
                if exc.code != "cognition_not_found":
                    raise
            else:
                if existing["project_id"] != project_id:
                    raise FileReadError("cognition_scope_mismatch", "调查身份不属于当前项目。")
                return existing
        if sum(value is not None for value in (refresh_context, followup_context, repair_context)) > 1:
            raise FileReadError("cognition_scope_mismatch", "定向调查与版本增量背景不能混用。")
        project = ProjectRegistry(self.state_dir).get_project(project_id)
        if project is None:
            raise FileReadError("project_not_found", "未找到该项目。")
        survey = SurveyStore(self.state_dir).show(survey_id) if survey_id else SurveyStore(self.state_dir).build(project)
        if target is None:
            from .models import ModelSettings
            settings = ModelSettings.from_env()
            target = {"provider": settings.provider, "model": settings.name, "base_url": settings.base_url}
        envelope = prepare_envelope(project, survey, target,
                                    project_scope=initialization or refresh_context is not None or repair_context is not None
                                    or bool((followup_context or {}).get('source', {}).get('source_scope')))
        if initialization:
            from .cognition_contracts import INITIALIZATION_GOALS
            envelope["initialization_goals"] = dict(INITIALIZATION_GOALS)
        if initialization or repair_context is not None or refresh_context is not None:
            from .cognition_workflow import WORKFLOW_CONTRACT
            envelope['workflow_contract'] = dict(WORKFLOW_CONTRACT)
        envelope["shared_knowledge"] = self.for_context(project_id, envelope["commit"])
        if repair_context is not None:
            from .cognition_repair import check_repair_context
            check_repair_context(self, envelope, repair_context)
            envelope["repair_context"] = repair_context
            envelope["shared_knowledge"] = []
        if followup_context is not None:
            from .cognition_followup import check_followup_context
            check_followup_context(self, envelope, followup_context)
            envelope["followup_context"] = followup_context
            # Existing auto-reviewed claims, including conflict counterparts,
            # remain leads; they must not anchor this targeted re-investigation.
            envelope["shared_knowledge"] = []
            from .cognition_context import scope_review
            envelope["scope_review"] = scope_review(envelope)
        if refresh_context is not None:
            from .cognition_refresh import check_refresh_context
            if envelope.get('source_scope'):
                paths = {s['evidence']['path']: s['id'] for s in envelope['files']}
                refresh_context = {**refresh_context, 'focus': [
                    {**item, 'source_id': paths.get(item['path']),
                     'availability': 'deleted' if item['availability'] == 'deleted' else
                                     'in_package' if item['path'] in paths else 'outside_package'}
                    for item in refresh_context['focus']]}
            check_refresh_context(self, envelope, refresh_context)
            envelope["refresh_context"] = refresh_context
            if envelope.get('incremental_contract'):
                from .knowledge_snapshots import freeze_context
                context = freeze_context(self.state_dir, project, envelope, refresh_context['baseline']['run_id'])
                if context:
                    envelope['incremental_context'] = context
            from .cognition_context import scope_review
            envelope["scope_review"] = scope_review(envelope)
        elif initialization and envelope.get('incremental_contract'):
            from .knowledge_snapshots import shared_context
            context = shared_context(project, envelope)
            if context:
                envelope['incremental_context'] = context
        if len(canonical(envelope).encode()) > MAX_ENVELOPE_BYTES:
            raise FileReadError("cognition_scope_budget", "完整资料包和共享认知超过保存预算。")
        if refresh_context is not None or followup_context is not None or repair_context is not None:
            from .git import selected_commit
            if selected_commit(project, inspect_repository(project["root_path"])) != envelope["commit"]:
                raise FileReadError("cognition_followup_version_changed" if followup_context is not None else "cognition_refresh_target_changed",
                                    "准备期间选定版本发生变化，请重新准备调查。")
        run_id, timestamp = run_id or str(uuid4()), now()
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
            if 'source_scope' in envelope:
                result['source_scope'] = envelope['source_scope']
            result["scope_review"] = envelope.get("scope_review")
            if "repair_context" in envelope:
                context = envelope["repair_context"]
                result["repair"] = {"version": context.get('version', 1), "source_run_id": context["source_run_id"], "leads": len(context["leads"]),
                                    "questions": len(context["questions"]), "failed_tasks": len(context["failed_tasks"])}
            if "followup_context" in envelope:
                context = envelope["followup_context"]
                result["followup"] = {"source_run_id": context["source"]["run_id"],
                                      "finding_id": context["source"]["selected"]["finding_id"],
                                      "statement": context["source"]["selected"]["statement"], "sections": context["sections"]}
            if "refresh_context" in envelope:
                refresh = envelope["refresh_context"]
                result["refresh"] = {"baseline_commit": refresh["baseline"]["commit"],
                                     "changed_files": len(refresh["changes"]),
                                     "historical_leads": len(refresh["historical_leads"]),
                                     "outside_package": sum(f["availability"] == "outside_package" for f in refresh["focus"])}
            result["jobs"] = [{"id": j["id"], "spec": json.loads(j["spec"]), "status": j["status"],
                               "result": json.loads(j["result"]) if j["result"] else None}
                              for j in conn.execute("SELECT * FROM cognition_jobs WHERE run_id=? ORDER BY id", (run_id,))]
            result["findings"] = [{"id": f["id"], "job_id": f["job_id"], "finding": json.loads(f["finding"]),
                                   "status": f["status"], "review": json.loads(f["review"]) if f["review"] else None}
                                  for f in conn.execute("SELECT * FROM cognition_findings WHERE run_id=? ORDER BY id", (run_id,))]
            result["events"] = [dict(event) for event in conn.execute("SELECT actor,action,detail,created_at FROM cognition_events WHERE run_id=? ORDER BY seq DESC LIMIT 100", (run_id,))][::-1]
            result["context_requests"] = self._context_records(conn, run_id)
            if 'workflow_contract' in envelope:
                from .cognition_workflow import workflow_progress
                result['workflow'] = workflow_progress(result)
            if envelope["policy"].get("review_batch_failure") == "isolate_known_format_v1":
                result["review_failures"] = self._review_failures(conn, run_id, envelope["policy"])
            result['document_generation_enabled'] = bool(envelope.get('document_generation'))
            result['incremental_enabled'] = bool(envelope.get('incremental_context'))
            if envelope.get('document_continuation'):
                result['document_continuation'] = envelope['document_continuation']
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
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognition_call_attempts'").fetchone():
                    attempts = {}
                    for row in conn.execute("SELECT * FROM cognition_call_attempts WHERE run_id=? ORDER BY attempt", (run_id,)):
                        entry = {key: row[key] for key in ("attempt", "status", "created_at")}
                        entry.update(receipt=json.loads(row["receipt"]) if row["receipt"] else None,
                                     error=json.loads(row["error"]) if row["error"] else None)
                        attempts.setdefault((row["actor"], row["sequence"]), []).append(entry)
                    for call in result["calls"]:
                        call["attempts"] = attempts.get((call["actor"], call["sequence"]), [])
        result["worker_active"] = self.worker_active(run_id)
        return result

    @staticmethod
    def _review_failures(conn, run_id, policy):
        """Derive isolated batches from durable terminal receipts, never exception text.

        This also covers a crash after saving the final invalid receipt but before
        the coordinator records its event. Historical policies keep their behavior.
        """
        if policy.get("review_batch_failure") != "isolate_known_format_v1":
            return []
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognition_call_attempts'").fetchone():
            return []
        failures = []
        for call in conn.execute("SELECT * FROM cognition_calls WHERE run_id=? AND purpose='project_review' AND status='invalid' ORDER BY created_at,actor", (run_id,)):
            payload = json.loads(call["payload"])
            batch = payload.get("findings", [])
            ids = [item.get("finding_id") for item in batch if isinstance(item, dict)]
            if (not 0 < len(ids) <= 4 or len(ids) != len(batch) or any(not isinstance(i, str) for i in ids)
                    or len(set(ids)) != len(ids) or call["sequence"] != 0
                    or call["actor"] not in ("review_" + digest(ids)[:12], "review_fix_" + digest(ids)[:12])
                    or call["payload_sha256"] != digest({"purpose": "project_review", "payload": payload})):
                continue
            attempts = list(conn.execute("SELECT * FROM cognition_call_attempts WHERE run_id=? AND actor=? AND sequence=? ORDER BY attempt",
                                         (run_id, call["actor"], call["sequence"])))
            if (len(attempts) != policy["max_format_retries"] + 1
                    or any(a["attempt"] != index or a["status"] != "invalid" or not a["receipt"]
                           or not known_format_failure(json.loads(a["receipt"]))
                           for index, a in enumerate(attempts, 1))):
                continue
            saved = {row["id"]: json.loads(row["finding"]) for row in conn.execute(
                "SELECT id,finding FROM cognition_findings WHERE run_id=?", (run_id,))}
            if any(i not in saved or item.get("statement") != saved[i]["statement"]
                   or item.get("kind") != saved[i]["kind"] for i, item in zip(ids, batch)):
                continue
            failures.append({"actor": call["actor"], "sequence": call["sequence"], "finding_ids": ids,
                             "payload_sha256": call["payload_sha256"], "attempts": len(attempts),
                             "code": "cognition_response_invalid"})
        return failures

    @classmethod
    def _blocking_calls(cls, conn, run_id, policy, *, allow_retry=False):
        isolated = {(f["actor"], f["sequence"]) for f in cls._review_failures(conn, run_id, policy)}
        # A failed job with a durable invalid response will never be dispatched
        # again. It must not prevent approval of another, still-unsent request.
        # Pending/uncertain/missing receipts remain blocking.
        for call in conn.execute("""SELECT c.actor,c.sequence FROM cognition_calls c
                JOIN cognition_jobs j ON j.run_id=c.run_id AND j.id=c.actor
                JOIN cognition_call_receipts r ON r.run_id=c.run_id AND r.actor=c.actor AND r.sequence=c.sequence
                WHERE c.run_id=? AND c.status='invalid' AND j.status='failed'
                AND c.purpose IN ('project_research','project_report')""", (run_id,)):
            attempt = conn.execute("""SELECT status,receipt FROM cognition_call_attempts
                WHERE run_id=? AND actor=? AND sequence=? ORDER BY attempt DESC LIMIT 1""",
                (run_id, call['actor'], call['sequence'])).fetchone()
            if attempt and attempt['status'] == 'invalid' and attempt['receipt']:
                isolated.add((call['actor'], call['sequence']))
        accepted = {"completed", "retry_ready"} if allow_retry else {"completed"}
        return any(row["status"] not in accepted and (row["actor"], row["sequence"]) not in isolated
                   for row in conn.execute("SELECT actor,sequence,status FROM cognition_calls WHERE run_id=?", (run_id,)))

    @staticmethod
    def _context_records(conn, run_id, *, full=False):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognition_context_requests'").fetchone():
            return []
        records = []
        run = conn.execute('SELECT envelope FROM cognition_runs WHERE id=?', (run_id,)).fetchone()
        envelope = json.loads(run['envelope']) if run else None
        jobs = {row['id']: json.loads(row['spec']) for row in conn.execute('SELECT id,spec FROM cognition_jobs WHERE run_id=?', (run_id,))}
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
            from .cognition_context import describe_context_request
            record['description'] = describe_context_request(value, envelope, jobs.get(row['actor']))
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
        return self._decide_contexts(run_id, [{'actor': actor, 'sequence': sequence, 'sha256': request_sha256}], approved=approved)

    def decide_contexts(self, run_id, requests, *, approved):
        return self._decide_contexts(run_id, requests, approved=approved, exact_pending=True)

    def _decide_contexts(self, run_id, requests, *, approved, exact_pending=False):
        from .cognition import execution_lock
        valid_id(run_id)
        with execution_lock(self.state_dir, f"run-{run_id}"):
            with self.open(write=True) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                run = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (run_id,)).fetchone()
                if run is None or run["status"] not in ("awaiting_context_approval", "planning", "investigating", "reviewing"):
                    raise FileReadError("cognition_not_pending", "工作流未等待请求扩展，不能重复决定。")
                records = self._context_records(conn, run_id, full=True)
                keys = [(item['actor'], item['sequence']) for item in requests]
                pending_keys = {(item['actor'], item['sequence']) for item in records if item['status'] == 'awaiting_approval'}
                if not keys or len(set(keys)) != len(keys) or (exact_pending and set(keys) != pending_keys):
                    raise FileReadError('approval_mismatch', '待确认请求已变化，请刷新后核对当前这一批。')
                for item in requests:
                    record = next((r for r in records if r['actor'] == item['actor'] and r['sequence'] == item['sequence']), None)
                    if record is None or record['status'] != 'awaiting_approval':
                        raise FileReadError('cognition_not_pending', '该请求已处理或未等待批准，请查看最新状态。')
                    if item['sha256'] != record['request_sha256'] or record['authorization']['envelope_sha256'] != run['envelope_sha256']:
                        raise FileReadError('approval_mismatch', '批准未匹配本次完整请求，整批均未处理。')
                envelope = self.envelope(run)
                if self._blocking_calls(conn, run_id, envelope["policy"]):
                    raise FileReadError("cognition_execution_unknown", "有模型请求尚未确认是否收到结果，调查已暂停。请核对原记录；批准新请求不会重发旧调用。")
                for item in requests:
                    conn.execute("UPDATE cognition_context_requests SET status=?,decided_at=? WHERE run_id=? AND actor=? AND sequence=?",
                                 ("approved" if approved else "rejected", now(), run_id, item['actor'], item['sequence']))
                pending = conn.execute("SELECT 1 FROM cognition_context_requests WHERE run_id=? AND status='awaiting_approval' LIMIT 1", (run_id,)).fetchone()
                conn.execute("UPDATE cognition_runs SET status=?,error=NULL,updated_at=? WHERE id=?",
                             ("awaiting_context_approval" if pending else "queued", now(), run_id))
                if not pending:
                    conn.execute("UPDATE cognition_jobs SET status='queued' WHERE run_id=? AND status='awaiting_context_approval'", (run_id,))
            for item in requests:
                self.event(run_id, item['actor'], "context_approved" if approved else "context_rejected", {"sequence": item['sequence'], "request_sha256": item['sha256']})
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

    def management_records(self, project_id):
        """Read all runs, findings and task gaps in one local read transaction."""
        if not self.db_path.exists():
            return []
        records = []
        with self.open() as conn:
            conn.execute("BEGIN")
            for row in conn.execute("SELECT * FROM cognition_runs WHERE project_id=? ORDER BY created_at,id", (project_id,)):
                envelope = self.envelope(row)
                try:
                    record = {key: row[key] for key in ("id", "status", "created_at", "updated_at", "call_count")}
                    record.update(commit=row["commit_id"], approval_sha256=row["envelope_sha256"],
                                  catalog={s["id"]: s["evidence"] for s in envelope["files"]},
                                  plan=json.loads(row["plan"]) if row["plan"] else None,
                                  error=json.loads(row["error"]) if row["error"] else None)
                    record["findings"] = [{"id": f["id"], "status": f["status"], "job_id": f["job_id"],
                                           "finding": json.loads(f["finding"]), "review": json.loads(f["review"]) if f["review"] else None}
                                          for f in conn.execute("SELECT * FROM cognition_findings WHERE run_id=? ORDER BY id", (row["id"],))]
                    record["jobs"] = [{"id": j["id"], "status": j["status"], "spec": json.loads(j["spec"]),
                                       "result": json.loads(j["result"]) if j["result"] else None}
                                      for j in conn.execute("SELECT * FROM cognition_jobs WHERE run_id=? ORDER BY id", (row["id"],))]
                    if envelope.get("policy", {}).get("review_batch_failure") == "isolate_known_format_v1":
                        record["review_failures"] = self._review_failures(conn, row["id"], envelope["policy"])
                except (ValueError, TypeError, KeyError, RecursionError) as exc:
                    raise FileReadError("cognition_record_corrupt", "项目认知记录损坏，未生成总览。") from exc
                records.append(record)
        return records

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

    def save_review(self, run_id, judgments, *, issues=(), partial_payload=None):
        from .cognition_review import accepts
        with self.open(write=True) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._save_review_issues(conn, run_id, issues)
            for judgment in judgments:
                row = conn.execute("SELECT finding,review,job_id,status FROM cognition_findings WHERE run_id=? AND id=?", (run_id, judgment["finding_id"])).fetchone()
                if row is None:
                    raise FileReadError("invalid_cognition_reference", "核对引用未知发现。")
                finding = json.loads(row[0])
                old = json.loads(row["review"]) if row["review"] else {}
                judgment = {**judgment, "conflict_links": old.get("conflict_links", [])}
                supported = accepts(judgment, finding)
                judgment["supported"] = supported
                conn.execute("UPDATE cognition_findings SET status=?,review=? WHERE run_id=? AND id=? AND status!='invalidated'",
                             ("auto_reviewed" if supported else "needs_investigation", canonical(judgment), run_id, judgment["finding_id"]))
                if partial_payload is not None and row['status'] != 'invalidated':
                    from .cognition_workflow import supported_parts
                    for index, part in supported_parts(finding, judgment, partial_payload):
                        identity = judgment['finding_id'] + f':C{index}'
                        part['partial_origin'] = {'finding_id': judgment['finding_id'], 'check_index': index}
                        proof_review = {**judgment, 'finding_id': identity, 'checks': [judgment['checks'][index - 1]],
                                        'coverage_complete': True, 'supported': True,
                                        'reason': '原结论中独立核对通过的部分；其余部分继续保留缺口。'}
                        conn.execute('INSERT OR IGNORE INTO cognition_findings VALUES (?,?,?,?,?,?)',
                                     (run_id, identity, row['job_id'], canonical(part), 'auto_reviewed', canonical(proof_review)))
            # A conflict discovered in a later batch also blocks the other side,
            # including an already accepted finding; invalidation is never undone.
            for judgment in judgments:
                for conflict in judgment["conflicts"]:
                    ref = conflict["reference_id"]
                    row = conn.execute("SELECT review FROM cognition_findings WHERE run_id=? AND id=?", (run_id, ref)).fetchone()
                    if row is None:  # A located unknown remains a hint in jobs.
                        continue
                    review = json.loads(row["review"]) if row["review"] else {"reason": "存在未解决的关联冲突，等待核对。"}
                    links = {item["reference_id"]: item for item in review.get("conflict_links", [])}
                    links[judgment["finding_id"]] = {"reference_id": judgment["finding_id"], "reason": conflict["reason"]}
                    review.update(supported=False, conflict_links=list(links.values()))
                    conn.execute("UPDATE cognition_findings SET status='needs_investigation',review=? WHERE run_id=? AND id=? AND status!='invalidated'",
                                 (canonical(review), run_id, ref))
            self._block_conflicted_parts(conn, run_id)

    @staticmethod
    def _block_conflicted_parts(conn, run_id):
        """A later conflict with either parent or part quarantines that family."""
        rows = list(conn.execute('SELECT id,finding,review,status FROM cognition_findings WHERE run_id=?', (run_id,)))
        families = {r['id']: (json.loads(r['finding']).get('partial_origin') or {}).get('finding_id', r['id']) for r in rows}
        blocked = {}
        for row in rows:
            review = json.loads(row['review']) if row['review'] else {}
            if review.get('conflicts') or review.get('conflict_links') or review.get('validation_error') or row['status'] == 'invalidated':
                blocked[families[row['id']]] = row['id']
        for row in rows:
            if families[row['id']] not in blocked or row['status'] != 'auto_reviewed':
                continue
            review = json.loads(row['review'])
            review.update(supported=False, reason='原结论或关联子项出现冲突或失效，暂停复用该部分。',
                          conflict_links=[{'reference_id': blocked[families[row['id']]], 'reason': '关联结论存在未解决冲突或失效。'}])
            conn.execute("UPDATE cognition_findings SET status='needs_investigation',review=? WHERE run_id=? AND id=?",
                         (canonical(review), run_id, row['id']))

    @staticmethod
    def _save_review_issues(conn, run_id, issues):
        for issue in issues:
            row = conn.execute("SELECT review FROM cognition_findings WHERE run_id=? AND id=?", (run_id, issue["finding_id"])).fetchone()
            if row is None:
                raise FileReadError("invalid_cognition_reference", "核对引用未知发现。")
            old = json.loads(row["review"]) if row["review"] else {}
            review = {"reason": "这条结论的核对引用或结构未通过校验，已单独保留待查；其他结论继续核对。",
                      "validation_error": issue["code"], "validation_detail": issue["reason"],
                      "supported": False, "checks": [], "conflicts": [], "conflict_links": old.get("conflict_links", [])}
            conn.execute("UPDATE cognition_findings SET status='needs_investigation',review=? WHERE run_id=? AND id=? AND status!='invalidated'",
                         (canonical(review), run_id, issue["finding_id"]))

    def invalidate(self, run_id, finding_id, reason):
        text(reason)
        with self.open(write=True) as conn, conn:
            cursor = conn.execute("UPDATE cognition_findings SET status='invalidated',review=? WHERE run_id=? AND id=?",
                                  (canonical({"reason": reason, "invalidated_at": now()}), run_id, finding_id))
            if cursor.rowcount != 1:
                raise FileReadError("cognition_finding_not_found", "未找到该认知发现。")
            self._block_conflicted_parts(conn, run_id)
        return self.show(run_id)

    def for_context(self, project_id, commit, *, query=""):
        if not self.db_path.exists() or commit is None:
            return []
        from .index import terms
        from .cognition_resolution import effective_resolutions, item_resolution
        effects = effective_resolutions(self.state_dir, project_id, commit)
        with self.open() as conn:
            rows = conn.execute("""SELECT f.id,f.run_id,f.finding,f.review,f.status,f.job_id,r.commit_id,r.updated_at
                FROM cognition_findings f JOIN cognition_runs r ON r.id=f.run_id
                WHERE r.project_id=? AND r.commit_id=? AND f.status='auto_reviewed'
                AND r.status IN ('completed','partial') ORDER BY r.updated_at DESC LIMIT 60""", (project_id, commit))
            records = []
            query_terms = set(terms(query))
            for row in rows:
                finding = json.loads(row["finding"])
                if item_resolution(effects, row['run_id'], {'id': row['id'], 'job_id': row['job_id'],
                        'status': row['status'], 'finding': finding, 'review': json.loads(row['review'])}):
                    continue
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
        from .cognition_resolution import effective_resolutions, item_resolution
        if item_resolution(effective_resolutions(self.state_dir, record['project_id'], record['commit']), run_id, item):
            raise FileReadError('cognition_knowledge_changed', '原认知已由补查处理决定替代，请重新准备上下文。')
        return {"project_id": record["project_id"], "commit": record["commit"],
                "finding_sha256": digest({"finding": item["finding"], "review": item["review"]}),
                "body": item["finding"]["statement"]}

    def pending_for_paths(self, project_id, commit, paths):
        """Conservative file overlap, not a complete semantic dependency analysis."""
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
            if self._blocking_calls(conn, run_id, current["policy"], allow_retry=True):
                raise FileReadError("cognition_execution_unknown", "存在结果未知的模型请求，仅读取核对，不重复调用。")
            retry_ready = conn.execute("SELECT 1 FROM cognition_calls WHERE run_id=? AND status='retry_ready' LIMIT 1", (run_id,)).fetchone() is not None
            conn.execute("UPDATE cognition_runs SET status='queued',error=NULL,updated_at=? WHERE id=?", (now(), run_id))
        self.event(run_id, "coordinator", "recovery_queued", {"mode": "saved_response_or_approved_format_retry" if retry_ready else "saved_response_reuse"})
        return self.show(run_id)

    def dossier(self, run_id, *, legacy=False):
        # One read traversal may revisit the same frozen ancestors many times.
        # Share snapshots only inside that traversal, never across requests or
        # model calls: invalidations must be seen by the next validation.
        cache = _dossier_reads.get()
        token = _dossier_reads.set({}) if cache is None else None
        cache = _dossier_reads.get()
        key = (str(self.state_dir), run_id, legacy)
        try:
            if key in cache:
                if cache[key] is None:
                    raise FileReadError('cognition_record_corrupt', '调查历史存在循环引用。')
                return deepcopy(cache[key])
            cache[key] = None
            result = self._dossier(run_id, legacy=legacy)
            cache[key] = deepcopy(result)
            return result
        finally:
            if token is not None:
                _dossier_reads.reset(token)

    def _dossier(self, run_id, *, legacy=False):
        from .survey import SECTIONS
        from .cognition_context import material_request_text
        record = self.show(run_id)
        if record.get('document_continuation'):
            from .document_rounds import source_dossier
            result = source_dossier(self, self.show(run_id, include_envelope=True)['envelope'])
            result.update(run_id=run_id, status=record['status'], document_only=True)
            if not legacy:
                from .cognition_backlog import normalize_backlog
                normalize_backlog(self, record, result)
            return result
        parts = [{"id": key, "title": title, "facts": [], "pending": []} for key, title, _ in SECTIONS]
        lookup = {part["id"]: part for part in parts}
        selected = []
        if record.get('repair', {}).get('version') == 2:
            selected = self.show(run_id, include_envelope=True)['envelope']['repair_context']['selected_issues']
        job_specs = {j['id']: j['spec'] for j in record['jobs']}
        def track(item, job_id):
            original_job = job_specs.get(job_id, {}).get('followup_finding', job_id).split(':')[0]
            if selected and original_job in ('T1', 'T2', 'T3', 'T4') and int(original_job[1:]) <= len(selected):
                return {**item, 'tracked_issue': selected[int(original_job[1:]) - 1]}
            return item
        partial_parents = {(item['finding'].get('partial_origin') or {}).get('finding_id') for item in record['findings']
                           if item['status'] == 'auto_reviewed'}
        for item in record["findings"]:
            if item['id'] in partial_parents:
                from .cognition_workflow import remaining_checks, check_statement
                item = {**item, 'remaining_statements': [check_statement(c) for c in remaining_checks(item)],
                        'supported_parts_published': True}
            target = lookup[item["finding"]["section"]]
            if item['status'] != 'auto_reviewed':
                item = track(item, item['job_id'])
            target["facts" if item["status"] == "auto_reviewed" else "pending"].append(item)
        for job in record["jobs"]:
            if job["result"]:
                for section in job["spec"]["sections"]:
                    for rejected in (job["result"].get("report_validation") or {}).get("rejected_findings", []):
                        lookup[section]["pending"].append({"question": f"报告候选未接收：{rejected['field']} · {rejected['code']} · {rejected['message']}",
                                                           "report_rejection": True, "task_id": job["id"]})
                    for rejected in (job["result"].get("report_validation") or {}).get("rejected_material_requests", []):
                        lookup[section]["pending"].append({"question": f"资料需求未接收：{rejected['field']} · {rejected['code']} · {rejected['message']}",
                                                           "material_request_rejection": True, "task_id": job["id"]})
                    lookup[section]["pending"].extend(track({"question": value, "task_id": job["id"]}, job['id']) for value in job["result"]["report"]["unknowns"])
                    for key in ("in_scope_requests", "scope_requests"):
                        lookup[section]["pending"].extend({"question": material_request_text(value),
                            "source_gap": key == "in_scope_requests" and not isinstance(value, str),
                            "scope_expansion": key == "scope_requests" and not isinstance(value, str),
                            "legacy_material_request": isinstance(value, str), "task_id": job["id"],
                            **({'material_request': value} if not isinstance(value, str) else {})}
                            for value in job["result"]["report"].get(key, []))
        if selected:
            for section in parts:
                section['pending'] = [track(i, i.get('task_id', i.get('job_id', ''))) for i in section['pending']]
        plan_gaps = record["plan"]["gaps"] if record["plan"] else []
        if record.get("repair"):
            source_id = record["repair"]["source_run_id"]
            envelope = self.show(run_id, include_envelope=True)["envelope"]
            from .cognition_repair import check_repair_context
            check_repair_context(self, envelope)
            previous = self.dossier(source_id, legacy=True)
            # Supplement results do not implicitly close historical questions.
            # Carry their provenance instead of silently dropping them on publish.
            for section in previous["sections"]:
                lookup[section["id"]]["facts"].extend({**item, "origin_run_id": item.get("origin_run_id", source_id)} for item in section["facts"])
                lookup[section["id"]]["pending"].extend({**item, "origin_run_id": item.get("origin_run_id", source_id),
                    'origin_finding_id': item.get('origin_finding_id', item.get('id')),
                    **({"id": source_id + ":" + item["id"]} if "finding" in item and not item.get("origin_run_id") else {})} for item in section["pending"])
            plan_gaps = list({canonical(gap): gap for gap in [*plan_gaps, *previous["plan_gaps"]]}.values())
            for part in parts:
                unique = {}
                for item in part["facts"]:
                    # Only identical statements at the same observed commit are
                    # coalesced; no semantic equivalence is guessed.
                    identity = digest({"statement": item["finding"]["statement"], "commit": record["commit"]})
                    unique.setdefault(identity, item)
                part["facts"] = list(unique.values())
        from .cognition_quality import classify_pending
        pending_count, pending_summary = classify_pending(parts)
        result = {"run_id": run_id, "project_id": record["project_id"], "commit": record["commit"], "status": record["status"],
                "pending_count": pending_count, "pending_summary": pending_summary,
                "sections": parts, "plan_gaps": plan_gaps,
                "limitations": ["automatic_review_not_human_confirmation", "static_snapshot_not_runtime_verification"]}
        if record.get("followup"):
            result["followup"] = record["followup"]
        if "review_failures" in record:
            result["review_failures"] = record["review_failures"]
        if not legacy:
            from .cognition_backlog import normalize_backlog
            normalize_backlog(self, record, result)
        return result
