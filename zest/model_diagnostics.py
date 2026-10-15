"""Opt-in LangChain collection and explicit LangSmith export for a single call.

Collection has no LangSmith client or network. Persist the model receipt and this
export locally before uploading; a telemetry failure never repeats a model call.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from .files import CREDENTIAL_PATTERNS, FileReadError


@dataclass(frozen=True)
class LangSmithSettings:
    api_key: str = field(repr=False)
    endpoint: str = "https://api.smith.langchain.com"
    project: str = "zest-structured-diagnostics"

    @classmethod
    def from_env(cls, env_file=None):
        from dotenv import dotenv_values
        values = dotenv_values(env_file or Path.cwd() / ".env", interpolate=False)

        def get(key, default=""):
            return (os.environ.get(key, values.get(key) or default)).strip()

        settings = cls(get("LANGSMITH_API_KEY"), get("LANGSMITH_ENDPOINT", cls.endpoint),
                       get("LANGSMITH_PROJECT", cls.project))
        if not settings.api_key:
            raise FileReadError("langsmith_not_configured", "请在本地 .env 配置 LANGSMITH_API_KEY。")
        target = urlsplit(settings.endpoint)
        if (target.scheme != "https" or not target.hostname or target.username or target.password
                or target.query or target.fragment):
            raise FileReadError("invalid_langsmith_url", "LangSmith 接口须为无凭据的 HTTPS URL。")
        if not settings.project or len(settings.project) > 100:
            raise FileReadError("invalid_langsmith_project", "LangSmith 项目名须为 1—100 字符。")
        return settings


def _ensure_safe(value, secrets=()):
    encoded = json.dumps(value, ensure_ascii=False)
    if (any(secret and secret in encoded for secret in secrets)
            or any(re.search(pattern, encoded.encode()) for pattern in CREDENTIAL_PATTERNS)):
        raise FileReadError("sensitive_trace", "追踪中发现凭据，未上传。")


def arguments_diagnostic(receipt):
    """Diagnose the original argument string, without repairing or accepting it."""
    raw = receipt.get("raw_response") or {}
    body = raw.get("provider_response") or {}
    choices = body.get("choices", []) if isinstance(body, dict) else []
    message = choices[0].get("message", {}) if choices else {}
    calls = message.get("tool_calls") or []
    results = []
    for index, call in enumerate(calls):
        function = call.get("function") or {}
        text = function.get("arguments")
        item = {"index": index, "tool_name": function.get("name"), "json_valid": False}
        if isinstance(text, str):
            item.update(chars=len(text), sha256=hashlib.sha256(text.encode()).hexdigest())
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                item.update(error="invalid_json", offset=exc.pos, line=exc.lineno, column=exc.colno)
            else:
                item["json_valid"] = True
        else:
            item["error"] = "arguments_not_string"
        results.append(item)
    return {"tool_calls": results, "structured_status": receipt.get("structured_status"),
            "structured_error": receipt.get("structured_error")}


class LocalModelTrace:
    """Collect actual nested LangChain runs; upload only a reviewed safe export."""

    def __init__(self):
        from langchain_core.tracers.run_collector import RunCollectorCallbackHandler
        self.collector = RunCollectorCallbackHandler()
        self.id = uuid4()
        self.started = None
        self.ended = None

    def invoke(self, chain, values):
        if self.started is not None:
            raise FileReadError("diagnostic_already_used", "单次诊断不得再次调用模型。")
        self.started = datetime.now(timezone.utc)
        try:
            return chain.invoke(values, config={"callbacks": [self.collector], "run_id": self.id,
                                                "run_name": "Zest structured model call"})
        finally:
            self.ended = datetime.now(timezone.utc)

    def export(self, request, receipt, *, secrets=()):
        """Use raw HTTP at the root; nested spans expose timing and safe errors.

        Never export callback exception reprs, constructor serialization, runtime
        environment, or redundant per-span copies of project text.
        """
        if self.ended is None:
            raise FileReadError("diagnostic_not_finished", "模型调用尚未结束，未准备上传。")
        if receipt.get("structured_error") in ("sensitive_model_output", "model_output_budget"):
            raise FileReadError("unsafe_trace_output", "模型原始响应不可导出，未上传。")
        body = request["body_text"]
        if hashlib.sha256(body.encode()).hexdigest() != request["body_sha256"]:
            raise FileReadError("diagnostic_request_changed", "诊断请求身份不一致，未上传。")
        diagnostic = arguments_diagnostic(receipt)
        root = {"id": str(self.id), "name": "Zest structured output diagnostic", "run_type": "chain",
                "trace_id": str(self.id), "parent_run_id": None,
                "start_time": self.started.isoformat(), "end_time": self.ended.isoformat(),
                "inputs": {"url": request["url"], "request": json.loads(body)},
                "outputs": {"receipt": receipt, "diagnostic": diagnostic},
                "error": None if receipt.get("structured_status") == "valid" else
                         receipt.get("structured_error", "model_request_failed"),
                "extra": {"metadata": {"request_sha256": request["body_sha256"],
                                         "purpose": request["purpose"], "model_calls": 1}}}
        root_order = self.started.strftime("%Y%m%dT%H%M%S%fZ") + str(self.id)
        root["dotted_order"] = root_order
        runs = [root]

        def walk(run, parent_id, order):
            # The collector root is replaced by our outcome-aware diagnostic root.
            if str(run.id) == str(self.id):
                for child in run.child_runs:
                    walk(child, str(self.id), root_order)
                return
            dotted = order + "." + run.start_time.strftime("%Y%m%dT%H%M%S%fZ") + str(run.id)
            runs.append({"id": str(run.id), "name": run.name, "run_type": run.run_type,
                         "trace_id": str(self.id), "parent_run_id": parent_id, "dotted_order": dotted,
                         "start_time": run.start_time.isoformat(),
                         "end_time": run.end_time.isoformat() if run.end_time else self.ended.isoformat(),
                         "inputs": {"details": "See root frozen HTTP request"},
                         "outputs": {"finished": run.end_time is not None},
                         "error": "langchain_step_failed" if run.error else None})
            for child in run.child_runs:
                walk(child, str(run.id), dotted)

        for run in self.collector.traced_runs:
            walk(run, str(self.id), root_order)
        result = {"version": 1, "trace_id": str(self.id), "runs": runs}
        _ensure_safe(result, secrets)
        return result


def publish_trace(export, settings, *, secrets=(), client=None):
    """Explicit upload of the persisted export. Never invokes a model.

    Call only after the destination and request/response upload were authorized.
    SDK upload errors propagate as stable codes; callers retain the local receipt.
    """
    _ensure_safe(export, (*secrets, settings.api_key))
    owned = client is None
    if owned:
        from langsmith import Client
        client = Client(api_url=settings.endpoint, api_key=settings.api_key,
                        auto_batch_tracing=False, omit_traced_runtime_info=True,
                        hide_inputs=False, hide_outputs=False, hide_metadata=False,
                        tracing_mode="langsmith", disable_prompt_cache=True,
                        timeout_ms=10000)
    uploaded = []
    try:
        for run in export["runs"]:
            client.create_run(**run, project_name=settings.project, api_url=settings.endpoint,
                              api_key=settings.api_key, address=None)
            uploaded.append(run["id"])
    except Exception:
        raise FileReadError("langsmith_upload_failed", "追踪上传未完成；保留本地记录，不重发模型请求。") from None
    finally:
        if owned:
            client.close(timeout=5)
    return {"status": "uploaded", "trace_id": export["trace_id"], "run_ids": uploaded,
            "endpoint": settings.endpoint, "project": settings.project}
