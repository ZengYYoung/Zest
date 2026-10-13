import contextlib
import hashlib
import io
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
from zest.git import git_environment, inspect_repository
from zest.repairs import RepairStore, load_proposal
from zest.storage import ProjectRegistry


class RepairsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="zest-repair-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Zest Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "README.md").write_text("Addition project\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.project = ProjectRegistry(self.state).register(inspect_repository(self.repo))
        self.store = RepairStore(self.state)
        self.proposal = {"summary": "修复加法", "changes": [{"path": "app.py",
            "expected_sha256": hashlib.sha256((self.repo / "app.py").read_bytes()).hexdigest(),
            "content": "def add(a, b):\n    return a + b\n"}]}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=git_environment(),
                              capture_output=True, check=True).stdout

    def approve(self, record):
        return self.store.decide(record["id"], approved=True, plan_sha256=record["plan_sha256"])

    def test_proposal_only_prepares_immutable_preview(self):
        before = self.git("show-ref")
        record = self.store.create(self.project, self.proposal)
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertIsNone(record["candidate_commit"])
        self.assertIn("+    return a + b", record["plan"]["changes"][0]["diff"])
        self.assertEqual(self.git("show-ref"), before)
        self.assertEqual(RepairStore(self.state).show(record["id"]), record)
        self.assertNotIn("plan", self.store.list_repairs(self.project["id"])[0])

    def test_candidate_contains_exact_changes_and_preserves_primary(self):
        record = self.store.create(self.project, self.proposal)
        before_head = self.git("rev-parse", "HEAD")
        before_index = (self.repo / ".git" / "index").read_bytes()
        before_content = (self.repo / "app.py").read_bytes()
        result = self.approve(record)
        self.assertEqual(result["status"], "candidate_ready")
        candidate = result["candidate_commit"]
        self.assertEqual(self.git("rev-parse", "HEAD"), before_head)
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), before_index)
        self.assertEqual((self.repo / "app.py").read_bytes(), before_content)
        self.assertFalse(inspect_repository(self.repo).dirty)
        self.assertEqual(self.git("show", f"{candidate}:app.py").decode(), self.proposal["changes"][0]["content"])
        self.assertEqual(self.git("show", f"{candidate}:README.md"), b"Addition project\n")
        self.assertEqual(self.git("rev-parse", f"{candidate}^"), before_head)
        self.assertEqual(self.git("rev-parse", result["candidate_ref"]).strip().decode(), candidate)
        self.assertIn("approved", [event["step"] for event in result["events"]])
        self.assertEqual(read_project_file(self.project, "app.py", commit=candidate)["content"],
                         self.proposal["changes"][0]["content"])

    def test_wrong_hash_and_rejection_make_no_candidate(self):
        record = self.store.create(self.project, self.proposal)
        with self.assertRaises(FileReadError) as caught:
            self.store.decide(record["id"], approved=True, plan_sha256="0" * 64)
        self.assertEqual(caught.exception.code, "repair_approval_mismatch")
        rejected = self.store.decide(record["id"], approved=False)
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(FileReadError):
            self.approve(record)
        self.assertEqual(self.git("for-each-ref", "refs/zest/candidates"), b"")

    def test_completed_cannot_approve_twice(self):
        record = self.store.create(self.project, self.proposal)
        self.approve(record)
        with self.assertRaises(FileReadError) as caught:
            self.approve(record)
        self.assertEqual(caught.exception.code, "repair_not_pending")

    def test_base_commit_and_dirty_state_changes_block_execution(self):
        record = self.store.create(self.project, self.proposal)
        (self.repo / "README.md").write_text("changed\n")
        with self.assertRaises(FileReadError):
            self.approve(record)
        self.git("add", "README.md")
        self.git("commit", "-m", "other change")
        with self.assertRaises(FileReadError) as caught:
            self.approve(record)
        self.assertEqual(caught.exception.code, "repair_base_changed")
        self.assertEqual(self.git("for-each-ref", "refs/zest/candidates"), b"")

    def test_source_hash_sensitive_and_duplicate_inputs_rejected(self):
        invalid = json.loads(json.dumps(self.proposal))
        invalid["changes"][0]["expected_sha256"] = "0" * 64
        with self.assertRaises(FileReadError):
            self.store.create(self.project, invalid)
        invalid = json.loads(json.dumps(self.proposal))
        invalid["changes"] *= 2
        with self.assertRaises(FileReadError):
            self.store.create(self.project, invalid)
        invalid = json.loads(json.dumps(self.proposal))
        invalid["changes"][0]["content"] = "sk-" + "x" * 40
        with self.assertRaises(FileReadError):
            self.store.create(self.project, invalid)
        self.assertFalse(self.store.db_path.exists())

    def test_traversal_secret_and_symlink_paths_rejected(self):
        for path in ("../outside.py", "/tmp/out.py", ".env", "missing.py"):
            proposal = json.loads(json.dumps(self.proposal))
            proposal["changes"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(FileReadError):
                self.store.create(self.project, proposal)
        (self.repo / "link.py").symlink_to("app.py")
        self.git("add", "link.py")
        self.git("commit", "-m", "link")
        proposal = json.loads(json.dumps(self.proposal))
        proposal["changes"][0]["path"] = "link.py"
        with self.assertRaises(FileReadError):
            self.store.create(self.project, proposal)

    def test_filters_and_hooks_not_executed(self):
        marker = self.root / "executed"
        hook = self.root / "hook.sh"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o700)
        (self.repo / ".gitattributes").write_text("app.py filter=unsafe\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "attributes")
        self.git("config", "filter.unsafe.clean", str(hook))
        self.git("config", "filter.unsafe.smudge", str(hook))
        self.git("config", "core.hooksPath", str(self.root))
        (self.root / "post-commit").symlink_to(hook)
        self.approve(self.store.create(self.project, self.proposal))
        self.assertFalse(marker.exists())

    def test_corrupt_plan_blocks_approval(self):
        record = self.store.create(self.project, self.proposal)
        with contextlib.closing(sqlite3.connect(self.store.db_path)) as conn, conn:
            conn.execute("UPDATE repairs SET plan=? WHERE id=?", ('{"tampered":true}', record["id"]))
        with self.assertRaises(FileReadError) as caught:
            self.approve(record)
        self.assertEqual(caught.exception.code, "repair_record_corrupt")

    def test_mid_execution_failure_records_failure_and_no_switch(self):
        record = self.store.create(self.project, self.proposal)
        before = self.git("rev-parse", "HEAD")
        with patch("zest.repairs._git_write", side_effect=FileReadError("fixture_failure", "fixture")):
            result = self.approve(record)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "fixture_failure")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_unknown_execution_state_does_not_replay(self):
        record = self.store.create(self.project, self.proposal)
        with contextlib.closing(sqlite3.connect(self.store.db_path)) as conn, conn:
            conn.execute("UPDATE repairs SET status='applying' WHERE id=?", (record["id"],))
        self.assertEqual(self.store.show(record["id"])["status"], "pending_execution")
        with self.assertRaises(FileReadError):
            self.approve(record)

    def test_lock_prevents_parallel_apply(self):
        record = self.store.create(self.project, self.proposal)
        with self.store._lock(record["id"]), self.assertRaises(FileReadError) as caught:
            self.approve(record)
        self.assertEqual(caught.exception.code, "repair_busy")

    def test_bounded_json_cli_roundtrip(self):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps(self.proposal))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state), "repair", "create", self.project["id"],
                                   "--plan", str(plan)]), 0)
        record = json.loads(output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state), "repair", "approve", record["id"],
                                   "--plan-sha256", record["plan_sha256"]]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "candidate_ready")
        plan.write_bytes(b" " * (321 * 1024))
        with self.assertRaises(FileReadError):
            load_proposal(plan)

    def test_content_budget_and_noop_rejected(self):
        proposal = json.loads(json.dumps(self.proposal))
        proposal["changes"][0]["content"] = "x" * (64 * 1024 + 1)
        with self.assertRaises(FileReadError) as caught:
            self.store.create(self.project, proposal)
        self.assertEqual(caught.exception.code, "repair_content_limit")
        proposal["changes"][0]["content"] = (self.repo / "app.py").read_text()
        with self.assertRaises(FileReadError) as caught:
            self.store.create(self.project, proposal)
        self.assertEqual(caught.exception.code, "repair_no_change")

    def test_executable_mode_and_no_newline_diff_are_preserved(self):
        (self.repo / "app.py").write_text("old")
        (self.repo / "app.py").chmod(0o755)
        self.git("add", "app.py")
        self.git("commit", "-m", "executable fixture")
        self.proposal["changes"][0].update(expected_sha256=hashlib.sha256(b"old").hexdigest(), content="new")
        record = self.store.create(self.project, self.proposal)
        self.assertIn("\\ No newline at end of file", record["plan"]["changes"][0]["diff"])
        result = self.approve(record)
        self.assertTrue(self.git("ls-tree", result["candidate_commit"], "app.py").startswith(b"100755"))


if __name__ == "__main__":
    unittest.main()
