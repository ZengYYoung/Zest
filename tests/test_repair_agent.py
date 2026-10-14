import contextlib
import io
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_projects as project_fixtures
from zest.cli import main
from zest.files import FileReadError, read_project_file
from zest.git import inspect_repository
from zest.knowledge import KnowledgeStore
from zest.models import DeepSeekModel, ModelSettings, REPAIR_PROMPT
from zest.repair_agent import prepare_repair_context, validate_proposal
from zest.repairs import RepairStore
from zest.storage import ProjectRegistry
from zest.tasks import TaskRunner


class OfflineRepairModel:
    def __init__(self):
        self.settings = ModelSettings("deepseek", "offline-repair", "https://fixture.invalid", "private-fixture")
        self.calls = []
        self.output = {"status": "proposed", "summary": "修复加法", "changes": [
            {"source_id": "E1", "content": "def add(a, b):\n    return a + b\n"}], "unknowns": []}

    def complete(self, payload, *, purpose="qa"):
        self.calls.append({"payload": payload, "purpose": purpose})
        return {"text": json.dumps(self.output), "usage": {"total_tokens": 1}, "reported_model": "offline-fixture"}


@unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "AI/MCP extras required")
class RepairAgentTest(unittest.TestCase):
    setUpBase = project_fixtures.ProjectsTest.setUp
    git = project_fixtures.ProjectsTest.git
    commit = project_fixtures.ProjectsTest.commit

    def setUp(self):
        self.setUpBase()
        (self.repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "README.md").write_text("unselected private documentation\n")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.model = OfflineRepairModel()
        self.runner = TaskRunner(self.state_dir, model=self.model)

    def create(self):
        return self.runner.create(self.project, "加法计算错误", files=["app.py"], purpose="repair_proposal")

    def approve(self, record, runner=None):
        return (runner or self.runner).decide(record["task_id"], approved=True, payload_sha256=record["payload_sha256"])

    def test_prepare_preview_has_only_complete_selected_files_and_no_call(self):
        record = self.create()
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertEqual(record["purpose"], "repair_proposal")
        self.assertEqual(self.model.calls, [])
        self.assertEqual([source["evidence"]["path"] for source in record["context"]["sources"]], ["app.py"])
        self.assertEqual(record["context"]["sources"][0]["content"], (self.repo / "app.py").read_text())
        self.assertFalse(RepairStore(self.state_dir).db_path.exists())

    def test_proposal_restores_across_runner_and_requires_separate_change_approval(self):
        record = self.create()
        result = self.approve(record, TaskRunner(self.state_dir, model=self.model))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0]["purpose"], "repair_proposal")
        self.assertNotIn(str(self.repo), json.dumps(self.model.calls[0]["payload"]))
        self.assertNotIn("private-fixture", json.dumps(self.model.calls))
        repairs = RepairStore(self.state_dir)
        repair = repairs.show(result["result"]["repair_id"])
        self.assertEqual(repair["id"], record["task_id"])
        self.assertEqual(repair["status"], "awaiting_approval")
        self.assertIsNone(repair["candidate_commit"])
        self.assertIn("a - b", (self.repo / "app.py").read_text())
        candidate = repairs.decide(repair["id"], approved=True, plan_sha256=repair["plan_sha256"])
        self.assertEqual(candidate["status"], "candidate_ready")
        self.assertIn("a + b", read_project_file(self.project, "app.py", commit=candidate["candidate_commit"])["content"])
        self.assertEqual(self.git("for-each-ref", "refs/zest/active"), b"")

    def test_wrong_hash_and_rejection_never_call_model(self):
        record = self.create()
        with self.assertRaises(FileReadError):
            self.runner.decide(record["task_id"], approved=True, payload_sha256="wrong")
        self.assertEqual(self.runner.decide(record["task_id"], approved=False)["status"], "rejected")
        self.assertEqual(self.model.calls, [])

    def test_context_budget_and_partial_files_fail_without_call(self):
        (self.repo / "app.py").write_text("print('line')\n" * 201)
        self.commit("large lines")
        result = self.create()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "repair_full_file_required")
        (self.repo / "app.py").write_text("#" + "x" * 15000 + "\n")
        self.commit("large context")
        result = self.create()
        self.assertEqual(result["error"]["code"], "repair_context_limit")
        self.assertEqual(self.model.calls, [])

    def test_changed_version_blocks_model_request(self):
        record = self.create()
        (self.repo / "README.md").write_text("changed\n")
        self.commit("new version")
        result = self.approve(record)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "repair_base_changed")
        self.assertEqual(self.model.calls, [])

    def test_revoked_knowledge_blocks_request(self):
        store = KnowledgeStore(self.state_dir)
        note = store.add(self.project, "范围", "只修复加法")
        store.confirm(note["id"])
        record = self.create()
        store.invalidate(note["id"], "失效")
        self.assertEqual(self.approve(record)["error"]["code"], "knowledge_changed")
        self.assertEqual(self.model.calls, [])

    def test_model_scope_violation_or_secret_never_creates_repair(self):
        for changes in ([{"source_id": "E99", "content": "evil"}],
                        [{"source_id": "E1", "content": "sk-" + "x" * 40}],
                        [{"source_id": "E1", "content": "new", "path": "other.py"}]):
            with self.subTest(changes=changes):
                self.model.output["changes"] = changes
                result = self.approve(self.create())
                self.assertEqual(result["status"], "failed")
                self.assertFalse(RepairStore(self.state_dir).db_path.exists())

    def test_insufficient_evidence_completes_without_actionable_repair(self):
        self.model.output.update(status="insufficient_evidence", changes=[], unknowns=["缺少预期行为"])
        result = self.approve(self.create())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["status"], "insufficient_evidence")
        self.assertFalse(RepairStore(self.state_dir).db_path.exists())

    def test_duplicate_knowledge_and_unknown_inputs_are_not_editable(self):
        context = prepare_repair_context(self.project, "fix", files=["app.py"], state_dir=self.state_dir)
        output = self.model.output.copy()
        output["changes"] = output["changes"] * 2
        with self.assertRaises(FileReadError):
            validate_proposal(json.dumps(output), context)
        output = self.model.output.copy()
        output["unknowns"] = ["未解决缺口"]
        with self.assertRaises(FileReadError):
            validate_proposal(json.dumps(output), context)
        with self.assertRaises(FileReadError):
            prepare_repair_context(self.project, "fix", files=[{}], state_dir=self.state_dir)

    def test_stable_task_id_is_idempotent_without_resetting_approval(self):
        result = self.approve(self.create())
        store = RepairStore(self.state_dir)
        repair = store.show(result["result"]["repair_id"])
        proposal = {"summary": repair["plan"]["summary"], "changes": [
            {key: change[key] for key in ("path", "content", "expected_sha256")}
            for change in repair["plan"]["changes"]]}
        repeat = store.create(self.project, proposal, source_task_id=repair["id"])
        self.assertEqual(repeat, repair)
        store.decide(repair["id"], approved=False)
        self.assertEqual(store.create(self.project, proposal, source_task_id=repair["id"])["status"], "rejected")
        proposal["changes"][0]["content"] = "new proposal\n"
        with self.assertRaises(FileReadError):
            store.create(self.project, proposal, source_task_id=repair["id"])

    def test_cli_suggest_and_saved_target_do_not_need_live_model(self):
        output = io.StringIO()
        with patch("zest.tasks.ModelSettings.from_env", return_value=self.model.settings), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state_dir), "repair", "suggest", self.project["id"],
                                   "加法错误", "--file", "app.py"]), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["status"], "awaiting_approval")
        self.model.settings = ModelSettings("deepseek", "changed-model", "https://fixture.invalid", "private-fixture")
        self.assertEqual(self.approve(record)["error"]["code"], "model_target_changed")
        self.assertEqual(self.model.calls, [])

    def test_real_langchain_repair_prompt_with_offline_http_transport(self):
        import httpx
        captured = []
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
                "model": "offline-report", "choices": [{"index": 0, "message": {"role": "assistant",
                "content": json.dumps(self.model.output)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            provider = DeepSeekModel(self.model.settings, http_client=client)
            runner = TaskRunner(self.state_dir, model=provider)
            record = runner.create(self.project, "fix", files=["app.py"], purpose="repair_proposal")
            result = self.approve(record, runner)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(captured[0]["messages"][0]["content"], REPAIR_PROMPT)
        self.assertEqual(captured[0]["max_tokens"], 4096)
        self.assertEqual(captured[0]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
