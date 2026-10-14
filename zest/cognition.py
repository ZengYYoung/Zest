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
                                  text, validate_plan, validate_report)
from .cognition_store import CognitionStore, digest, now, valid_id
from .cognition_tools import PackageTools, execute_mcp_actions
from .files import FileReadError
from .models import DeepSeekModel, ModelSettings, model_target, output_contract, safe_model_receipt


class InvestigationState(TypedDict, total=False):
    turns: int
    actions_used: int
    tool_results: list
    evidence: dict
    response: dict
    report: dict
    spec: dict


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
        self.check_knowledge(envelope)
        if len(canonical(payload)) > envelope["policy"]["max_prompt_chars"]:
            raise FileReadError("cognition_prompt_budget", "单次模型请求超过批准预算。")
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
        with self.store.open(write=True) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM cognition_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ("planning", "investigating", "reviewing"):
                raise FileReadError("cognition_not_executing", "工作流未处于已批准执行阶段。")
            current = self.store.envelope(row)
            if digest(envelope) != row["envelope_sha256"] or current != envelope:
                raise FileReadError("approval_mismatch", "执行授权包不匹配。")
            old = conn.execute("SELECT * FROM cognition_calls WHERE run_id=? AND actor=? AND sequence=?", (run_id, actor, sequence)).fetchone()
            if old:
                if old["payload_sha256"] != request_hash or old["purpose"] != purpose:
                    raise FileReadError("cognition_request_changed", "恢复请求与保存身份不一致，不再次调用。")
                if old["status"] == "invalid":
                    raise FileReadError("cognition_response_invalid", "已保存的响应不符合契约，请审阅调用记录；不重新调用。")
                if old["status"] != "completed":
                    raise FileReadError("cognition_execution_unknown", "旧请求没有可复用结果，不重复调用。")
                return json.loads(old["response"])
            if row["call_count"] >= envelope["policy"]["max_model_calls"]:
                raise FileReadError("cognition_call_budget", "模型调用预算已用完。")
            if row["started_at"] and (datetime.now(timezone.utc) - datetime.fromisoformat(row["started_at"])).total_seconds() > envelope["policy"]["max_seconds"]:
                raise FileReadError("cognition_time_budget", "批准工作流已达到执行时间预算。")
            conn.execute("INSERT INTO cognition_calls VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (run_id, actor, sequence, purpose, canonical(payload), request_hash, "pending", None, None, now()))
            conn.execute("UPDATE cognition_runs SET call_count=call_count+1,updated_at=? WHERE id=?", (now(), run_id))
        self.store.event(run_id, actor, "model_request_started", {"sequence": sequence, "purpose": purpose, "payload_sha256": request_hash})
        try:
            response = provider.complete(payload, purpose=purpose)
        except Exception as exc:
            code = exc.code if isinstance(exc, FileReadError) else "model_request_failed"
            with self.store.open(write=True) as conn, conn:
                conn.execute("UPDATE cognition_calls SET status='uncertain',error=? WHERE run_id=? AND actor=? AND sequence=?",
                             (canonical({"code": code}), run_id, actor, sequence))
            raise FileReadError("cognition_execution_unknown", "模型请求结果未知，未自动重试。") from exc
        parse_error = None
        try:
            value = parse_response(response)
        except FileReadError as exc:
            parse_error = exc
        # A durable known response and its classification are one transaction.
        # A crash before this commit leaves no reusable receipt, never a retry.
        with self.store.open(write=True) as conn, conn:
            conn.execute("INSERT INTO cognition_call_receipts VALUES (?,?,?,?)",
                         (run_id, actor, sequence, canonical(safe_model_receipt(response))))
            if parse_error is not None:
                conn.execute("UPDATE cognition_calls SET status='invalid',error=? WHERE run_id=? AND actor=? AND sequence=?",
                             (canonical({"code": parse_error.code}), run_id, actor, sequence))
            else:
                conn.execute("UPDATE cognition_calls SET status='completed',response=? WHERE run_id=? AND actor=? AND sequence=?",
                             (canonical(value), run_id, actor, sequence))
        if parse_error is not None:
            raise parse_error
        self.store.event(run_id, actor, "model_response_saved", {"sequence": sequence})
        return value

    def check_knowledge(self, envelope):
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
        result = []
        for source in envelope["files"]:
            item = {"id": source["id"], "path": source["evidence"]["path"], "roles": source["roles"]}
            if len(canonical([*result, item])) > 5000 or len(result) >= 60:
                break
            result.append(item)
        return result

    def plan(self, run_id, envelope, tools):
        current = self.store.show(run_id)
        if current["plan"]:
            return current["plan"]
        docs = []
        for source in envelope["files"]:
            if set(source["roles"]) & {"document", "manifest"}:
                try:
                    candidate = tools.read(source["id"])
                except FileReadError:
                    continue
                if len(canonical([*docs, candidate])) > 14000:
                    break
                docs.append(candidate)
                if len(docs) >= 8:
                    break
        payload = {"commit": envelope["commit"], "material_catalog": self.catalog(envelope),
                   "material_count": len(envelope["files"]), "starting_documents": docs,
                   "shared_knowledge": self.knowledge_hints(envelope),
                   "gaps": envelope["survey_gaps"], "policy": envelope["policy"]}
        result = validate_plan(self.call(run_id, "coordinator", 0, "project_plan", payload, envelope), set(tools.files))
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
            payload = {"task": spec, "overview": plan["overview"], "material_catalog": self.catalog(envelope),
                       "shared_knowledge": self.knowledge_hints(envelope),
                       "dependency_reports": [{"task_id": job["id"], "report": job["result"]["report"]}
                                              for job in self.store.show(run_id)["jobs"]
                                              if job["id"] in spec["depends_on"] and job["status"] == "completed" and job["result"]],
                       "tool_results": state["tool_results"], "remaining_turns": envelope["policy"]["max_turns_per_task"] - state["turns"],
                       "remaining_tool_actions": envelope["policy"]["max_tool_actions_per_task"] - state["actions_used"]}
            output = self.call(run_id, actor, state["turns"], "project_research", payload, envelope)
            keys(output, ("actions", "report"))
            actions = items(output["actions"], 4)
            for action in actions:
                keys(action, ("tool", "args"))
            if output["report"] is not None:
                if actions:
                    raise FileReadError("invalid_cognition_output", "结束报告不能同时请求工具。")
                return {"turns": state["turns"] + 1, "report": validate_report(output["report"], state["evidence"])}
            if not actions:
                raise FileReadError("invalid_cognition_output", "调查既未请求工具也未交付报告。")
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
                    ref = f"R{len(evidence) + 1}"
                    data = value["result"]["data"]
                    evidence[ref] = data
                    value = {**value, "citation_id": ref}
                results.append(value)
            # Keep original read proofs durable; discard old non-read locations first.
            while len(canonical(results)) > 18000:
                index = next((i for i, result in enumerate(results) if result["tool"] != "read"), None)
                if index is None:
                    return {"report": {"status": "partial", "findings": [], "unknowns": ["证据上下文已达预算，材料与读取记录仍保存。"], "scope_requests": []},
                            "evidence": evidence, "tool_results": results}
                results.pop(index)
            self.store.event(run_id, actor, "tools_finished", {"actions": len(actions), "read_proofs": len(evidence)})
            return {"actions_used": state["actions_used"] + len(actions), "evidence": evidence, "tool_results": results}

        graph = StateGraph(InvestigationState)
        graph.add_node("reason", reason)
        graph.add_node("tools", use_tools)
        graph.add_edge(START, "reason")
        graph.add_conditional_edges("reason", lambda state: END if state.get("report") else "tools")
        graph.add_conditional_edges("tools", lambda state: END if state.get("report") else "reason")
        try:
            with self.graph(run_id, actor, graph) as compiled:
                result = self.invoke(compiled, run_id, actor, {"spec": spec, "turns": 0, "actions_used": 0, "evidence": {}, "tool_results": []})
            self.store.publish_job(run_id, actor, {"report": result["report"], "evidence": result["evidence"],
                                                   "tool_results": result["tool_results"], "turns": result["turns"]})
            self.store.event(run_id, actor, "investigation_finished", {"status": result["report"]["status"]})
        except FileReadError as exc:
            status = "pending_execution" if exc.code == "cognition_execution_unknown" else "partial" if "budget" in exc.code else "failed"
            result = {"report": {"status": "partial", "findings": [], "unknowns": [f"{exc.code}: {exc}"], "scope_requests": []}, "evidence": {}}
            self.store.job_status(run_id, actor, status, result)
            self.store.event(run_id, actor, "investigation_stopped", {"code": exc.code, "status": status})
        except Exception:
            self.store.job_status(run_id, actor, "failed", {"report": {"status": "partial", "findings": [], "unknowns": ["调查内部错误，原始异常未回显。"], "scope_requests": []}, "evidence": {}})

    def dispatch(self, run_id, envelope, tools, plan):
        self.store.set_status(run_id, "investigating")
        with ThreadPoolExecutor(max_workers=envelope["policy"]["max_parallel_tasks"]) as pool:
            while True:
                jobs = self.store.show(run_id)["jobs"]
                by_id = {job["id"]: job for job in jobs}
                ready = [job for job in jobs if job["status"] in ("queued", "running") and
                         all(by_id[ref]["status"] == "completed" for ref in job["spec"]["depends_on"])]
                if not ready:
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
        pending = [f for f in self.store.show(run_id)["findings"] if f["status"] == "pending_review"]
        for offset in range(0, len(pending), 4):
            batch = pending[offset:offset + 4]
            other = [{"finding_id": item["id"], "statement": item["finding"]["statement"], "kind": item["finding"]["kind"]}
                     for item in self.store.show(run_id)["findings"] if item["id"] not in {finding["id"] for finding in batch}]
            payload = {"commit": envelope["commit"], "findings": [{"finding_id": item["id"], **item["finding"]} for item in batch],
                       "other_findings": other}
            actor = "review_" + digest([item["id"] for item in batch])[:12]
            response = self.call(run_id, actor, 0, "project_review", payload, envelope)
            keys(response, ("judgments",))
            judgments = items(response["judgments"], len(batch), minimum=len(batch))
            expected = {item["id"] for item in batch}
            for judgment in judgments:
                keys(judgment, ("finding_id", "supported", "reason"))
                if not isinstance(judgment["finding_id"], str) or judgment["finding_id"] not in expected or type(judgment["supported"]) is not bool:
                    raise FileReadError("invalid_cognition_output", "知识核对身份或结论无效。")
                expected.remove(judgment["finding_id"])
                text(judgment["reason"])
            self.store.save_review(run_id, judgments)
            self.store.event(run_id, "coordinator", "findings_reviewed", {"count": len(batch)})

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
            complete = (all(job["status"] == "completed" for job in result["jobs"])
                        and all(f["status"] == "auto_reviewed" for f in result["findings"])
                        and {f["finding"]["section"] for f in result["findings"] if f["status"] == "auto_reviewed"} == {"purpose", "architecture", "flows", "dependencies", "development", "constraints"}
                        and not result["plan"]["gaps"])
            self.store.set_status(run_id, "pending_execution" if unknown else "completed" if complete else "partial")
        except FileReadError as exc:
            self.store.set_status(run_id, "pending_execution" if exc.code == "cognition_execution_unknown" else "partial" if "budget" in exc.code else "failed",
                                  error={"code": exc.code, "message": str(exc)})
        except Exception:
            self.store.set_status(run_id, "failed", error={"code": "cognition_execution_failed", "message": "认知执行失败，未回显原始异常。"})
        self.store.event(run_id, "coordinator", "workflow_finished", {"status": self.store.show(run_id)["status"]})
        return self.store.show(run_id)


def worker(state_dir, *, once=False, idle_seconds=30, model=None):
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
                        return
                    continue
            if once or time.monotonic() - idle_since >= idle_seconds:
                return
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
