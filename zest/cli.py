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
                        help="项目登记目录，默认当前目录下的 .zest；项目资源分别存于各项目的 .zest/state")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ui", help="进入交互式项目问答工作台，展示真实阶段与请求预览")
    doctor = commands.add_parser("doctor", help="只读检查本地环境，不调用模型或执行项目代码")
    doctor.add_argument("--docker", action="store_true", help="读取本地 Docker 服务版本")
    doctor.add_argument("--image", help="核对已安装测试镜像并返回固定 ID，不自动下载")
    doctor.add_argument("--docker-socket", type=Path)
    web = commands.add_parser("web", help="本地只读 Web API，不包含模型/修改端点")
    web_actions = web.add_subparsers(dest="action", required=True)
    web_serve = web_actions.add_parser("serve")
    web_serve.add_argument("--port", type=int, default=8000)
    projects = commands.add_parser("project", help="登记和查询本地 Git 项目")
    actions = projects.add_subparsers(dest="action", required=True)
    imported = actions.add_parser("import", help="登记项目并准备忽略的 .zest 资源目录，不执行项目")
    imported.add_argument("path", type=Path, help="本地 Git 仓库根目录")
    mapped = actions.add_parser("map", help="只读提取固定版本的 Python 模块、符号和 import 声明")
    mapped.add_argument("project_id")
    mapped.add_argument("--commit")
    initialized = actions.add_parser("init", help="创建全新项目；已接入项目只连接现有管理状态")
    initialized.add_argument("path", type=Path)
    initialized.add_argument("--name", required=True)
    initialized.add_argument("--goal", required=True)
    initialized.add_argument("--author-name", help="与邮箱同时提供；省略时读取已有 Git 作者配置")
    initialized.add_argument("--author-email")
    actions.add_parser("list", help="列出已登记项目，返回上次采集的状态")
    shown = actions.add_parser("show", help="查看登记记录，返回上次采集的状态")
    shown.add_argument("project_id", help="登记生成的项目 ID")
    guide = actions.add_parser("guide", help="显式补齐缺少的 .zest/AGENTS.md；不覆盖现有管理指引")
    guide.add_argument("project_id")
    context = actions.add_parser("context", help="只读汇总当前版本的记忆、认知、索引和调查待办")
    context.add_argument("project_id")
    context.add_argument("--format", choices=("json", "text"), default="json")
    context.add_argument("--progress", action="store_true")
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
    survey = commands.add_parser("survey", help="固定版本的本地项目摸底，不调用模型或执行项目")
    survey_actions = survey.add_subparsers(dest="action", required=True)
    survey_build = survey_actions.add_parser("build", help="采集并保存一份独立摸底记录")
    survey_build.add_argument("project_id")
    survey_build.add_argument("--commit", help="可选完整 commit；默认固定开始时的管理源码/HEAD")
    survey_build.add_argument("--format", choices=("json", "text"), default="json")
    survey_build.add_argument("--progress", action="store_true", help="在 stderr 展示真实采集阶段，终端默认开启")
    survey_list = survey_actions.add_parser("list", help="列出本项目最近 100 份记录摘要")
    survey_list.add_argument("project_id")
    survey_show = survey_actions.add_parser("show", help="读取已保存记录，不重新扫描仓库")
    survey_show.add_argument("survey_id")
    survey_show.add_argument("--format", choices=("json", "text"), default="json")
    cognition = commands.add_parser("cognition", help="项目认知规划、后台调查与精简档案")
    cognition_actions = cognition.add_subparsers(dest="action", required=True)
    cognition_create = cognition_actions.add_parser("create", help="准备固定版本完整资料包，等待一次工作流批准")
    cognition_create.add_argument("project_id")
    cognition_create.add_argument("--survey-id")
    followup = cognition_actions.add_parser("followup-create", help="按一条未解决发现准备同版本定向调查包，不调用模型或启动 worker")
    followup.add_argument("run_id")
    followup.add_argument("finding_id")
    followup.add_argument("--format", choices=("json", "text"), default="json")
    followup.add_argument("--progress", action="store_true")
    cognition_list = cognition_actions.add_parser("list")
    cognition_list.add_argument("project_id")
    refresh = cognition_actions.add_parser("refresh-preview", help="只读比较选定版本与认知证据，生成待刷新清单")
    refresh.add_argument("project_id")
    refresh.add_argument("--base-run-id", help="可选历史认知基线；默认优先选定版本，否则最近已结束调查")
    refresh.add_argument("--format", choices=("json", "text"), default="json")
    refresh.add_argument("--progress", action="store_true", help="在 stderr 展示真实版本/证据对比阶段")
    refresh_create = cognition_actions.add_parser("refresh-create", help="准备目标版本增量调查包，等待单独批准，不调用模型")
    refresh_create.add_argument("project_id")
    refresh_create.add_argument("--base-run-id")
    refresh_create.add_argument("--format", choices=("json", "text"), default="json")
    refresh_create.add_argument("--progress", action="store_true")
    for name in ("show", "preview", "approve", "reject", "recover", "dossier", "watch"):
        command = cognition_actions.add_parser(name)
        command.add_argument("run_id")
        if name == "approve":
            command.add_argument("--approval-sha256", required=True)
            command.add_argument("--no-start", action="store_true", help="只入队，由已有独立 worker 执行")
        if name == "show":
            command.add_argument("--calls", action="store_true", help="包含逐次请求与响应记录")
        if name in ("show", "dossier"):
            command.add_argument("--format", choices=("json", "text"), default="json")
    context_preview = cognition_actions.add_parser("context-preview", help="审阅适度超限但尚未外发的完整请求")
    context_preview.add_argument("run_id")
    for name in ("context-approve", "context-reject"):
        command = cognition_actions.add_parser(name, help="决定单个完整请求的字符扩展，不扩大调查权限")
        command.add_argument("run_id")
        command.add_argument("actor")
        command.add_argument("sequence", type=int)
        command.add_argument("--request-sha256", required=True)
        command.add_argument("--no-start", action="store_true", help="只保存决定；全部待扩展请求决定后可由 worker 继续")
    worker_command = cognition_actions.add_parser("worker", help="独立本地 worker，关闭工作台不影响已启动工作")
    worker_command.add_argument("--once", action="store_true")
    worker_command.add_argument("--idle-seconds", type=int, default=30)
    invalidated = cognition_actions.add_parser("invalidate", help="带原因失效自动核对的认知")
    invalidated.add_argument("run_id")
    invalidated.add_argument("finding_id")
    invalidated.add_argument("--reason", required=True)
    evaluation = commands.add_parser("evaluation", help="独立认知评分；新资料包与新批准，不修改项目知识")
    evaluation_actions = evaluation.add_subparsers(dest="action", required=True)
    evaluation_create = evaluation_actions.add_parser("create", help="冻结 Skill、参考、结果与完整评分请求，不调用模型")
    evaluation_create.add_argument("run_id")
    evaluation_create.add_argument("--criteria", type=Path, required=True)
    evaluation_create.add_argument("--max-calls", type=int, default=32)
    for name in ("preview", "show", "approve", "reject", "run", "recover", "report"):
        command = evaluation_actions.add_parser(name)
        command.add_argument("evaluation_id")
        if name == "approve":
            command.add_argument("--approval-sha256", required=True)
            command.add_argument("--no-run", action="store_true", help="只批准，不执行评分")
            command.add_argument("--allow-extended-requests", action="store_true", help="另行同意预览中精确列出的 32000—48000 字符请求清单")
        if name == "report":
            command.add_argument("--format", choices=("json", "text"), default="json")
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
    list_verification = verification_actions.add_parser("list")
    list_verification.add_argument("project_id")
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
    captured = release_actions.add_parser("capture", help="将已完成验证后的源码选择留存为待确认维护知识")
    captured.add_argument("operation_id")
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
        if args.command == "ui":
            from .terminal import launch_workspace
            launch_workspace(args.state_dir)
            return 0
        if args.command == "web":
            from .web import serve
            serve(args.state_dir, port=args.port)
            return 0
        if args.command == "doctor":
            from .doctor import readiness
            print(json.dumps(readiness(check_docker=args.docker, image=args.image, socket_path=args.docker_socket),
                             ensure_ascii=True, indent=2))
            return 0
        if args.command == "survey":
            from .survey import SurveyStore, format_survey
            store = SurveyStore(args.state_dir)
            if args.action == "show":
                result = store.show(args.survey_id)
            else:
                project = registry.get_project(args.project_id)
                if project is None:
                    raise FileReadError("project_not_found", "未找到该项目。")
                if args.action == "list":
                    result = store.list_surveys(project["id"])
                else:
                    show_progress = args.progress or sys.stderr.isatty()
                    labels = {"snapshot": "固定源码版本", "inventory": "采集目录和安全文本材料",
                              "structure": "生成固定版本的 Python 声明地图", "collected": "材料采集结束", "saving": "保存摸底记录"}
                    def progress(event):
                        detail = f" · 已收集 {event['files_read']} 个文本文件" if "files_read" in event else ""
                        print(f"[{event['elapsed_seconds']:.3f}s] {labels[event['phase']]}{detail}", file=sys.stderr)
                    result = store.build(project, commit=args.commit, progress=progress if show_progress else None)
                    if show_progress:
                        print(f"已保存摸底记录：{result['id']}", file=sys.stderr)
            print(format_survey(result) if getattr(args, "format", "json") == "text"
                  else json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "cognition":
            from .cognition_store import CognitionStore
            from .cognition_views import format_cognition, format_dossier, watch_cognition
            store = CognitionStore(args.state_dir)
            if args.action == "followup-create":
                from .cognition_followup import prepare_followup
                labels = {"source": "核对原发现与关联冲突", "materials": "收集同版本完整安全资料",
                          "survey_snapshot": "固定摸底版本", "survey_inventory": "采集目录和安全文本",
                          "survey_structure": "生成 Python 声明地图", "survey_collected": "材料采集完成",
                          "survey_saving": "保存固定版本摸底", "preparing": "冻结定向目标与资料包",
                          "prepared": "定向调查包已保存，等待批准"}
                def progress(event):
                    print(f"[{event['elapsed_seconds']:.3f}s] {labels[event['phase']]}", file=sys.stderr)
                result = prepare_followup(args.state_dir, args.run_id, args.finding_id,
                    progress=progress if args.progress or sys.stderr.isatty() else None)
                print(format_cognition(result) if args.format == "text" else json.dumps(result, ensure_ascii=True, indent=2))
                return 0
            if args.action in ("refresh-preview", "refresh-create"):
                from .cognition_refresh import build_refresh_preview, format_refresh_preview, prepare_refresh
                labels = {"snapshot": "固定当前选定版本", "baseline": "读取既有认知基线",
                          "compare": "比较提交文件元数据", "evidence": "关联已有原文证据", "completed": "只读待刷新清单生成完成",
                          "materials": "收集目标版本的安全资料", "survey_snapshot": "固定摸底版本",
                          "survey_inventory": "采集目录和安全文本", "survey_structure": "生成 Python 声明地图",
                          "survey_collected": "材料采集完成", "survey_saving": "保存固定版本摸底",
                          "preparing": "冻结资料、变化与待核对线索", "prepared": "增量调查包已保存，等待批准"}
                def progress(event):
                    print(f"[{event['elapsed_seconds']:.3f}s] {labels[event['phase']]}", file=sys.stderr)
                prepare = args.action == "refresh-create"
                result = (prepare_refresh if prepare else build_refresh_preview)(args.state_dir, args.project_id,
                    base_run_id=args.base_run_id, progress=progress if args.progress or sys.stderr.isatty() else None)
                formatter = format_cognition if prepare else format_refresh_preview
                print(formatter(result) if args.format == "text" else json.dumps(result, ensure_ascii=True, indent=2))
                return 0
            if args.action == "worker":
                from .cognition import worker
                if not 1 <= args.idle_seconds <= 300:
                    raise FileReadError("invalid_worker_timeout", "worker 空闲退出时间须为 1—300 秒。")
                worker(args.state_dir, once=args.once, idle_seconds=args.idle_seconds)
                return 0
            if args.action == "create":
                result = store.create(args.project_id, survey_id=args.survey_id)
            elif args.action == "list":
                result = store.list_runs(args.project_id)
            elif args.action == "preview":
                result = store.show(args.run_id, include_envelope=True)
            elif args.action == "show":
                result = store.show(args.run_id, include_calls=args.calls)
            elif args.action == "context-preview":
                result = store.context_requests(args.run_id)
            elif args.action in ("context-approve", "context-reject"):
                result = store.decide_context(args.run_id, args.actor, args.sequence,
                    approved=args.action == "context-approve", request_sha256=args.request_sha256)
                if result["status"] == "queued" and not args.no_start:
                    from .cognition import start_worker
                    from .resources import record_state
                    result["worker"] = start_worker(record_state(args.state_dir, "cognition", args.run_id))
            elif args.action == "dossier":
                result = store.dossier(args.run_id)
            elif args.action == "invalidate":
                result = store.invalidate(args.run_id, args.finding_id, args.reason)
            elif args.action == "watch":
                watch_cognition(store, args.run_id)
                return 0
            elif args.action == "recover":
                result = store.recover(args.run_id)
                from .cognition import start_worker
                from .resources import record_state
                result["worker"] = start_worker(record_state(args.state_dir, "cognition", args.run_id))
            else:
                result = store.decide(args.run_id, approved=args.action == "approve", approval_sha256=getattr(args, "approval_sha256", None))
                if args.action == "approve" and not args.no_start:
                    from .cognition import start_worker
                    from .resources import record_state
                    result["worker"] = start_worker(record_state(args.state_dir, "cognition", args.run_id))
            if getattr(args, "format", "json") == "text":
                print(format_dossier(result) if args.action == "dossier" else format_cognition(result))
            else:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "evaluation":
            from .evaluation import EvaluationStore, load_criteria, format_evaluation
            store = EvaluationStore(args.state_dir)
            if args.action == "create":
                result = store.create(args.run_id, load_criteria(args.criteria), max_calls=args.max_calls)
            elif args.action in ("show", "preview"):
                result = store.show(args.evaluation_id, preview=args.action == "preview")
            elif args.action in ("approve", "reject"):
                result = store.decide(args.evaluation_id, approved=args.action == "approve",
                                      approval_sha256=getattr(args, "approval_sha256", None),
                                      allow_extended_requests=getattr(args, "allow_extended_requests", False))
                if args.action == "approve" and not args.no_run:
                    result = store.run(args.evaluation_id)
            elif args.action == "recover":
                result = store.recover(args.evaluation_id)
            elif args.action == "run":
                result = store.run(args.evaluation_id)
            else:
                result = store.report(args.evaluation_id)
            if getattr(args, "format", "json") == "text":
                print(format_evaluation(result))
            else:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "release":
            from .releases import ReleaseStore
            store = ReleaseStore(args.state_dir)
            if args.action == "capture":
                from .history import capture_maintenance
                result = capture_maintenance(args.state_dir, args.operation_id)
            elif args.action == "export":
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
            elif args.action == "list":
                if ProjectRegistry(args.state_dir).get_project(args.project_id) is None:
                    raise FileReadError("project_not_found", "未找到该项目。")
                result = store.list_verifications(args.project_id)
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
        if args.action == "context":
            from .management_context import build_management_context, format_management_context
            labels = {"snapshot": "固定选定版本", "memory": "读取项目记忆", "cognition": "汇总认知与调查待办",
                      "index": "读取原生文件索引", "completed": "只读管理总览完成"}
            def context_progress(event):
                print(f"[{event['elapsed_seconds']:.3f}s] {labels[event['phase']]}", file=sys.stderr)
            result = build_management_context(args.state_dir, args.project_id,
                progress=context_progress if args.progress or sys.stderr.isatty() else None)
            print(format_management_context(result) if args.format == "text" else json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.action == "init":
            from .development import create_project
            result = create_project(args.path, args.name, args.goal, args.state_dir,
                                    author_name=args.author_name, author_email=args.author_email)
        elif args.action == "import":
            # Validate and inspect first: invalid input must not create state.
            result = registry.register(inspect_repository(args.path), workspace=True)
        elif args.action == "list":
            result = registry.list_projects()
        else:
            result = (registry.get_project_readonly(args.project_id) if args.action == "guide"
                      else registry.get_project(args.project_id))
            if result is None:
                print(json.dumps({"error": {"code": "project_not_found", "message": "未找到该项目登记记录。"}},
                                 ensure_ascii=True), file=sys.stderr)
                return 1
            if args.action == "guide":
                from .resources import preflight_workspace, write_management_guide
                identity = preflight_workspace(Path(result["root_path"]))
                if identity is None or identity["project_id"] != result["id"]:
                    raise FileReadError("resource_identity_mismatch", "请先核对并接入项目资源目录。")
                registry.attach_workspace(result)
                result = write_management_guide(result["root_path"])
            elif args.action == "overview":
                result = build_overview(result)
            elif args.action == "map":
                from .structure import build_structure
                result = build_structure(result, commit=args.commit)
            elif args.action == "read":
                result = read_project_file(result, args.path, commit=args.commit,
                                           start_line=args.start_line, end_line=args.end_line)
            elif args.action == "search":
                result = search_project(result, args.query, commit=args.commit,
                                        limit=args.limit, case_sensitive=args.case_sensitive)
            elif args.action == "ask":
                from .qa import answer_context, prepare_context
                from .models import output_contract
                context = prepare_context(result, args.question, keywords=args.keyword, files=args.file,
                                          state_dir=args.state_dir, retrieval=args.retrieval)
                context["execution_policy"] = {"strategy": args.strategy, "max_model_calls": 3 if args.strategy == "multi" else 1,
                                               "structured_output": output_contract("qa")}
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
