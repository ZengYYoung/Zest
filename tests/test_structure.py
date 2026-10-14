import hashlib
import json
from unittest.mock import patch
import unittest

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository, managed_ref
from zest.storage import ProjectRegistry
from zest.structure import build_structure


class PythonStructureTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def project(self, code="def add(a,b):\n    return a+b\n"):
        (self.repo / "app.py").write_text(code)
        commit = self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo)), commit

    def test_scoped_symbols_decorators_async_and_relative_import_evidence(self):
        code = "from . import local\nimport os as operating\n\n@decorate\nclass Service:\n    async def execute(self):\n        from ..utils import helper as h\n        def nested():\n            return 1\n        return nested()\n"
        project, commit = self.project(code)
        record = build_structure(project)
        module = record["modules"][0]
        symbols = module["symbols"]
        self.assertEqual([s["lexical_name"] for s in symbols], ["Service", "Service.execute", "Service.execute.nested"])
        self.assertEqual(symbols[0]["evidence"]["start_line"], 4)
        self.assertEqual(symbols[0]["evidence"]["end_line"], 10)
        self.assertEqual(symbols[1]["kind"], "async_function")
        self.assertEqual(module["imports"][2]["scope"], "Service.execute")
        self.assertEqual(module["imports"][2]["level"], 2)
        self.assertEqual(module["imports"][2]["names"], [{"name": "helper", "alias": "h"}])
        self.assertEqual(module["evidence"]["commit"], commit)
        self.assertEqual(module["evidence"]["content_sha256"], hashlib.sha256(code.encode()).hexdigest())
        self.assertIn("declarations_not_runtime_dependencies", record["limitations"])

    def test_code_is_not_imported_or_executed_and_dirty_bytes_are_excluded(self):
        marker = self.root / "should-not-exist"
        project, commit = self.project(f"open({str(marker)!r}, 'w').write('executed')\nraise RuntimeError('do not run')\ndef stable():\n    pass\n")
        (self.repo / "app.py").write_text("def uncommitted():\n    pass\n")
        record = build_structure(project)
        self.assertEqual(record["modules"][0]["symbols"][0]["name"], "stable")
        self.assertFalse(marker.exists())
        self.assertFalse(record["working_tree_changes_included"])
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), commit)

    def test_sensitive_link_binary_and_syntax_sources_are_counted_not_returned(self):
        project, _ = self.project()
        (self.repo / "secrets").mkdir()
        (self.repo / "secrets/private.py").write_text("def private(): pass\n")
        (self.repo / "token.py").write_text("key = 'sk-" + "x" * 24 + "'\n")
        (self.repo / "linked.py").symlink_to(self.root / "outside")
        (self.repo / "bad.py").write_text("def broken(:\n")
        (self.repo / "binary.py").write_bytes(b"\x00binary")
        self.commit("bad sources")
        record = build_structure(project)
        self.assertEqual([m["path"] for m in record["modules"]], ["app.py"])
        for code in ("excluded_directory", "sensitive_content", "unsupported_file_type", "structure_parse_failed", "binary_file"):
            self.assertEqual(record["skipped_counts"][code], 1)
        self.assertNotIn("sk-", json.dumps(record))
        self.assertNotIn("private.py", json.dumps(record))

    def test_managed_and_explicit_versions_have_correct_source_identity(self):
        project, original = self.project()
        (self.repo / "app.py").write_text("def newer():\n    pass\n")
        newer = self.commit("new version")
        self.git("update-ref", managed_ref(project["id"]), original)
        self.assertEqual(build_structure(project)["modules"][0]["symbols"][0]["name"], "add")
        self.assertEqual(build_structure(project, commit=newer)["modules"][0]["symbols"][0]["name"], "newer")
        with self.assertRaises(FileReadError):
            build_structure(project, commit="main")

    def test_budgets_report_incomplete_analysis_instead_of_claiming_coverage(self):
        project, _ = self.project()
        (self.repo / "second.py").write_text("def second(): pass\n")
        self.commit("second")
        with patch("zest.structure.MAX_FILES", 1):
            record = build_structure(project)
        self.assertEqual(len(record["modules"]), 1)
        self.assertIn({"code": "structure_file_budget"}, record["warnings"])
        with patch("zest.structure.MAX_NODES", 1):
            record = build_structure(project)
        self.assertEqual(record["modules"], [])
        self.assertEqual(record["skipped_counts"]["structure_node_budget"], 2)
        with patch("zest.structure.MAX_BYTES", 1):
            self.assertIn({"code": "structure_byte_budget"}, build_structure(project)["warnings"])

    def test_cli_needs_no_model_or_new_state(self):
        project, _ = self.project()
        before = sorted(p.name for p in self.state_dir.iterdir())
        code, output, _ = self.cli("project", "map", project["id"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["backend"], "python_ast_declarations")
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir()), before)
        self.assertFalse((self.state_dir / "tasks.sqlite3").exists())

    def test_unborn_repository_has_no_map_or_state_writes(self):
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        result = build_structure(project)
        self.assertIsNone(result["commit"])
        self.assertEqual(result["modules"], [])
        self.assertEqual(result["warnings"], [{"code": "unborn_repository"}])
