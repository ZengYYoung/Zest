from contextlib import closing
from dataclasses import replace
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.cognition import CognitionEngine, execution_lock, worker
from zest.cognition_contracts import POLICY, SECTION_IDS, validate_plan, validate_report
from zest.cognition_store import CognitionStore
from zest.cognition_tools import PackageTools, create_package_server
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.models import ModelSettings
from zest.storage import ProjectRegistry


def review_judgment(finding, *, supported=True):
    return {"finding_id": finding["finding_id"], "coverage_complete": True, "conflicts": [],
            "reason": "离线核对夹具，不代表模型语义质量。", "checks": [
                {"subject": "add", "attribute": "返回值",
                 "condition": "无附加条件", "assertion": finding["statement"],
                 "evidence_ids": finding["evidence_ids"],
                 "verdict": "supported" if supported else "insufficient_evidence", "reason": "原文包含返回表达式。"}]}


class OfflineCognitionModel:
    """Explicit fixture; outputs are not model accuracy measurements."""
    def __init__(self, *, block=False, reject=False):
        self.settings = ModelSettings("deepseek", "cognition-fixture", "https://fixture.invalid", "fixture-private-key")
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entered = threading.Event()
        self.both_entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.reject = reject

    def complete(self, payload, *, purpose="qa"):
        with self.lock:
            self.calls.append((purpose, payload))
        if purpose == "project_plan":
            ids = [source["id"] for source in payload["material_catalog"]]
            tasks = []
            for number, sections in enumerate((sorted(SECTION_IDS)[:3], sorted(SECTION_IDS)[3:]), 1):
                tasks.append({"id": f"T{number}", "title": "调查加法实现", "goal": "确认 add 的实现", "reason": "了解示例代码",
                              "source_ids": ids[:2], "sections": sections, "deliverable": "带证据的实现说明",
                              "acceptance": ["引用 add 原文"], "depends_on": [], "priority": 1})
            value = {"overview": [{"statement": "这是一个待调查的示例。", "kind": "inferred", "source_ids": ids[:1]}],
                     "tasks": tasks, "gaps": []}
        elif purpose in ("project_research", "project_report"):
            source = next(source for source in payload["material_catalog"] if source["path"] == "app.py")
            if not payload["tool_results"]:
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    if self.active == 2:
                        self.both_entered.set()
                self.entered.set()
                self.release.wait(10)
                with self.lock:
                    self.active -= 1
                value = {"actions": [{"tool": "search", "args": {"query": "add"}},
                                     {"tool": "read", "args": {"source_id": source["id"]}}], "report": None}
            else:
                ref = next(result["citation_id"] for result in payload["tool_results"] if result["tool"] == "read")
                value = {"actions": [], "report": {"status": "completed", "findings": [
                    {"section": section, "kind": "observed", "statement": "观察版本中的 add 函数返回 a + b。", "citations": [ref]}
                    for section in payload["task"]["sections"]], "unknowns": [], "material_requests": []}}
        elif purpose == "project_review":
            value = {"judgments": [review_judgment(finding, supported=not self.reject) for finding in payload["findings"]]}
        else:
            value = {"status": "answered", "answer": "示例加法。[E1]", "citations": ["E1"], "unknowns": []}
        return {"text": json.dumps(value, ensure_ascii=False), "reported_model": "offline-fixture"}


@unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
class CognitionTest(unittest.TestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def setUp(self):
        self.setUpBase()
        (self.repo / "README.md").write_text("# Offline fixture\n用于验证代码流程，不是真实模型效果。\n")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.store = CognitionStore(self.state_dir)
        self.model = OfflineCognitionModel()
        self.target = {"provider": self.model.settings.provider, "model": self.model.settings.name, "base_url": self.model.settings.base_url}
        self.engine = CognitionEngine(self.state_dir, model=self.model)

    def prepare(self):
        return self.store.create(self.project["id"], target=self.target)

    def approved(self):
        record = self.prepare()
        self.store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        return record

    def test_scope_preview_is_complete_and_preparation_never_calls_model(self):
        record = self.prepare()
        full = self.store.show(record["id"], include_envelope=True)
        self.assertEqual(len(full["envelope"]["files"]), 2)
        self.assertIn("return a + b", json.dumps(full["envelope"]))
        self.assertNotIn(self.model.settings.api_key, self.store.db_path.read_bytes().decode(errors="ignore"))
        self.assertEqual(self.model.calls, [])
        self.assertEqual(record["status"], "awaiting_approval")
        with self.assertRaises(FileReadError):
            self.engine.run(record["id"])
        with self.assertRaises(FileReadError):
            self.store.decide(record["id"], approved=True, approval_sha256="wrong")
        self.assertEqual(self.store.show(record["id"])["status"], "awaiting_approval")

    def test_end_to_end_plan_mcp_research_review_and_six_part_dossier(self):
        record = self.approved()
        result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual(len(result["findings"]), 6)
        self.assertTrue(all(f["status"] == "auto_reviewed" for f in result["findings"]))
        self.assertEqual(len(self.store.dossier(record["id"])["sections"]), 6)
        self.assertLessEqual(result["call_count"], POLICY["max_model_calls"])
        calls = self.store.show(record["id"], include_calls=True)["calls"]
        self.assertTrue(all(call["status"] == "completed" for call in calls))
        self.assertEqual(len(calls), len(self.model.calls))
        self.assertTrue(list((self.state_dir / "cognition-checkpoints").glob("*.sqlite3")))
        self.assertEqual(len(self.store.for_context(self.project["id"], result["commit"], query="add")), 3)
        self.assertFalse((self.state_dir / "repairs.sqlite3").exists())

    def test_parallel_jobs_do_not_block_foreground_queries_and_never_double_dispatch(self):
        model = OfflineCognitionModel(block=True)
        record = self.approved()
        errors = []
        def execute():
            try:
                CognitionEngine(self.state_dir, model=model).run(record["id"])
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=execute)
        thread.start()
        self.addCleanup(model.release.set)
        self.assertTrue(model.entered.wait(10))
        self.assertTrue(model.both_entered.wait(10))
        current = self.store.show(record["id"])
        self.assertEqual(current["status"], "investigating")
        with self.assertRaises(FileReadError):
            self.engine.run(record["id"])
        with self.assertRaises(FileReadError):
            self.store.recover(record["id"])
        model.release.set()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(model.max_active, 2)

    def test_changed_target_fails_before_transmission(self):
        record = self.approved()
        self.model.settings = replace(self.model.settings, name="changed")
        result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "model_target_changed")
        self.assertEqual(self.model.calls, [])

    def test_response_cache_prevents_rebilling_after_missing_plan_checkpoint(self):
        record = self.approved()
        original = self.store.save_plan
        with patch.object(self.engine.store, "save_plan", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.engine.run(record["id"])
        self.assertEqual(len(self.model.calls), 1)
        self.store.recover(record["id"])
        result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(sum(purpose == "project_plan" for purpose, _ in self.model.calls), 1)

    def test_unknown_transport_result_is_never_retried(self):
        record = self.approved()
        with patch.object(self.model, "complete", side_effect=TimeoutError("do not echo")) as call:
            result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "pending_execution")
        self.assertEqual(call.call_count, 1)
        with self.assertRaises(FileReadError) as caught:
            self.store.recover(record["id"])
        self.assertEqual(caught.exception.code, "cognition_execution_unknown")
        self.assertNotIn("do not echo", json.dumps(result))

    def test_rejected_or_invalidated_findings_do_not_enter_shared_context(self):
        record = self.approved()
        result = self.engine.run(record["id"])
        for finding in result["findings"]:
            self.store.invalidate(record["id"], finding["id"], "错误陈述")
        self.assertEqual(self.store.for_context(self.project["id"], result["commit"]), [])
        self.assertEqual(self.store.for_context("other", result["commit"]), [])
        self.assertEqual(self.store.for_context(self.project["id"], "0" * 40), [])
        another = self.approved()
        rejected = CognitionEngine(self.state_dir, model=OfflineCognitionModel(reject=True)).run(another["id"])
        self.assertEqual(rejected["status"], "partial")
        self.assertLessEqual(len(rejected["jobs"]), 6)
        self.assertEqual(self.store.for_context(self.project["id"], rejected["commit"]), [])

    def test_budget_reservations_are_atomic_and_do_not_issue_extra_requests(self):
        with patch.dict(POLICY, {"max_model_calls": 1}):
            record = self.approved()
            result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(result["call_count"], 1)

    def test_package_tools_cannot_read_new_files_or_execute_code(self):
        import asyncio
        from fastmcp import Client
        record = self.prepare()
        envelope = self.store.show(record["id"], include_envelope=True)["envelope"]
        (self.repo / "new.py").write_text("SHOULD NOT ENTER APPROVED PACKAGE")
        tools = PackageTools(envelope)
        self.assertTrue(tools.search("add")["matches"])
        self.assertTrue(tools.symbols("add")["matches"])
        async def run():
            async with Client(create_package_server(tools)) as client:
                read = await client.call_tool("read", {"source_id": "../new.py"})
                listed = await client.list_tools()
            self.assertEqual(read.data["error"]["code"], "cognition_scope_expansion_required")
            self.assertTrue(all(item.annotations.read_only_hint for item in listed))
        asyncio.run(run())

    def test_plan_rejects_cycles_and_unknown_sources(self):
        record = self.prepare()
        tools = PackageTools(self.store.show(record["id"], include_envelope=True)["envelope"])
        plan = json.loads(self.model.complete({"material_catalog": self.engine.catalog(tools.envelope)}, purpose="project_plan")["text"])
        plan["tasks"][0]["depends_on"] = ["T2"]
        plan["tasks"][1]["depends_on"] = ["T1"]
        with self.assertRaises(FileReadError):
            validate_plan(plan, set(tools.files))
        plan["tasks"][0]["depends_on"] = []
        plan["tasks"][1]["depends_on"] = []
        plan["tasks"][0]["source_ids"] = ["FORGED"]
        with self.assertRaises(FileReadError):
            validate_plan(plan, set(tools.files))

    def test_cli_reading_and_rejection_require_no_model(self):
        record = self.prepare()
        code, output, error = self.cli("cognition", "preview", record["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["approval_sha256"], record["approval_sha256"])
        code, output, error = self.cli("cognition", "reject", record["id"])
        self.assertEqual(json.loads(output)["status"], "rejected")
        code, output, error = self.cli("cognition", "show", record["id"], "--format", "text")
        self.assertIn("已拒绝", output)
        self.assertEqual(self.model.calls, [])

    def test_read_only_empty_state_and_corrupt_scope(self):
        absent = CognitionStore(self.root / "absent")
        self.assertEqual(absent.list_runs(self.project["id"]), [])
        self.assertFalse(absent.state_dir.exists())
        record = self.prepare()
        with closing(sqlite3.connect(self.store.db_path)) as conn, conn:
            conn.execute("UPDATE cognition_runs SET envelope='{}' WHERE id=?", (record["id"],))
        with self.assertRaises(FileReadError):
            self.store.show(record["id"])

    def test_shared_knowledge_is_in_new_scope_and_invalidation_blocks_both_consumers(self):
        from zest.tasks import TaskRunner
        first = self.engine.run(self.approved()["id"])
        second = self.approved()
        packet = self.store.show(second["id"], include_envelope=True)["envelope"]
        self.assertEqual(len(packet["shared_knowledge"]), 3)
        runner = TaskRunner(self.state_dir, model=self.model)
        task = runner.create(self.project, "add 的实现是什么？", files=["app.py"])
        sources = task["context"]["sources"]
        shared = [source for source in sources if source["evidence"].get("kind") == "auto_reviewed_project_knowledge"]
        self.assertEqual(len(shared), 3)
        self.assertIn("return a + b", json.dumps(shared))
        before = len(self.model.calls)
        for finding in first["findings"]:
            self.store.invalidate(first["id"], finding["id"], "人工发现原文解释错误。")
        result = self.engine.run(second["id"])
        self.assertEqual(result["status"], "failed")
        runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(len(self.model.calls), before)
        self.assertEqual(runner.show(task["task_id"])["status"], "failed")

    def test_dependency_results_are_reused_and_inferred_is_never_auto_reviewed(self):
        original = self.model.complete
        def dependent(payload, *, purpose="qa"):
            response = original(payload, purpose=purpose)
            value = json.loads(response["text"])
            if purpose == "project_plan":
                value["tasks"][1]["depends_on"] = ["T1"]
            if purpose == "project_research" and value["report"]:
                value["report"]["findings"][0]["kind"] = "inferred"
            response["text"] = json.dumps(value, ensure_ascii=False)
            return response
        self.model.complete = dependent
        record = self.engine.run(self.approved()["id"])
        self.assertEqual(record["status"], "partial")
        inferred = [item for item in record["findings"] if item["finding"]["kind"] == "inferred"]
        self.assertTrue(inferred)
        self.assertTrue(all(item["status"] == "needs_investigation" for item in inferred))
        dependent_calls = [payload for purpose, payload in self.model.calls if purpose == "project_research" and payload["task"]["id"] == "T2"]
        self.assertEqual(dependent_calls[0]["dependency_reports"][0]["task_id"], "T1")
        for invalid in (["fake"], []):
            with self.assertRaises(FileReadError):
                validate_report({"status": "completed", "findings": [{"section": "flows", "kind": "observed", "statement": "事实", "citations": invalid}], "unknowns": [], "material_requests": []}, {})

    def test_related_changes_wait_without_blocking_unrelated_files_or_questions(self):
        from zest.tasks import TaskRunner
        record = self.approved()
        self.assertTrue(self.store.pending_for_paths(self.project["id"], record["commit"], ["app.py"]))
        self.assertFalse(self.store.pending_for_paths(self.project["id"], record["commit"], ["unrelated.py"]))
        runner = TaskRunner(self.state_dir, model=self.model)
        proposal = runner.create(self.project, "修改 add", files=["app.py"], purpose="repair_proposal")
        result = runner.decide(proposal["task_id"], approved=True, payload_sha256=proposal["payload_sha256"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "cognition_related_investigation_pending")
        self.assertEqual(self.model.calls, [])
        question = runner.create(self.project, "这个项目做什么？", files=["app.py"])
        result = runner.decide(question["task_id"], approved=True, payload_sha256=question["payload_sha256"])
        self.assertEqual(result["status"], "completed")
        self.engine.run(record["id"])
        self.assertFalse(self.store.pending_for_paths(self.project["id"], record["commit"], ["app.py"]))

    def test_worker_is_an_independent_process_and_unknown_crash_cannot_retry(self):
        record = self.approved()
        # A fixture provider in a separate interpreter exercises the production queue/locks/graphs.
        program = "from zest.cognition import worker; from tests.test_cognition import OfflineCognitionModel; import sys; worker(sys.argv[1], once=True, model=OfflineCognitionModel())"
        process = subprocess.Popen([sys.executable, "-c", program, str(self.state_dir)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            self.assertEqual(process.wait(timeout=30), 0)
            result = self.store.show(record["id"])
            self.assertEqual(result["status"], "completed", result.get("error"))
            self.assertFalse(result["worker_active"])
            self.assertTrue(result["events"])
            self.assertEqual(self.model.calls, [])  # Parent session did not run the investigation.
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
        crashed = self.approved()
        marker = self.root / "entered"
        program = ("from zest.cognition import worker; from tests.test_cognition import OfflineCognitionModel; import sys,time; from pathlib import Path; "
                   "m=OfflineCognitionModel(); m.complete=lambda *a,**k: (Path(sys.argv[2]).touch(),time.sleep(60))[1]; worker(sys.argv[1], once=True, model=m)")
        process = subprocess.Popen([sys.executable, "-c", program, str(self.state_dir), str(marker)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            for _ in range(100):
                if marker.exists():
                    break
                self.assertIsNone(process.poll())
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            self.assertTrue(self.store.worker_active(crashed["id"]))
            with self.assertRaises(FileReadError):
                self.store.recover(crashed["id"])
        finally:
            process.kill(); process.wait(timeout=5)
        self.assertFalse(self.store.worker_active(crashed["id"]))
        with self.assertRaises(FileReadError) as caught:
            self.store.recover(crashed["id"])
        self.assertEqual(caught.exception.code, "cognition_execution_unknown")
        self.assertEqual(self.store.show(crashed["id"], include_calls=True)["calls"][0]["status"], "pending")

    def test_launcher_detaches_and_reaps_child_and_does_not_duplicate_worker(self):
        from unittest.mock import Mock
        from zest.cognition import start_worker
        child = Mock(pid=42)
        with patch("subprocess.Popen", return_value=child) as popen:
            launched = start_worker(self.state_dir)
            self.assertEqual(launched, {"status": "started", "pid": 42})
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            for _ in range(50):
                if child.wait.called:
                    break
                time.sleep(0.01)
            self.assertTrue(child.wait.called)
            with execution_lock(self.state_dir):
                self.assertEqual(start_worker(self.state_dir), {"status": "already_running"})
            self.assertEqual(popen.call_count, 1)
        self.assertEqual((self.state_dir / "cognition-worker.log").stat().st_mode & 0o777, 0o600)
