import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from zest.files import FileReadError
from zest.git import git_environment, inspect_repository
from zest.repairs import RepairStore
from zest.storage import ProjectRegistry
from zest.verification import DockerEngine, VerificationStore, collect_snapshot, _export


class OfflineVerificationEngine:
    """Explicit offline fixture: no project execution or Docker daemon."""
    backend = "offline_fixture"
    socket = Path("/offline-fixture.sock")

    def __init__(self):
        self.calls = []
        self.outcome = {"status": "passed", "exit_code": 0, "tests_run": 1, "oom_killed": False,
                        "output": "offline fixture", "output_truncated": False, "cleanup_ok": True,
                        "policy_verified": True}

    def resolve_image(self, image):
        return "sha256:" + "a" * 64

    def run(self, plan, snapshot, run_id):
        self.calls.append({"plan": plan, "snapshot": snapshot, "files": sorted(
            str(path.relative_to(snapshot)) for path in snapshot.rglob("*") if path.is_file())})
        return dict(self.outcome)


class VerificationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="zest-verification-")
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
        self.commit("base")
        self.project = ProjectRegistry(self.state).register(inspect_repository(self.repo))
        self.repair_store = RepairStore(self.state)
        self.repair = self.make_repair()
        self.engine = OfflineVerificationEngine()
        self.store = VerificationStore(self.state, engine=self.engine)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=git_environment(),
                              capture_output=True, check=True).stdout

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def make_repair(self):
        record = self.repair_store.create(self.project, {"summary": "fix", "changes": [{
            "path": "app.py", "expected_sha256": hashlib.sha256((self.repo / "app.py").read_bytes()).hexdigest(),
            "content": "def add(a, b):\n    return a + b\n"}]})
        return self.repair_store.decide(record["id"], approved=True, plan_sha256=record["plan_sha256"])

    def prepare(self):
        return self.store.create(self.repair["id"], "offline-fixture", timeout=10)

    def approve(self, record):
        return self.store.decide(record["id"], approved=True, plan_sha256=record["plan_sha256"])

    def test_list_does_not_change_old_plan_or_execute_engine(self):
        record = self.prepare()
        before = self.store.show(record["id"])
        with patch("zest.verification.DockerEngine", side_effect=AssertionError("no Docker")):
            listed = VerificationStore(self.state).list_verifications(self.project["id"])
        self.assertEqual([item["id"] for item in listed], [record["id"]])
        self.assertEqual(listed[0]["project_id"], self.project["id"])
        self.assertNotIn("plan", listed[0])
        self.assertNotIn("result", listed[0])
        self.assertEqual(self.store.show(record["id"]), before)
        self.assertNotIn("project_id", before["plan"])
        self.assertEqual(self.engine.calls, [])

    def test_list_absent_database_does_not_create_state(self):
        missing = self.root / "missing-state"
        self.assertEqual(VerificationStore(missing).list_verifications(self.project["id"]), [])
        self.assertFalse(missing.exists())

    def test_list_filters_identity_and_preserves_pending_execution(self):
        from uuid import uuid4
        record = self.prepare()
        with self.store._open() as conn:
            record["status"] = "running"
            self.store._save(conn, record)
        self.assertEqual(self.store.list_verifications(str(uuid4())), [])
        self.assertEqual(self.store.list_verifications(self.project["id"])[0]["status"], "pending_execution")
        record["plan_sha256"] = "changed"
        with self.store._open() as conn:
            self.store._save(conn, record)
        with self.assertRaises(FileReadError) as raised:
            self.store.list_verifications(self.project["id"])
        self.assertEqual(raised.exception.code, "verification_record_corrupt")

    def test_list_cli_reads_project_without_execution(self):
        import io
        from zest.cli import main
        record = self.prepare()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("zest.verification.DockerEngine", side_effect=AssertionError("no Docker")):
            self.assertEqual(main(["--state-dir", str(self.state), "verification", "list", self.project["id"]]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["id"], record["id"])

    def test_prepare_does_not_execute_and_binds_complete_manifest(self):
        record = self.prepare()
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertEqual(record["plan"]["backend"], "offline_fixture")
        self.assertEqual(record["plan"]["candidate_commit"], self.repair["candidate_commit"])
        self.assertEqual({item["path"] for item in record["plan"]["candidate_manifest"]},
                         {"app.py", "tests/test_add.py"})
        self.assertEqual(VerificationStore(self.state).show(record["id"]), record)

    def test_exact_base_and_candidate_export_preserves_primary(self):
        before = (self.repo / "app.py").read_bytes()
        record = self.prepare()
        result = self.approve(record)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.engine.calls), 2)
        base, candidate = self.engine.calls
        self.assertIn("a - b", (base["snapshot"] / "app.py").read_text())
        self.assertIn("a + b", (candidate["snapshot"] / "app.py").read_text())
        self.assertFalse((candidate["snapshot"] / ".git").exists())
        self.assertEqual((self.repo / "app.py").read_bytes(), before)
        self.assertFalse(inspect_repository(self.repo).dirty)
        with self.assertRaises(FileReadError):
            self.approve(record)

    def test_wrong_approval_and_rejection_do_not_run(self):
        record = self.prepare()
        with self.assertRaises(FileReadError):
            self.store.decide(record["id"], approved=True, plan_sha256="0" * 64)
        self.assertEqual(self.store.decide(record["id"], approved=False)["status"], "rejected")
        self.assertEqual(self.engine.calls, [])

    def test_failed_empty_oom_and_cleanup_results_never_pass(self):
        for changes in ({"status": "failed", "exit_code": 1}, {"tests_run": 0},
                        {"oom_killed": True}, {"cleanup_ok": False}, {"output_truncated": True}):
            with self.subTest(changes=changes):
                engine = OfflineVerificationEngine()
                engine.outcome.update(changes)
                self.store = VerificationStore(self.state, engine=engine)
                self.assertEqual(self.approve(self.prepare())["status"], "failed")

    def test_revoked_candidate_ref_blocks_execution(self):
        record = self.prepare()
        self.git("update-ref", self.repair["candidate_ref"], self.repair["plan"]["base_commit"])
        with self.assertRaises(FileReadError) as caught:
            self.approve(record)
        self.assertEqual(caught.exception.code, "candidate_changed")
        self.assertEqual(self.engine.calls, [])

    def test_snapshot_change_after_execution_is_detected(self):
        record = self.prepare()
        def mutate(plan, snapshot, run_id):
            (snapshot / "app.py").write_text("changed\n")
            return self.engine.outcome
        with patch.object(self.engine, "run", side_effect=mutate):
            result = self.approve(record)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["result"]["error"]["code"], "snapshot_changed")

    def test_extra_snapshot_file_blocks_success(self):
        def mutate(plan, snapshot, run_id):
            (snapshot / "extra.py").write_text("unexpected\n")
            return self.engine.outcome
        with patch.object(self.engine, "run", side_effect=mutate):
            result = self.approve(self.prepare())
        self.assertEqual(result["status"], "error")

    def test_container_policy_mismatch_does_not_start(self):
        engine = object.__new__(DockerEngine)
        engine.socket = Path("/fixture-local.sock")
        calls = []
        def control(*args, **kwargs):
            calls.append(args)
            if args[0] == "create":
                return b"b" * 64
            if args[0] == "inspect":
                return b"{}"
            return b""
        with patch.object(engine, "resolve_image", return_value="sha256:" + "a" * 64), \
                patch.object(engine, "_control", side_effect=control):
            result = engine.run({"image_id": "sha256:" + "a" * 64, "timeout_seconds": 1},
                                self.root / "snapshot", "fixture-run")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "container_policy_mismatch")
        self.assertNotIn("start", [args[0] for args in calls])
        self.assertTrue(result["cleanup_ok"])

    def test_secret_binary_or_linked_tracked_files_block_whole_snapshot(self):
        for filename, content in ((".env", "private"), ("image.png", "binary fixture"),
                                  ("secret.txt", "sk-" + "x" * 40)):
            (self.repo / filename).write_text(content)
            self.commit("unsupported snapshot")
            with self.subTest(filename=filename), self.assertRaises(FileReadError):
                collect_snapshot(self.repo, inspect_repository(self.repo).head_commit)
            self.git("rm", filename)
            self.commit("remove fixture")
        (self.repo / "link.py").symlink_to("app.py")
        self.commit("link fixture")
        with self.assertRaises(FileReadError):
            collect_snapshot(self.repo, inspect_repository(self.repo).head_commit)

    def test_snapshot_budget_is_enforced(self):
        with patch("zest.verification.MAX_FILES", 1), self.assertRaises(FileReadError) as caught:
            collect_snapshot(self.repo, self.repair["candidate_commit"])
        self.assertEqual(caught.exception.code, "snapshot_limit")

    def test_socket_must_be_local_and_existing(self):
        for path in (self.root / "absent.sock", self.repo / "app.py", "tcp://remote.example:2375"):
            with self.subTest(path=path), self.assertRaises(FileReadError):
                DockerEngine(path)

    def test_unknown_running_state_does_not_replay(self):
        record = self.prepare()
        record["status"] = "running"
        with self.store._open() as conn:
            self.store._save(conn, record)
        self.assertEqual(self.store.show(record["id"])["status"], "pending_execution")
        with self.assertRaises(FileReadError):
            self.approve(record)

    def test_invalid_timeout_and_unapproved_repair_rejected(self):
        for timeout in (0, 121, True):
            with self.assertRaises(FileReadError):
                self.store.create(self.repair["id"], "fixture", timeout=timeout)
        with self.repair_store._open() as conn, conn:
            conn.execute("UPDATE repairs SET status='awaiting_approval' WHERE id=?", (self.repair["id"],))
        with self.assertRaises(FileReadError):
            self.prepare()

    def test_corrupt_record_is_rejected(self):
        record = self.prepare()
        with contextlib.closing(sqlite3.connect(self.store.db_path)) as conn, conn:
            conn.execute("UPDATE verifications SET record=? WHERE id=?", ('{}', record["id"]))
        with self.assertRaises(FileReadError):
            self.store.show(record["id"])

    @unittest.skipUnless(os.environ.get("ZEST_RUN_DOCKER_TESTS") == "1", "explicit real Docker integration opt-in")
    def test_real_docker_baseline_fails_candidate_passes_and_limits_hold(self):
        (self.repo / "tests/test_limits.py").write_text(
            "import os, socket, unittest\nclass LimitsTest(unittest.TestCase):\n"
            "    def test_non_root(self):\n        self.assertEqual(os.getuid(), 65534)\n"
            "    def test_snapshot_read_only(self):\n"
            "        with self.assertRaises(OSError):\n            open('/workspace/created.txt', 'w')\n"
            "    def test_no_external_network(self):\n"
            "        with socket.socket() as connection:\n            connection.settimeout(1)\n"
            "            with self.assertRaises(OSError):\n                connection.connect(('1.1.1.1', 443))\n")
        self.commit("container limit fixtures")
        self.repair = self.make_repair()
        self.store = VerificationStore(self.state)
        record = self.store.create(self.repair["id"], os.environ.get("ZEST_TEST_IMAGE", "python:3.14-slim"))
        result = self.approve(record)
        self.assertEqual(result["status"], "passed", result["result"])
        self.assertEqual(result["plan"]["backend"], "docker")
        self.assertEqual(result["result"]["baseline"]["status"], "failed")
        self.assertEqual(result["result"]["candidate"]["tests_run"], 4)
        self.assertTrue(result["result"]["candidate"]["policy_verified"])
        self.assertTrue(result["result"]["candidate"]["cleanup_ok"])

    @unittest.skipUnless(os.environ.get("ZEST_RUN_DOCKER_TESTS") == "1", "explicit real Docker integration opt-in")
    def test_real_docker_timeout_cleans_container(self):
        (self.repo / "tests/test_add.py").write_text(
            "import time, unittest\nclass SlowTest(unittest.TestCase):\n"
            "    def test_slow(self):\n        time.sleep(30)\n")
        self.commit("timeout fixture")
        _, files = collect_snapshot(self.repo, inspect_repository(self.repo).head_commit)
        snapshot = self.root / "timeout-snapshot"
        _export(files, snapshot)
        engine = DockerEngine()
        image = engine.resolve_image(os.environ.get("ZEST_TEST_IMAGE", "python:3.14-slim"))
        result = engine.run({"image_id": image, "timeout_seconds": 1}, snapshot, "timeout-fixture")
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["cleanup_ok"])


if __name__ == "__main__":
    unittest.main()
