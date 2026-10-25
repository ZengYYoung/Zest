"""Loopback workspace API for a single local operator, without login."""

from pathlib import Path
import sqlite3
from uuid import UUID

from .files import FileReadError, read_project_file
from .git import RepositoryError
from .storage import ProjectRegistry, StorageError


def create_app(state_dir):
    try:
        from fastapi import APIRouter, FastAPI, Query
        from fastapi.exceptions import RequestValidationError
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.middleware.trustedhost import TrustedHostMiddleware
        from fastapi.responses import JSONResponse
        from starlette.requests import Request
    except ImportError as exc:
        raise FileReadError("web_dependencies_missing", "请执行 uv sync --extra ai --extra mcp --extra web。") from exc
    registry = ProjectRegistry(state_dir)
    app = FastAPI(title="Zest local workspace API", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    origins = ["http://127.0.0.1:5173", "http://localhost:5173"]
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
    router = APIRouter(prefix="/api")

    @app.middleware("http")
    async def private_response(request, call_next):
        origin = request.headers.get("origin")
        allowed = origin in origins or origin == str(request.base_url).rstrip("/")
        mutation = request.method not in ("GET", "HEAD", "OPTIONS")
        if request.url.path.startswith("/api/") and ((origin is not None and not allowed) or (mutation and not allowed)):
            response = JSONResponse(status_code=403, content={"error": {
                "code": "local_origin_required", "message": "请从本地 Zest 网页提交操作。"}})
        elif mutation and len(await request.body()) > 1024 * 1024:
            response = JSONResponse(status_code=413, content={"error": {
                "code": "request_too_large", "message": "网页操作请求最多 1 MiB。"}})
        else:
            from .model_settings import model_settings_scope
            with model_settings_scope(state_dir):
                response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(FileReadError)
    async def file_error(request, exc):
        missing = exc.code.endswith("not_found")
        return JSONResponse(status_code=404 if missing else 400,
                            content={"error": {"code": exc.code, "message": str(exc)}})

    @app.exception_handler(RepositoryError)
    async def repository_error(request, exc):
        return JSONResponse(status_code=400, content={"error": {"code": "repository_error", "message": str(exc)}})

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error(request, exc):
        return JSONResponse(status_code=503, content={"error": {"code": "state_error", "message": "无法读取或保存本地操作记录。"}})

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return JSONResponse(status_code=503, content={"error": {"code": "state_error", "message": "无法读取或保存本地状态。"}})

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Validation details echo input values by default; keep errors bounded and private.
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "参数不符合接口要求。"}})

    def project(project_id):
        record = registry.get_project(str(project_id))
        if record is None:
            raise FileReadError("project_not_found", "未找到该项目。")
        return record

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "local_workspace"}

    @router.get("/model")
    def model_status():
        from .doctor import readiness
        from .model_connection import ModelConnectionStore
        config = readiness()["checks"]["model"]
        latest = ModelConnectionStore(state_dir).latest()
        current = bool(latest and config.get("configured") and
                       latest["envelope"].get("configuration_revision") == config.get("revision") and
                       latest["envelope"]["target"] == {"provider": config["provider"], "model": config["name"], "base_url": config["base_url"]})
        return {"data": {"configuration": config, "latest_check": latest,
                         "connected": bool(current and latest["status"] == "passed"), "check_matches_target": current}}

    @router.post("/model/settings")
    async def model_settings(request: Request):
        # Secrets bypass the durable operation queue and its request receipts.
        from .model_settings import ModelSettingsStore
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            raise FileReadError("invalid_model_key", "请填写有效的 DeepSeek API Key。") from None
        if not isinstance(body, dict) or set(body) != {"api_key"}:
            raise FileReadError("invalid_model_key", "这里只需填写 DeepSeek API Key。")
        return {"data": ModelSettingsStore(state_dir).save(body["api_key"])}

    @router.get("/model/checks/{check_id}")
    def model_check(check_id: UUID):
        from .model_connection import ModelConnectionStore
        return {"data": ModelConnectionStore(state_dir).show(str(check_id))}

    @router.get("/projects")
    def projects():
        return {"data": registry.list_projects()}

    @router.get("/projects/{project_id}")
    def shown(project_id: UUID):
        from .releases import ReleaseStore
        return {"data": {"registration": project(project_id), "version": ReleaseStore(state_dir).active(str(project_id))}}

    @router.get("/projects/{project_id}/overview")
    def overview(project_id: UUID):
        from .overview import build_overview
        return {"data": build_overview(project(project_id))}

    @router.get("/projects/{project_id}/file")
    def file(project_id: UUID, path: str = Query(min_length=1, max_length=1024), commit: str | None = None,
             start_line: int = Query(default=1, ge=1), end_line: int | None = Query(default=None, ge=1)):
        return {"data": read_project_file(project(project_id), path, commit=commit, start_line=start_line, end_line=end_line)}

    @router.get("/projects/{project_id}/knowledge")
    def knowledge(project_id: UUID):
        from .knowledge import KnowledgeStore
        project(project_id)
        return {"data": KnowledgeStore(state_dir).list_notes(str(project_id))}

    @router.get("/projects/{project_id}/structure")
    def structure(project_id: UUID, commit: str | None = None):
        from .structure import build_structure
        return {"data": build_structure(project(project_id), commit=commit)}

    @router.get("/tasks")
    def tasks(project_id: UUID | None = None):
        from .tasks import TaskRunner
        if project_id is not None:
            project(project_id)
        records = TaskRunner(state_dir).list_tasks()
        return {"data": [record for record in records if project_id is None or record["project_id"] == str(project_id)]}

    @router.get("/projects/{project_id}/conversations")
    def conversations(project_id: UUID):
        from .conversations import ConversationStore
        project(project_id)
        return {"data": sorted(ConversationStore(state_dir).list_for_project(str(project_id)),
                               key=lambda row: row["updated_at"], reverse=True)}

    @router.get("/projects/{project_id}/initialization")
    def initialization(project_id: UUID):
        from .initialization import InitializationStore
        return {"data": InitializationStore(state_dir).show(str(project_id), observe_git=False)}

    @router.get("/projects/{project_id}/maintenance")
    def maintenance_status(project_id: UUID):
        from .maintenance import MaintenanceStore
        return {"data": MaintenanceStore(state_dir).show(str(project_id))}

    @router.get("/projects/{project_id}/understanding-approval")
    def understanding_approval(project_id: UUID):
        from .understanding_approval import UnderstandingApproval
        return {"data": UnderstandingApproval(state_dir).show(str(project_id))}

    @router.post("/projects/{project_id}/check")
    async def check_project(project_id: UUID, request: Request):
        from .maintenance import MaintenanceStore
        try:
            body = await request.json()
        except ValueError:
            raise FileReadError("invalid_check_reason", "请提供更新检查的触发来源。") from None
        if not isinstance(body, dict) or set(body) != {"reason"} or not isinstance(body["reason"], str) or body["reason"] not in {"open", "switch", "task", "manual"}:
            raise FileReadError("invalid_check_reason", "更新检查的触发来源无效。")
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(MaintenanceStore(state_dir).check, str(project_id), reason=body["reason"])
        return {"data": result}

    @router.post("/projects/{project_id}/maintenance/enable")
    def enable_maintenance(project_id: UUID):
        from .maintenance import MaintenanceStore
        return {"data": MaintenanceStore(state_dir).enable(str(project_id))}

    @router.get("/conversations/{conversation_id}")
    def conversation(conversation_id: UUID, before: int | None = Query(default=None, ge=1)):
        from .conversations import ConversationStore
        return {"data": ConversationStore(state_dir).show(str(conversation_id), before=before)}

    @router.get("/projects/{project_id}/cognition")
    def cognition_runs(project_id: UUID):
        from .cognition_store import CognitionStore
        project(project_id)
        return {"data": CognitionStore(state_dir).list_runs(str(project_id))}

    @router.get("/cognition/{run_id}")
    def cognition_run(run_id: UUID):
        from .cognition_store import CognitionStore
        return {"data": CognitionStore(state_dir).show(str(run_id))}

    @router.get("/cognition/{run_id}/dossier")
    def cognition_dossier(run_id: UUID):
        from .cognition_store import CognitionStore
        return {"data": CognitionStore(state_dir).dossier(str(run_id))}

    @router.get("/tasks/{task_id}")
    def task(task_id: UUID):
        from .tasks import TaskRunner
        return {"data": TaskRunner(state_dir).show(str(task_id))}

    @router.get("/projects/{project_id}/repairs")
    def repairs(project_id: UUID):
        from .repairs import RepairStore
        project(project_id)
        return {"data": RepairStore(state_dir).list_repairs(str(project_id))}

    @router.get("/repairs/{repair_id}")
    def repair(repair_id: UUID):
        from .repairs import RepairStore
        return {"data": RepairStore(state_dir).show(str(repair_id))}

    @router.get("/verifications/{verification_id}")
    def verification(verification_id: UUID):
        from .verification import VerificationStore
        return {"data": VerificationStore(state_dir).show(str(verification_id))}

    @router.get("/projects/{project_id}/verifications")
    def verification_list(project_id: UUID):
        from .verification import VerificationStore
        project(project_id)
        return {"data": VerificationStore(state_dir).list_verifications(str(project_id))}

    @router.get("/projects/{project_id}/releases")
    def release_list(project_id: UUID):
        from .releases import ReleaseStore
        project(project_id)
        return {"data": ReleaseStore(state_dir).list_operations(str(project_id))}

    @router.get("/releases/{operation_id}")
    def release(operation_id: UUID):
        from .releases import ReleaseStore
        return {"data": ReleaseStore(state_dir).show(str(operation_id))}

    from .web_commands import Command
    from .web_operations import WebOperations
    operations = WebOperations(state_dir)

    @router.post("/operations", status_code=202)
    def submit(command: Command):
        record, created = operations.submit(command.request_id, command.action, command.parameters)
        if created:
            operations.launch(record["id"])
        return {"data": record}

    @router.get("/operations")
    def operation_list():
        return {"data": operations.list()}

    @router.get("/operations/{operation_id}")
    def operation(operation_id: UUID):
        return {"data": operations.show(str(operation_id))}

    @router.post("/operations/{operation_id}/start", status_code=202)
    def operation_start(operation_id: UUID):
        operations.launch(str(operation_id))
        return {"data": operations.show(str(operation_id))}

    @router.get("/projects/{project_id}/context")
    def management_context(project_id: UUID):
        from .management_context import build_management_context
        project(project_id)
        return {"data": build_management_context(state_dir, str(project_id))}

    @router.get("/projects/{project_id}/refresh-preview")
    def refresh_preview(project_id: UUID):
        from .cognition_refresh import build_refresh_preview
        project(project_id)
        return {"data": build_refresh_preview(state_dir, str(project_id))}

    @router.get("/projects/{project_id}/documents")
    def shared_documents(project_id: UUID):
        from .documents import document_catalog
        return {"data": document_catalog(project(project_id))}

    @router.get("/projects/{project_id}/documents/workspace")
    def workspace_documents(project_id: UUID):
        from .documents import workspace_documents
        return {"data": workspace_documents(project(project_id))}

    @router.get("/projects/{project_id}/documents/read")
    def workspace_document(project_id: UUID, path: str = Query(min_length=1, max_length=1024),
                           start_line: int = Query(default=1, ge=1), end_line: int = Query(default=200, ge=1)):
        from .documents import read_workspace_document
        return {"data": read_workspace_document(project(project_id), path, start_line=start_line, end_line=end_line)}

    @router.get("/cognition/{run_id}/preview")
    def cognition_preview(run_id: UUID):
        from .cognition_store import CognitionStore
        return {"data": CognitionStore(state_dir).show(str(run_id), include_envelope=True)}

    @router.get("/cognition/{run_id}/context-requests")
    def cognition_context(run_id: UUID):
        from .cognition_store import CognitionStore
        return {"data": CognitionStore(state_dir).context_requests(str(run_id))}

    @router.get("/cognition/{run_id}/calls")
    def cognition_calls(run_id: UUID):
        from .cognition_store import CognitionStore
        return {"data": CognitionStore(state_dir).show(str(run_id), include_calls=True)}

    @router.get("/tasks/{task_id}/preview")
    def task_preview(task_id: UUID):
        from .tasks import TaskRunner
        from .qa import model_payload
        record = TaskRunner(state_dir).show(str(task_id))
        return {"data": {"task_id": str(task_id), "status": record["status"],
                         "payload_sha256": record["payload_sha256"], "target": record["target"],
                         "payload": model_payload(record["context"]) if record["context"] else None}}

    @router.get("/knowledge/{note_id}")
    def knowledge_note(note_id: UUID):
        from .knowledge import KnowledgeStore
        return {"data": KnowledgeStore(state_dir).get(str(note_id))}

    @router.get("/projects/{project_id}/surveys")
    def survey_list(project_id: UUID):
        from .survey import SurveyStore
        project(project_id)
        return {"data": SurveyStore(state_dir).list_surveys(str(project_id))}

    @router.get("/surveys/{survey_id}")
    def survey(survey_id: UUID):
        from .survey import SurveyStore
        return {"data": SurveyStore(state_dir).show(str(survey_id))}

    def analysis_store(kind):
        from .evaluation import EvaluationStore
        from .cognition_resolution import ResolutionStore
        return (EvaluationStore if kind == "evaluations" else ResolutionStore)(state_dir)

    @router.get("/projects/{project_id}/{kind}")
    def analysis_list(project_id: UUID, kind: str):
        from contextlib import closing
        import sqlite3
        from .resources import project_states
        if kind not in ("evaluations", "resolutions"):
            raise FileReadError("route_not_found", "未找到该接口。")
        project(project_id)
        ids = set()
        for directory in project_states(state_dir, str(project_id)):
            path = directory / (kind + ".sqlite3")
            if path.is_symlink():
                raise FileReadError("analysis_storage_error", "分析数据库不能是符号链接。")
            if not path.exists():
                continue
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (kind,)).fetchone():
                    ids.update(row[0] for row in conn.execute(f"SELECT id FROM {kind} WHERE project_id=?", (str(project_id),)))
        store = analysis_store(kind)
        records = [store.show(identity) for identity in ids]
        return {"data": sorted(records, key=lambda r: r["created_at"], reverse=True)[:100]}

    # Static analysis route names keep OpenAPI and validation explicit.
    @router.get("/evaluations/{record_id}")
    def evaluation(record_id: UUID):
        return {"data": analysis_store("evaluations").show(str(record_id))}

    @router.get("/evaluations/{record_id}/preview")
    def evaluation_preview(record_id: UUID):
        return {"data": analysis_store("evaluations").show(str(record_id), preview=True)}

    @router.get("/evaluations/{record_id}/report")
    def evaluation_report(record_id: UUID):
        return {"data": analysis_store("evaluations").report(str(record_id))}

    @router.get("/resolutions/{record_id}")
    def resolution(record_id: UUID):
        return {"data": analysis_store("resolutions").show(str(record_id))}

    @router.get("/resolutions/{record_id}/preview")
    def resolution_preview(record_id: UUID):
        return {"data": analysis_store("resolutions").show(str(record_id), preview=True)}

    app.include_router(router)
    frontend = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if frontend.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=frontend, html=True), name="console")
    return app


def serve(state_dir, *, port=8000):
    if not 1024 <= port <= 65535:
        raise FileReadError("invalid_web_port", "端口须为 1024—65535。")
    app = create_app(state_dir)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
