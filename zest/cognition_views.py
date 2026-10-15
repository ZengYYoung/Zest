"""Human-readable cognition progress and compact six-part project dossiers."""

import time
import json
from .terminal import display_text
from .cognition_context import material_request_text

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
             f"格式失败最多补试 {record['policy'].get('max_format_retries', 0)} 次，计入上述总预算；结果未知不重发",
             f"批准资料：{record['file_count']} 个文件；包外资料需重新授权", ""]
    scope = record.get("scope_review") or {}
    if record.get("refresh"):
        refresh = record["refresh"]
        lines.append(f"增量调查 · 基线 {refresh['baseline_commit']} · 变更 {refresh['changed_files']} 个文件 · 历史待核对线索 {refresh['historical_leads']} 项 · 包外变更 {refresh['outside_package']} 个")
        lines.append("旧结论不直接迁移；目标版本的发现仍需重新读原文并核对。")
    if record.get("followup"):
        followup = record["followup"]
        lines.append("定向调查 · " + display_text(f"{followup['source_run_id']} / {followup['finding_id']} · {followup['statement']}"))
        lines.append("完成标准仅针对选定问题；旧发现及其后续行动保持原样，继续调查须单独批准。")
    if scope.get("requires_discussion"):
        lines.append(f"范围待确认：已纳入 {scope['source_count']} 个文件，规划清单展示 {scope['catalog_count']} 个；" + display_text(scope["message"]))
    if record["plan"]:
        lines.extend(["初步认知：", *["  " + display_text(item["statement"]) for item in record["plan"]["overview"]], "调查任务："])
    for job in record["jobs"]:
        lines.append(f"  {job['id']} · {LABELS.get(job['status'], job['status'])} · {display_text(job['spec']['title'])}")
        if job["result"]:
            for rejected in (job["result"].get("report_validation") or {}).get("rejected_findings", []):
                lines.append("    候选未接收：" + display_text(f"{rejected['field']} · {rejected['code']} · {rejected['message']}"))
            for rejected in (job["result"].get("report_validation") or {}).get("rejected_material_requests", []):
                lines.append("    资料需求未接收：" + display_text(f"{rejected['field']} · {rejected['code']} · {rejected['message']}"))
            lines.extend("    待补充：" + display_text(item) for item in job["result"]["report"]["unknowns"])
            for key in ("in_scope_requests", "scope_requests"):
                lines.extend("    " + display_text(material_request_text(item)) for item in job["result"]["report"].get(key, []))
    if record["error"]:
        lines.append("错误：" + display_text(record["error"]))
    for event in record.get("events", [])[-10:]:
        if event["action"] in ("model_format_retry_ready", "model_format_retry_started"):
            detail = json.loads(event["detail"])
            message = (f"第 {detail['failed_attempt']} 次格式失败已保存，等待预算内补试" if event["action"] == "model_format_retry_ready"
                       else f"已开始第 {detail['attempt']} 次请求（格式补试）")
            lines.append(display_text(f"{event['actor']} / {detail['sequence']}：{message}"))
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
    if record.get("followup"):
        followup = record["followup"]
        lines.extend(["定向调查来源：" + display_text(f"{followup['source_run_id']} / {followup['finding_id']}"),
                      "本轮完成仅针对选定问题：" + display_text(followup["statement"]),
                      "原发现与后续行动没有自动关闭。", ""])
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
                review = item.get("review") or {}
                for check in review.get("checks", []):
                    lines.append("  属性核对：" + display_text(f"{check['subject']} / {check['attribute']} / {check['verdict']} · {check['reason']}"))
                for conflict in [*review.get("conflicts", []), *review.get("conflict_links", [])]:
                    lines.append("  未解决冲突：" + display_text(f"{conflict['reference_id']} · {conflict['reason']}"))
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
