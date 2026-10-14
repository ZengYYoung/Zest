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


def _range_argument(value):
    try:
        path, start, end = value.rsplit(":", 2)
        return {"path": path, "start_line": int(start), "end_line": int(end)}
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("行段格式为 path:start:end，如 app.py:10:30") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zest", description="Zest 项目维护 Agent")
    parser.add_argument("--state-dir", type=Path, default=Path.cwd() / ".zest",
                        help="Zest 状态目录，默认当前目录下的 .zest")
    commands = parser.add_subparsers(dest="command", required=True)
    projects = commands.add_parser("project", help="登记和查询本地 Git 项目")
    actions = projects.add_subparsers(dest="action", required=True)
    imported = actions.add_parser("import", help="只读采集 Git 状态并登记项目")
    imported.add_argument("path", type=Path, help="本地 Git 仓库根目录")
    initialized = actions.add_parser("init", help="创建全新本地项目和 Git 基线")
    initialized.add_argument("path", type=Path)
    initialized.add_argument("--name", required=True)
    initialized.add_argument("--goal", required=True)
    initialized.add_argument("--author-name", help="与邮箱同时提供；省略时读取已有 Git 作者配置")
    initialized.add_argument("--author-email")
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
    ask.add_argument("--retrieval", choices=("keyword", "lexical"), default="keyword")
    ask.add_argument("--strategy", choices=("single", "multi"), default="single")
    tasks = commands.add_parser("task", help="持久化问答任务及批准")
    task_actions = tasks.add_subparsers(dest="action", required=True)
    create = task_actions.add_parser("create", help="准备上下文并暂停等待批准")
    create.add_argument("project_id")
    create.add_argument("question")
    create.add_argument("--keyword", action="append", default=[])
    create.add_argument("--file", action="append", default=[])
    create.add_argument("--retrieval", choices=("keyword", "lexical"), default="keyword")
    create.add_argument("--strategy", choices=("single", "multi"), default="single")
    task_actions.add_parser("list")
    for name in ("show", "approve", "reject"):
        action = task_actions.add_parser(name)
        action.add_argument("task_id")
        if name == "approve":
            action.add_argument("--payload-sha256", required=True)
    knowledge = commands.add_parser("knowledge", help="长期项目知识的草稿、确认和失效")
    knowledge_actions = knowledge.add_subparsers(dest="action", required=True)
    add = knowledge_actions.add_parser("add")
    add.add_argument("project_id")
    add.add_argument("--title", required=True)
    add.add_argument("--body", required=True)
    add.add_argument("--kind", choices=("decision", "constraint", "maintenance"), default="decision")
    add.add_argument("--scope", choices=("snapshot", "project"), default="snapshot")
    listed = knowledge_actions.add_parser("list")
    listed.add_argument("project_id")
    for name in ("show", "confirm", "invalidate"):
        action = knowledge_actions.add_parser(name)
        action.add_argument("note_id")
        if name == "invalidate":
            action.add_argument("--reason", required=True)
    index = commands.add_parser("index", help="版本化词法检索索引")
    index_actions = index.add_subparsers(dest="action", required=True)
    index_build = index_actions.add_parser("build")
    index_build.add_argument("project_id")
    index_search = index_actions.add_parser("search")
    index_search.add_argument("project_id")
    index_search.add_argument("query")
    index_search.add_argument("--commit")
    index_search.add_argument("--limit", type=int, default=5)
    mcp = commands.add_parser("mcp", help="绑定单一项目的只读 MCP 服务")
    mcp_actions = mcp.add_subparsers(dest="action", required=True)
    serve = mcp_actions.add_parser("serve")
    serve.add_argument("project_id")
    repairs = commands.add_parser("repair", help="审核具体变更并生成隔离 Git 候选版本")
    repair_actions = repairs.add_subparsers(dest="action", required=True)
    proposal = repair_actions.add_parser("create")
    proposal.add_argument("project_id")
    proposal.add_argument("--plan", type=Path, required=True, help="本地 JSON 方案文件")
    suggestion = repair_actions.add_parser("suggest", help="准备修复 Agent 数据预览并等待模型调用批准")
    suggestion.add_argument("project_id")
    suggestion.add_argument("question")
    suggestion.add_argument("--file", action="append", default=[], help="明确既有完整文件，与新路径合计最多 5 项")
    suggestion.add_argument("--new-file", action="append", default=[], help="明确待创建文件，不允许覆盖已有 Git 路径")
    suggestion.add_argument("--range", action="append", default=[], type=_range_argument, help="明确可编辑行段，格式 path:start:end，每项最多 200 行")
    listed_repairs = repair_actions.add_parser("list")
    listed_repairs.add_argument("project_id")
    for name in ("show", "approve", "reject"):
        repair_action = repair_actions.add_parser(name)
        repair_action.add_argument("repair_id")
        if name == "approve":
            repair_action.add_argument("--plan-sha256", required=True)
    verification = commands.add_parser("verification", help="在本地受限 Docker 中验证候选版本")
    verification_actions = verification.add_subparsers(dest="action", required=True)
    prepare_verification = verification_actions.add_parser("create")
    prepare_verification.add_argument("repair_id")
    prepare_verification.add_argument("--image", required=True, help="已安装且信任的本地 Python 测试镜像")
    prepare_verification.add_argument("--timeout", type=int, default=30)
    prepare_verification.add_argument("--docker-socket", type=Path)
    for name in ("show", "approve", "reject"):
        action = verification_actions.add_parser(name)
        action.add_argument("verification_id")
        if name == "approve":
            action.add_argument("--plan-sha256", required=True)
    releases = commands.add_parser("release", help="批准管理源码版本切换与回退，不部署运行服务")
    release_actions = releases.add_subparsers(dest="action", required=True)
    promotion = release_actions.add_parser("promote")
    promotion.add_argument("verification_id")
    rollback = release_actions.add_parser("rollback")
    rollback.add_argument("operation_id", help="回到该已完成操作的 previous_commit")
    exported = release_actions.add_parser("export", help="将已批准管理版本导出到项目外全新目录")
    exported.add_argument("project_id")
    exported.add_argument("destination", type=Path)
    checked_export = release_actions.add_parser("verify-export", help="只核对导出文件与清单，不执行源码")
    checked_export.add_argument("directory", type=Path)
    for name in ("active", "list"):
        action = release_actions.add_parser(name)
        action.add_argument("project_id")
    for name in ("show", "approve", "reject", "recover"):
        action = release_actions.add_parser(name)
        action.add_argument("operation_id")
        if name == "approve":
            action.add_argument("--plan-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = ProjectRegistry(args.state_dir)
        if args.command == "release":
            from .releases import ReleaseStore
            store = ReleaseStore(args.state_dir)
            if args.action == "export":
                from .exports import SourceExporter
                result = SourceExporter(args.state_dir).export(args.project_id, args.destination)
            elif args.action == "verify-export":
                from .exports import verify_export
                result = verify_export(args.directory)
            elif args.action == "promote":
                result = store.promote(args.verification_id)
            elif args.action == "rollback":
                result = store.rollback(args.operation_id)
            elif args.action == "active":
                result = store.active(args.project_id)
            elif args.action == "list":
                result = store.list_operations(args.project_id)
            elif args.action == "show":
                result = store.show(args.operation_id)
            elif args.action == "recover":
                result = store.recover(args.operation_id)
            else:
                result = store.decide(args.operation_id, approved=args.action == "approve",
                                      plan_sha256=getattr(args, "plan_sha256", None))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "verification":
            from .verification import VerificationStore
            store = VerificationStore(args.state_dir)
            if args.action == "create":
                result = store.create(args.repair_id, args.image, timeout=args.timeout, socket_path=args.docker_socket)
            elif args.action == "show":
                result = store.show(args.verification_id)
            else:
                result = store.decide(args.verification_id, approved=args.action == "approve",
                                      plan_sha256=getattr(args, "plan_sha256", None))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "repair":
            from .repairs import RepairStore, load_proposal
            store = RepairStore(args.state_dir)
            if args.action in ("create", "list", "suggest"):
                project = registry.get_project(args.project_id)
                if project is None:
                    raise FileReadError("project_not_found", "未找到该项目。")
                if args.action == "suggest":
                    from .tasks import TaskRunner
                    result = TaskRunner(args.state_dir).create(project, args.question, files=args.file,
                                                               new_files=args.new_file, ranges=args.range, purpose="repair_proposal")
                else:
                    result = (store.create(project, load_proposal(args.plan)) if args.action == "create"
                              else store.list_repairs(args.project_id))
            elif args.action == "show":
                result = store.show(args.repair_id)
            else:
                result = store.decide(args.repair_id, approved=args.action == "approve",
                                      plan_sha256=getattr(args, "plan_sha256", None))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "mcp":
            from .mcp_server import create_server
            create_server(args.project_id, args.state_dir).run(transport="stdio", show_banner=False)
            return 0
        if args.command == "index":
            from .index import RetrievalIndex
            project = registry.get_project(args.project_id)
            if project is None:
                raise FileReadError("project_not_found", "未找到该项目。")
            index = RetrievalIndex(args.state_dir)
            result = (index.build(project) if args.action == "build" else
                      index.search(project, args.query, commit=args.commit, limit=args.limit))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "knowledge":
            from .knowledge import KnowledgeStore
            store = KnowledgeStore(args.state_dir)
            if args.action in ("add", "list"):
                project = registry.get_project(args.project_id)
                if project is None:
                    raise FileReadError("project_not_found", "未找到该项目。")
                result = (store.add(project, args.title, args.body, kind=args.kind, scope=args.scope)
                          if args.action == "add" else store.list_notes(args.project_id))
            elif args.action == "show":
                result = store.get(args.note_id)
            elif args.action == "confirm":
                result = store.confirm(args.note_id)
            else:
                result = store.invalidate(args.note_id, args.reason)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "task":
            from .tasks import TaskRunner
            runner = TaskRunner(args.state_dir)
            if args.action == "create":
                project = registry.get_project(args.project_id)
                if project is None:
                    raise FileReadError("project_not_found", "未找到该项目。")
                result = runner.create(project, args.question, keywords=args.keyword, files=args.file,
                                       retrieval=args.retrieval, strategy=args.strategy)
            elif args.action == "list":
                result = runner.list_tasks()
            elif args.action == "show":
                result = runner.show(args.task_id)
            else:
                result = runner.decide(args.task_id, approved=args.action == "approve",
                                       payload_sha256=getattr(args, "payload_sha256", None))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.action == "init":
            from .development import create_project
            result = create_project(args.path, args.name, args.goal, args.state_dir,
                                    author_name=args.author_name, author_email=args.author_email)
        elif args.action == "import":
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
                context = prepare_context(result, args.question, keywords=args.keyword, files=args.file,
                                          state_dir=args.state_dir, retrieval=args.retrieval)
                context["execution_policy"] = {"strategy": args.strategy, "max_model_calls": 3 if args.strategy == "multi" else 1}
                if args.preview:
                    result = context
                elif args.strategy == "multi":
                    from .agents import answer_multi_context
                    result = answer_multi_context(context, allow_model=args.allow_model, state_dir=args.state_dir)
                else:
                    result = answer_context(context, allow_model=args.allow_model)
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
