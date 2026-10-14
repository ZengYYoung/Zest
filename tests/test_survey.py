import hashlib
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository, managed_ref
from zest.storage import ProjectRegistry
from zest.survey import SurveyStore, collect_survey, format_survey


class SurveyTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def project(self):
        self.write("README.md", "# Demo\n说明项目目标的材料，尚待 Agent 调查。\n")
        self.write("backend/pyproject.toml", '[project]\nname="demo"\ndependencies=["example"]\n[project.scripts]\ndemo="app:main"\n')
        self.write("frontend/package.json", '{"name":"ui","scripts":{"test":"node test.js"},"dependencies":{"react":"example"}}')
        self.write("frontend/src/index.ts", "export const ready = true;\n")
        self.write("docs/nested/guide.md", "# Setup\nrun demo\n")
        self.write("backend/__main__.py", "def main():\n    pass\n")
        self.write("tests/test_main.py", "def test_main():\n    assert True\n")
        commit = self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        return project, commit

    def test_nested_materials_versions_hashes_and_pending_dossier_sections(self):
        project, commit = self.project()
        record = SurveyStore(self.state_dir).build(project)
        self.assertEqual((self.state_dir / "surveys.sqlite3").stat().st_mode & 0o777, 0o600)
        self.assertEqual(record["source"]["commit"], commit)
        sources = {source["evidence"]["path"]: source for source in record["sources"]}
        self.assertIn("document", sources["docs/nested/guide.md"]["roles"])
        self.assertIn("test_candidate", sources["tests/test_main.py"]["roles"])
        self.assertIn("entrypoint_candidate", sources["backend/__main__.py"]["roles"])
        self.assertEqual({m["ecosystem"] for m in record["manifests"]}, {"python", "node"})
        self.assertEqual({c["target"] for c in record["declared_commands"]}, {"app:main", "node test.js"})
        self.assertEqual(record["structure"]["commit"], commit)
        for path, source in sources.items():
            raw = (self.repo / path).read_bytes()
            self.assertEqual(source["evidence"]["content_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(source["evidence"]["commit"], commit)
        self.assertEqual(len(record["sections"]), 6)
        self.assertEqual({s["status"] for s in record["sections"]}, {"pending_investigation"})
        self.assertNotIn("root_path", record)
        self.assertIn("runtime_not_verified", {gap["code"] for gap in record["gaps"]})
        self.assertIn({"code": "non_python_structure_not_parsed", "suffixes": [".ts"]}, record["gaps"])
        self.assertFalse((self.state_dir / "knowledge.sqlite3").exists())
        self.assertFalse((self.state_dir / "tasks.sqlite3").exists())

    def test_managed_version_and_explicit_snapshot_exclude_dirty_content(self):
        project, initial = self.project()
        self.write("README.md", "# New version\n")
        newer = self.commit("new version")
        self.git("update-ref", managed_ref(project["id"]), initial)
        self.write("README.md", "DIRTY BODY MUST NOT APPEAR")
        store = SurveyStore(self.state_dir)
        record = store.build(project)
        self.assertEqual(record["source"]["commit"], initial)
        self.assertIn("managed_version_selected", {w["code"] for w in record["warnings"]})
        self.assertNotIn("DIRTY BODY", json.dumps(record))
        self.assertEqual(store.build(project, commit=newer)["source"]["commit"], newer)
        with self.assertRaises(FileReadError):
            store.build(project, commit="main")

    def test_structure_uses_pinned_commit_when_head_changes_mid_scan(self):
        from zest.structure import build_structure
        project, initial = self.project()
        def change_then_map(project, *, commit):
            self.write("backend/__main__.py", "def changed_later(): pass\n")
            self.commit("changed during collection")
            return build_structure(project, commit=commit)
        with patch("zest.survey.build_structure", side_effect=change_then_map):
            record = SurveyStore(self.state_dir).build(project)
        self.assertEqual(record["source"]["commit"], initial)
        self.assertEqual(record["structure"]["commit"], initial)
        names = [s["name"] for m in record["structure"]["modules"] for s in m["symbols"]]
        self.assertIn("main", names)
        self.assertNotIn("changed_later", names)

    def test_protected_sources_and_credentials_never_reach_saved_materials(self):
        project, _ = self.project()
        secret = "sk-" + "x" * 24
        self.write(".env", secret)
        self.write("secrets/private.md", "PRIVATE BODY")
        self.write("sensitive.json", '{"key":"' + secret + '"}')
        self.write("frontend/package.json", '{"scripts":{"private":"' + secret + '"}}')
        self.write("docs/token.md", secret)
        self.write("docs/unsafe\nname.md", "BAD PATH BODY")
        self.write("bad.py", "\0BINARY BODY")
        self.write(f"docs/{secret}.md", "SECRET PATH BODY")
        self.write(f"{secret}.py", "def safe_body(): pass\n")
        self.write(".env-config/leak.py", "def blocked_directory_body(): pass\n")
        (self.repo / "linked.md").symlink_to(self.root / "outside")
        self.commit("unsafe sources")
        store = SurveyStore(self.state_dir)
        record = store.build(project)
        encoded = json.dumps(record)
        for value in (secret, "PRIVATE BODY", "BAD PATH BODY", "BINARY BODY", "SECRET PATH BODY"):
            self.assertNotIn(value, encoded)
            self.assertNotIn(value.encode(), store.db_path.read_bytes())
        for code in ("blocked_path", "excluded_directory", "sensitive_content", "invalid_path", "binary_file", "unsupported_file_type", "sensitive_path"):
            self.assertGreater(record["coverage"]["skipped_counts"][code], 0)

    def test_project_scripts_hooks_and_imports_are_not_executed_and_index_unchanged(self):
        project, _ = self.project()
        marker = self.root / "executed"
        self.write("app.py", f"open({str(marker)!r}, 'w').write('executed')\nraise RuntimeError('do not import')\n")
        self.write("package.json", json.dumps({"scripts": {"start": f"touch {marker}"}}))
        self.commit("malicious execution declarations")
        hook = self.root / "monitor.sh"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        index = self.repo / ".git/index"
        before = index.read_bytes()
        SurveyStore(self.state_dir).build(project)
        self.assertFalse(marker.exists())
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((self.state_dir / "index.sqlite3").exists())
        self.assertFalse((self.state_dir / "tasks.sqlite3").exists())

    def test_budget_interruptions_are_explicit_and_do_not_replace_prior_record(self):
        project, _ = self.project()
        store = SurveyStore(self.state_dir)
        original = store.build(project)
        for parameter, value, warning in (("MAX_FILES", 1, "survey_file_budget"), ("MAX_BYTES", 1, "survey_byte_budget"),
                                          ("MAX_TREE_ENTRIES", 1, "survey_scan_budget"), ("MAX_SECONDS", -1, "survey_time_budget")):
            with self.subTest(parameter=parameter), patch("zest.survey." + parameter, value):
                partial = store.build(project)
                self.assertEqual(partial["status"], "partial")
                self.assertFalse(partial["coverage"]["inventory_scan_finished"])
                self.assertIn(warning, {w["code"] for w in partial["warnings"]})
        self.assertEqual(store.show(original["id"]), original)

    def test_excerpts_have_physical_ranges_truncation_and_budgets(self):
        project, _ = self.project()
        self.write("docs/long.md", "\n".join(str(i) for i in range(100)) + "\n")
        self.write("docs/wide.md", "z" * 7000 + "\n")
        self.commit("long docs")
        record = collect_survey(project)
        sources = {s["evidence"]["path"]: s for s in record["sources"]}
        excerpt = sources["docs/long.md"]["excerpt"]
        self.assertEqual((excerpt["start_line"], excerpt["end_line"], excerpt["more_after"]), (1, 80, True))
        self.assertEqual(excerpt["text_sha256"], hashlib.sha256(excerpt["text"].encode()).hexdigest())
        self.assertEqual(sources["docs/wide.md"]["excerpt_omitted"], "survey_excerpt_budget")
        self.assertLessEqual(sum(len(s.get("excerpt", {}).get("text", "")) for s in sources.values()), 24000)

    def test_bad_or_unparsed_manifests_and_missing_materials_are_visible(self):
        self.write("pom.xml", "<project/>\n")
        self.write("Cargo.toml", "[package]\nname='example'\n")
        self.write("package.json", "{invalid")
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        record = collect_survey(project)
        self.assertEqual(record["status"], "partial")
        codes = {g["code"] for g in record["gaps"]}
        self.assertTrue({"manifest_parser_unavailable", "no_readable_documents_found", "no_test_candidates_found"}.issubset(codes))
        self.assertEqual({m["status"] for m in record["manifests"]}, {"invalid", "inventory_only"})
        self.assertIn("rust", {m["ecosystem"] for m in record["manifests"]})

    def test_unborn_record_and_read_only_empty_history(self):
        store = SurveyStore(self.state_dir)
        self.assertEqual(store.list_surveys("not-registered"), [])
        with self.assertRaises(FileReadError):
            store.show("00000000-0000-0000-0000-000000000000")
        self.assertFalse(self.state_dir.exists())
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        record = store.build(project)
        self.assertEqual(record["status"], "no_committed_source")
        self.assertIsNone(record["source"]["commit"])
        self.assertEqual(record["sources"], [])
        self.assertEqual(store.show(record["id"]), record)

    def test_independent_history_project_scoped_list_and_read_without_repository(self):
        project, _ = self.project()
        store = SurveyStore(self.state_dir)
        first = store.build(project)
        second = store.build(project)
        foreign = store.build(dict(project, id="00000000-0000-0000-0000-000000000001"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({r["id"] for r in store.list_surveys(project["id"])}, {first["id"], second["id"]})
        self.assertEqual(len(store.list_surveys(foreign["project_id"])), 1)
        self.assertNotIn("sources", store.list_surveys(project["id"])[0])
        self.repo.rename(self.root / "moved")
        self.assertEqual(SurveyStore(self.state_dir).show(first["id"]), first)

    def test_invalid_ids_corrupt_database_record_and_record_budget(self):
        project, _ = self.project()
        store = SurveyStore(self.state_dir)
        record = store.build(project)
        with self.assertRaises(FileReadError) as caught:
            store.show("../escape")
        self.assertEqual(caught.exception.code, "invalid_survey_id")
        with patch("zest.survey.MAX_RECORD_BYTES", 1), self.assertRaises(FileReadError) as caught:
            store.build(project)
        self.assertEqual(caught.exception.code, "survey_record_budget")
        with closing(sqlite3.connect(store.db_path)) as conn, conn:
            conn.execute("UPDATE surveys SET record='{}' WHERE id=?", (record["id"],))
        with self.assertRaises(FileReadError) as caught:
            store.show(record["id"])
        self.assertEqual(caught.exception.code, "survey_record_corrupt")
        store.db_path.write_bytes(b"not sqlite")
        with self.assertRaises(FileReadError) as caught:
            store.list_surveys(project["id"])
        self.assertEqual(caught.exception.code, "survey_storage_error")
        store.db_path.unlink()
        target = self.root / "do-not-write"
        target.write_bytes(b"unchanged")
        store.db_path.symlink_to(target)
        with self.assertRaises(FileReadError) as caught:
            store.build(project)
        self.assertEqual(caught.exception.code, "survey_storage_error")
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_cli_json_progress_human_summary_and_errors(self):
        project, _ = self.project()
        code, output, error = self.cli("survey", "build", project["id"], "--progress")
        self.assertEqual(code, 0)
        record = json.loads(output)
        self.assertIn("固定源码版本", error)
        self.assertIn("已保存摸底记录", error)
        code, output, error = self.cli("survey", "show", record["id"], "--format", "text")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("项目认知仍待调查", output)
        self.assertIn("backend/pyproject.toml", output)
        code, output, error = self.cli("survey", "list", project["id"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)[0]["id"], record["id"])
        code, output, error = self.cli("survey", "build", "missing")
        self.assertEqual((code, output), (1, ""))
        self.assertEqual(json.loads(error)["error"]["code"], "project_not_found")

    def test_human_output_does_not_interpret_terminal_controls_in_git_names(self):
        project, _ = self.project()
        self.write("docs/\x1b[31munsafe.md", "not displayed")
        self.commit("unsafe filename")
        rendered = format_survey(SurveyStore(self.state_dir).build(project))
        self.assertNotIn("\x1b", rendered)
