import hashlib
import json
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError, MAX_FILE_BYTES, read_project_file
from zest.git import inspect_repository
from zest.storage import ProjectRegistry


class FilesTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def prepare(self, content=b"first\nsecond\nthird\n", path="src/main.py"):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def assert_error(self, code, project, path, **kwargs):
        with self.assertRaises(FileReadError) as result:
            read_project_file(project, path, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_line_range_hashes_and_version_evidence(self):
        raw = b"first\nsecond\nthird\n"
        project = self.prepare(raw)
        result = read_project_file(project, "src/main.py", start_line=2, end_line=3)
        self.assertEqual(result["lines"], [{"number": 2, "text": "second"}, {"number": 3, "text": "third"}])
        self.assertEqual(result["content"], "second\nthird\n")
        self.assertEqual(result["source"]["commit"], project["git"]["head_commit"])
        self.assertEqual(result["content_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["excerpt_sha256"], hashlib.sha256(b"second\nthird\n").hexdigest())
        self.assertEqual(result["evidence"], {
            "path": "src/main.py", "commit": project["git"]["head_commit"],
            "blob_id": self.git("rev-parse", "HEAD:src/main.py").decode().strip(), "start_line": 2, "end_line": 3})
        self.assertEqual((result["more_before"], result["more_after"]), (True, False))

    def test_default_window_is_200_lines_and_can_continue(self):
        project = self.prepare(b"line\n" * 201)
        first = read_project_file(project, "src/main.py")
        self.assertEqual(len(first["lines"]), 200)
        self.assertTrue(first["more_after"])
        last = read_project_file(project, "src/main.py", start_line=201)
        self.assertEqual(last["lines"], [{"number": 201, "text": "line"}])
        self.assertFalse(last["more_after"])

    def test_end_past_eof_is_clamped(self):
        project = self.prepare()
        result = read_project_file(project, "src/main.py", end_line=10)
        self.assertEqual(result["evidence"]["end_line"], 3)

    def test_invalid_line_ranges_are_rejected(self):
        project = self.prepare()
        for kwargs in ({"start_line": 0}, {"start_line": 4}, {"start_line": 2, "end_line": 1}):
            with self.subTest(kwargs=kwargs):
                self.assert_error("invalid_line_range", project, "src/main.py", **kwargs)
        self.assert_error("line_limit", project, "src/main.py", end_line=201)

    def test_empty_file_has_no_fake_line(self):
        project = self.prepare(b"")
        result = read_project_file(project, "src/main.py")
        self.assertEqual((result["total_lines"], result["lines"], result["content"]), (0, [], ""))
        self.assertIsNone(result["evidence"]["start_line"])
        self.assert_error("invalid_line_range", project, "src/main.py", start_line=2)

    def test_crlf_and_missing_final_newline_keep_exact_content(self):
        raw = b"first\r\nsecond\r\nthird"
        project = self.prepare(raw)
        result = read_project_file(project, "src/main.py", start_line=2)
        self.assertEqual(result["lines"], [{"number": 2, "text": "second"}, {"number": 3, "text": "third"}])
        self.assertEqual(result["content"], "second\r\nthird")
        self.assertEqual(result["content_sha256"], hashlib.sha256(raw).hexdigest())

    def test_unicode_separator_is_not_a_physical_line_break(self):
        project = self.prepare("中文\u2028仍在第一行\n第二行\n".encode())
        result = read_project_file(project, "src/main.py")
        self.assertEqual(result["total_lines"], 2)
        self.assertEqual(result["lines"][0]["text"], "中文\u2028仍在第一行")

    def test_blank_line_is_preserved(self):
        project = self.prepare(b"\n")
        result = read_project_file(project, "src/main.py")
        self.assertEqual(result["total_lines"], 1)
        self.assertEqual(result["lines"], [{"number": 1, "text": ""}])

    def test_historical_commit_reads_old_content(self):
        project = self.prepare(b"old\n")
        old = project["git"]["head_commit"]
        (self.repo / "src/main.py").write_text("new\n")
        self.commit("new version")
        historical = read_project_file(project, "src/main.py", commit=old)
        current = read_project_file(project, "src/main.py")
        self.assertEqual(historical["content"], "old\n")
        self.assertEqual(current["content"], "new\n")
        self.assertIn("historical_commit", {w["code"] for w in historical["warnings"]})

    def test_dirty_and_staged_content_are_excluded(self):
        project = self.prepare(b"committed\n")
        (self.repo / "src/main.py").write_text("staged\n")
        self.git("add", "src/main.py")
        (self.repo / "src/main.py").write_text("working\n")
        result = read_project_file(project, "src/main.py")
        self.assertEqual(result["content"], "committed\n")
        self.assertIn("working_tree_excluded", {w["code"] for w in result["warnings"]})

    def test_head_change_during_read_keeps_selected_version(self):
        project = self.prepare(b"old\n")
        from zest.files import list_tree
        changed = False

        def move_head(*args):
            nonlocal changed
            if not changed:
                changed = True
                (self.repo / "src/main.py").write_text("new\n")
                self.commit("concurrent update")
            return list_tree(*args)

        with patch("zest.files.list_tree", side_effect=move_head):
            result = read_project_file(project, "src/main.py")
        self.assertEqual(result["content"], "old\n")
        self.assertEqual(result["evidence"]["commit"], project["git"]["head_commit"])

    def test_revision_expressions_missing_ids_and_blob_ids_are_rejected(self):
        project = self.prepare()
        blob = self.git("rev-parse", "HEAD:src/main.py").decode().strip()
        for commit in ("HEAD", "main", "HEAD:src/main.py", "-h", "a" * 40, blob):
            with self.subTest(commit=commit):
                self.assert_error("invalid_commit", project, "src/main.py", commit=commit)

    def test_unsafe_paths_are_rejected(self):
        project = self.prepare()
        for path in ("../main.py", "/tmp/main.py", "src/../main.py", "src//main.py",
                     "./src/main.py", "src\\main.py", "C:/main.py", "src/main\n.py", ""):
            with self.subTest(path=path):
                self.assert_error("invalid_path", project, path)

    def test_sensitive_and_generated_paths_are_blocked(self):
        project = self.prepare()
        for path in (".env", ".env.example", "config/credentials.json", "secrets/key.py",
                     ".git/config", ".zest/data.json", ".aws/config", "node_modules/index.js", ".venv/main.py"):
            with self.subTest(path=path):
                self.assert_error("blocked_path", project, path)
        for path in ("private.pem", "private.key", "image.png"):
            with self.subTest(path=path):
                self.assert_error("unsupported_extension", project, path)

    def test_symlink_file_and_parent_are_not_followed(self):
        external = self.root / "outside.py"
        external.write_text("not allowed")
        (self.repo / "main.py").symlink_to(external)
        (self.repo / "src").symlink_to(self.root, target_is_directory=True)
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.assert_error("unsupported_file_type", project, "main.py")
        self.assert_error("unsupported_file_type", project, "src/outside.py")

    def test_paths_with_spaces_unicode_and_glob_characters_are_literal(self):
        path = "文档/说明 [1].md"
        project = self.prepare("中文内容\n".encode(), path)
        result = read_project_file(project, path)
        self.assertEqual(result["content"], "中文内容\n")
        self.assertEqual(result["evidence"]["path"], path)

    def test_binary_non_utf8_and_large_files_are_rejected(self):
        project = self.prepare(b"a\0b")
        self.assert_error("binary_file", project, "src/main.py")
        (self.repo / "src/main.py").write_bytes(b"\xff")
        self.commit("non utf8")
        self.assert_error("unsupported_encoding", project, "src/main.py")
        (self.repo / "src/main.py").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        self.commit("large")
        self.assert_error("file_too_large", project, "src/main.py")

    def test_credentials_outside_requested_lines_block_entire_read(self):
        token = b"ghp_" + b"a" * 30
        project = self.prepare(b"ordinary\n" + token + b"\n")
        self.assert_error("sensitive_content", project, "src/main.py", end_line=1)
        code, stdout, stderr = self.cli("project", "read", project["id"], "src/main.py")
        self.assertEqual((code, stdout), (1, ""))
        self.assertNotIn(token.decode(), stderr)

    def test_missing_or_untracked_file_does_not_fall_back_to_worktree(self):
        project = self.prepare()
        (self.repo / "new.py").write_text("untracked")
        self.assert_error("file_not_found", project, "new.py")

    def test_unborn_repository_has_clear_error(self):
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.assert_error("unborn_repository", project, "main.py")

    def test_cli_read_roundtrip_and_error(self):
        project = self.prepare()
        code, stdout, stderr = self.cli("project", "read", project["id"], "src/main.py",
                                        "--start-line", "2", "--end-line", "2")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["content"], "second\n")
        code, stdout, stderr = self.cli("project", "read", project["id"], "../main.py")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "invalid_path")

    def test_read_preserves_worktree_index_and_registration(self):
        project = self.prepare()
        index = self.repo / ".git/index"
        original_index = index.read_bytes()
        original_file = (self.repo / "src/main.py").read_bytes()
        read_project_file(project, "src/main.py")
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual((self.repo / "src/main.py").read_bytes(), original_file)
        self.assertEqual(ProjectRegistry(self.state_dir).get_project(project["id"]), project)


if __name__ == "__main__":
    unittest.main()
