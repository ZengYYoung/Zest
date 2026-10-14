import contextlib
import io
import importlib.util
import json
import unittest
from unittest.mock import patch

from tests import test_projects as project_fixtures
from zest.cli import main
from zest.files import FileReadError, read_project_file
from zest.git import inspect_repository
from zest.repairs import RepairStore, anchored_content
from zest.storage import ProjectRegistry


class AnchoredEditsTest(unittest.TestCase):
    setUp = project_fixtures.ProjectsTest.setUp
    git = project_fixtures.ProjectsTest.git
    commit = project_fixtures.ProjectsTest.commit

    def project(self, raw=None):
        self.original = raw if raw is not None else ("# unchanged\n" * 250 + "def add(a, b):\n    return a - b\n# unseen tail\n").encode()
        (self.repo / "app.py").write_bytes(self.original)
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def proposal(self, project, start=251, end=252, old="return a - b", new="return a + b"):
        record = read_project_file(project, "app.py", start_line=start, end_line=end)
        return {"summary": "修复指定行段", "changes": [{"operation": "edit", "path": "app.py",
            "expected_sha256": record["content_sha256"], "start_line": start, "end_line": end,
            "old_text": old, "new_text": new}]}

    def test_unseen_prefix_and_suffix_are_preserved_in_candidate(self):
        project = self.project()
        store = RepairStore(self.state_dir)
        record = store.create(project, self.proposal(project))
        change = record["plan"]["changes"][0]
        self.assertEqual(change["operation"], "edit")
        self.assertEqual(change["content"].encode(), self.original.replace(b"return a - b", b"return a + b"))
        self.assertIn("+    return a + b", change["diff"])
        result = store.decide(record["id"], approved=True, plan_sha256=record["plan_sha256"])
        self.assertEqual(result["status"], "candidate_ready", result)
        self.assertEqual(self.git("show", result["candidate_commit"] + ":app.py"), change["content"].encode())
        self.assertEqual((self.repo / "app.py").read_bytes(), self.original)

    def test_only_occurrence_inside_approved_span_changes(self):
        raw = b"same\nsame\nsame\n"
        self.assertEqual(anchored_content(raw, 2, 2, "same", "changed"), b"same\nchanged\nsame\n".decode())

    def test_crlf_unicode_and_no_final_newline_are_preserved(self):
        raw = "前缀\r\n旧文本\r\n末尾".encode()
        self.assertEqual(anchored_content(raw, 2, 2, "旧文本", "新文本").encode(), "前缀\r\n新文本\r\n末尾".encode())

    def test_missing_duplicate_empty_and_outside_anchors_are_rejected(self):
        for old in ("absent", "", "outside"):
            with self.subTest(old=old), self.assertRaises(FileReadError):
                anchored_content(b"outside\ninside\n", 2, 2, old, "new")
        with self.assertRaises(FileReadError):
            anchored_content(b"duplicate duplicate\n", 1, 1, "duplicate", "new")

    def test_invalid_ranges_and_noop_are_rejected(self):
        for start, end in ((0, 1), (2, 1), (1, 201), (True, 1), (1, 2)):
            with self.subTest(start=start, end=end), self.assertRaises(FileReadError):
                anchored_content(b"only\n", start, end, "only", "new")
        project = self.project()
        with self.assertRaises(FileReadError) as caught:
            RepairStore(self.state_dir).create(project, self.proposal(project, new="return a - b"))
        self.assertEqual(caught.exception.code, "repair_no_change")

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "AI/MCP extras required")
    def test_model_receives_only_range_and_emits_anchored_proposal(self):
        from tests.test_repair_agent import OfflineRepairModel
        from zest.tasks import TaskRunner
        project = self.project()
        model = OfflineRepairModel()
        model.output["changes"] = [{"source_id": "E1", "old_text": "return a - b", "new_text": "return a + b"}]
        runner = TaskRunner(self.state_dir, model=model)
        task = runner.create(project, "修复加法", purpose="repair_proposal", ranges=[
            {"path": "app.py", "start_line": 251, "end_line": 252}])
        self.assertEqual(task["status"], "awaiting_approval", task)
        self.assertEqual(task["context"]["sources"][0]["edit_mode"], "anchor")
        self.assertNotIn("unseen tail", json.dumps(task["context"]))
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("# unchanged", json.dumps(model.calls))
        repair = RepairStore(self.state_dir).show(result["result"]["repair_id"])
        self.assertEqual(repair["status"], "awaiting_approval")
        self.assertEqual(repair["plan"]["changes"][0]["content"].encode(),
                         self.original.replace(b"return a - b", b"return a + b"))

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "AI/MCP extras required")
    def test_model_cannot_replace_full_file_from_a_partial_source(self):
        from tests.test_repair_agent import OfflineRepairModel
        from zest.tasks import TaskRunner
        project = self.project()
        model = OfflineRepairModel()
        runner = TaskRunner(self.state_dir, model=model)
        task = runner.create(project, "修复", purpose="repair_proposal", ranges=[
            {"path": "app.py", "start_line": 251, "end_line": 252}])
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["error"]["code"], "proposal_scope_violation")
        self.assertFalse(RepairStore(self.state_dir).db_path.exists())

    @unittest.skipUnless(importlib.util.find_spec("langgraph"), "AI extra required")
    def test_cli_range_preview_and_large_byte_limit(self):
        from tests.test_repair_agent import OfflineRepairModel
        from zest.tasks import TaskRunner
        project = self.project()
        output = io.StringIO()
        model = OfflineRepairModel()
        with patch("zest.tasks.ModelSettings.from_env", return_value=model.settings), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state_dir), "repair", "suggest", project["id"],
                                   "修复", "--range", "app.py:251:252"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "awaiting_approval")
        (self.repo / "app.py").write_text("#" + "x" * (64 * 1024) + "\ntarget\n")
        self.commit("byte limit")
        result = TaskRunner(self.state_dir, model=model).create(project, "fix", purpose="repair_proposal",
                              ranges=[{"path": "app.py", "start_line": 2, "end_line": 2}])
        self.assertEqual(result["error"]["code"], "repair_content_limit")
        self.assertEqual(model.calls, [])


if __name__ == "__main__":
    unittest.main()
