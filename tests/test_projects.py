import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from zest.cli import main
from zest.git import RepositoryError, git_environment, inspect_repository
from zest.storage import ProjectRegistry, StorageError


class ProjectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zest-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "项目 repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Zest Test")
        self.git("config", "user.email", "test@example.invalid")
        self.state_dir = self.root / "state"

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              env=git_environment(), capture_output=True, check=True).stdout

    def commit(self, text="initial"):
        self.git("add", ".")
        self.git("commit", "-m", text)
        return self.git("rev-parse", "HEAD").decode().strip()

    def populated(self):
        (self.repo / "app.py").write_text("print('hello')\n")
        return self.commit()

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--state-dir", str(self.state_dir), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_empty_git_repository_has_unborn_branch(self):
        state = inspect_repository(self.repo)
        self.assertTrue(state.unborn)
        self.assertFalse(state.detached)
        self.assertEqual(state.branch, "main")
        self.assertIsNone(state.head_commit)
        self.assertFalse(state.dirty)

    def test_clean_repository_reports_exact_commit(self):
        commit = self.populated()
        state = inspect_repository(self.repo)
        self.assertEqual(state.head_commit, commit)
        self.assertEqual(state.branch, "main")
        self.assertFalse(state.dirty)
        self.assertFalse(state.unborn)

    def test_detached_head_is_supported(self):
        commit = self.populated()
        self.git("checkout", "--detach", commit)
        state = inspect_repository(self.repo)
        self.assertTrue(state.detached)
        self.assertIsNone(state.branch)
        self.assertEqual(state.head_commit, commit)

    def test_staged_unstaged_and_untracked_are_distinct(self):
        self.populated()
        (self.repo / "app.py").write_text("staged\n")
        self.git("add", "app.py")
        (self.repo / "app.py").write_text("unstaged\n")
        (self.repo / "new.txt").write_text("new\n")
        state = inspect_repository(self.repo)
        self.assertTrue(state.dirty)
        self.assertEqual((state.staged_changes, state.unstaged_changes, state.untracked_entries), (1, 1, 1))

    def test_ignored_secrets_do_not_enter_record(self):
        (self.repo / ".gitignore").write_text(".env\n")
        self.populated()
        (self.repo / ".env").write_text("API_KEY=fixture-secret\n")
        state = inspect_repository(self.repo)
        self.assertFalse(state.dirty)
        result = ProjectRegistry(self.state_dir).register(state)
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_rename_with_newline_in_filename_is_one_change(self):
        self.populated()
        self.git("mv", "app.py", "new\nname.py")
        state = inspect_repository(self.repo)
        self.assertEqual((state.staged_changes, state.unstaged_changes, state.untracked_entries), (1, 0, 0))

    def test_nonexistent_and_non_git_directories_are_rejected(self):
        for path in (self.root / "missing", self.root):
            with self.subTest(path=path), self.assertRaises(RepositoryError):
                inspect_repository(path)

    def test_file_and_repository_subdirectory_are_rejected(self):
        self.populated()
        subdir = self.repo / "src"
        subdir.mkdir()
        for path in (self.repo / "app.py", subdir):
            with self.subTest(path=path), self.assertRaises(RepositoryError):
                inspect_repository(path)

    def test_bare_repository_is_rejected(self):
        bare = self.root / "bare.git"
        subprocess.run(["git", "init", "--bare", str(bare)], env=git_environment(), capture_output=True, check=True)
        with self.assertRaises(RepositoryError):
            inspect_repository(bare)

    def test_symlink_uses_canonical_project_identity(self):
        link = self.root / "alias"
        link.symlink_to(self.repo, target_is_directory=True)
        registry = ProjectRegistry(self.state_dir)
        first = registry.register(inspect_repository(self.repo))
        second = registry.register(inspect_repository(link))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(registry.list_projects()), 1)

    def test_inherited_git_overrides_cannot_redirect_inspection(self):
        self.populated()
        with patch.dict(os.environ, {"GIT_DIR": "/not-the-project", "GIT_WORK_TREE": "/missing"}):
            state = inspect_repository(self.repo)
        self.assertEqual(state.root_path, str(self.repo.resolve()))

    def test_fsmonitor_hook_is_not_executed(self):
        self.populated()
        marker = self.root / "hook-ran"
        hook = self.root / "monitor.sh"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        inspect_repository(self.repo)
        self.assertFalse(marker.exists())

    def test_inspection_does_not_modify_worktree_or_index(self):
        self.populated()
        (self.repo / "app.py").write_text("changed\n")
        index = self.repo / ".git" / "index"
        before_index, before_file = index.read_bytes(), (self.repo / "app.py").read_bytes()
        inspect_repository(self.repo)
        self.assertEqual(index.read_bytes(), before_index)
        self.assertEqual((self.repo / "app.py").read_bytes(), before_file)
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

    def test_linked_git_worktree_is_supported(self):
        self.populated()
        worktree = self.root / "linked"
        self.git("worktree", "add", "-b", "feature", str(worktree))
        state = inspect_repository(worktree)
        self.assertEqual(state.branch, "feature")
        self.assertEqual(state.root_path, str(worktree.resolve()))

    def test_duplicate_registration_updates_without_changing_id(self):
        self.populated()
        registry = ProjectRegistry(self.state_dir)
        first = registry.register(inspect_repository(self.repo))
        (self.repo / "app.py").write_text("modified\n")
        second = registry.register(inspect_repository(self.repo))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertTrue(second["git"]["dirty"])
        self.assertEqual(len(registry.list_projects()), 1)

    def test_records_persist_across_registry_instances(self):
        first = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        loaded = ProjectRegistry(self.state_dir).get_project(first["id"])
        self.assertEqual(first, loaded)
        if os.name == "posix":
            self.assertEqual((self.state_dir / "state.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_listing_uninitialized_state_does_not_create_files(self):
        self.assertEqual(ProjectRegistry(self.state_dir).list_projects(), [])
        self.assertFalse(self.state_dir.exists())

    def test_show_unknown_project_returns_none(self):
        self.assertIsNone(ProjectRegistry(self.state_dir).get_project("unknown"))

    def test_invalid_import_has_nonzero_exit_and_no_state(self):
        code, stdout, stderr = self.cli("project", "import", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "repository_error")
        self.assertFalse(self.state_dir.exists())

    def test_cli_import_list_show_round_trip(self):
        self.populated()
        code, stdout, stderr = self.cli("project", "import", str(self.repo))
        self.assertEqual((code, stderr), (0, ""))
        record = json.loads(stdout)
        code, stdout, stderr = self.cli("project", "show", record["id"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), record)
        code, stdout, stderr = self.cli("project", "list")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), [record])

    def test_cli_unknown_project_has_error_on_stderr(self):
        code, stdout, stderr = self.cli("project", "show", "unknown")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "project_not_found")

    def test_cli_process_can_read_persisted_state(self):
        self.cli("project", "import", str(self.repo))
        result = subprocess.run([sys.executable, "-m", "zest", "--state-dir", str(self.state_dir),
                                 "project", "list"], capture_output=True, check=True)
        self.assertEqual(len(json.loads(result.stdout)), 1)

    def test_corrupted_database_is_reported_without_traceback(self):
        self.state_dir.mkdir()
        (self.state_dir / "state.sqlite3").write_bytes(b"not a database")
        code, stdout, stderr = self.cli("project", "list")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "state_error")
        self.assertNotIn("Traceback", stderr)

    def test_newer_state_schema_is_rejected(self):
        registry = ProjectRegistry(self.state_dir)
        registry.register(inspect_repository(self.repo))
        with contextlib.closing(sqlite3.connect(registry.db_path)) as conn, conn:
            conn.execute("PRAGMA user_version = 2")
        with self.assertRaises(StorageError):
            registry.list_projects()

    def test_head_change_during_inspection_is_rejected(self):
        with patch("zest.git._head", side_effect=[("a" * 40, "main"), ("b" * 40, "main")]):
            with self.assertRaisesRegex(RepositoryError, "HEAD"):
                inspect_repository(self.repo)


if __name__ == "__main__":
    unittest.main()
