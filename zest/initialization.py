"""Project onboarding: local preparation, one scope approval, durable research, documents.

GETs only observe. A stable research identity survives preparation/process crashes;
unknown provider outcomes are never relaunched without explicit reconciliation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from uuid import UUID, uuid4

from .cognition_store import CognitionStore
from .documents import initialize_documents
from .files import FileReadError, CREDENTIAL_PATTERNS
from .git import inspect_repository, selected_commit
from .models import ModelSettings, model_target
from .resources import _directory, _regular, project_states
from .storage import ProjectRegistry


ACTIVE = {"queued", "planning", "investigating", "reviewing"}
TERMINAL = {"completed", "partial", "failed"}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, raw):
    _directory(path.parent)
    _regular(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".initialization-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class InitializationStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()

    def location(self, project_id):
        project = ProjectRegistry(self.state_dir).get_project_readonly(str(UUID(str(project_id))))
        if project is None:
            raise FileReadError("project_not_found", "未找到该项目。")
        directory = project_states(self.state_dir, project["id"])[0]
        _directory(directory)
        return project, directory

    def read(self, directory):
        path = directory / "initialization.json"
        _regular(path)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            if len(raw) > 1024 * 1024:
                raise ValueError
            value = json.loads(raw)
            if (value["schema_version"] != 1 or str(UUID(value["id"])) != value["id"]
                    or str(UUID(value["project_id"])) != value["project_id"]
                    or not isinstance(value["stage"], str)
                    or value.get("run_id") is not None and str(UUID(value["run_id"])) != value["run_id"]):
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise FileReadError("initialization_state_error", "初始化记录无法读取，未重新发送模型请求。") from exc

    def save(self, directory, value):
        value["updated_at"] = now()
        atomic(directory / "initialization.json", json.dumps(value, ensure_ascii=False).encode())

    @contextmanager
    def lock(self, directory, *, worker=False, required=True):
        import fcntl
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / ("initialization-worker.lock" if worker else "initialization.lock")
        _regular(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if required:
                    raise FileReadError("initialization_busy", "项目正在初始化，请等待当前步骤完成。")
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def active(self, directory, *, worker=True):
        import fcntl
        path = directory / ("initialization-worker.lock" if worker else "initialization.lock")
        _regular(path)
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def show(self, project_id, *, observe_git=True):
        project, directory = self.location(project_id)
        value = self.read(directory)
        if value is None:
            return {"project_id": project_id, "status": "not_started"}
        if value["project_id"] != project_id:
            raise FileReadError("initialization_scope_mismatch", "初始化记录不属于该项目。")
        result = {**value, "worker_active": self.active(directory), "preparation_active": self.active(directory, worker=False)}
        if result.get('document_generation', {}).get('run_id') != value.get('run_id'):
            result.pop('document_generation', None)
        if observe_git:
            git = inspect_repository(project["root_path"])
            result.update(head_commit=git.head_commit, current_commit=selected_commit(project, git), dirty=git.dirty)
        else:
            result.update(head_commit=value.get("commit"), current_commit=value.get("commit"))
        result["stale"] = bool(value.get("commit") and result["current_commit"] != value["commit"])
        if value.get("run_id"):
            try:
                run = CognitionStore(self.state_dir).show(value["run_id"])
            except FileReadError as exc:
                if exc.code != "cognition_not_found":
                    raise
                result["status"] = value["stage"]
            else:
                result["research"] = run
                from .document_pages import DocumentPages
                pages = DocumentPages(self.state_dir, value['run_id'])
                if pages.value is not None:
                    result['document_generation'] = pages.summary()
                if value["stage"] not in ("published", "rejected", "publication_failed"):
                    result["status"] = ("publishing" if result['worker_active'] else "publication_pending") if run["status"] in TERMINAL else run["status"]
                if run["status"] in ACTIVE - {"queued"} and not run["worker_active"] and not result["worker_active"]:
                    result["status"] = "interrupted"
        result.setdefault("status", value["stage"])
        if value['stage'] == 'rejected':
            result['status'] = 'rejected'
        if value['stage'] == 'published' and value.get('run_id'):
            dossier = CognitionStore(self.state_dir).dossier(value['run_id'])
            result['summary'] = {**result.get('summary', {}), **gap_summary(dossier)}
        return result

    def prepare(self, project_id, *, replace_pending=False, target=None):
        project, directory = self.location(project_id)
        if not project.get("resource_dir"):
            ProjectRegistry(self.state_dir).register(inspect_repository(project["root_path"]), workspace=True)
            project, directory = self.location(project_id)
        with self.lock(directory):
            value = self.read(directory)
            previous = None
            if value and value.get("stage") == "repair_preparing":
                return self.show(project_id)
            owned_artifacts = dict((value or {}).get("owned_artifacts", {}))
            if value and value.get("run_id"):
                try:
                    run = CognitionStore(self.state_dir).show(value["run_id"])
                except FileReadError as exc:
                    if exc.code != "cognition_not_found":
                        raise
                else:
                    if run["status"] == "rejected" and value["stage"] == "rejected":
                        value = None
                    elif not replace_pending:
                        return self.show(project_id)
                    elif run["status"] in TERMINAL and not run["worker_active"] and not self.active(directory):
                        from .resources import record_state
                        research_store = CognitionStore(record_state(self.state_dir, "cognition", run["id"]))
                        with research_store.open() as conn:
                            if conn.execute("SELECT 1 FROM cognition_calls WHERE run_id=? AND status IN ('pending','uncertain') LIMIT 1", (run["id"],)).fetchone():
                                raise FileReadError("initialization_execution_unknown", "仍有结果不明的模型调用，须先核对原记录，不能重新初始化绕过。")
                        previous = value
                        value = None
                    elif run["status"] != "awaiting_approval":
                        raise FileReadError("initialization_already_started", "已开始的调查不能重新创建或重发，请先核对原记录。")
                    else:
                        CognitionStore(self.state_dir).decide(run["id"], approved=False)
                        value = None
            if value is None or value.get("stage") in ("needs_model", "needs_commit", "preparation_failed"):
                value = {"schema_version": 1, "id": str(uuid4()), "project_id": project_id,
                         "created_at": now(), "stage": "preparing", "warnings": [], "documents": [], "owned_artifacts": owned_artifacts}
                if previous:
                    value.update(previous_initialization={key: previous.get(key) for key in ("id", "run_id", "commit", "status")},
                                 document_hashes=previous.get("document_hashes", {}))
            self.save(directory, value)
            try:
                from .web_operations import report_progress
                report_progress('documents', '正在建立 .zest 并整理已有文档')
                value["migration"] = initialize_documents(project)
                value.setdefault("owned_artifacts", {}).update(value["migration"].get("written_hashes", {}))
                git = inspect_repository(project["root_path"])
                value.update(commit=selected_commit(project, git), branch=git.branch)
                if value["commit"] is None:
                    value["stage"] = "needs_commit"
                    self.save(directory, value)
                    return self.show(project_id)
                from .index import RetrievalIndex
                try:
                    report_progress('index', '正在建立源码检索索引')
                    value["index"] = RetrievalIndex(self.state_dir).build(project)
                except FileReadError as exc:
                    value["warnings"].append({"code": exc.code, "message": "索引未建立；仍可调查当前安全资料包。"})
                if target is None:
                    try:
                        settings = ModelSettings.from_env()
                    except FileReadError as exc:
                        if exc.code not in ("model_not_configured", "ai_dependencies_missing"):
                            raise
                        value["stage"] = "needs_model"
                        self.save(directory, value)
                        return self.show(project_id)
                    target = model_target(settings)
                    value["configuration_revision"] = settings.revision
                # Save the intended ID before creating any research/checkpoint.
                value.setdefault("run_id", str(uuid4()))
                self.save(directory, value)
                report_progress('scope', '正在整理项目调查范围，尚未调用模型')
                run = CognitionStore(self.state_dir).create(project_id, target=target, run_id=value["run_id"], initialization=True)
                value.update(commit=run["commit"], stage="awaiting_approval")
                report_progress('ready', '项目已接入，等待你确认初始化范围', files=run['file_count'])
            except FileReadError as exc:
                value.update(stage="preparation_failed", error={"code": exc.code, "message": str(exc)})
            self.save(directory, value)
        self.auto_start(project_id)
        return self.show(project_id)

    def begin(self, project_id, *, resume_setup=False):
        """One explicit initialize action prepares and starts read-only understanding."""
        if resume_setup:
            current = self.show(project_id, observe_git=False)
            if current['status'] != 'needs_model':
                return current
        value = self.prepare(project_id)
        if value.get('status') == 'awaiting_approval' and value.get('stale'):
            value = self.prepare(project_id, replace_pending=True)
        if value.get('status') in ('awaiting_approval', 'awaiting_context_approval'):
            from .understanding_approval import UnderstandingApproval
            UnderstandingApproval(self.state_dir).configure(project_id, enabled=True)
        return self.show(project_id)

    def republish(self, project_id):
        """Render the saved dossier with current presentation rules, without research."""
        project, directory = self.location(project_id)
        with self.lock(directory), self.lock(directory, worker=True):
            value = self.read(directory)
            if not value or not value.get("run_id"):
                raise FileReadError("initialization_not_prepared", "请先准备项目初始化。")
            store = CognitionStore(self.state_dir)
            run = store.show(value["run_id"])
            if run["status"] not in TERMINAL or run["worker_active"]:
                raise FileReadError("initialization_busy", "调查尚未结束，暂不重新整理文档。")
            from .document_pages import DocumentPages
            pages = DocumentPages(self.state_dir, run['id'])
            value.update(publish_documents(project, store.dossier(run["id"]), value, pages=pages if pages.value else None))
            value.update(stage="published", status="partial" if run["status"] != "completed" or value["conflicts"] or value['summary']['pending_items'] or value['summary']['unresolved_gaps'] or value.get('document_generation', {}).get('status', 'completed') != 'completed' else "completed")
            self.save(directory, value)
        return self.show(project_id)

    def repair(self, project_id, *, replace_pending=False):
        """Prepare a supplement; approval and background execution remain separate."""
        _, directory = self.location(project_id)
        with self.lock(directory), self.lock(directory, worker=True):
            value = self.read(directory)
            if not value or not value.get("run_id"):
                raise FileReadError("initialization_not_prepared", "请先初始化项目。")
            cancel_id = None
            if value.get("stage") == "repair_preparing":
                source_id = value["repair_source_run_id"]
                next_id = value["run_id"]
            elif value.get("stage") == "published" and value.get("status") != "completed":
                source_id, next_id = value["run_id"], str(uuid4())
            elif value.get("repair_source_run_id") and value.get("stage") in ("awaiting_approval", "rejected") and replace_pending:
                pending = CognitionStore(self.state_dir).show(value["run_id"])
                if pending["status"] not in ("awaiting_approval", "rejected"):
                    raise FileReadError("initialization_already_started", "补全已开始，不能通过重新准备重发请求。")
                cancel_id = pending["id"] if pending["status"] == "awaiting_approval" else None
                source_id, next_id = value["repair_source_run_id"], str(uuid4())
            else:
                return self.show(project_id)
            from .cognition_repair import repair_context, prepare_repair
            # Validate the old ledger before replacing the displayed active identity.
            context = repair_context(CognitionStore(self.state_dir), source_id, version=2)
            if not context['selected_issues'] and value.get('stage') == 'published':
                from .document_pages import DocumentPages
                pages = DocumentPages(self.state_dir, source_id)
                value.update(publish_documents(self.location(project_id)[0], CognitionStore(self.state_dir).dossier(source_id), value,
                                               pages=pages if pages.value else None))
                self.save(directory, value)
                return self.show(project_id)
            settings = ModelSettings.from_env()
            if cancel_id:
                CognitionStore(self.state_dir).decide(cancel_id, approved=False)
            if value.get("stage") != "repair_preparing":
                if not replace_pending:
                    value["previous_initialization"] = {key: value.get(key) for key in ("id", "run_id", "commit", "status")}
                value.update(id=str(uuid4()), run_id=next_id, repair_source_run_id=source_id, stage="repair_preparing")
                value.pop("error", None)
                self.save(directory, value)
            from .web_operations import report_progress
            report_progress('scope', '正在复用已有资料并准备补全范围，尚未调用模型')
            run = prepare_repair(self.state_dir, source_id, target=model_target(settings), new_run_id=next_id)
            value.update(stage="awaiting_approval", commit=run["commit"], configuration_revision=settings.revision)
            self.save(directory, value)
        self.auto_start(project_id)
        return self.show(project_id)

    def auto_start(self, project_id):
        """Apply a project standing permission only to the saved pending run."""
        project, directory = self.location(project_id)
        launch = False
        with self.lock(directory):
            value = self.read(directory)
            if not value or not value.get('run_id') or value['stage'] in ('published', 'rejected'):
                return False
            run = CognitionStore(self.state_dir).show(value['run_id'])
            if run['status'] not in ('awaiting_approval', 'awaiting_context_approval') or run['worker_active'] or self.active(directory):
                return False
            from .understanding_approval import UnderstandingApproval
            approval = UnderstandingApproval(self.state_dir)
            if not approval.show(project_id)['active']:
                return False
            if selected_commit(project, inspect_repository(project['root_path'])) != value.get('commit'):
                return False
            if approval.approve(project_id, run['id'], contexts=run['status'] == 'awaiting_context_approval'):
                value['stage'] = 'queued'
                self.save(directory, value)
                launch = True
        if launch:
            self.launch(project_id)
        return launch

    def decide(self, project_id, *, approved, sha256=None, automatic_maintenance=False):
        project, directory = self.location(project_id)
        with self.lock(directory):
            value = self.read(directory)
            if not value or not value.get("run_id"):
                raise FileReadError("initialization_not_prepared", "请先准备项目初始化。")
            if approved:
                if selected_commit(project, inspect_repository(project["root_path"])) != value["commit"]:
                    raise FileReadError("initialization_version_changed", "项目版本已变化，请重新准备初始化。")
                settings = ModelSettings.from_env()
                run = CognitionStore(self.state_dir).show(value["run_id"])
                if settings.revision != value.get("configuration_revision") or model_target(settings) != run["target"]:
                    raise FileReadError("initialization_model_changed", "模型配置已变化，请重新准备初始化。")
            CognitionStore(self.state_dir).decide(value["run_id"], approved=approved, approval_sha256=sha256)
            value["stage"] = "queued" if approved else "rejected"
            self.save(directory, value)
        if approved:
            if automatic_maintenance:
                from .maintenance import MaintenanceStore
                MaintenanceStore(self.state_dir).enable(project_id)
            self.launch(project_id)
        return self.show(project_id)

    def launch(self, project_id):
        _, directory = self.location(project_id)
        value = self.read(directory)
        if not value or not value.get("run_id"):
            raise FileReadError("initialization_not_prepared", "请先准备项目初始化。")
        run = CognitionStore(self.state_dir).show(value["run_id"])
        if run["status"] not in {"queued", *TERMINAL} or value["stage"] in ("published", "rejected"):
            raise FileReadError("initialization_not_queued", "初始化未排队；未知结果须先核对，不能直接重发。")
        if self.active(directory):
            return
        from .model_settings import worker_environment
        try:
            process = subprocess.Popen([sys.executable, "-m", "zest.initialization", str(self.state_dir), project_id],
                                       env=worker_environment(), cwd=Path(__file__).resolve().parent.parent,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
        except OSError as exc:
            raise FileReadError("initialization_worker_start_failed", "初始化已排队但后台未启动，可以继续同一初始化。") from exc
        threading.Thread(target=process.wait, daemon=True).start()

    def recover(self, project_id):
        _, directory = self.location(project_id)
        value = self.read(directory)
        if not value or not value.get("run_id"):
            raise FileReadError("initialization_not_prepared", "请先准备项目初始化。")
        run = CognitionStore(self.state_dir).show(value["run_id"])
        if run["status"] not in {"queued", *TERMINAL}:
            CognitionStore(self.state_dir).recover(value["run_id"])
        if value['stage'] == 'published' and value.get('document_generation', {}).get('status') == 'partial':
            with self.lock(directory):
                value['stage'] = 'publication_failed'
                self.save(directory, value)
        self.launch(project_id)
        return self.show(project_id)

    def decide_context(self, project_id, actor, sequence, *, approved, sha256):
        _, directory = self.location(project_id)
        value = self.read(directory)
        if not value or not value.get("run_id"):
            raise FileReadError("initialization_not_prepared", "请先准备项目初始化。")
        run = CognitionStore(self.state_dir).decide_context(value["run_id"], actor, sequence,
                                                          approved=approved, request_sha256=sha256)
        if run["status"] == "queued":
            self.launch(project_id)
        return self.show(project_id)

    def decide_contexts(self, project_id, requests, *, approved):
        _, directory = self.location(project_id)
        value = self.read(directory)
        if not value or not value.get('run_id'):
            raise FileReadError('initialization_not_prepared', '请先准备项目初始化。')
        run = CognitionStore(self.state_dir).decide_contexts(value['run_id'], requests, approved=approved)
        if run['status'] == 'queued':
            self.launch(project_id)
        return self.show(project_id)

    def run(self, project_id, *, model=None, check_updates=True):
        project, directory = self.location(project_id)
        with self.lock(directory, worker=True, required=False) as acquired:
            if not acquired:
                return self.show(project_id)
            value = self.read(directory)
            if not value or not value.get("run_id") or value["stage"] in ("published", "rejected"):
                return self.show(project_id)
            store = CognitionStore(self.state_dir)
            run = store.show(value["run_id"])
            if run["status"] == "queued":
                from .understanding_approval import run_understanding
                run = run_understanding(self.state_dir, project_id, run['id'], model=model)
            if run["status"] in TERMINAL:
                value.update(stage="publishing")
                self.save(directory, value)
                try:
                    from .document_pages import generate_documents
                    dossier = store.dossier(run['id'])
                    pages = generate_documents(self.state_dir, project, dossier, model=model)
                    value.update(publish_documents(project, dossier, value, pages=pages))
                    value["status"] = ("partial" if value['summary']['reviewed_findings'] else "failed") if run["status"] == "failed" else "partial" if run["status"] == "partial" or value["conflicts"] or value['summary']['pending_items'] or value['summary']['unresolved_gaps'] else "completed"
                    if value.get('document_generation', {}).get('status', 'completed') != 'completed':
                        value['status'] = 'partial'
                    value["stage"] = "published"
                    value.pop("error", None)
                except Exception as exc:
                    value.update(stage="publication_failed", error={"code": getattr(exc, "code", "document_write_failed"),
                                                                    "message": "调查已保留，文档写入未完成；可核对后继续，不重发模型请求。"})
                self.save(directory, value)
        if check_updates and value.get("stage") == "published" and value.get("status") in {"completed", "partial"}:
            from .maintenance import MaintenanceStore
            MaintenanceStore(self.state_dir).check(project_id, reason="task")
        return self.show(project_id)


def gap_summary(dossier):
    backlog = dossier.get('backlog')
    if not backlog:
        return {}
    return {'pending_items': backlog['total'], 'pending_total': backlog['total'],
            'pending_categories': dossier['pending_summary'], 'repairable_items': backlog['actionable'],
            **({'repair_progress': dossier['repair_progress']} if dossier.get('repair_progress') else {}),
            'backlog': {k: v for k, v in backlog.items() if k != 'issues'}}


def publish_documents(project, dossier, value, *, pages=None):
    if pages is not None:
        from .document_pages import publish_pages
        return publish_pages(project, dossier, value, pages)
    from .documents import _lock, _parents, _create
    root = Path(project["root_path"])
    prefix = ".zest/docs/initialization/"
    names = {"purpose": "purpose.md", "architecture": "architecture.md", "flows": "api.md",
             "dependencies": "dependencies.md", "development": "development.md", "constraints": "decisions.md"}
    header = (f"\n\n观察版本：`{dossier['commit']}` · 调查：`{dossier['run_id']}`。\n\n"
              "基于静态资料的自动核对，尚非人工确认。未运行项目、测试或部署；合并和线上状态未知。\n\n")
    outputs = {}
    from .cognition_quality import classify_pending
    if dossier.get('backlog'):
        pending_count, pending_summary = dossier['pending_count'], dossier['pending_summary']
    else:
        pending_count, pending_summary = classify_pending(dossier["sections"])
    fact_count = 0
    for section in dossier["sections"]:
        lines = ["# " + section["title"], header, "## 自动核对的发现\n"]
        for item in section["facts"]:
            fact_count += 1
            finding = item["finding"]
            lines.append(f"- {finding['statement']}\n")
            if finding.get('partial_origin'):
                lines.append(f"  来自结论 `{finding['partial_origin']['finding_id']}` 中独立核对通过的部分。\n")
            if item.get("origin_run_id"):
                lines.append(f"  原调查自动核对：`{item['origin_run_id']}`（同一观察版本）。\n")
            for proof in finding["evidence"]:
                evidence = proof["evidence"]
                lines.append(f"  来源：`{evidence['path']}:{evidence['start_line']}–{evidence['end_line']}` · `{evidence['commit']}`\n")
        if not section["facts"]:
            lines.append("尚无可发布的自动核对发现。\n")
        lines.append("\n## 待补充的项目知识\n\n以下内容按原因分类，不阻止使用已核对的知识；无需开发者逐条批准。后续遇到相关问题时继续调查。\n")
        category = None
        for item in sorted(section["pending"], key=lambda item: item["category"]):
            if item["category"] != category:
                category = item["category"]
                lines.append("\n### " + item["category_title"] + "\n")
            statement = item.get('question') or item.get('finding', {}).get('statement', '待核对来源')
            if item.get('supported_parts_published'):
                statement = '其余部分待补充：' + ('；'.join(item['remaining_statements']) or '原结论仍有未经完整检查的内容。')
            lines.append('- ' + statement + '\n')
            if item.get("origin_run_id"):
                lines.append(f"  沿用原调查 `{item['origin_run_id']}` 的未完成事项；本轮不自动关闭。\n")
            if (item.get("review") or {}).get("reason"):
                lines.append("  核对说明：" + item["review"]["reason"] + "\n")
            if item.get('gap_state') == 'attempted':
                lines.append('  当前版本已定向调查，仍有缺口；同一资料不再重复补查。\n')
            elif item.get('gap_state') == 'needs_question':
                lines.append('  历史调查线索，需围绕具体问题再定位，不作为自动补全任务。\n')
        if not section["pending"]:
            lines.append("当前调查未列出具体缺口，不代表该领域已穷尽。\n")
        if section["id"] == "flows":
            lines.append("\n调用方式以已读实现及文档为准；未读源码应继续调查。仓库没有记录的运行、部署及团队约定再向对应团队确认。\n")
        if section["id"] == "constraints":
            lines.append("\n历史设计理由、负责人和未写入仓库的团队约定，不能仅凭代码推断，请联系对应团队补充。\n")
        outputs[prefix + names[section["id"]]] = "\n".join(lines)
    covered = [s['id'] for s in dossier['sections'] if s['facts']]
    readiness = ('ready' if {'purpose', 'architecture', 'flows'} <= set(covered) and dossier['status'] in ('completed', 'partial')
                 else 'limited' if fact_count else 'unavailable')
    knowledge_status = 'has_gaps' if pending_count or dossier['plan_gaps'] else 'initial_scope_covered'
    outputs[prefix + "index.md"] = ("# 项目初始化理解\n" + header +
        ('使用状态：可以开始提问。\n\n' if readiness == 'ready' else '使用状态：初步资料已整理，核心理解仍不完整。\n\n') +
        f"本轮调查已结束，自动核对发现 {fact_count} 项，待补充知识记录 {pending_count} 项（含计划缺口，跨栏目及历史轮次去重）。\n\n" +
        (f"可自动处理 {dossier['backlog']['actionable']} 项；已尝试而仍缺依据 {dossier['backlog']['attempted']} 项；"
         f"需外部资料 {dossier['backlog']['external']} 项；分析或待明确问题 {dossier['backlog']['deferred']} 项。\n\n" if dossier.get('backlog') else '') +
        "\n".join(f"- {item['title']}：{item['count']} 项" for item in pending_summary) + "\n\n" +
        "\n".join(f"- [{section['title']}]({names[section['id']]})" for section in dossier["sections"]) +
        "\n\n## 调查缺口\n\n" + "\n".join("- " + (f"{gap.get('section', '项目')}：{gap['question']}" if isinstance(gap, dict) else gap) for gap in dossier["plan_gaps"]) +
        "\n\n已有资料保留在上层目录与 imported/；此处不覆盖团队已有文档。\n")
    created, conflicts, hashes = [], [item["destination"] for item in value.get("migration", {}).get("conflicts", [])], dict(value.get("document_hashes", {}))
    with _lock(root):
        for path, body in outputs.items():
            raw = body.encode()
            if any(re.search(pattern, raw) for pattern in CREDENTIAL_PATTERNS):
                raise FileReadError("sensitive_initialization_document", "生成文档包含已识别凭据，未发布。")
            _parents(root, path)
            existing = root / path
            digest = hashlib.sha256(raw).hexdigest()
            if existing.exists():
                previous = hashlib.sha256(existing.read_bytes()).hexdigest()
                if previous != digest and hashes.get(path) != previous:
                    conflicts.append(path)
                    continue
                if previous != digest:
                    atomic(existing, raw)
            else:
                _create(root, path, raw)
            created.append(path)
            hashes[path] = digest
    return {"documents": created, "conflicts": conflicts, "document_hashes": hashes,
            "summary": {"reviewed_findings": fact_count, "pending_items": pending_count,
                        'readiness': readiness, 'knowledge_status': knowledge_status, 'covered_sections': covered,
                        "pending_categories": pending_summary,
                        "documents": len(created), "unresolved_gaps": len(dossier["plan_gaps"]), **gap_summary(dossier)}}


if __name__ == "__main__":
    from .model_settings import model_settings_scope
    catalog, project_id = sys.argv[1:]
    with model_settings_scope(os.environ.get("ZEST_MODEL_SETTINGS_DIRECTORY") or catalog):
        InitializationStore(catalog).run(project_id)
