"""Create a fresh local project baseline, without executing project hooks or code."""

import os
from pathlib import Path
import subprocess

from .files import FileReadError
from .git import RepositoryError, git_environment, inspect_repository
from .knowledge import KnowledgeStore, _text
from .storage import ProjectRegistry, StorageError


IGNORE = """# Local Zest state and credentials
.zest/*
!.zest/.gitignore
!.zest/docs/
.env
.env.*
!.env.example
# Common local environments and build output
.venv/
__pycache__/
node_modules/
dist/
build/
.DS_Store
"""


def _identity(name, email):
    if (name is None) != (email is None):
        raise FileReadError("invalid_author", "作者姓名和邮箱需要同时提供。")
    if name is None:
        values = []
        for key in ("user.name", "user.email"):
            try:
                result = subprocess.run(["git", "config", "--get", key], env=git_environment(),
                                        capture_output=True, timeout=10, check=False)
                value = result.stdout.decode("utf-8").strip() if result.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
                raise FileReadError("invalid_author", "无法读取 Git 作者配置，请显式提供作者姓名和邮箱。") from exc
            values.append(value)
        name, email = values
    for value in (name, email):
        if not isinstance(value, str) or not value.strip():
            raise FileReadError("invalid_author", "缺少 Git 作者姓名或邮箱，请配置 Git 或同时显式提供。")
        _text(value, 200)
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise FileReadError("invalid_author", "Git 作者配置不能包含控制字符。")
    if "@" not in email or any(char in email for char in "<> \t"):
        raise FileReadError("invalid_author", "请提供有效的 Git 作者邮箱。")
    return name, email


def _git_create(root, *args):
    # Ignore global/system attributes, filters, signing and templates for this new repo.
    env = git_environment()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        result = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
             "-c", "commit.gpgSign=false", "-C", str(root), *args],
            env=env, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryError("新项目 Git 操作失败或超时。") from exc
    if result.returncode != 0:
        raise RepositoryError("新项目 Git 操作失败，请检查作者配置和目录权限。")


def create_project(path, name, goal, state_dir, *, author_name=None, author_email=None):
    _text(name, 100)
    _text(goal, 4000)
    if any(ord(char) < 32 or ord(char) == 127 for char in name) or "\0" in goal:
        raise FileReadError("invalid_project_text", "项目名称必须为单行，目标不能包含 NUL。")
    # Resolve only the existing parent; do not follow a final symlink or create parents.
    requested = Path(path).expanduser().absolute()
    try:
        parent = requested.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileReadError("invalid_project_parent", "新项目的父目录必须存在。") from exc
    root = parent / requested.name
    if root.exists() or root.is_symlink():
        from .resources import workspace_identity
        if root.is_dir() and not root.is_symlink() and workspace_identity(root):
            project = ProjectRegistry(state_dir).register(inspect_repository(root), workspace=True)
            return {"project": project, "goal_note": None, "created_files": [],
                    "warnings": ["existing_project_reused_goal_unchanged"]}
        raise FileReadError("project_path_exists", "目标路径已经存在；新项目只能创建于全新目录。")
    author_name, author_email = _identity(author_name, author_email)
    created = False
    try:
        root.mkdir(mode=0o700)
        created = True
        for filename, content in {
            "README.md": f"# {name}\n\n## 用户声明的项目目标\n\n{goal}\n\n## 当前状态\n\n仅已创建 Git 基线，业务功能尚未实现。\n",
            ".gitignore": IGNORE,
        }.items():
            with (root / filename).open("x", encoding="utf-8", newline="\n") as output:
                output.write(content)
        _git_create(root, "init", "--template=", "-b", "main")
        _git_create(root, "config", "user.name", author_name)
        _git_create(root, "config", "user.email", author_email)
        _git_create(root, "add", "--", "README.md", ".gitignore")
        _git_create(root, "commit", "-m", "chore: initialize project baseline")
        project = ProjectRegistry(state_dir).register(inspect_repository(root), mode="development", workspace=True)
        note = KnowledgeStore(state_dir).add(project, "项目目标", goal, kind="decision", scope="project")
        return {"project": project, "goal_note": note, "created_files": ["README.md", ".gitignore"],
                "warnings": ["goal_note_requires_confirmation"]}
    except (OSError, RepositoryError, StorageError, FileReadError) as exc:
        if created:
            # Never remove a directory on partial failure: it may contain new user work.
            raise FileReadError("project_creation_incomplete",
                                "创建未完成，已创建的目录和 Git 状态保留。检查后可用 project import 登记，并手动补充目标知识。") from exc
        if isinstance(exc, FileReadError):
            raise
        raise FileReadError("project_creation_failed", "无法创建新项目目录，未覆盖既有路径。") from exc
