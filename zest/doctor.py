"""Read-only local readiness checks; no model requests or project execution."""

from importlib import metadata, util
import platform
import sqlite3
import subprocess
import sys

from .files import FileReadError
from .git import git_environment
from .models import ModelSettings, model_target
from .verification import DockerEngine, _redact


AI_PACKAGES = ("langchain", "langchain-deepseek", "langgraph", "langgraph-checkpoint-sqlite", "python-dotenv")
MCP_PACKAGES = ("fastmcp",)


def readiness(*, check_docker=False, image=None, socket_path=None):
    checks = {"python": {"available": sys.version_info >= (3, 11), "version": platform.python_version()},
              "sqlite": {"available": True, "version": sqlite3.sqlite_version},
              "execution_lock": {"available": util.find_spec("fcntl") is not None}}
    try:
        git = subprocess.run(["git", "--version"], env=git_environment(), capture_output=True, timeout=5, check=False)
        checks["git"] = {"available": git.returncode == 0, "version": git.stdout.decode(errors="replace").strip()}
    except (OSError, subprocess.TimeoutExpired):
        checks["git"] = {"available": False, "error": "git_unavailable"}
    packages = {}
    for package in (*AI_PACKAGES, *MCP_PACKAGES):
        try:
            packages[package] = {"installed": True, "version": metadata.version(package)}
        except metadata.PackageNotFoundError:
            packages[package] = {"installed": False}
    checks["packages"] = packages
    try:
        settings = ModelSettings.from_env()
        # Even wrongly configured names/URL paths cannot print known credential patterns.
        checks["model"] = {"configured": True, "provider": settings.provider,
                           "name": _redact(settings.name.encode()), "base_url": _redact(model_target(settings)["base_url"].encode()),
                           "configured_base_url": _redact(settings.base_url.encode()),
                           "connectivity_tested": False}
    except FileReadError as exc:
        checks["model"] = {"configured": False, "error": exc.code, "connectivity_tested": False}
    checks["docker"] = {"checked": False, "available": None}
    if check_docker or image is not None:
        try:
            engine = DockerEngine(socket_path)
            version = engine._control("version", "--format", "{{.Server.Version}}")
            checks["docker"] = {"checked": True, "available": True, "server_version": version.decode().strip()}
            if image is not None:
                checks["docker"]["image_id"] = engine.resolve_image(image)
        except FileReadError as exc:
            checks["docker"] = {"checked": True, "available": False, "error": exc.code}
    core = checks["python"]["available"] and checks["git"]["available"]
    ai = all(packages[name]["installed"] for name in AI_PACKAGES)
    mcp = all(packages[name]["installed"] for name in MCP_PACKAGES)
    return {"scope": "environment_readiness_only", "checks": checks,
            "capabilities": {"project_reads": core, "grounded_questions": core and ai and checks["model"]["configured"],
                             "repair_proposals": core and ai and mcp and checks["model"]["configured"],
                             "candidate_changes": core and checks["execution_lock"]["available"],
                             "container_verification": (core and checks["docker"]["available"] is True) if checks["docker"]["checked"] else None,
                             "requested_image_ready": "image_id" in checks["docker"] if image else None},
            "model_called": False, "project_code_executed": False,
            "limitations": ["依赖就绪不代表模型请求已通过", "Docker 可连接不代表项目测试已通过", "不检查运行部署或多人权限"]}
