import contextlib
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from zest.cli import main
from zest.files import FileReadError, read_project_file
from zest.git import RepositoryError, git_environment, inspect_repository, managed_ref
from zest.index import RetrievalIndex
from zest.knowledge import KnowledgeStore
from zest.overview import build_overview
from zest.qa import prepare_context
from zest.releases import ReleaseStore
from zest.repairs import RepairStore
from zest.search import search_project
from zest.storage import ProjectRegistry
from zest.verification import VerificationStore
from tests.test_verification import OfflineVerificationEngine


class ReleasesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="zest-release-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_add.py").write_text(
            "import unittest\nfrom app import add\nclass AddTest(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.project = ProjectRegistry(self.state).register(inspect_repository(self.repo))
        self.repairs = RepairStore(self.state)
        self.repair = self.make_repair("def add(a, b):\n    return a + b\n")
        verification_store = VerificationStore(self.state, engine=OfflineVerificationEngine())
        pending = verification_store.create(self.repair["id"], "offline_fixture")
        self.verification = verification_store.decide(pending["id"], approved=True, plan_sha256=pending["plan_sha256"])
        # Positive unit cases isolate ref mechanics using an explicit offline gate.
        # Only the opt-in integration test below exercises the production Docker gate.
        self.gate = patch("zest.releases._validated_verification", return_value=(self.verification, self.repair))
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.store = ReleaseStore(self.state)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=git_environment(),
                              capture_output=True, check=True).stdout

    def make_repair(self, content):
        original = read_project_file(self.project, "app.py")
        pending = self.repairs.create(self.project, {"summary": "fix", "changes": [{
            "path": "app.py", "expected_sha256": original["content_sha256"], "content": content}]})
        return self.repairs.decide(pending["id"], approved=True, plan_sha256=pending["plan_sha256"])

    def prepare(self):
        return self.store.promote(self.verification["id"])

    def approve(self, record):
        return self.store.decide(record["id"], approved=True, plan_sha256=record["plan_sha256"])

    def test_preview_and_initial_active_do_not_mutate_refs(self):
        before = self.git("show-ref")
        record = self.prepare()
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertIsNone(record["plan"]["expected_ref_commit"])
        self.assertFalse(record["plan"]["policy"]["deploy_runtime"])
        self.assertEqual(self.git("show-ref"), before)
        self.assertEqual(ReleaseStore(self.state).show(record["id"]), record)
        self.assertIsNone(self.store.active(self.project["id"])["managed_commit"])

    def test_promotion_changes_read_version_and_pins_both_history_refs(self):
        before_head = self.git("rev-parse", "HEAD")
        before_index = (self.repo / ".git/index").read_bytes()
        before_file = (self.repo / "app.py").read_bytes()
        record = self.approve(self.prepare())
        self.assertEqual(record["status"], "applied")
        self.assertEqual(self.git("rev-parse", "HEAD"), before_head)
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual((self.repo / "app.py").read_bytes(), before_file)
        active = self.store.active(self.project["id"])
        self.assertEqual(active["selected_commit"], self.repair["candidate_commit"])
        self.assertFalse(active["runtime_deployed"])
        self.assertIn("return a + b", read_project_file(self.project, "app.py")["content"])
        self.assertEqual(self.git("rev-parse", self.store._history_ref(record["id"], "before")).strip().decode(),
                         self.project["git"]["head_commit"])
        self.assertEqual(self.git("rev-parse", self.store._history_ref(record["id"], "after")).strip().decode(),
                         self.repair["candidate_commit"])

    def test_read_overview_qa_index_knowledge_and_search_agree(self):
        self.approve(self.prepare())
        commit = self.repair["candidate_commit"]
        self.assertEqual(build_overview(self.project)["source"]["commit"], commit)
        self.assertEqual(prepare_context(self.project, "add", files=["app.py"])["commit"], commit)
        self.assertEqual(search_project(self.project, "a + b")["source"]["commit"], commit)
        index = RetrievalIndex(self.state)
        self.assertEqual(index.build(self.project)["commit"], commit)
        self.assertEqual(index.search(self.project, "add")["metadata"]["commit"], commit)
        note = KnowledgeStore(self.state).add(self.project, "约束", "加法支持正数")
        self.assertEqual(note["observed_commit"], commit)
        original = read_project_file(self.project, "app.py", commit=self.project["git"]["head_commit"])
        self.assertIn("a - b", original["content"])

    def test_rollback_is_approved_and_preserves_uncommitted_work(self):
        applied = self.approve(self.prepare())
        (self.repo / "app.py").write_text("uncommitted user work\n")
        rollback = self.store.rollback(applied["id"])
        self.assertEqual(rollback["status"], "awaiting_approval")
        self.assertIn("a + b", read_project_file(self.project, "app.py")["content"])
        self.assertEqual(self.approve(rollback)["status"], "applied")
        self.assertIn("a - b", read_project_file(self.project, "app.py")["content"])
        self.assertEqual((self.repo / "app.py").read_text(), "uncommitted user work\n")

    def test_offline_failed_and_incomplete_verification_cannot_promote(self):
        self.gate.stop()
        with self.assertRaises(FileReadError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "release_verification_required")
        self.assertIsNone(self.store.active(self.project["id"])["managed_commit"])

    def test_wrong_approval_reject_and_duplicate_are_blocked(self):
        record = self.prepare()
        with self.assertRaises(FileReadError):
            self.store.decide(record["id"], approved=True, plan_sha256="0" * 64)
        self.assertEqual(self.store.decide(record["id"], approved=False)["status"], "rejected")
        with self.assertRaises(FileReadError):
            self.approve(record)
        record = self.approve(self.prepare())
        with self.assertRaises(FileReadError):
            self.approve(record)

    def test_two_previews_cannot_both_update_the_same_version(self):
        first, second = self.prepare(), self.prepare()
        self.assertEqual(self.approve(first)["status"], "applied")
        with self.assertRaises(FileReadError) as caught:
            self.approve(second)
        self.assertEqual(caught.exception.code, "release_base_changed")

    def test_changed_head_or_evidence_blocks_execution(self):
        record = self.prepare()
        (self.repo / "README.md").write_text("concurrent change\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "changed HEAD")
        with self.assertRaises(FileReadError):
            self.approve(record)
        self.git("reset", "--hard", self.project["git"]["head_commit"])
        changed = dict(self.verification, result={"changed": True})
        with patch("zest.releases._validated_verification", return_value=(changed, self.repair)), self.assertRaises(FileReadError):
            self.approve(record)

    def test_symbolic_active_ref_is_rejected_and_main_unchanged(self):
        head = self.git("rev-parse", "HEAD")
        self.git("symbolic-ref", managed_ref(self.project["id"]), "refs/heads/main")
        with self.assertRaises(RepositoryError):
            self.prepare()
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_ref_transaction_failure_has_no_partial_history(self):
        record = self.prepare()
        with patch("zest.releases._git_write", side_effect=FileReadError("fixture", "offline failure")):
            failed = self.approve(record)
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(self.store.active(self.project["id"])["managed_commit"])
        self.assertEqual(self.git("for-each-ref", "refs/zest/releases"), b"")

    def test_candidate_race_is_blocked_by_the_git_transaction(self):
        record = self.prepare()
        from zest.releases import _git_write
        def concurrent_write(*args, **kwargs):
            self.git("update-ref", self.repair["candidate_ref"], self.repair["plan"]["base_commit"])
            return _git_write(*args, **kwargs)
        with patch("zest.releases._git_write", side_effect=concurrent_write):
            result = self.approve(record)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(self.store.active(self.project["id"])["managed_commit"])
        self.assertEqual(self.git("for-each-ref", "refs/zest/releases"), b"")

    @unittest.skipUnless(importlib.util.find_spec("fastmcp"), "MCP extra is not installed")
    def test_real_mcp_reads_the_selected_version(self):
        import asyncio
        from fastmcp import Client
        from zest.mcp_server import create_server
        self.approve(self.prepare())
        async def read():
            async with Client(create_server(self.project["id"], self.state)) as client:
                return await client.call_tool("read_file", {"path": "app.py"})
        result = asyncio.run(read())
        self.assertEqual(result.data["data"]["source"]["commit"], self.repair["candidate_commit"])

    def test_sqlite_crash_after_git_commit_recovers_without_reapplying(self):
        record = self.prepare()
        real_save = self.store._save
        def save(conn, value):
            if value["status"] == "applied":
                raise sqlite3.OperationalError("offline crash fixture")
            real_save(conn, value)
        with patch.object(self.store, "_save", side_effect=save), self.assertRaises(FileReadError):
            self.approve(record)
        self.assertEqual(self.store.show(record["id"])["status"], "pending_execution")
        refs = self.git("show-ref")
        self.assertEqual(ReleaseStore(self.state).recover(record["id"])["status"], "applied")
        self.assertEqual(self.git("show-ref"), refs)

    def test_ambiguous_refs_remain_pending_without_replay(self):
        record = self.prepare()
        record["status"] = "applying"
        with self.store._open() as conn:
            self.store._save(conn, record)
        self.git("update-ref", managed_ref(self.project["id"]), self.repair["candidate_commit"])
        self.assertEqual(self.store.recover(record["id"])["status"], "pending_execution")
        with self.assertRaises(FileReadError):
            self.approve(record)

    def test_subsequent_repair_bases_on_active_version(self):
        self.approve(self.prepare())
        next_repair = self.make_repair("def add(a, b):\n    return sum([a, b])\n")
        self.assertEqual(next_repair["status"], "candidate_ready")
        self.assertEqual(next_repair["plan"]["base_commit"], self.repair["candidate_commit"])
        self.assertEqual(next_repair["plan"]["workspace_head"], self.project["git"]["head_commit"])

    def test_cli_preview_approve_list_and_active(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state), "release", "promote", self.verification["id"]]), 0)
        record = json.loads(output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state), "release", "approve", record["id"],
                                   "--plan-sha256", record["plan_sha256"]]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "applied")
        self.assertEqual(len(self.store.list_operations(self.project["id"])), 1)

    @unittest.skipUnless(os.environ.get("ZEST_RUN_DOCKER_TESTS") == "1", "explicit real Docker integration opt-in")
    def test_real_docker_gate_promotion_and_rollback_end_to_end(self):
        self.gate.stop()
        verification = VerificationStore(self.state)
        pending = verification.create(self.repair["id"], os.environ.get("ZEST_TEST_IMAGE", "python:3.14-slim"))
        result = verification.decide(pending["id"], approved=True, plan_sha256=pending["plan_sha256"])
        self.assertEqual(result["status"], "passed", result["result"])
        promoted = self.approve(self.store.promote(result["id"]))
        self.assertEqual(promoted["status"], "applied")
        self.assertIn("a + b", read_project_file(self.project, "app.py")["content"])
        self.assertEqual(self.approve(self.store.rollback(promoted["id"]))["status"], "applied")
        self.assertIn("a - b", read_project_file(self.project, "app.py")["content"])


if __name__ == "__main__":
    unittest.main()
