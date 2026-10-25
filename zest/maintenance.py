"""Event-triggered Git checks and bounded, explicitly enabled automatic refresh.

Only POST/event handlers check Git. Reading progress never polls Git or executes
a model. An immutable run ID is saved before dispatch; failed/unknown versions
are never automatically retried by another event.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

from .cognition_contracts import POLICY
from .cognition_store import CognitionStore, digest
from .files import FileReadError
from .git import compare_commits, inspect_repository, selected_commit
from .git import RepositoryError
from .initialization import InitializationStore, atomic, now, publish_documents
from .models import ModelSettings, model_target
from .resources import _regular
from .maintenance_summary import meaningful_changes, summarize_changes

REASONS = {"open", "switch", "task", "manual"}
RUNNING = {"queued", "preparing", "planning", "investigating", "reviewing", "publishing"}
AUTO_POLICY = {"version": 2, "triggers": sorted(REASONS), "source": "safe_project_commit_sources",
               "max_model_calls_per_update": POLICY["max_model_calls"], "max_seconds_per_update": POLICY["max_seconds"],
               "automatic_retry": False, "scope_expansion": "project_boundary_only",
               "source_policy_version": 1, "max_request_chars": POLICY['max_extended_prompt_chars']}


class MaintenanceStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.initialization = InitializationStore(state_dir)

    def read(self, directory):
        path = directory / "maintenance.json"
        _regular(path)
        if not path.exists():
            return {"schema_version": 1, "attempts": {}, "owned_hashes": {}}
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError
            value = json.loads(path.read_bytes())
            if value["schema_version"] != 1 or not isinstance(value["attempts"], dict):
                raise ValueError
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FileReadError("maintenance_state_error", "更新记录无法读取，未重新调用模型。") from exc

    def save(self, directory, value):
        value["updated_at"] = now()
        raw = json.dumps(value, ensure_ascii=False).encode()
        if len(raw) > 1024 * 1024:
            raise FileReadError("maintenance_history_budget", "更新记录达到保存容量，请先归档历史记录。")
        atomic(directory / "maintenance.json", raw)

    @contextmanager
    def lock(self, directory, *, worker=False, required=True):
        import fcntl
        path = directory / ("maintenance-worker.lock" if worker else "maintenance.lock")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _regular(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if required:
                    raise FileReadError("maintenance_busy", "项目更新检查正在处理，请稍后查看原记录。")
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def worker_active(self, directory):
        import fcntl
        path = directory / "maintenance-worker.lock"
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

    def show(self, project_id):
        _, directory = self.initialization.location(project_id)
        value = self.read(directory)
        result = {k: v for k, v in value.items() if k not in {"attempts", "owned_hashes", "grant", "snapshots"}}
        result.update(project_id=project_id, enabled=bool(value.get("grant")), policy=AUTO_POLICY,
                      worker_active=self.worker_active(directory))
        result.setdefault("status", "not_checked")
        if value.get("active_run_id"):
            try:
                research = CognitionStore(self.state_dir).show(value["active_run_id"])
            except FileReadError as exc:
                if exc.code != "cognition_not_found":
                    raise
            else:
                result["research"] = research
                from .document_pages import DocumentPages
                pages = DocumentPages(self.state_dir, value['active_run_id'])
                if result.get('document_generation', {}).get('run_id') != value['active_run_id']:
                    result.pop('document_generation', None)
                if pages.value:
                    result['document_generation'] = pages.summary()
                    recovery = result['document_generation']['recovery']
                    if result['status'] == 'partial' and recovery['action'] in ('new_round', 'inspect', 'resume') and not result['worker_active']:
                        result['message'] = recovery['message']
                    if result['status'] == 'partial' and result['document_generation']['status'] == 'completed' and result['document_generation'].get('index_candidate'):
                        result['message'] = '本轮正文已全部生成并保存；原文档目录有人工修改，已保留，并生成新目录候选供查看。'
                if ((research.get('error') or {}).get('code') == 'cognition_prompt_budget'
                        and research.get('call_count') == 0 and not result['worker_active']
                        and result['status'] in {'partial', 'failed', 'interrupted'}):
                    result.update(status='blocked', message='增量理解尚未开始：请求资料过多，在发送前已停止。旧版本项目理解仍保留。',
                                  error=research['error'], can_reprepare=True)
                if value.get("status") in RUNNING and research["status"] not in {"completed", "partial", "failed"}:
                    result["status"] = research["status"]
        if result["status"] in RUNNING - {"queued"} and not result["worker_active"] and not result.get("research", {}).get("worker_active"):
            result["status"] = "interrupted"
        return result

    def reprepare(self, project_id):
        """Explicitly replace only an unsent oversized attempt, never unknown calls."""
        _, directory = self.initialization.location(project_id)
        with self.lock(directory):
            shown = self.show(project_id)
            if not shown.get('can_reprepare'):
                raise FileReadError('maintenance_reprepare_not_allowed', '只能重新准备发送前因资料过多而停止的更新；其他结果请核对原任务。')
            value = self.read(directory)
            target = value['target_commit']
            value.setdefault('previous_attempts', []).append({'target_commit': target, **value['attempts'].get(target, {})})
            value['attempts'].pop(target, None)
            value.update(status='reprepare_requested', message='正在重新准备有界的增量调查。')
            self.save(directory, value)
        return self.check(project_id, reason='manual')

    def enable(self, project_id):
        project, directory = self.initialization.location(project_id)
        initial = self.initialization.read(directory)
        if not initial or not initial.get("run_id"):
            raise FileReadError("maintenance_no_baseline", "请先准备项目初始化。")
        settings = ModelSettings.from_env()
        grant = {"project_id": project_id, "root_path": project["root_path"], "target": model_target(settings),
                 "policy": AUTO_POLICY, "initial_run_id": initial["run_id"], "created_at": now()}
        with self.lock(directory):
            value = self.read(directory)
            if (value.get("grant") and digest(value["grant"]) == value.get("grant_sha256")
                    and all(value["grant"].get(key) == grant[key]
                            for key in ("target", "policy", "root_path", "initial_run_id"))):
                return self.show(project_id)
            value.update(grant=grant, grant_sha256=digest(grant))
            self.remember_owned(value, initial.get("owned_artifacts", {}))
            self.save(directory, value)
        return self.show(project_id)

    @staticmethod
    def remember_owned(value, hashes):
        for path, checksum in hashes.items():
            known = value.setdefault("owned_hashes", {}).setdefault(path, [])
            value["owned_hashes"][path] = list(dict.fromkeys([*known, checksum]))[-32:]

    def disable(self, project_id):
        _, directory = self.initialization.location(project_id)
        with self.lock(directory):
            value = self.read(directory)
            value.pop('grant', None)
            value.pop('grant_sha256', None)
            self.save(directory, value)
        return self.show(project_id)

    def auto_start(self, project_id):
        _, directory = self.initialization.location(project_id)
        with self.lock(directory):
            value = self.read(directory)
            if value.get('status') not in ('awaiting_approval', 'awaiting_context_approval') or not value.get('active_run_id') or self.worker_active(directory):
                return False
            from .understanding_approval import UnderstandingApproval
            if not UnderstandingApproval(self.state_dir).approve(project_id, value['active_run_id'], contexts=value['status'] == 'awaiting_context_approval'):
                return False
            value.update(status='queued', message='已自动批准，继续本轮项目理解。')
            self.save(directory, value)
        self.launch(project_id)
        return True

    def check(self, project_id, *, reason):
        if reason not in REASONS:
            raise FileReadError("invalid_check_reason", "更新检查仅由打开、切换、任务完成或手动操作触发。")
        project, directory = self.initialization.location(project_id)
        git = inspect_repository(project["root_path"])
        current = selected_commit(project, git)
        launch = False
        checked_changes = None
        with self.lock(directory):
            value = self.read(directory)
            initial = self.initialization.read(directory)
            value.update(project_id=project_id, last_check={"id": str(uuid4()), "reason": reason, "at": now(), "commit": current,
                         "head_commit": git.head_commit, "branch": git.branch, "dirty": git.dirty})
            if not initial or not initial.get("run_id"):
                value.update(status="not_initialized", message="项目状态已检查，尚未完成初始化。")
            else:
                initial_run = CognitionStore(self.state_dir).show(initial["run_id"])
                if initial_run["status"] in {"completed", "partial"}:
                    value.setdefault("snapshots", {}).setdefault(initial_run["commit"], {
                        "run_id": initial_run["id"], "document_hashes": initial.get("document_hashes", {})})
                baseline = value.get("baseline_run_id") or (initial_run["id"] if initial_run["status"] in {"completed", "partial"} else None)
                self.remember_owned(value, initial.get("document_hashes", {}))
                self.remember_owned(value, initial.get("owned_artifacts", {}))
                if not value.get('baseline_run_id') and baseline == initial_run['id']:
                    # The switch may be enabled before initialization publishes.
                    # Replace that early empty placeholder with its owned hashes.
                    value['document_hashes'] = initial.get('document_hashes', {})
                else:
                    value.setdefault("document_hashes", initial.get("document_hashes", {}))
                if baseline is None:
                    value.update(status="initializing", message="首次初始化尚未完成，已保留当前状态。")
                elif value.get("status") in RUNNING or value.get("status") in {"awaiting_approval", "awaiting_context_approval", "pending_execution", "interrupted", "publication_failed"}:
                    if current != value.get("target_commit"):
                        value["pending_commit"] = current
                    # An event observes an existing request; it never replays it.
                    launch = value.get("status") == "queued" and reason == "manual" and not self.worker_active(directory)
                else:
                    snapshot = value.get("snapshots", {}).get(current)
                    if snapshot:
                        baseline = snapshot["run_id"]
                        value["document_hashes"] = snapshot["document_hashes"]
                    base = CognitionStore(self.state_dir).show(baseline)
                    value.update(baseline_run_id=baseline, knowledge_commit=base["commit"])
                    if current is None or current == base["commit"]:
                        value.pop('error', None)
                        value.update(status="current", message="项目状态已检查，认知版本未变化。")
                    elif current in value["attempts"]:
                        old = value["attempts"][current]
                        value.update(status=old["status"], message=old["message"], active_run_id=old.get("run_id"), target_commit=current)
                    else:
                        value.pop('error', None)
                        changes = compare_commits(Path(project["root_path"]), base["commit"], current)
                        meaningful, excluded = meaningful_changes(project, current, changes, value["owned_hashes"])
                        checked_changes = meaningful, excluded
                        value.update(changes=meaningful, excluded_paths=excluded, target_commit=current)
                        if not meaningful:
                            value.update(status="generated_only", message="已检查新版本，未发现需要重新调查的项目资料。")
                            value["attempts"][current] = {"status": value["status"], "message": value["message"]}
                        elif not value.get("grant"):
                            value.update(status="permission_required", message="发现新提交，启用自动维护后将增量更新项目理解。")
                        else:
                            settings = ModelSettings.from_env()
                            grant = value["grant"]
                            if digest(grant) != value.get("grant_sha256") or grant["root_path"] != project["root_path"] or grant["policy"] != AUTO_POLICY or grant["target"] != model_target(settings):
                                value.update(status="permission_required", message="模型或自动维护范围已变化，请重新确认。")
                            else:
                                run_id = str(uuid4())
                                value.update(status="queued", active_run_id=run_id, message=f"发现 {len(meaningful)} 个文件变化，正在增量更新。")
                                value["attempts"][current] = {"status": "queued", "message": value["message"], "run_id": run_id}
                                launch = True
            # A receipt belongs to this check and the knowledge version at its
            # start. Never reuse stale `changes` from an earlier update.
            before = value.get('knowledge_commit')
            if before and current:
                if before == current:
                    checked_changes = [], []
                elif checked_changes is None:
                    checked_changes = meaningful_changes(project, current,
                        compare_commits(Path(project['root_path']), before, current), value.get('owned_hashes', {}))
                value['last_check']['summary'] = summarize_changes(project, before, current, *checked_changes)
            value.pop('document_integrity', None)
            if value.get('status') in ('current', 'generated_only'):
                from .knowledge_snapshots import artifact_integrity
                integrity = artifact_integrity(project, value.get('document_hashes', {}))
                if integrity:
                    value.update(document_integrity=integrity, status='partial',
                                 message='源码版本已检查，生成文档存在人工改动或缺失；原文已保留，请在项目状态核对。')
                elif baseline:
                    from .document_pages import DocumentPages
                    pages = DocumentPages(self.state_dir, baseline)
                    if pages.value and pages.value['status'] != 'completed':
                        value.update(status='partial', message='源码版本已检查，原文档流程仍有未完成页面；不会自动重发旧请求。')
            self.save(directory, value)
        if launch:
            self.launch(project_id)
        return self.show(project_id)

    def launch(self, project_id):
        _, directory = self.initialization.location(project_id)
        value = self.read(directory)
        if value.get("status") != "queued" or self.worker_active(directory):
            return
        from .model_settings import worker_environment
        try:
            child = subprocess.Popen([sys.executable, "-m", "zest.maintenance", str(self.state_dir), project_id],
                cwd=Path(__file__).resolve().parent.parent, env=worker_environment(), start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise FileReadError("maintenance_worker_start_failed", "检查记录已保存，但后台更新未启动；请手动检查原任务。") from exc
        threading.Thread(target=child.wait, daemon=True).start()

    def run(self, project_id, *, model=None):
        project, directory = self.initialization.location(project_id)
        follow = False
        with self.lock(directory, worker=True, required=False) as acquired:
            if not acquired:
                return self.show(project_id)
            with self.lock(directory):
                value = self.read(directory)
                if value.get("status") != "queued":
                    return self.show(project_id)
                value["status"] = "preparing"
                self.save(directory, value)
            try:
                grant = value.get("grant")
                if not grant:
                    raise FileReadError('maintenance_grant_changed', '自动批准已关闭，未批准新的调查请求。')
                settings = model.settings if model else ModelSettings.from_env()
                if digest(grant) != value["grant_sha256"] or grant["target"] != model_target(settings) or grant["policy"] != AUTO_POLICY:
                    raise FileReadError("maintenance_grant_changed", "自动维护授权或模型目标已变化，未继续调用。")
                if selected_commit(project, inspect_repository(project["root_path"])) != value["target_commit"]:
                    raise FileReadError("maintenance_target_changed", "准备更新时项目版本已变化，本次未调用模型，请再次检查。")
                from .cognition_refresh import prepare_refresh
                try:
                    active = CognitionStore(self.state_dir).show(value['active_run_id'])
                except FileReadError as exc:
                    if exc.code != 'cognition_not_found': raise
                    active = None
                run = active if active and active.get('document_continuation') else prepare_refresh(
                    self.state_dir, project_id, base_run_id=value["baseline_run_id"], target=grant["target"],
                    run_id=value["active_run_id"], focus_paths={item["path"] for item in value.get("changes", [])})
                if run["commit"] != value["target_commit"]:
                    raise FileReadError("maintenance_target_changed", "资料准备时版本发生变化，本次未调用模型，请再次检查。")
                value["automatic_decision"] = {"grant_sha256": value["grant_sha256"], "scope_sha256": run["approval_sha256"], "at": now()}
                with self.lock(directory):
                    current = self.read(directory)
                    current["automatic_decision"] = value["automatic_decision"]
                    self.save(directory, current)
                from .understanding_approval import UnderstandingApproval, run_understanding
                approval = UnderstandingApproval(self.state_dir)
                automatic = approval.show(project_id)['configured']
                approved = approval.approve(project_id, run['id']) if automatic and run['status'] == 'awaiting_approval' else False
                if run["status"] == "awaiting_approval" and not approved and (automatic or run["scope_review"].get("requires_discussion")):
                    value.update(status="awaiting_approval", message="增量范围较大，已暂停等待核对，尚未调用模型。")
                else:
                    if run["status"] == "awaiting_approval" and not approved:
                        CognitionStore(self.state_dir).decide(run["id"], approved=True, approval_sha256=run["approval_sha256"])
                    run = CognitionStore(self.state_dir).show(run["id"])
                    result = run_understanding(self.state_dir, project_id, run['id'], model=model) if run['status'] == 'queued' else run
                    value["status"] = result["status"]
                    value["message"] = "项目理解已增量更新。" if result["status"] == "completed" else "增量更新保留了待核对内容。"
                    if result["status"] in {"completed", "partial"}:
                        dossier = CognitionStore(self.state_dir).dossier(run["id"])
                        if (any(section["facts"] for section in dossier["sections"]) or
                                result.get('incremental_enabled') and result.get('plan') and result['call_count'] > 0):
                            from .document_pages import generate_documents
                            pages = generate_documents(self.state_dir, project, dossier, model=model)
                            value.update(publish_documents(project, dossier, value, pages=pages))
                            value.update(baseline_run_id=run["id"], knowledge_commit=run["commit"])
                            value.setdefault("snapshots", {})[run["commit"]] = {"run_id": run["id"], "document_hashes": value["document_hashes"]}
                            self.remember_owned(value, value["document_hashes"])
                            if value["conflicts"]:
                                value.update(status="partial", message="理解已更新，部分文档存在人工改动，已保留原文。")
                            elif value.get('document_generation', {}).get('status', 'completed') != 'completed':
                                value.update(status='partial', message='理解已更新，部分文档页面尚未完成，已保留原文和页面记录。')
                            elif run.get('document_continuation'):
                                value.update(status='completed', message='本轮文档已生成并保存；沿用原调查事实，原有知识缺口仍保留。')
                        from .index import RetrievalIndex
                        try:
                            value["index"] = RetrievalIndex(self.state_dir).build(project)
                        except FileReadError:
                            value["message"] += "源码索引未更新，可继续按当前文件检索。"
                    if result["status"] in {"failed", "pending_execution", "awaiting_context_approval"}:
                        value["message"] = "增量更新已暂停，保留原认知版本，不自动重试。"
                        value["error"] = result.get("error")
            except (FileReadError, RepositoryError, OSError) as exc:
                value.update(status="failed", message="增量更新未完成，保留原认知版本，不自动重试。",
                             error={"code": getattr(exc, "code", "maintenance_write_failed"), "message": str(exc) if isinstance(exc, FileReadError) else "无法保存项目更新结果。"})
            with self.lock(directory):
                current = self.read(directory)
                value["last_check"] = current.get("last_check", value["last_check"])
                pending = current.get("pending_commit")
                value.pop("pending_commit", None)
                value["attempts"][value["target_commit"]] = {"status": value["status"], "message": value["message"], "run_id": value["active_run_id"]}
                self.save(directory, value)
                follow = bool(pending and pending != value["target_commit"] and value["status"] in {"completed", "partial"})
        if follow:
            self.check(project_id, reason="task")
        return self.show(project_id)

    def decide(self, project_id, *, approved, sha256, actor=None, sequence=None):
        _, directory = self.initialization.location(project_id)
        with self.lock(directory):
            value = self.read(directory)
            if not value.get("active_run_id"):
                raise FileReadError("maintenance_not_pending", "没有等待决定的增量调查。")
            store = CognitionStore(self.state_dir)
            if actor is None:
                run = store.decide(value["active_run_id"], approved=approved, approval_sha256=sha256)
            else:
                run = store.decide_context(value["active_run_id"], actor, sequence, approved=approved, request_sha256=sha256)
            value.update(status="queued" if run["status"] == "queued" else run["status"], message="继续原增量更新。" if approved else "本次增量更新已暂缓。")
            self.save(directory, value)
        self.launch(project_id)
        return self.show(project_id)

    def resume(self, project_id):
        _, directory = self.initialization.location(project_id)
        with self.lock(directory):
            value = self.read(directory)
            if not value.get("active_run_id") or self.worker_active(directory):
                return self.show(project_id)
            from .document_pages import DocumentPages
            pages = DocumentPages(self.state_dir, value['active_run_id'])
            if pages.value:
                recovery = pages.summary()['recovery']
                if recovery['action'] == 'new_round':
                    raise FileReadError('document_new_round_required', recovery['message'])
                if recovery['action'] == 'inspect':
                    return self.show(project_id)
            store = CognitionStore(self.state_dir)
            try:
                run = store.show(value["active_run_id"])
            except FileReadError as exc:
                if exc.code != "cognition_not_found":
                    raise
                run = {"id": value["active_run_id"], "status": "queued"}
            if run["status"] not in {"queued", "completed", "partial"}:
                run = store.recover(run["id"])
            if run["status"] in {"queued", "completed", "partial"}:
                value.update(status="queued", message="已核对原记录，继续增量更新。")
                self.save(directory, value)
        self.launch(project_id)
        return self.show(project_id)

    def continue_documents(self, project_id, *, source_run_id, source_sha256):
        project, directory = self.initialization.location(project_id)
        from .document_rounds import create_round
        with self.lock(directory):
            value = self.read(directory)
            active_id = value.get('active_run_id')
            if active_id != source_run_id:
                current = CognitionStore(self.state_dir).show(active_id) if active_id else {}
                context = current.get('document_continuation', {})
                if context.get('previous_run_id') == source_run_id and context.get('previous_pages_sha256') == source_sha256:
                    return self.show(project_id)  # A duplicate click observes the same new round.
                raise FileReadError('document_source_changed', '当前更新身份已变化，请刷新后重新确认。')
            if self.worker_active(directory) or value.get('status') in RUNNING:
                raise FileReadError('maintenance_busy', '原更新仍在执行，请等待原任务。')
            if not value.get('grant'):
                raise FileReadError('maintenance_grant_changed', '请先按当前配置开启项目自动理解，再开始文档轮次。')
            pending = value.get('document_round_pending')
            if not pending or pending['source_run_id'] != source_run_id or pending['source_sha256'] != source_sha256:
                pending = {'run_id': str(uuid4()), 'source_run_id': source_run_id, 'source_sha256': source_sha256}
                value['document_round_pending'] = pending
                self.save(directory, value)
            run = create_round(self.state_dir, project, source_run_id, pending['run_id'], source_sha256)
            value.update(active_run_id=run['id'], target_commit=run['commit'], status='queued', message='已明确开启新一轮文档生成，复用原调查事实。')
            value.setdefault('document_round_history', []).append(pending)
            value.pop('document_round_pending', None)
            value['attempts'][run['commit']] = {'status': 'queued', 'message': value['message'], 'run_id': run['id']}
            self.save(directory, value)
        self.launch(project_id)
        return self.show(project_id)

    def decide_contexts(self, project_id, requests, *, approved):
        _, directory = self.initialization.location(project_id)
        with self.lock(directory):
            value = self.read(directory)
            if not value.get('active_run_id'):
                raise FileReadError('maintenance_not_pending', '没有等待决定的增量调查。')
            run = CognitionStore(self.state_dir).decide_contexts(value['active_run_id'], requests, approved=approved)
            value.update(status=run['status'], message='已处理本批请求，继续原增量更新。')
            self.save(directory, value)
        self.launch(project_id)
        return self.show(project_id)


def after_task(state_dir, result):
    """A completed task is an event; reads/resume views never call this hook."""
    if result.get("status") != "completed":
        return
    try:
        store = MaintenanceStore(state_dir)
        _, directory = store.initialization.location(result["project_id"])
        if store.initialization.read(directory) is not None:
            store.check(result["project_id"], reason="task")
    except (FileReadError, RepositoryError, OSError):
        result["maintenance_warning"] = "任务已完成，项目状态检查未完成，请使用检查更新。"


if __name__ == "__main__":
    from .model_settings import model_settings_scope
    catalog, project_id = sys.argv[1:]
    with model_settings_scope(os.environ.get("ZEST_MODEL_SETTINGS_DIRECTORY") or catalog):
        MaintenanceStore(catalog).run(project_id)
