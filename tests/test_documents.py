"""Portable documents across real local Git clones; no external model/network."""

import json
from pathlib import Path
import subprocess
import unittest

from tests import test_projects as fixtures
from zest.document_layout import LEGACY_IGNORE
from zest.documents import document_catalog, initialize_documents
from zest.files import FileReadError, read_project_file
from zest.git import git_environment, inspect_repository
from zest.index import RetrievalIndex
from zest.qa import prepare_context
from zest.repair_agent import prepare_repair_context, validate_proposal
from zest.repairs import validate_change_paths
from zest.search import search_project
from zest.storage import ProjectRegistry
from zest.survey import collect_survey
from zest.verification import collect_snapshot


class DocumentsTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def project(self):
        (self.repo / "app.py").write_text("def cancel_order():\n    return 'pending'\n")
        (self.repo / "README.md").write_text("# Order service\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/api.md").write_text("# 取消订单接口\nPOST /orders/{id}/cancel\n只允许取消未付款订单。\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)

    def test_navigation_title_survives_a_utf8_character_split_at_preview_boundary(self):
        from zest.documents import workspace_documents
        project=self.project();folder=self.repo/'.zest/docs';folder.mkdir(parents=True,exist_ok=True)
        for name,prefix,title in [('heading','# 中文导航标题\n\n','中文导航标题'),
                                  ('metadata','---\ntitle: "中文元信息标题"\n---\n\n','中文元信息标题')]:
            head=prefix.encode('utf-8')
            (folder/(name+'.md')).write_bytes(head+b'a'*(2047-len(head))+'中'.encode('utf-8'))
        (folder/'invalid.md').write_bytes(b'# Wrong\n\n\xff')
        docs={d['path']:d for d in workspace_documents(project)['documents']}
        self.assertEqual(docs['.zest/docs/heading.md']['title'],'中文导航标题')
        self.assertEqual(docs['.zest/docs/metadata.md']['title'],'中文元信息标题')
        self.assertNotIn('title',docs['.zest/docs/invalid.md'])

    def test_explicit_import_preserves_originals_index_and_local_state_and_is_idempotent(self):
        project = self.project()
        original = (self.repo / "docs/api.md").read_bytes()
        state = {p.name: p.read_bytes() for p in Path(project["resource_dir"]).glob("*.sqlite3")}
        head, index = self.git("rev-parse", "HEAD"), self.git("ls-files", "--stage")
        result = initialize_documents(project)
        self.assertIn(".zest/docs/imported/docs/api.md", result["created"])
        self.assertEqual((self.repo / ".zest/docs/imported/docs/api.md").read_bytes(), original)
        self.assertEqual((self.repo / "docs/api.md").read_bytes(), original)
        manifest = json.loads((self.repo / ".zest/docs/imports.json").read_text())
        self.assertEqual(manifest["status"], "imported_unverified")
        self.assertTrue(all(not Path(d["source_path"]).is_absolute() for d in manifest["documents"]))
        snapshot = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.repo / ".zest/docs").rglob("*") if p.is_file()}
        again = initialize_documents(project)
        self.assertEqual((again["created"], again["updated"], again["conflicts"]), ([], [], []))
        self.assertEqual(snapshot, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.repo / ".zest/docs").rglob("*") if p.is_file()})
        self.assertEqual(head, self.git("rev-parse", "HEAD"))
        self.assertEqual(index, self.git("ls-files", "--stage"))
        self.assertEqual(state, {p.name: p.read_bytes() for p in Path(project["resource_dir"]).glob("*.sqlite3")})

    def test_source_or_shared_edits_are_reported_without_overwriting_team_documents(self):
        project = self.project()
        initialize_documents(project)
        destination = self.repo / ".zest/docs/imported/docs/api.md"
        destination.write_text("# Team decision\nOnly cancel pending orders.\n")
        (self.repo / "docs/api.md").write_text("Old docs changed after migration\n")
        self.commit("reviewed shared documents")
        before = destination.read_bytes()
        report = initialize_documents(project)
        self.assertTrue(any(c["code"] == "source_changed" for c in report["conflicts"]))
        self.assertEqual(destination.read_bytes(), before)
        destination.unlink()
        report = initialize_documents(project)
        self.assertTrue(report["conflicts"])
        self.assertFalse(destination.exists())

    def test_legacy_ignore_is_upgraded_without_resetting_identity_or_history(self):
        project = self.project()
        marker = self.repo / ".zest/workspace.json"
        marker_bytes = marker.read_bytes()
        (self.repo / ".zest/.gitignore").write_bytes(LEGACY_IGNORE)
        (self.repo / ".gitignore").write_bytes(b"# existing\r\n.zest/\r\n")
        result = initialize_documents(project)
        self.assertIn(".gitignore", result["updated"])
        self.assertTrue((self.repo / ".gitignore").read_bytes().startswith(b"# existing\r\n.zest/\r\n"))
        self.assertEqual(marker.read_bytes(), marker_bytes)
        ignored = subprocess.run(["git", "-C", str(self.repo), "check-ignore", "--no-index", "--quiet", ".zest/docs/index.md"], env=git_environment())
        self.assertEqual(ignored.returncode, 1)
        for path in (".zest/workspace.json", ".zest/state/state.sqlite3", ".zest/personal.md", ".zest/docs/not-a-doc.sqlite3"):
            self.git("check-ignore", "--", path)
        self.assertEqual(ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)["id"], project["id"])

    def test_private_resources_and_unsafe_paths_cannot_become_sources(self):
        project = self.project()
        initialize_documents(project)
        for path in (".zest/personal.md", ".zest/state/private.md", ".zest/docs/.env.md", ".zest/docs/nested/.zest/private.md"):
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("PRIVATE_MARKER\n")
            self.git("add", "-f", "--", path)
        self.commit()
        for path in (".zest/personal.md", ".zest/state/private.md", ".zest/docs/.env.md", ".zest/docs/../workspace.json"):
            with self.subTest(path=path), self.assertRaises(FileReadError):
                read_project_file(project, path)
        catalog = document_catalog(project)
        self.assertTrue(all("private" not in d["path"] and ".env" not in d["path"] for d in catalog["documents"]))
        self.assertEqual(search_project(project, "PRIVATE_MARKER")["matches"], [])
        self.assertNotIn("PRIVATE_MARKER", json.dumps(prepare_context(project, "取消订单接口"), ensure_ascii=False))

    def test_committed_docs_enter_question_survey_index_and_readonly_repair_context(self):
        project = self.project()
        initialize_documents(project)
        self.commit("shared contract")
        path = ".zest/docs/imported/docs/api.md"
        context = prepare_context(project, "取消订单接口", state_dir=self.state_dir)
        self.assertIn(path, [s["evidence"].get("path") for s in context["sources"]])
        survey = collect_survey(project)
        self.assertIn(path, [s["evidence"]["path"] for s in survey["sources"]])
        RetrievalIndex(self.state_dir).build(project)
        ranked = RetrievalIndex(self.state_dir).search(project, "取消订单", limit=10)
        self.assertIn(path, [m["evidence"]["path"] for m in ranked["matches"]])
        repair = prepare_repair_context(project, "按照取消订单接口约定修改实现", files=["app.py"], state_dir=self.state_dir)
        source = next(s for s in repair["sources"] if s["evidence"].get("path") == path)
        self.assertNotIn(source["id"], repair["execution_policy"]["editable_source_ids"])
        with self.assertRaises(FileReadError) as caught:
            validate_proposal(json.dumps({"status": "proposed", "summary": "change", "unknowns": [],
                "changes": [{"source_id": source["id"], "content": "rewritten"}]}), repair)
        self.assertEqual(caught.exception.code, "proposal_scope_violation")
        _, snapshot = collect_snapshot(self.repo, self.git("rev-parse", "HEAD").decode().strip())
        self.assertIn(path, [p for p, _, _ in snapshot])

    def test_documents_can_be_edited_only_in_the_managed_home(self):
        validate_change_paths([".zest/docs/api.md", "app.py"])
        for path in ("docs/api.md", "README.md", "openapi.yaml", ".zest/state/notes.md"):
            with self.subTest(path=path), self.assertRaises(FileReadError):
                validate_change_paths([path])

    def test_import_ignores_uncommitted_source_and_rejects_symlink_destination(self):
        project = self.project()
        (self.repo / "docs/api.md").write_text("Uncommitted requirement\n")
        initialize_documents(project)
        destination = self.repo / ".zest/docs/imported/docs/api.md"
        self.assertIn("只允许取消未付款订单", destination.read_text())
        outside = self.root / "outside"
        outside.mkdir()
        destination.unlink()
        destination.symlink_to(outside / "untouched.md")
        with self.assertRaises(FileReadError):
            initialize_documents(project)
        self.assertEqual(list(outside.iterdir()), [])

    def test_custom_ignore_is_not_overwritten_and_cli_uses_same_service(self):
        project = self.project()
        ignore = self.repo / ".zest/.gitignore"
        ignore.write_text("# custom\ncache/\n")
        before = ignore.read_bytes()
        with self.assertRaises(FileReadError):
            initialize_documents(project)
        self.assertEqual(before, ignore.read_bytes())
        ignore.write_bytes(LEGACY_IGNORE)
        code, out, error = self.cli("project", "documents", project["id"], "--init")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(out)["requires_review"])

    def test_two_developers_share_only_pushed_documents_and_keep_distinct_local_state(self):
        project_a = self.project()
        initialize_documents(project_a)
        self.commit("shared documents")
        remote, clone = self.root / "remote.git", self.root / "developer-b"
        def git_run(*args):
            return subprocess.run(["git", *map(str, args)], env=git_environment(), capture_output=True, check=True).stdout
        git_run("init", "--bare", remote)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "origin", "main")
        git_run("clone", "--branch", "main", remote, clone)
        self.assertFalse((clone / ".zest/workspace.json").exists())
        project_b = ProjectRegistry(self.root / "b-catalog").register(inspect_repository(clone), workspace=True)
        self.assertNotEqual(project_a["id"], project_b["id"])
        (self.repo / ".zest/personal.md").write_text("Developer A private discussion\n")
        path = ".zest/docs/imported/docs/api.md"
        (self.repo / path).write_text("# 取消订单接口\nPOST /orders/{id}/cancel\n新增客户端幂等键。\n")
        self.assertNotIn("幂等", read_project_file(project_b, path)["content"])
        self.commit("new contract")
        self.assertNotIn("幂等", read_project_file(project_b, path)["content"])
        self.git("push", "origin", "main")
        git_run("-C", clone, "pull", "--ff-only")
        self.assertIn("幂等", read_project_file(project_b, path)["content"])
        self.assertFalse((clone / ".zest/personal.md").exists())
        self.assertTrue(document_catalog(project_b)["documents"])
        self.assertEqual(ProjectRegistry(self.root / "b-catalog").register(inspect_repository(clone), workspace=True)["id"], project_b["id"])

    def test_web_get_is_readonly_and_operation_uses_document_service(self):
        from fastapi.testclient import TestClient
        from zest.web import create_app
        from zest.web_commands import execute, validate_command
        project = self.project()
        client = TestClient(create_app(self.state_dir))
        url = "/api/projects/" + project["id"] + "/documents"
        self.assertEqual(client.get(url).json()["data"]["documents"], [])
        self.assertFalse((self.repo / ".zest/docs").exists())
        command = validate_command("project.documents.init", {"project_id": project["id"]})
        result = execute(self.state_dir, "project.documents.init", command)
        self.assertTrue(result["created"])
        self.assertEqual(client.get(url).json()["data"]["documents"], [])
        self.commit()
        self.assertTrue(client.get(url).json()["data"]["documents"])
