"""CLI for local project registration and versioned read-only evidence."""

import argparse
import json
from pathlib import Path
import sys

from .git import RepositoryError, inspect_repository
from .files import FileReadError, read_project_file
from .overview import build_overview
from .search import search_project
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
    search = actions.add_parser("search", help="按关键词搜索已提交的允许文本文件")
    search.add_argument("project_id")
    search.add_argument("query", help="字面关键词，不作为正则表达式")
    search.add_argument("--commit", help="完整 commit ID；省略时查询当前 HEAD")
    search.add_argument("--limit", type=int, default=20, help="最多返回 1—100 个匹配行")
    search.add_argument("--case-sensitive", action="store_true", help="区分大小写")
    ask = actions.add_parser("ask", help="基于检索证据回答项目问题")
    ask.add_argument("project_id")
    ask.add_argument("question")
    ask.add_argument("--keyword", action="append", default=[], help="可重复，最多 3 个")
    ask.add_argument("--file", action="append", default=[], help="优先读取指定文件，最多 5 个")
    ask.add_argument("--preview", action="store_true", help="只展示选定上下文，不调用模型")
    ask.add_argument("--allow-model", action="store_true", help="允许把问题和选定证据发送到配置的模型")
    tasks = commands.add_parser("task", help="持久化问答任务及批准")
    task_actions = tasks.add_subparsers(dest="action", required=True)
    create = task_actions.add_parser("create", help="准备上下文并暂停等待批准")
    create.add_argument("project_id")
    create.add_argument("question")
    create.add_argument("--keyword", action="append", default=[])
    create.add_argument("--file", action="append", default=[])
    task_actions.add_parser("list")
    for name in ("show", "approve", "reject"):
        action = task_actions.add_parser(name)
        action.add_argument("task_id")
        if name == "approve":
            action.add_argument("--payload-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = ProjectRegistry(args.state_dir)
        if args.command == "task":
            from .tasks import TaskRunner
            runner = TaskRunner(args.state_dir)
            if args.action == "create":
                project = registry.get_project(args.project_id)
                if project is None:
                    raise FileReadError("project_not_found", "未找到该项目。")
                result = runner.create(project, args.question, keywords=args.keyword, files=args.file)
            elif args.action == "list":
                result = runner.list_tasks()
            elif args.action == "show":
                result = runner.show(args.task_id)
            else:
                result = runner.decide(args.task_id, approved=args.action == "approve",
                                       payload_sha256=getattr(args, "payload_sha256", None))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
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
            elif args.action == "search":
                result = search_project(result, args.query, commit=args.commit,
                                        limit=args.limit, case_sensitive=args.case_sensitive)
            elif args.action == "ask":
                from .qa import answer_context, prepare_context
                context = prepare_context(result, args.question, keywords=args.keyword, files=args.file)
                result = context if args.preview else answer_context(context, allow_model=args.allow_model)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except ImportError:
        print(json.dumps({"error": {"code": "ai_dependencies_missing", "message": "请先执行 uv sync --extra ai。"}},
                         ensure_ascii=True), file=sys.stderr)
        return 1
    except (FileReadError, RepositoryError, StorageError, OSError) as exc:
        if isinstance(exc, FileReadError):
            code = exc.code
        else:
            code = "repository_error" if isinstance(exc, RepositoryError) else "state_error"
        message = str(exc) if not isinstance(exc, OSError) else "无法访问本地状态路径。"
        print(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=True), file=sys.stderr)
        return 1
