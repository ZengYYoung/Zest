import json
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.search import search_project
from zest.storage import ProjectRegistry


class SearchTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def prepare(self, files=None):
        for name, content in (files or {"src/main.py": "Alpha\nbeta Alpha\n"}).items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def test_matches_have_line_numbers_and_version(self):
        project = self.prepare()
        result = search_project(project, "alpha")
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual([m["evidence"]["start_line"] for m in result["matches"]], [1, 2])
        self.assertEqual(result["matches"][0]["evidence"]["commit"], project["git"]["head_commit"])
        self.assertTrue(result["scan_complete"])

    def test_literal_search_and_case_sensitivity(self):
        project = self.prepare({"a.py": "foo.*\nFOO\nfoo\n"})
        self.assertEqual(len(search_project(project, "foo.*")["matches"]), 1)
        self.assertEqual(len(search_project(project, "FOO", case_sensitive=True)["matches"]), 1)

    def test_no_matches_are_an_empty_result(self):
        self.assertEqual(search_project(self.prepare(), "absent")["matches"], [])

    def test_untracked_and_working_changes_are_excluded(self):
        project = self.prepare()
        (self.repo / "src/main.py").write_text("uncommitted")
        (self.repo / "new.py").write_text("uncommitted")
        self.assertEqual(search_project(project, "uncommitted")["matches"], [])

    def test_historical_commit(self):
        project = self.prepare()
        old = project["git"]["head_commit"]
        (self.repo / "src/main.py").write_text("new")
        self.commit("update")
        self.assertEqual(search_project(project, "Alpha")["matches"], [])
        self.assertEqual(len(search_project(project, "Alpha", commit=old)["matches"]), 2)

    def test_sensitive_directories_and_content_are_skipped(self):
        token = "ghp_" + "x" * 30
        project = self.prepare({".env": "match", "node_modules/a.py": "match", "secrets/a.py": "match",
                                "secret.py": token + "\nmatch", "ok.py": "match"})
        result = search_project(project, "match")
        self.assertEqual([m["evidence"]["path"] for m in result["matches"]], ["ok.py"])
        self.assertEqual(result["skipped_counts"]["sensitive_content"], 1)
        self.assertNotIn(token, json.dumps(result))

    def test_symlink_is_not_followed(self):
        (self.root / "external.py").write_text("match")
        (self.repo / "linked.py").symlink_to(self.root / "external.py")
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        result = search_project(project, "match")
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["skipped_counts"]["unsupported_file_type"], 1)

    def test_binary_and_invalid_encoding_are_skipped(self):
        self.prepare({"a.py": "normal"})
        (self.repo / "a.py").write_bytes(b"match\0")
        (self.repo / "b.py").write_bytes(b"\xff")
        self.commit("binary")
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        result = search_project(project, "match")
        self.assertEqual(result["skipped_counts"], {"binary_file": 1, "unsupported_encoding": 1})

    def test_match_and_file_budgets_are_explicit(self):
        project = self.prepare({"a.py": "match\nmatch\n", "b.py": "match"})
        result = search_project(project, "match", limit=1)
        self.assertFalse(result["scan_complete"])
        self.assertEqual(len(result["matches"]), 1)
        with patch("zest.search.MAX_SEARCH_FILES", 1):
            result = search_project(project, "absent")
        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["warnings"][-1]["code"], "search_file_limit")

    def test_byte_budget_is_explicit(self):
        project = self.prepare()
        with patch("zest.search.MAX_SEARCH_BYTES", 1):
            result = search_project(project, "Alpha")
        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["warnings"][-1]["code"], "search_byte_limit")

    def test_preview_truncation_is_explicit(self):
        project = self.prepare({"a.py": "x" * 2100 + "match"})
        match = search_project(project, "match")["matches"][0]
        self.assertTrue(match["text_truncated"])
        self.assertFalse(match["match_in_preview"])

    def test_invalid_queries_limits_and_revisions_are_rejected(self):
        project = self.prepare()
        for query in ("", "  ", "x\ny", "x" * 201):
            with self.subTest(query=query), self.assertRaises(FileReadError):
                search_project(project, query)
        with self.assertRaises(FileReadError):
            search_project(project, "a", limit=0)
        with self.assertRaises(FileReadError):
            search_project(project, "a", commit="HEAD")

    def test_empty_repo(self):
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        result = search_project(project, "anything")
        self.assertEqual(result["matches"], [])
        self.assertIsNone(result["source"]["commit"])

    def test_cli_and_readonly_behaviour(self):
        project = self.prepare()
        index = (self.repo / ".git/index").read_bytes()
        code, stdout, stderr = self.cli("project", "search", project["id"], "Alpha")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(json.loads(stdout)["matches"]), 2)
        self.assertEqual(index, (self.repo / ".git/index").read_bytes())
        self.assertEqual(project, ProjectRegistry(self.state_dir).get_project(project["id"]))


if __name__ == "__main__":
    unittest.main()
