import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_projects as project_fixtures
from zest.cli import main
from zest.files import FileReadError, read_project_file
from zest.git import inspect_repository
from zest.repairs import RepairStore
from zest.storage import ProjectRegistry
from zest.verification import VerificationStore


class FileCreationTest(unittest.TestCase):
    setUp = project_fixtures.ProjectsTest.setUp
    git = project_fixtures.ProjectsTest.git
    commit = project_fixtures.ProjectsTest.commit

    def project(self):
        (self.repo / "README.md").write_text("Build an addition service\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    @staticmethod
    def proposal(path="src/app.py", content="print('new')\n"):
        return {"summary": "创建源码", "changes": [{"operation": "create", "path": path,
                 "expected_sha256": None, "content": content}]}

    def test_new_nested_files_and_empty_initializer_preserve_primary(self):
        project = self.project()
        proposal = self.proposal()
        proposal["changes"].append(self.proposal("src/__init__.py", "")["changes"][0])
        store = RepairStore(self.state_dir)
        before_index = (self.repo / ".git/index").read_bytes()
        record = store.create(project, proposal)
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertIn("new file mode 100644", record["plan"]["changes"][1]["diff"])
        result = store.decide(record["id"], approved=True, plan_sha256=record["plan_sha256"])
        self.assertEqual(result["status"], "candidate_ready", result)
        self.assertFalse((self.repo / "src").exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual(read_project_file(project, "src/app.py", commit=result["candidate_commit"])["content"], "print('new')\n")
        self.assertEqual(self.git("show", result["candidate_commit"] + ":src/__init__.py"), b"")

    def test_cannot_overwrite_existing_file_or_directory(self):
        project = self.project()
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text("existing\n")
        self.commit("existing directory")
        for path in ("README.md", "src/app.py", "src"):
            with self.subTest(path=path), self.assertRaises(FileReadError):
                RepairStore(self.state_dir).create(project, self.proposal(path))

    def test_new_path_cannot_descend_through_file_or_link(self):
        project = self.project()
        (self.repo / "blocked.py").write_text("old\n")
        (self.repo / "linked").symlink_to(self.root)
        self.commit("blocking entries")
        for path in ("blocked.py/new.py", "linked/new.py"):
            with self.subTest(path=path), self.assertRaises(FileReadError) as caught:
                RepairStore(self.state_dir).create(project, self.proposal(path))
            self.assertEqual(caught.exception.code, "unsupported_file_type")

    def test_case_duplicate_and_parent_conflicts_are_rejected(self):
        project = self.project()
        for paths in (("A.py", "a.py"), ("src/A.py", "SRC/b.py"), ("new.py", "new.py/child.py")):
            proposal = {"summary": "conflict", "changes": [self.proposal(path)["changes"][0] for path in paths]}
            with self.subTest(paths=paths), self.assertRaises(FileReadError):
                RepairStore(self.state_dir).create(project, proposal)
        (self.repo / "Src").mkdir()
        (self.repo / "Src/old.py").write_text("old\n")
        self.commit("case fixture")
        with self.assertRaises(FileReadError) as caught:
            RepairStore(self.state_dir).create(project, self.proposal("src/new.py"))
        self.assertEqual(caught.exception.code, "repair_path_collision")

    def test_traversal_private_paths_and_nonnull_creation_hash_are_rejected(self):
        project = self.project()
        for path in ("../out.py", ".git/hooks/prepare.py", ".env", "credentials.json", "image.png"):
            with self.subTest(path=path), self.assertRaises(FileReadError):
                RepairStore(self.state_dir).create(project, self.proposal(path))
        proposal = self.proposal()
        proposal["changes"][0]["expected_sha256"] = "0" * 64
        with self.assertRaises(FileReadError):
            RepairStore(self.state_dir).create(project, proposal)
        proposal = self.proposal()
        del proposal["changes"][0]["operation"]
        with self.assertRaises(FileReadError):
            RepairStore(self.state_dir).create(project, proposal)

    def test_source_change_before_approval_blocks_creation(self):
        project = self.project()
        store = RepairStore(self.state_dir)
        record = store.create(project, self.proposal())
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text("user work\n")
        self.commit("user source")
        with self.assertRaises(FileReadError):
            store.decide(record["id"], approved=True, plan_sha256=record["plan_sha256"])
        self.assertEqual((self.repo / "src/app.py").read_text(), "user work\n")

    def test_bootstrap_strategy_never_skips_existing_test_files_or_replacements(self):
        from tests.test_verification import OfflineVerificationEngine
        project = self.project()
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_old.py").write_text("# existing baseline tests\n")
        self.commit("existing tests")
        store = RepairStore(self.state_dir)
        repair = store.create(project, self.proposal("app.py"))
        repair = store.decide(repair["id"], approved=True, plan_sha256=repair["plan_sha256"])
        verification = VerificationStore(self.state_dir, engine=OfflineVerificationEngine())
        self.assertEqual(verification.create(repair["id"], "fixture")["plan"]["baseline_strategy"], "run")
        self.git("rm", "tests/test_old.py")
        self.commit("remove tests fixture")
        old = read_project_file(project, "README.md")
        repair = store.create(project, {"summary": "modify", "changes": [{"path": "README.md",
                              "expected_sha256": old["content_sha256"], "content": "changed\n"}]})
        repair = store.decide(repair["id"], approved=True, plan_sha256=repair["plan_sha256"])
        self.assertEqual(verification.create(repair["id"], "fixture")["plan"]["baseline_strategy"], "run")

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "AI/MCP extras required")
    def test_model_only_creates_declared_paths_after_separate_approval(self):
        from tests.test_repair_agent import OfflineRepairModel
        from zest.tasks import TaskRunner
        project = self.project()
        model = OfflineRepairModel()
        model.output["changes"] = [{"source_id": "E1", "content": "def add(a, b):\n    return a + b\n"}]
        runner = TaskRunner(self.state_dir, model=model)
        task = runner.create(project, "创建加法函数", purpose="repair_proposal", new_files=["app.py"])
        self.assertEqual(task["status"], "awaiting_approval", task)
        self.assertEqual(task["context"]["sources"][0]["evidence"]["kind"], "planned_new_file")
        self.assertEqual(model.calls, [])
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "completed", result)
        store = RepairStore(self.state_dir)
        repair = store.show(result["result"]["repair_id"])
        self.assertEqual(repair["status"], "awaiting_approval")
        self.assertEqual(repair["plan"]["changes"][0]["operation"], "create")
        candidate = store.decide(repair["id"], approved=True, plan_sha256=repair["plan_sha256"])
        self.assertEqual(candidate["status"], "candidate_ready")
        self.assertFalse((self.repo / "app.py").exists())

    @unittest.skipUnless(importlib.util.find_spec("langgraph"), "AI extra required")
    def test_cli_new_file_preview_and_existing_path_rejection(self):
        from tests.test_repair_agent import OfflineRepairModel
        project = self.project()
        output = io.StringIO()
        with patch("zest.tasks.ModelSettings.from_env", return_value=OfflineRepairModel().settings), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state_dir), "repair", "suggest", project["id"],
                                   "创建功能", "--new-file", "app.py"]), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["status"], "awaiting_approval")
        output = io.StringIO()
        with patch("zest.tasks.ModelSettings.from_env", return_value=OfflineRepairModel().settings), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state_dir), "repair", "suggest", project["id"],
                                   "创建功能", "--new-file", "README.md"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    @unittest.skipUnless(os.environ.get("ZEST_RUN_DOCKER_TESTS") == "1", "explicit real Docker integration opt-in")
    def test_real_new_app_and_tests_validate_and_promote_without_host_checkout(self):
        from zest.releases import ReleaseStore
        project = self.project()
        proposal = self.proposal("app.py", "def add(a, b):\n    return a + b\n")
        proposal["changes"].append(self.proposal("tests/test_add.py",
            "import unittest\nfrom app import add\nclass AddTest(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")["changes"][0])
        store = RepairStore(self.state_dir)
        repair = store.create(project, proposal)
        repair = store.decide(repair["id"], approved=True, plan_sha256=repair["plan_sha256"])
        verification = VerificationStore(self.state_dir)
        pending = verification.create(repair["id"], os.environ.get("ZEST_TEST_IMAGE", "python:3.14-slim"))
        verified = verification.decide(pending["id"], approved=True, plan_sha256=pending["plan_sha256"])
        self.assertEqual(verified["status"], "passed", verified)
        self.assertEqual(verified["result"]["baseline"]["status"], "not_applicable")
        self.assertFalse(verified["result"]["baseline"]["container_started"])
        self.assertEqual(verified["result"]["candidate"]["status"], "passed")
        release_store = ReleaseStore(self.state_dir)
        pending_release = release_store.promote(verified["id"])
        applied = release_store.decide(pending_release["id"], approved=True, plan_sha256=pending_release["plan_sha256"])
        self.assertEqual(applied["status"], "applied")
        self.assertIn("a + b", read_project_file(project, "app.py")["content"])
        self.assertFalse((self.repo / "app.py").exists())


if __name__ == "__main__":
    unittest.main()
