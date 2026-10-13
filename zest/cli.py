"""CLI for local project registration and versioned read-only evidence."""

import argparse
import json
from pathlib import Path
import sys

from .git import RepositoryError, inspect_repository
from .files import FileReadError, read_project_file
from .overview import build_overview
from .storage import ProjectRegistry, StorageError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zest", description="Zest 项目维护 Agent")
    parser.add_argument("--state-dir", type=Path, default=Path.cwd() / ".zest",
                        help="Zest 状态目录，默认当前目录下的 .zest")
    commands = parser.add_subparsers(dest="command", required=True)
    projects = commands.add_parser("project", help="登记和查询本地 Git 项目")
    actions = projects.add_subparsers(dest="action", required=True)
    imported = actions.add_parser("import", help="只读采集 Git 状态并登记项目")
    imported.add_argument("path", type=Path, help="本地 Git 仓库根目录")
    actions.add_parser("list", help="列出已登记项目，返回上次采集的状态")
    shown = actions.add_parser("show", help="查看登记记录，返回上次采集的状态")
    shown.add_argument("project_id", help="登记生成的项目 ID")
    overview = actions.add_parser("overview", help="读取已登记项目当前 commit 的只读概览")
    overview.add_argument("project_id", help="登记生成的项目 ID")
    reader = actions.add_parser("read", help="读取已提交文本文件并返回行号和版本来源")
    reader.add_argument("project_id", help="登记生成的项目 ID")
    reader.add_argument("path", help="仓库相对文件路径")
    reader.add_argument("--commit", help="完整 commit ID；省略时使用查询时的 HEAD")
    reader.add_argument("--start-line", type=int, default=1, help="开始行，默认 1")
    reader.add_argument("--end-line", type=int, help="结束行；省略时最多返回 200 行")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = ProjectRegistry(args.state_dir)
        if args.action == "import":
            # Validate and inspect first: invalid input must not create state.
            result = registry.register(inspect_repository(args.path))
        elif args.action == "list":
            result = registry.list_projects()
        else:
            result = registry.get_project(args.project_id)
            if result is None:
                print(json.dumps({"error": {"code": "project_not_found", "message": "未找到该项目登记记录。"}},
                                 ensure_ascii=True), file=sys.stderr)
                return 1
            if args.action == "overview":
                result = build_overview(result)
            elif args.action == "read":
                result = read_project_file(result, args.path, commit=args.commit,
                                           start_line=args.start_line, end_line=args.end_line)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (FileReadError, RepositoryError, StorageError, OSError) as exc:
        if isinstance(exc, FileReadError):
            code = exc.code
        else:
            code = "repository_error" if isinstance(exc, RepositoryError) else "state_error"
        message = str(exc) if not isinstance(exc, OSError) else "无法访问本地状态路径。"
        print(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=True), file=sys.stderr)
        return 1
