"""Loopback read-only API for Zest's local records. No model or write endpoints."""

from dataclasses import dataclass, field
import hmac
import os
from pathlib import Path
import re
from uuid import UUID

from .files import CREDENTIAL_PATTERNS, FileReadError, read_project_file
from .git import RepositoryError
from .storage import ProjectRegistry, StorageError


@dataclass(frozen=True)
class WebSettings:
    token: str = field(repr=False)

    def __post_init__(self):
        if (not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.token)
                or any(re.search(pattern, self.token.encode()) for pattern in CREDENTIAL_PATTERNS)):
            raise FileReadError("invalid_web_token", "请配置独立的 32—256 位 URL-safe Web token，不复用模型或 GitHub 密钥。")

    @classmethod
    def from_env(cls):
        from dotenv import dotenv_values
        values = dotenv_values(Path.cwd() / ".env", interpolate=False)
        token = os.environ.get("ZEST_WEB_API_TOKEN", values.get("ZEST_WEB_API_TOKEN") or "")
        model_key = os.environ.get("ZEST_MODEL_API_KEY", values.get("ZEST_MODEL_API_KEY") or "")
        if not token or token == model_key:
            raise FileReadError("web_not_configured", "请在本地 .env 配置独立的 ZEST_WEB_API_TOKEN，不能与模型密钥相同。")
        return cls(token)


def create_app(state_dir, *, settings=None):
    try:
        from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
        from fastapi.exceptions import RequestValidationError
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.middleware.trustedhost import TrustedHostMiddleware
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise FileReadError("web_dependencies_missing", "请执行 uv sync --extra ai --extra mcp --extra web。") from exc
    config = settings or WebSettings.from_env()
    registry = ProjectRegistry(state_dir)
    app = FastAPI(title="Zest local read-only API", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
                       allow_credentials=False, allow_methods=["GET"], allow_headers=["Authorization"])

    def authorized(authorization: str | None = Header(default=None)):
        if (authorization is None or len(authorization) > 512 or not authorization.startswith("Bearer ")
                or not hmac.compare_digest(authorization[7:].encode(), config.token.encode())):
            raise HTTPException(status_code=401, detail="需要有效的本地 Bearer token。", headers={"WWW-Authenticate": "Bearer"})

    router = APIRouter(prefix="/api", dependencies=[Depends(authorized)])

    @app.middleware("http")
    async def private_response(request, call_next):
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

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return JSONResponse(status_code=503, content={"error": {"code": "state_error", "message": "无法读取本地状态。"}})

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
        return {"status": "ok", "mode": "local_read_only"}

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

    @router.get("/tasks")
    def tasks(project_id: UUID | None = None):
        from .tasks import TaskRunner
        if project_id is not None:
            project(project_id)
        records = TaskRunner(state_dir).list_tasks()
        return {"data": [record for record in records if project_id is None or record["project_id"] == str(project_id)]}

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

    app.include_router(router)
    return app


def serve(state_dir, *, port=8000):
    if not 1024 <= port <= 65535:
        raise FileReadError("invalid_web_port", "端口须为 1024—65535。")
    app = create_app(state_dir)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
