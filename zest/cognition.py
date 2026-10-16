"""Checkpointed coordination and independent investigation jobs over approved scopes."""

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import TypedDict

from .cognition_contracts import (COGNITION_PROMPTS, POLICY, canonical, items, keys, parse_response,
                                  text, validate_plan, partition_report, known_format_failure)
from .cognition_store import CognitionStore, digest, now, valid_id
from .cognition_tools import PackageTools, execute_mcp_actions
from .cognition_context import compact_proof, investigation_view, review_payload, review_unknowns, material_catalog, approved_materials, read_source_ids
from .files import FileReadError
from .models import DeepSeekModel, ModelSettings, model_target, output_contract, safe_model_receipt, prepare_model_request


class InvestigationState(TypedDict, total=False):
    turns: int
    actions_used: int
    tool_results: list
    evidence: dict
    response: dict
    report: dict
    spec: dict
    report_required: bool
    report_validation: dict


class CoordinationState(TypedDict, total=False):
    followups: bool
    done: bool


@contextmanager
def execution_lock(state_dir, name="worker", *, required=True):
    import fcntl
    directory = Path(state_dir).resolve() / "cognition-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / f"{name}.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            if required:
                raise FileReadError("cognition_worker_busy", "本地认知 worker 已在运行。")
        yield acquired
    finally:
        os.close(fd)


class CognitionEngine:
    def __init__(self, state_dir, *, model=None):
        self.store = CognitionStore(state_dir)
        self.model = model

    def call(self, run_id, actor, sequence, purpose, payload, envelope):
        from .resources import record_state
        destination = record_state(self.store.state_dir, "cognition", run_id)
        if destination != self.store.state_dir:
            return CognitionEngine(destination, model=self.model).call(run_id, actor, sequence, purpose, payload, envelope)
        # Each physical attempt is durable and consumes the approved total budget.
        # Only _call_attempt can admit another attempt from a known invalid receipt.
        while True:
            value, retry = self._call_attempt(run_id, actor, sequence, purpose, payload, envelope)
            if not retry:
                return value

    @staticmethod
    def _saved_call(old, request_hash, purpose):
        if old["payload_sha256"] != request_hash or old["purpose"] != purpose:
            raise FileReadError("cognition_request_changed", "恢复请求与保存身份不一致，不再次调用。")
        if old["status"] == "completed":
            return json.loads(old["response"]), False
        if old["status"] == "invalid":
            raise FileReadError("cognition_response_invalid", "已保存响应不符合契约，补试不可用或已用完。")
        if old["status"] != "retry_ready":
            raise FileReadError("cognition_execution_unknown", "旧请求没有可复用结果，不重复调用。")
        return None

    def _call_attempt(self, run_id, actor, sequence, purpose, payload, envelope):
        self.check_knowledge(envelope)
        provider = self.model or DeepSeekModel(ModelSettings.from_env())
        settings = provider.settings
        target = model_target(settings)
        if target != envelope["target"]:
            raise FileReadError("model_target_changed", "模型目标已变化，需要重新准备批准。")
        if envelope["instructions"] != COGNITION_PROMPTS or envelope["policy"] != POLICY or settings.timeout > POLICY["max_request_seconds"]:
            raise FileReadError("cognition_contract_changed", "工作流指令或预算已变化，需要重新批准。")
        if envelope.get("structured_outputs") != {kind: output_contract(kind) for kind in COGNITION_PROMPTS}:
            raise FileReadError("cognition_contract_changed", "结构化输出契约已变化，需要重新准备并批准。")
        request_hash = digest({"purpose": purpose, "payload": payload})
        # Reuse completed results first. Only an explicitly budgeted, durably
        # recorded format failure may reach a new physical attempt.
        with self.store.open() as conn:
            old = conn.execute("SELECT * FROM cognition_calls WHERE run_id=? AND actor=? AND sequence=?", (run_id, actor, sequence)).fetchone()
            if old:
                saved = self._saved_call(old, request_hash, purpose)
                if saved is not None:
                    return saved
        request = prepare_model_request(settings, payload, purpose=purpose)
        body = json.loads(request["body_text"])
        response_tokens = envelope["policy"]["max_report_tokens"] if purpose == "project_report" else envelope["policy"]["max_response_tokens"]
        if body.get("model") != envelope["target"]["model"] or body.get("max_tokens") != response_tokens:
            raise FileReadError("cognition_contract_changed", "实际请求的模型或响应预算与固定批准不同，未外发。")
        if request["chars"] > envelope["policy"]["max_extended_prompt_chars"]:
            raise FileReadError("cognition_prompt_budget", "完整请求超过 48000 字符，须调整上下文或拆分调查；未外发。")
        if request["chars"] > envelope["policy"]["max_prompt_chars"]:
            self.store.require_context(run_id, actor, sequence, envelope, request)
        with self.store.open(write=True) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ("planning", "investigating", "reviewing"):
                raise FileReadError("cognition_not_executing", "工作流未处于已批准执行阶段。")
            current = self.store.envelope(row)
            if digest(envelope) != row["envelope_sha256"] or current != envelope:
                raise FileReadError("approval_mismatch", "执行授权包不匹配。")
            old = conn.execute("SELECT * FROM cognition_calls WHERE run_id=? AND actor=? AND sequence=?", (run_id, actor, sequence)).fetchone()
            attempt = 1
            if old:
                saved = self._saved_call(old, request_hash, purpose)
                if saved is not None:
                    return saved
                previous = conn.execute("SELECT * FROM cognition_call_attempts WHERE run_id=? AND actor=? AND sequence=? ORDER BY attempt DESC LIMIT 1",
                                        (run_id, actor, sequence)).fetchone()
                wire = conn.execute("SELECT request FROM cognition_request_bodies WHERE run_id=? AND actor=? AND sequence=?",
                                    (run_id, actor, sequence)).fetchone()
                if (previous is None or previous["status"] != "invalid" or not previous["receipt"]
                        or not self._retryable(json.loads(previous["receipt"]))
                        or previous["attempt"] > envelope["policy"]["max_format_retries"]):
                    raise FileReadError("cognition_response_invalid", "没有可补试的已保存格式失败，或补试次数已用完。")
                if wire is None or json.loads(wire["request"]) != request:
                    raise FileReadError("cognition_request_changed", "补试请求与已保存的完整请求不同，未外发。")
                attempt = previous["attempt"] + 1
            if row["call_count"] >= envelope["policy"]["max_model_calls"]:
                raise FileReadError("cognition_call_budget", "模型调用预算已用完。")
            if row["started_at"] and (datetime.now(timezone.utc) - datetime.fromisoformat(row["started_at"])).total_seconds() > envelope["policy"]["max_seconds"]:
                raise FileReadError("cognition_time_budget", "批准工作流已达到执行时间预算。")
            if old is None:
                conn.execute("INSERT INTO cognition_calls VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (run_id, actor, sequence, purpose, canonical(payload), request_hash, "pending", None, None, now()))
                conn.execute("INSERT INTO cognition_request_bodies VALUES (?,?,?,?)", (run_id, actor, sequence, canonical(request)))
            else:
                conn.execute("UPDATE cognition_calls SET status='pending',error=NULL WHERE run_id=? AND actor=? AND sequence=?",
                             (run_id, actor, sequence))
            conn.execute("INSERT INTO cognition_call_attempts VALUES (?,?,?,?,?,?,?,?)",
                         (run_id, actor, sequence, attempt, "pending", None, None, now()))
            conn.execute("UPDATE cognition_runs SET call_count=call_count+1,updated_at=? WHERE id=?", (now(), run_id))
        self.store.event(run_id, actor, "model_request_started", {"sequence": sequence, "purpose": purpose, "payload_sha256": request_hash, "attempt": attempt})
        if attempt > 1:
            self.store.event(run_id, actor, "model_format_retry_started", {"sequence": sequence, "attempt": attempt})
        try:
            if isinstance(provider, DeepSeekModel):
                response = provider.complete(payload, purpose=purpose, expected_request=request)
            else:
                response = provider.complete(payload, purpose=purpose)
        except Exception as exc:
            code = exc.code if isinstance(exc, FileReadError) else "model_request_failed"
            with self.store.open(write=True) as conn, conn:
                conn.execute("UPDATE cognition_calls SET status=?,error=? WHERE run_id=? AND actor=? AND sequence=?",
                             ("not_sent" if code == "model_request_changed" else "uncertain", canonical({"code": code}), run_id, actor, sequence))
                conn.execute("UPDATE cognition_call_attempts SET status=?,error=? WHERE run_id=? AND actor=? AND sequence=? AND attempt=?",
                             ("not_sent" if code == "model_request_changed" else "uncertain", canonical({"code": code}), run_id, actor, sequence, attempt))
            if code == "model_request_changed":
                raise
            raise FileReadError("cognition_execution_unknown", "模型请求结果未知，未自动重试。") from exc
        parse_error = None
        try:
            value = parse_response(response)
        except FileReadError as exc:
            parse_error = exc
        receipt = safe_model_receipt(response)
        retry = (parse_error is not None and self._retryable(receipt)
                 and attempt <= envelope["policy"]["max_format_retries"])
        # A durable known response and its classification are one transaction.
        # A crash before this commit leaves no reusable receipt, never a retry.
        with self.store.open(write=True) as conn, conn:
            conn.execute("INSERT INTO cognition_call_receipts VALUES (?,?,?,?) ON CONFLICT(run_id,actor,sequence) DO UPDATE SET receipt=excluded.receipt",
                         (run_id, actor, sequence, canonical(receipt)))
            conn.execute("UPDATE cognition_call_attempts SET status=?,receipt=?,error=? WHERE run_id=? AND actor=? AND sequence=? AND attempt=?",
                         ("invalid" if parse_error else "completed", canonical(receipt),
                          canonical({"code": parse_error.code}) if parse_error else None, run_id, actor, sequence, attempt))
            if parse_error is not None:
                conn.execute("UPDATE cognition_calls SET status=?,error=? WHERE run_id=? AND actor=? AND sequence=?",
                             ("retry_ready" if retry else "invalid", canonical({"code": parse_error.code}), run_id, actor, sequence))
            else:
                conn.execute("UPDATE cognition_calls SET status='completed',response=? WHERE run_id=? AND actor=? AND sequence=?",
                             (canonical(value), run_id, actor, sequence))
        if parse_error is not None:
            if retry:
                self.store.event(run_id, actor, "model_format_retry_ready", {"sequence": sequence, "failed_attempt": attempt})
                return None, True
            if self._retryable(receipt):
                raise FileReadError("cognition_response_invalid", f"模型结构化输出无效；已保存 {attempt} 次尝试，补试次数已用完。")
            raise parse_error
        self.store.event(run_id, actor, "model_response_saved", {"sequence": sequence, "attempt": attempt})
        return value, False

    @staticmethod
    def _retryable(receipt):
        # A complete provider body is required. Timeouts, sensitive/oversized
        # output, and later business/evidence rejection never qualify.
        return known_format_failure(receipt)

    def check_knowledge(self, envelope):
        from .cognition_refresh import check_refresh_context
        from .cognition_followup import check_followup_context
        check_refresh_context(self.store, envelope)
        check_followup_context(self.store, envelope)
        for knowledge in envelope.get("shared_knowledge", []):
            current = self.store.reviewed_finding(knowledge["run_id"], knowledge["id"])
            if current["finding_sha256"] != knowledge["finding_sha256"] or current["commit"] != envelope["commit"]:
                raise FileReadError("cognition_knowledge_changed", "批准包中的共享认知已变化，需要重新准备。")

    @staticmethod
    def knowledge_hints(envelope):
        return [{key: knowledge[key] for key in ("id", "run_id", "body", "section", "finding_sha256")}
                for knowledge in envelope.get("shared_knowledge", [])]

    @contextmanager
    def graph(self, run_id, actor, builder):
        from langgraph.checkpoint.sqlite import SqliteSaver
        directory = self.store.state_dir / "cognition-checkpoints"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{valid_id(run_id)}-{actor}.sqlite3"
        if not re.fullmatch(r"coordinator|T[1-6]", actor) or path.is_symlink():
            raise FileReadError("invalid_cognition_actor", "图执行身份或路径无效。")
        import sqlite3
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path, timeout=30, check_same_thread=False)) as conn:
            yield builder.compile(checkpointer=SqliteSaver(conn))

    @staticmethod
    def invoke(graph, run_id, actor, initial):
        config = {"configurable": {"thread_id": f"{run_id}:{actor}"}}
        snapshot = graph.get_state(config)
        if snapshot.values and not snapshot.next:
            return snapshot.values
        return graph.invoke(None if snapshot.values else initial, config, durability="sync")

    @staticmethod
    def catalog(envelope):
        return material_catalog(envelope)

    def plan(self, run_id, envelope, tools):
        current = self.store.show(run_id)
        if current["plan"]:
            return current["plan"]
        docs, document_count = [], 0
        for source in envelope["files"]:
            if set(source["roles"]) & {"document", "manifest"}:
                document_count += 1
                try:
                    candidate = tools.read(source["id"])
                except FileReadError:
                    continue
                if len(canonical([*docs, compact_proof(candidate)])) > envelope["policy"]["starting_document_chars"]:
                    continue
                docs.append(compact_proof(candidate))
        catalog = self.catalog(envelope)
        payload = {"commit": envelope["commit"], "material_catalog": catalog,
                   "material_count": len(envelope["files"]), "starting_documents": docs,
                   "catalog_coverage": {"shown": len(catalog), "total_in_scope": len(envelope["files"]), "truncated": len(catalog) < len(envelope["files"])},
                   "starting_documents_coverage": {"included": len(docs), "candidates": document_count},
                   "shared_knowledge": self.knowledge_hints(envelope),
                   "gaps": envelope["survey_gaps"], "policy": envelope["policy"]}
        if "refresh_context" in envelope:
            payload["refresh_context"] = envelope["refresh_context"]
        if "management_guidance" in envelope:
            payload["management_guidance"] = envelope["management_guidance"]
        if "followup_context" in envelope:
            from .cognition_followup import focused_plan
            payload["followup_context"] = envelope["followup_context"]
            result = focused_plan(self.call(run_id, "coordinator", 0, "project_followup_plan", payload, envelope),
                                  envelope["followup_context"], {s["id"] for s in catalog})
        else:
            result = validate_plan(self.call(run_id, "coordinator", 0, "project_plan", payload, envelope), {source["id"] for source in catalog})
        self.store.save_plan(run_id, result)
        self.store.event(run_id, "coordinator", "plan_ready", {"tasks": len(result["tasks"])})
        return result

    def investigate(self, run_id, spec, envelope, tools, plan):
        from langsmith import tracing_context
        # ThreadPoolExecutor does not inherit the coordinator's ContextVar settings.
        with tracing_context(enabled=False):
            return self._investigate(run_id, spec, envelope, tools, plan)

    def _investigate(self, run_id, spec, envelope, tools, plan):
        from langgraph.graph import StateGraph, START, END
        actor = spec["id"]
        self.store.job_status(run_id, actor, "running")
        self.store.event(run_id, actor, "investigation_started", {"title": spec["title"]})

        def reason(state):
            if state["turns"] >= envelope["policy"]["max_turns_per_task"]:
                return {"report": {"status": "partial", "findings": [], "unknowns": ["调查已达本任务模型轮次预算。"], "scope_requests": []}}
            payload = {"task": spec, "overview": plan["overview"], "material_catalog": approved_materials(envelope),
                       "read_source_ids": read_source_ids(state["tool_results"]),
                       "shared_knowledge": self.knowledge_hints(envelope),
                       "dependency_reports": [{"task_id": job["id"], "report": job["result"]["report"]}
                                              for job in self.store.show(run_id)["jobs"]
                                              if job["id"] in spec["depends_on"] and job["status"] == "completed" and job["result"]],
                       **investigation_view(state["tool_results"]), "remaining_turns": envelope["policy"]["max_turns_per_task"] - state["turns"],
                       "remaining_tool_actions": envelope["policy"]["max_tool_actions_per_task"] - state["actions_used"]}
            if "refresh_context" in envelope:
                payload["refresh_context"] = envelope["refresh_context"]
            if "followup_context" in envelope:
                payload["followup_context"] = envelope["followup_context"]
            if "management_guidance" in envelope:
                payload["management_guidance"] = envelope["management_guidance"]
            closing = (payload["remaining_turns"] == 1 or state.get("report_required", False)
                       or payload["remaining_tool_actions"] == 0)
            payload["report_required"] = closing
            def closing_view():
                return {**payload, "report_required": True, "remaining_tool_actions": 0,
                        "tool_results": [item for item in payload["tool_results"] if item["tool"] == "read"]}
            if closing:
                payload = closing_view()
            try:
                output = self.call(run_id, actor, state["turns"], "project_report" if closing else "project_research", payload, envelope)
            except FileReadError as exc:
                if exc.code != "cognition_prompt_budget" or closing:
                    raise
                # The oversized candidate was never sent or reserved. Reorganize
                # into a smaller report request, retaining all original proofs.
                payload = closing_view()
                closing = True
                self.store.event(run_id, actor, "context_reorganized", {"reason": "complete_request_over_limit"})
                output = self.call(run_id, actor, state["turns"], "project_report", payload, envelope)
            keys(output, ("actions", "report"))
            actions = items(output["actions"], 4)
            for action in actions:
                keys(action, ("tool", "args"))
            if output["report"] is not None:
                if actions:
                    raise FileReadError("invalid_cognition_output", "结束报告不能同时请求工具。")
                report, audit = partition_report(output["report"], state["evidence"], approved_materials(envelope), payload["read_source_ids"])
                return {"turns": state["turns"] + 1, "report": report, "report_validation": audit}
            if not actions:
                raise FileReadError("invalid_cognition_output", "调查既未请求工具也未交付报告。")
            if closing:
                raise FileReadError("invalid_cognition_output", "保留的报告轮不能再请求工具，原文证据仍保存。")
            return {"turns": state["turns"] + 1, "response": output}

        def use_tools(state):
            actions = state["response"]["actions"]
            if state["actions_used"] + len(actions) > envelope["policy"]["max_tool_actions_per_task"]:
                return {"report": {"status": "partial", "findings": [], "unknowns": ["调查工具动作预算已用完。"], "scope_requests": []}}
            output = asyncio.run(execute_mcp_actions(tools, actions))
            evidence = dict(state["evidence"])
            results = list(state["tool_results"])
            for value in output:
                if value["tool"] == "read" and value["result"].get("ok"):
                    data = value["result"]["data"]
                    if data.get("text"):
                        ref = next((ref for ref, proof in evidence.items() if proof == data), f"R{len(evidence) + 1}")
                        evidence[ref] = data
                        value = {**value, "citation_id": ref}
                results.append(value)
            report_required = len(canonical(investigation_view(results))) > envelope["policy"]["evidence_warning_chars"]
            self.store.event(run_id, actor, "tools_finished", {"actions": len(actions), "read_proofs": len(evidence)})
            if report_required:
                self.store.event(run_id, actor, "report_required", {"reason": "evidence_context_warning"})
            return {"actions_used": state["actions_used"] + len(actions), "evidence": evidence, "tool_results": results,
                    "report_required": report_required}

        graph = StateGraph(InvestigationState)
        graph.add_node("reason", reason)
        graph.add_node("tools", use_tools)
        graph.add_edge(START, "reason")
        graph.add_conditional_edges("reason", lambda state: END if state.get("report") else "tools")
        graph.add_conditional_edges("tools", lambda state: END if state.get("report") else "reason")
        saved = {}
        try:
            with self.graph(run_id, actor, graph) as compiled:
                try:
                    result = self.invoke(compiled, run_id, actor, {"spec": spec, "turns": 0, "actions_used": 0, "evidence": {}, "tool_results": []})
                except Exception:
                    saved = compiled.get_state({"configurable": {"thread_id": f"{run_id}:{actor}"}}).values
                    raise
            self.store.publish_job(run_id, actor, {"report": result["report"], "evidence": result["evidence"],
                                                   "tool_results": result["tool_results"], "turns": result["turns"],
                                                   "report_validation": result.get("report_validation")})
            self.store.event(run_id, actor, "investigation_finished", {"status": result["report"]["status"]})
        except FileReadError as exc:
            status = ("awaiting_context_approval" if exc.code == "cognition_context_approval_required" else
                      "pending_execution" if exc.code == "cognition_execution_unknown" else
                      "partial" if "budget" in exc.code or exc.code == "cognition_context_rejected" else "failed")
            result = {"report": {"status": "partial", "findings": [], "unknowns": [f"{exc.code}: {exc}"], "scope_requests": []},
                      "evidence": saved.get("evidence", {}), "tool_results": saved.get("tool_results", []), "turns": saved.get("turns", 0)}
            self.store.job_status(run_id, actor, status, result)
            self.store.event(run_id, actor, "investigation_stopped", {"code": exc.code, "status": status})
        except Exception:
            self.store.job_status(run_id, actor, "failed", {"report": {"status": "partial", "findings": [], "unknowns": ["调查内部错误，原始异常未回显。"], "scope_requests": []},
                "evidence": saved.get("evidence", {}), "tool_results": saved.get("tool_results", []), "turns": saved.get("turns", 0)})

    def dispatch(self, run_id, envelope, tools, plan):
        self.store.set_status(run_id, "investigating")
        with ThreadPoolExecutor(max_workers=envelope["policy"]["max_parallel_tasks"]) as pool:
            while True:
                jobs = self.store.show(run_id)["jobs"]
                by_id = {job["id"]: job for job in jobs}
                ready = [job for job in jobs if job["status"] in ("queued", "running") and
                         all(by_id[ref]["status"] == "completed" for ref in job["spec"]["depends_on"])]
                if not ready:
                    if any(job["status"] == "awaiting_context_approval" for job in jobs):
                        raise FileReadError("cognition_context_approval_required", "有完整请求待扩展批准；已完成的调查保留，依赖任务继续等待。")
                    for job in jobs:
                        if job["status"] in ("queued", "running"):
                            self.store.job_status(run_id, job["id"], "blocked", {"report": {"status": "partial", "findings": [],
                                  "unknowns": ["依赖调查未完成，未执行本任务。"], "scope_requests": []}, "evidence": {}})
                    break
                ready.sort(key=lambda job: (job["spec"]["priority"], job["id"]))
                futures = [pool.submit(self.investigate, run_id, job["spec"], envelope, tools, plan) for job in ready]
                for future in as_completed(futures):
                    future.result()

    def review(self, run_id, envelope):
        self.store.set_status(run_id, "reviewing")
        record = self.store.show(run_id)
        failed_ids = {identity for failure in record.get("review_failures", []) for identity in failure["finding_ids"]}
        pending = [f for f in record["findings"] if f["status"] == "pending_review" and f["id"] not in failed_ids]
        offset = 0
        while offset < len(pending):
            batch = pending[offset:offset + 4]
            while True:
                record = self.store.show(run_id)
                other = [{"finding_id": item["id"], "statement": item["finding"]["statement"], "kind": item["finding"]["kind"]}
                         for item in record["findings"] if item["id"] not in {finding["id"] for finding in batch}]
                payload = review_payload(envelope["commit"], batch, other, review_unknowns(record["jobs"]))
                actor = "review_" + digest([item["id"] for item in batch])[:12]
                try:
                    response = self.call(run_id, actor, 0, "project_review", payload, envelope)
                    break
                except FileReadError as exc:
                    if exc.code == "cognition_response_invalid":
                        failure = next((item for item in self.store.show(run_id).get("review_failures", [])
                                        if item["actor"] == actor and item["sequence"] == 0
                                        and item["finding_ids"] == [f["id"] for f in batch]), None)
                        if failure is not None:
                            self.store.event(run_id, actor, "review_batch_isolated", failure)
                            response = None
                            break
                    if exc.code != "cognition_prompt_budget" or len(batch) == 1:
                        raise
                    # Repartition only unsent overlarge candidates; never retry a
                    # received, unknown or approval-pending request.
                    batch = batch[:max(1, len(batch) // 2)]
                    self.store.event(run_id, "coordinator", "review_batch_reorganized", {"count": len(batch)})
            if response is None:
                offset += len(batch)
                continue
            keys(response, ("judgments",))
            from .cognition_review import validate_judgments
            judgments = validate_judgments(response["judgments"], payload)
            self.store.save_review(run_id, judgments)
            self.store.event(run_id, "coordinator", "findings_reviewed", {"count": len(batch)})
            offset += len(batch)

    def followups(self, run_id):
        record = self.store.show(run_id)
        available = self.store.show(run_id, include_envelope=True)["envelope"]["policy"]["max_tasks"] - len(record["jobs"])
        if available <= 0:
            return False
        jobs = {job["id"]: job for job in record["jobs"]}
        unresolved = [f for f in record["findings"] if f["status"] == "needs_investigation" and f["finding"]["kind"] == "observed"
                      and f["job_id"] not in ("T5", "T6")
                      and not any(job["spec"].get("followup_finding") == f["id"] for job in record["jobs"])]
        added = False
        with self.store.open(write=True) as conn, conn:
            for finding in unresolved[:min(available, 2)]:
                task_id = "T5" if "T5" not in jobs else "T6"
                if task_id in jobs:
                    break
                spec = dict(jobs[finding["job_id"]]["spec"])
                spec.update(id=task_id, title="补充核对：" + spec["title"][:130],
                            goal="重新读取原文核对这项发现，证据不足请保留未知：" + finding["finding"]["statement"][:300] + "；核对意见：" + finding["review"]["reason"][:200],
                            depends_on=[])
                spec["followup_finding"] = finding["id"]
                conn.execute("INSERT OR IGNORE INTO cognition_jobs VALUES (?,?,?,?,?,?)", (run_id, task_id, canonical(spec), "queued", None, now()))
                jobs[task_id] = {"spec": spec}
                added = True
        return added

    def run(self, run_id):
        valid_id(run_id)
        from .resources import record_state
        destination = record_state(self.store.state_dir, "cognition", run_id)
        if destination != self.store.state_dir:
            return CognitionEngine(destination, model=self.model).run(run_id)
        with execution_lock(self.store.state_dir, f"run-{run_id}"):
            return self._run(run_id)

    def _run(self, run_id):
        from langgraph.graph import StateGraph, START, END
        from langsmith import tracing_context
        valid_id(run_id)
        record = self.store.show(run_id, include_envelope=True)
        if record["status"] != "queued":
            raise FileReadError("cognition_not_queued", "工作流未处于已批准队列。")
        envelope = record["envelope"]
        tools = PackageTools(envelope)
        with self.store.open(write=True) as conn, conn:
            changed = conn.execute("UPDATE cognition_runs SET status='planning',started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND status='queued'", (now(), now(), run_id))
            if changed.rowcount != 1:
                raise FileReadError("cognition_worker_busy", "认知工作流已被其他执行者领取。")
        def plan_node(state):
            self.plan(run_id, envelope, tools)
            return {}
        def dispatch_node(state):
            self.dispatch(run_id, envelope, tools, self.store.show(run_id)["plan"])
            return {}
        def review_node(state):
            self.review(run_id, envelope)
            return {"followups": self.followups(run_id)}
        def finish_node(state):
            return {"done": True}
        builder = StateGraph(CoordinationState)
        builder.add_node("plan", plan_node)
        builder.add_node("dispatch", dispatch_node)
        builder.add_node("review", review_node)
        builder.add_node("finish", finish_node)
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "dispatch")
        builder.add_edge("dispatch", "review")
        builder.add_conditional_edges("review", lambda state: "dispatch" if state["followups"] else "finish")
        builder.add_edge("finish", END)
        try:
            with tracing_context(enabled=False), self.graph(run_id, "coordinator", builder) as graph:
                self.invoke(graph, run_id, "coordinator", {"done": False, "followups": False})
            result = self.store.show(run_id)
            self.check_knowledge(envelope)
            unknown = any(job["status"] == "pending_execution" for job in result["jobs"])
            with self.store.open() as conn:
                unknown = unknown or conn.execute("SELECT 1 FROM cognition_calls WHERE run_id=? AND status IN ('pending','uncertain') LIMIT 1", (run_id,)).fetchone() is not None
            reviewed_sections = {f["finding"]["section"] for f in result["findings"] if f["status"] == "auto_reviewed"}
            coverage_complete = (set(envelope["followup_context"]["sections"]) <= reviewed_sections
                                 if "followup_context" in envelope else
                                 reviewed_sections == {"purpose", "architecture", "flows", "dependencies", "development", "constraints"})
            complete = (all(job["status"] == "completed" for job in result["jobs"])
                        and not result.get("review_failures")
                        and all(f["status"] == "auto_reviewed" for f in result["findings"])
                        and coverage_complete
                        and not result["plan"]["gaps"])
            self.store.set_status(run_id, "pending_execution" if unknown else "completed" if complete else "partial")
        except FileReadError as exc:
            self.store.set_status(run_id, "awaiting_context_approval" if exc.code == "cognition_context_approval_required" else
                                  "pending_execution" if exc.code == "cognition_execution_unknown" else
                                  "partial" if "budget" in exc.code or exc.code == "cognition_context_rejected" else "failed",
                                  error={"code": exc.code, "message": str(exc)})
        except Exception:
            self.store.set_status(run_id, "failed", error={"code": "cognition_execution_failed", "message": "认知执行失败，未回显原始异常。"})
        final_status = self.store.show(run_id)["status"]
        self.store.event(run_id, "coordinator", "workflow_paused" if final_status == "awaiting_context_approval" else "workflow_finished", {"status": final_status})
        return self.store.show(run_id)


def worker(state_dir, *, once=False, idle_seconds=30, model=None):
    from .resources import resource_states
    from .storage import ProjectRegistry
    origin = Path(state_dir).resolve()
    projects = ProjectRegistry(origin).list_projects_readonly()
    # A project worker owns only its own queue. A catalog worker can service
    # registered projects without changing their durable lock/checkpoint roots.
    scopes = [origin] if any(p.get("resource_dir") == str(origin) for p in projects) else resource_states(origin)
    if len(scopes) == 1:
        return _worker_local(origin, once=once, idle_seconds=idle_seconds, model=model)
    idle_since = time.monotonic()
    while True:
        processed = False
        for directory in scopes:
            try:
                processed = _worker_local(directory, once=True, idle_seconds=idle_seconds, model=model) or processed
            except FileReadError as exc:
                if exc.code != "cognition_worker_busy":
                    raise
            if processed and once:
                return
        if processed:
            idle_since = time.monotonic()
        if once or time.monotonic() - idle_since >= idle_seconds:
            return
        time.sleep(0.5)


def _worker_local(state_dir, *, once=False, idle_seconds=30, model=None):
    """Separate local process; no retries of requests with unknown outcomes."""
    engine = CognitionEngine(state_dir, model=model)
    with execution_lock(state_dir):
        idle_since = time.monotonic()
        while True:
            if engine.store.db_path.exists():
                with engine.store.open() as conn:
                    row = conn.execute("SELECT id,config_dir FROM cognition_runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    # Project/model config stays local; cwd never enters the provider payload.
                    previous = Path.cwd()
                    try:
                        if model is None:
                            os.chdir(row["config_dir"])
                        engine.run(row["id"])
                    finally:
                        os.chdir(previous)
                    idle_since = time.monotonic()
                    if once:
                        return True
                    continue
            if once or time.monotonic() - idle_since >= idle_seconds:
                return False
            time.sleep(0.5)


def start_worker(state_dir):
    import subprocess
    import sys
    import threading
    state_dir = Path(state_dir).resolve()
    with execution_lock(state_dir, required=False) as available:
        if not available:
            return {"status": "already_running"}
    log = state_dir / "cognition-worker.log"
    fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        process = subprocess.Popen([sys.executable, "-m", "zest", "--state-dir", str(state_dir), "cognition", "worker"],
                                   cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL, stdout=fd,
                                   stderr=fd, start_new_session=True)
    finally:
        os.close(fd)
    # Retain/reap the child while this launcher lives; child remains independent on exit.
    threading.Thread(target=process.wait, name="zest-cognition-reaper", daemon=True).start()
    return {"status": "started", "pid": process.pid}
