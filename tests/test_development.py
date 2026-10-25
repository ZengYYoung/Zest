import contextlib
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
from zest.development import create_project
from zest.files import FileReadError, read_project_file
from zest.git import RepositoryError, inspect_repository
from zest.knowledge import KnowledgeStore
from zest.storage import ProjectRegistry, StorageError


class DevelopmentTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="zest-development-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / "新项目 app"
        self.state = self.root / "state"

    def create(self, **kwargs):
        return create_project(self.target, "测试项目", "提供故障协作功能", self.state,
                              author_name="Zest Test", author_email="test@example.invalid", **kwargs)

    def test_baseline_and_shared_metadata_waiting_for_review(self):
        result = self.create()
        project = result["project"]
        self.assertEqual(project["mode"], "development")
        self.assertEqual(project["git"]["branch"], "main")
        self.assertTrue(project["git"]["dirty"])
        self.assertIsNotNone(project["git"]["head_commit"])
        read = read_project_file(project, "README.md")
        self.assertIn("提供故障协作功能", read["content"])
        self.assertIn("业务功能尚未实现", read["content"])
        self.assertEqual(set(result["created_files"]), {"README.md", ".gitignore"})
        self.assertFalse((self.target / "pyproject.toml").exists())

    def test_goal_is_draft_and_not_context_until_confirmed(self):
        result = self.create()
        note = result["goal_note"]
        store = KnowledgeStore(self.state)
        self.assertEqual((note["status"], note["scope"]), ("draft", "project"))
        self.assertEqual(store.for_context(result["project"]["id"], "f" * 40), [])
        store.confirm(note["id"])
        self.assertEqual(len(store.for_context(result["project"]["id"], "f" * 40)), 1)

    def test_registration_refresh_preserves_development_mode_and_id(self):
        first = self.create()["project"]
        later = ProjectRegistry(self.state).register(inspect_repository(self.target))
        self.assertEqual((first["id"], "development"), (later["id"], later["mode"]))

    def test_existing_directory_file_and_symlink_are_untouched(self):
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    self.target.mkdir()
                elif kind == "file":
                    self.target.write_text("preserve")
                else:
                    self.target.symlink_to(self.root / "missing")
                with self.assertRaises(FileReadError) as caught:
                    self.create()
                self.assertEqual(caught.exception.code, "project_path_exists")
                self.assertFalse(self.state.exists())
                if kind == "directory":
                    self.assertEqual(list(self.target.iterdir()), [])
                    self.target.rmdir()
                else:
                    if kind == "file":
                        self.assertEqual(self.target.read_text(), "preserve")
                    self.target.unlink()

    def test_parent_must_exist(self):
        self.target = self.root / "missing" / "new"
        with self.assertRaises(FileReadError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "invalid_project_parent")
        self.assertFalse(self.target.parent.exists())

    def test_invalid_text_and_incomplete_author_do_not_create(self):
        for kwargs in ({"name": "bad\nname", "goal": "goal"}, {"name": "app", "goal": ""}):
            with self.assertRaises(FileReadError):
                create_project(self.target, state_dir=self.state, author_name="Test",
                               author_email="test@example.invalid", **kwargs)
        with self.assertRaises(FileReadError):
            create_project(self.target, "app", "goal", self.state, author_name="Only Name")
        self.assertFalse(self.target.exists())

    def test_known_credential_goal_is_not_written(self):
        secret_fixture = "sk-" + "x" * 40
        with self.assertRaises(FileReadError):
            create_project(self.target, "app", secret_fixture, self.state,
                           author_name="Test", author_email="test@example.invalid")
        self.assertFalse(self.target.exists())

    def test_git_failure_preserves_directory_without_registration(self):
        with patch("zest.development._git_create", side_effect=RepositoryError("fixture")):
            with self.assertRaises(FileReadError) as caught:
                self.create()
        self.assertEqual(caught.exception.code, "project_creation_incomplete")
        self.assertTrue((self.target / "README.md").exists())
        self.assertFalse(self.state.exists())

    def test_global_hook_and_template_are_not_executed_or_copied(self):
        hook_dir = self.root / "hooks"
        hook_dir.mkdir()
        marker = self.root / "hook-ran"
        hook = hook_dir / "pre-commit"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        hook.chmod(0o700)
        template = self.root / "template"
        template.mkdir()
        (template / "unexpected").write_text("do not copy")
        global_config = self.root / "global.gitconfig"
        for key, value in (("core.hooksPath", str(hook_dir)), ("init.templateDir", str(template)),
                           ("commit.gpgSign", "true")):
            subprocess.run(["git", "config", "--file", str(global_config), key, value], check=True)
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}):
            self.create()
        self.assertFalse(marker.exists())
        self.assertFalse((self.target / ".git" / "unexpected").exists())

    def test_cli_init_and_import_share_project_id(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--state-dir", str(self.state), "project", "init", str(self.target),
                         "--name", "app", "--goal", "goal", "--author-name", "Test",
                         "--author-email", "test@example.invalid"])
        self.assertEqual(code, 0)
        project = json.loads(output.getvalue())["project"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state), "project", "import", str(self.target)]), 0)
        self.assertEqual(json.loads(output.getvalue())["id"], project["id"])
        self.assertEqual(json.loads(output.getvalue())["mode"], "development")

    def test_repeated_init_reuses_management_without_author_goal_or_git_writes(self):
        initial = self.create()
        note = initial["goal_note"]
        KnowledgeStore(self.state).confirm(note["id"])
        def snapshot():
            return {str(path.relative_to(self.target)): (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in self.target.rglob("*") if path.is_file()}
        before = snapshot()
        with patch("zest.development._identity", side_effect=AssertionError("must not read author")), \
                patch("zest.development._git_create", side_effect=AssertionError("must not write Git")):
            repeated = create_project(self.target, "Different name", "Different goal", self.state)
        self.assertEqual(repeated["project"], initial["project"])
        self.assertIsNone(repeated["goal_note"])
        self.assertEqual(repeated["created_files"], [])
        self.assertIn("existing_project_reused_goal_unchanged", repeated["warnings"])
        self.assertEqual(before, snapshot())
        notes = KnowledgeStore(self.state).list_notes(initial["project"]["id"])
        self.assertEqual([item["id"] for item in notes], [note["id"]])
        self.assertEqual(notes[0]["status"], "confirmed")

    def test_legacy_database_additive_migration_preserves_record(self):
        project = self.create()["project"]
        db = self.state / "state.sqlite3"
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("ALTER TABLE projects DROP COLUMN mode")
        loaded = ProjectRegistry(self.state).get_project(project["id"])
        self.assertEqual(loaded["id"], project["id"])
        self.assertEqual(loaded["git"], project["git"])
        self.assertEqual(loaded["mode"], "import")
        with contextlib.closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_invalid_mode_is_rejected(self):
        project = self.create()["project"]
        registry = ProjectRegistry(self.state)
        with self.assertRaises(StorageError):
            registry.register(inspect_repository(self.target), mode="unknown")
        with contextlib.closing(sqlite3.connect(registry.db_path)) as conn, conn:
            conn.execute("UPDATE projects SET mode='unknown'")
        with self.assertRaises(StorageError):
            registry.get_project(project["id"])


if __name__ == "__main__":
    unittest.main()
