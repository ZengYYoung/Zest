import json
import unittest
from unittest.mock import patch

from zest.cli import main
from zest.git import inspect_repository
from zest.overview import MAX_MANIFEST_BYTES, build_overview
from zest.storage import ProjectRegistry
from tests import test_projects as fixtures


class OverviewTest(unittest.TestCase):
    # Reuse fixture helpers, without inheriting the M001 tests.
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def register(self):
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def test_python_metadata_commands_and_evidence(self):
        self.write("pyproject.toml", '[project]\nname="demo"\nversion="1.0"\ndependencies=["httpx>=0.1"]\n'
                   '[project.optional-dependencies]\ntest=["pytest"]\n'
                   '[project.scripts]\ndemo="demo.cli:main"\n')
        self.write("demo/__main__.py", "raise RuntimeError('must not run')\n")
        self.write("README.md", "# Demo\n")
        self.write("docs/guide.md", "Guide\n")
        commit = self.commit()
        result = build_overview(self.register())
        manifest = result["manifests"][0]
        self.assertEqual(manifest["status"], "parsed")
        self.assertEqual(manifest["metadata"]["dependencies"], ["httpx>=0.1"])
        self.assertEqual(manifest["metadata"]["optional-dependencies"], {"test": ["pytest"]})
        self.assertEqual(result["declared_commands"][0]["target"], "demo.cli:main")
        self.assertEqual(result["entrypoint_candidates"][0]["evidence"]["path"], "demo/__main__.py")
        self.assertEqual({item["path"] for item in result["documents"]}, {"README.md", "docs/guide.md"})
        blob = self.git("rev-parse", "HEAD:pyproject.toml").decode().strip()
        self.assertEqual(manifest["evidence"], {"path": "pyproject.toml", "commit": commit, "blob_id": blob})

    def test_node_scripts_are_reported_without_execution(self):
        marker = self.root / "executed"
        self.write("package.json", json.dumps({"name": "demo", "main": "index.js", "scripts": {
            "start": "node index.js", "postinstall": f"touch {marker}"},
            "dependencies": {"example": "^1"}, "devDependencies": {"test": "^2"}}))
        self.write("index.js", "throw new Error('must not run')\n")
        self.commit()
        result = build_overview(self.register())
        metadata = result["manifests"][0]["metadata"]
        self.assertEqual(metadata["dependencies"], {"example": "^1"})
        self.assertEqual({item["name"] for item in result["declared_commands"]}, {"start", "postinstall"})
        self.assertFalse(marker.exists())

    def test_dirty_files_and_staged_metadata_are_excluded(self):
        self.write("package.json", '{"name":"committed"}')
        self.commit()
        project = self.register()
        self.write("package.json", '{"name":"staged"}')
        self.git("add", "package.json")
        self.write("package.json", '{"name":"working"}')
        self.write("README.md", "untracked\n")
        result = build_overview(project)
        self.assertEqual(result["manifests"][0]["metadata"]["name"], "committed")
        self.assertEqual(result["documents"], [])
        self.assertTrue(result["git"]["dirty"])
        self.assertIn("working_tree_excluded", {w["code"] for w in result["warnings"]})

    def test_updated_head_is_used_without_updating_registration(self):
        self.write("package.json", '{"name":"old"}')
        old = self.commit()
        project = self.register()
        self.write("package.json", '{"name":"new"}')
        new = self.commit("update")
        result = build_overview(project)
        self.assertEqual(result["source"]["commit"], new)
        self.assertEqual(result["registered_head_commit"], old)
        self.assertTrue(result["registration_head_changed"])
        self.assertEqual(result["manifests"][0]["metadata"]["name"], "new")
        self.assertEqual(ProjectRegistry(self.state_dir).get_project(project["id"]), project)

    def test_head_change_during_read_still_uses_selected_commit(self):
        self.write("package.json", '{"name":"old"}')
        old = self.commit()
        project = self.register()
        from zest.overview import list_tree
        changed = False

        def move_head(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                self.write("package.json", '{"name":"new"}')
                self.commit("concurrent update")
            return list_tree(*args, **kwargs)

        with patch("zest.overview.list_tree", side_effect=move_head):
            result = build_overview(project)
        self.assertEqual(result["source"]["commit"], old)
        self.assertEqual(result["manifests"][0]["metadata"]["name"], "old")

    def test_symlink_manifests_and_documents_are_not_followed(self):
        external = self.root / "outside"
        external.write_text('{"name":"outside-secret"}')
        (self.repo / "package.json").symlink_to(external)
        (self.repo / "README.md").symlink_to(external)
        self.commit()
        result = build_overview(self.register())
        self.assertEqual(result["manifests"], [])
        self.assertEqual(result["documents"], [])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertNotIn("outside-secret", json.dumps(result))

    def test_tracked_env_and_generated_directories_are_not_collected(self):
        self.write(".env", "TOKEN=private-fixture\n")
        self.write(".venv/__main__.py", "private-fixture")
        self.write("node_modules/demo.js", "private-fixture")
        self.write("README.md", "# Normal\n")
        self.commit()
        result = build_overview(self.register())
        self.assertNotIn("private-fixture", json.dumps(result))
        self.assertEqual(result["root_directories"], [])
        self.assertEqual(result["entrypoint_candidates"], [])

    def test_malformed_toml_and_json_report_warnings(self):
        self.write("pyproject.toml", "[invalid")
        self.write("package.json", "{invalid")
        self.commit()
        result = build_overview(self.register())
        self.assertEqual([m["status"] for m in result["manifests"]], ["invalid", "invalid"])
        self.assertEqual({w["code"] for w in result["warnings"]}, {"invalid_manifest"})

    def test_wrong_manifest_field_types_are_not_inferred(self):
        self.write("pyproject.toml", "[project]\ndependencies=1\n")
        self.write("package.json", '{"scripts":["npm start"]}')
        self.commit()
        result = build_overview(self.register())
        self.assertEqual([m["status"] for m in result["manifests"]], ["invalid", "invalid"])

    def test_oversized_manifest_is_skipped_before_content_read(self):
        self.write("package.json", " " * (MAX_MANIFEST_BYTES + 1))
        self.commit()
        result = build_overview(self.register())
        self.assertEqual(result["manifests"][0]["status"], "too_large")
        self.assertEqual(result["declared_commands"], [])

    def test_non_utf8_manifest_is_reported_as_invalid(self):
        (self.repo / "pyproject.toml").write_bytes(b"\xff\xfe")
        self.commit()
        self.assertEqual(build_overview(self.register())["manifests"][0]["status"], "invalid")

    def test_other_manifests_are_inventory_only(self):
        self.write("pom.xml", "<project/>\n")
        self.write("go.mod", "module demo\n")
        self.commit()
        manifests = build_overview(self.register())["manifests"]
        self.assertEqual({m["path"] for m in manifests}, {"pom.xml", "go.mod"})
        self.assertEqual({m["status"] for m in manifests}, {"inventory_only"})

    def test_empty_repository_returns_explicit_gap(self):
        result = build_overview(self.register())
        self.assertIsNone(result["source"]["commit"])
        self.assertEqual(result["manifests"], [])
        self.assertEqual(result["warnings"][0]["code"], "unborn_repository")

    def test_docs_listing_is_shallow_and_regular_files_only(self):
        self.write("docs/guide.md", "guide")
        self.write("docs/文档\n说明.txt", "guide")
        self.write("docs/nested/deep.md", "deep")
        self.write("docs/image.png", "not documentation")
        (self.repo / "docs/link.md").symlink_to("guide.md")
        self.commit()
        docs = build_overview(self.register())["documents"]
        self.assertEqual({d["path"] for d in docs}, {"docs/guide.md", "docs/文档\n说明.txt"})

    def test_document_limit_is_explicit(self):
        for number in range(51):
            self.write(f"docs/{number:03d}.md", "guide")
        self.commit()
        result = build_overview(self.register())
        self.assertEqual(len(result["documents"]), 50)
        self.assertIn("document_limit", {w["code"] for w in result["warnings"]})

    def test_overview_keeps_worktree_and_index_unchanged(self):
        self.write("package.json", '{"name":"test"}')
        self.commit()
        project = self.register()
        index = self.repo / ".git/index"
        before_index = index.read_bytes()
        before_files = {p.relative_to(self.repo): p.read_bytes() for p in self.repo.rglob("*")
                        if p.is_file() and ".git" not in p.parts}
        build_overview(project)
        self.assertEqual(index.read_bytes(), before_index)
        after_files = {p.relative_to(self.repo): p.read_bytes() for p in self.repo.rglob("*")
                       if p.is_file() and ".git" not in p.parts}
        self.assertEqual(before_files, after_files)

    def test_cli_overview_and_unknown_project(self):
        self.write("package.json", '{"name":"test"}')
        self.commit()
        project = self.register()
        code, stdout, stderr = self.cli("project", "overview", project["id"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["project_id"], project["id"])
        code, stdout, stderr = self.cli("project", "overview", "missing")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "project_not_found")

    def test_moved_repository_has_clear_cli_error(self):
        project = self.register()
        self.repo.rename(self.root / "moved")
        code, stdout, stderr = self.cli("project", "overview", project["id"])
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "repository_error")


if __name__ == "__main__":
    unittest.main()
