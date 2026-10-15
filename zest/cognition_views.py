"""Human-readable cognition progress and compact six-part project dossiers."""

import time
from .terminal import display_text

TERMINAL_STATUSES = {"completed", "partial", "failed", "pending_execution", "rejected", "awaiting_context_approval"}
LABELS = {"awaiting_approval": "等待资料包批准", "queued": "已入队", "planning": "制定调查计划",
          "investigating": "后台调查", "reviewing": "核对认知", "completed": "本轮任务完成",
          "partial": "已形成部分认知", "failed": "失败", "pending_execution": "执行结果待核对",
          "rejected": "已拒绝", "running": "正在调查", "blocked": "等待依赖结果",
          "auto_reviewed": "自动核对", "needs_investigation": "待核实", "pending_review": "待核对", "invalidated": "已失效",
          "awaiting_context_approval": "完整请求待字符扩展批准"}


def format_cognition(record):
    lines = ["Zest · 项目认知", f"工作流：{record['id']}", f"版本：{record['commit']}",
             f"阶段：{LABELS.get(record['status'], record['status'])}",
             f"模型请求：{record['call_count']} / {record['policy']['max_model_calls']} 次",
             f"批准资料：{record['file_count']} 个文件；包外资料需重新授权", ""]
    scope = record.get("scope_review") or {}
    if scope.get("requires_discussion"):
        lines.append(f"范围待确认：已纳入 {scope['source_count']} 个文件，规划清单展示 {scope['catalog_count']} 个；" + display_text(scope["message"]))
    if record["plan"]:
        lines.extend(["初步认知：", *["  " + display_text(item["statement"]) for item in record["plan"]["overview"]], "调查任务："])
    for job in record["jobs"]:
        lines.append(f"  {job['id']} · {LABELS.get(job['status'], job['status'])} · {display_text(job['spec']['title'])}")
        if job["result"]:
            lines.extend("    待补充：" + display_text(item) for item in job["result"]["report"]["unknowns"])
            lines.extend("    待扩展授权：" + display_text(item) for item in job["result"]["report"]["scope_requests"])
    if record["error"]:
        lines.append("错误：" + display_text(record["error"]))
    for request in record.get("context_requests", []):
        if request["status"] == "awaiting_approval":
            lines.append(f"待扩展：{request['actor']} / {request['sequence']} · {request['chars']} 字符，默认 {request['default_chars']}，超出 {request['over_percent']}%；最高 {request['max_chars']}。尚未外发，需审阅完整请求。")
    if record.get("worker_active") is False and record["status"] in ("planning", "investigating", "reviewing"):
        lines.append("worker 已退出；请核对保存的请求记录并显式恢复，未知请求不会重发。")
    lines.append(f"自动核对成果：{sum(f['status'] == 'auto_reviewed' for f in record['findings'])} 项；其余发现保留待核实/失效状态。")
    lines.append("本轮完成不代表完整理解或运行验证；主工作台可继续对话。")
    return "\n".join(lines)


def format_dossier(record):
    lines = ["# Zest 项目认知档案", "", f"观察版本：{record['commit']}", f"认知工作流：{record['run_id']}",
             f"本轮状态：{LABELS.get(record['status'], record['status'])}",
             "自动核对与人工确认分别记录；当前材料为静态快照，未证明生产运行状态。", ""]
    for section in record["sections"]:
        lines.extend([f"## {section['title']}", ""])
        if not section["facts"]:
            lines.append("尚无自动核对通过的认知。")
        for item in section["facts"]:
            lines.append("- " + display_text(item["finding"]["statement"]))
            for proof in item["finding"]["evidence"]:
                evidence = proof["evidence"]
                lines.append(f"  来源：{display_text(evidence['path'])}:{evidence['start_line']}–{evidence['end_line']} · {evidence['commit']}")
        if section["pending"]:
            lines.append("待核实：")
            for item in section["pending"]:
                statement = item.get("question") or item["finding"]["statement"]
                lines.append("- " + display_text(statement))
        lines.append("")
    if record["plan_gaps"]:
        lines.extend(["## 尚未解决的背景", "", *["- " + display_text(gap["question"]) for gap in record["plan_gaps"]]])
    return "\n".join(lines)


def watch_cognition(store, run_id):
    last = None
    try:
        while True:
            record = store.show(run_id)
            record["worker_active"] = store.worker_active(run_id)
            marker = (record["status"], record["call_count"], tuple((j["id"], j["status"]) for j in record["jobs"]))
            if marker != last:
                print(format_cognition(record), flush=True)
                last = marker
            if record["status"] in TERMINAL_STATUSES or record["status"] == "awaiting_approval" or (record["status"] in ("planning", "investigating", "reviewing") and not record["worker_active"]):
                return
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("停止观察；独立 worker 不因此取消，记录仍可查询。", flush=True)
