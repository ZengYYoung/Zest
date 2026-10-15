"""Deterministic management inventory, not a model-quality acceptance run."""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_projects as fixtures
from tests.test_cognition import OfflineCognitionModel
from zest.cognition import CognitionEngine
from zest.cognition_store import CognitionStore, canonical, digest
from zest.files import FileReadError
from zest.git import inspect_repository, managed_ref
from zest.index import RetrievalIndex
from zest.knowledge import KnowledgeStore
from zest.management_context import build_management_context, format_management_context
from zest.resources import project_states
from zest.storage import ProjectRegistry


class ManagementContextTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def project(self, *, workspace=True):
        (self.repo / "README.md").write_text("# Addition example\n")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=workspace)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def seed(self, project, *, commit=None, status="completed", finding_status="auto_reviewed", kind="observed", destination=None):
        commit = commit or project["git"]["head_commit"]
        from zest.files import read_project_file
        proof = read_project_file(project, "app.py", commit=commit, end_line=2)
        source = {key: value for key, value in proof["evidence"].items() if key not in ("start_line", "end_line")}
        source["content_sha256"] = proof["content_sha256"]
        envelope = {"project_id": project["id"], "commit": commit,
                    "files": [{"id": "S1", "evidence": source, "body": "def add(a, b):\n    return a + b\n"}]}
        finding = {"section": "flows", "kind": kind, "statement": "add 返回 a + b。",
                   "evidence": [{"source_id": "S1", "text": "return a + b", "evidence": {
                       **source, "start_line": 2, "end_line": 2}}]}
        plan = {"gaps": [{"section": "constraints", "question": "为何选择此设计？"}]}
        spec = {"sections": ["flows"]}
        result = {"report": {"unknowns": ["尚未证明运行状态。"], "in_scope_requests": [
            {"source_id": "S1", "path": "app.py", "reason": "补读", "location": "approved_read"}],
            "scope_requests": [{"source_id": "", "path": "runtime.log", "reason": "运行证据", "location": "outside_package"}]}}
        store = CognitionStore(destination or project_states(self.state_dir, project["id"])[0])
        run_id = str(uuid4())
        with store.open(write=True) as conn, conn:
            conn.execute("""INSERT INTO cognition_runs(id,project_id,commit_id,created_at,updated_at,status,envelope,envelope_sha256,plan,config_dir)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (run_id, project["id"], commit, "2026-10-15T01:00:00+00:00",
                    "2026-10-15T01:00:00+00:00", status, canonical(envelope), digest(envelope), canonical(plan), str(self.root)))
            conn.execute("INSERT INTO cognition_findings VALUES (?,?,?,?,?,?)",
                         (run_id, "T1:F1", "T1", canonical(finding), finding_status, canonical({"supported": True})))
            conn.execute("INSERT INTO cognition_jobs VALUES (?,?,?,?,?,?)",
                         (run_id, "T1", canonical(spec), "completed", canonical(result), "2026-10-15T01:00:00+00:00"))
        return run_id

    def test_unknown_project_and_unborn_repository_do_not_create_resources(self):
        missing = self.root / "missing"
        with self.assertRaises(FileReadError) as caught:
            build_management_context(missing, str(uuid4()))
        self.assertEqual(caught.exception.code, "project_not_found")
        self.assertFalse(missing.exists())
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        before = self.snapshot()
        result = build_management_context(self.state_dir, project["id"])
        self.assertEqual(result["index"]["status"], "no_committed_target")
        self.assertEqual(result["cognition"]["coverage"]["sections_with_facts"], 0)
        self.assertEqual(before, self.snapshot())

    def test_current_memory_cognition_references_gaps_and_read_only_progress(self):
        project = self.project()
        notes = KnowledgeStore(self.state_dir)
        note = notes.add(project, "Business constraint", "Preserve addition", scope="project")
        notes.confirm(note["id"])
        draft = notes.add(project, "Draft", "Needs confirmation")
        run = self.seed(project)
        RetrievalIndex(self.state_dir).build(project)
        before = self.snapshot()
        phases = []
        with patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("No model")):
            result = build_management_context(self.state_dir, project["id"], progress=lambda e: phases.append(e["phase"]))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(phases, ["snapshot", "memory", "cognition", "index", "completed"])
        self.assertEqual([n["id"] for n in result["memory"]["applicable"]], [note["id"]])
        self.assertEqual(result["memory"]["other"][0]["id"], draft["id"])
        flows = next(s for s in result["cognition"]["sections"] if s["id"] == "flows")
        self.assertEqual(flows["facts"][0]["run_id"], run)
        self.assertEqual(flows["facts"][0]["evidence"][0]["path"], "app.py")
        self.assertEqual({g["kind"] for g in result["cognition"]["gaps"]}, {"plan_gap", "unknown", "in_scope_requests", "scope_requests"})
        self.assertEqual(result["index"]["status"], "available")
        self.assertEqual({f["path"] for f in result["index"]["files"]}, {"app.py", "README.md"})
        self.assertNotIn("return a + b", json.dumps(result["index"]))
        self.assertIn("来源：app.py", format_management_context(result))
        self.assertEqual(result["model_calls"], 0)

    def test_version_change_excludes_old_facts_snapshot_notes_and_index(self):
        project = self.project()
        notes = KnowledgeStore(self.state_dir)
        snapshot = notes.add(project, "Version memory", "Old version only")
        notes.confirm(snapshot["id"])
        persistent = notes.add(project, "Project memory", "Project decision", scope="project")
        notes.confirm(persistent["id"])
        run = self.seed(project)
        RetrievalIndex(self.state_dir).build(project)
        (self.repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        latest = self.commit("new version")
        before = self.snapshot()
        result = build_management_context(self.state_dir, project["id"])
        self.assertEqual(result["target_commit"], latest)
        self.assertEqual([n["id"] for n in result["memory"]["applicable"]], [persistent["id"]])
        self.assertEqual(result["memory"]["other"][0]["reason"], "different_version")
        self.assertEqual(result["cognition"]["coverage"]["sections_with_facts"], 0)
        self.assertEqual(result["cognition"]["historical_runs"][0]["id"], run)
        self.assertEqual(result["cognition"]["gaps"], [])
        self.assertEqual(result["index"]["status"], "no_current_version_index")
        self.assertEqual(before, self.snapshot())

    def test_managed_version_and_dirty_worktree_do_not_replace_fixed_target(self):
        project = self.project()
        self.seed(project)
        selected = project["git"]["head_commit"]
        (self.repo / "app.py").write_text("new committed contents\n")
        self.commit("advance HEAD")
        self.git("update-ref", managed_ref(project["id"]), selected)
        (self.repo / "app.py").write_text("uncommitted contents\n")
        result = build_management_context(self.state_dir, project["id"])
        self.assertEqual(result["target_commit"], selected)
        self.assertNotEqual(result["head_commit"], selected)
        self.assertTrue(result["working_tree_dirty"])
        self.assertEqual(result["cognition"]["coverage"]["sections_with_facts"], 1)
        self.assertNotIn("uncommitted contents", json.dumps(result))

    def test_inferred_invalidated_unfinished_and_broken_identity_never_become_facts(self):
        project = self.project()
        self.seed(project, kind="inferred")
        self.seed(project, finding_status="invalidated")
        uncertain = self.seed(project, status="pending_execution")
        broken = self.seed(project)
        directory = Path(project["resource_dir"])
        with CognitionStore(directory).open(write=True) as conn, conn:
            value = json.loads(conn.execute("SELECT finding FROM cognition_findings WHERE run_id=?", (broken,)).fetchone()[0])
            value["evidence"][0]["evidence"]["blob_id"] = "0" * 40
            conn.execute("UPDATE cognition_findings SET finding=? WHERE run_id=?", (canonical(value), broken))
        before = self.snapshot()
        result = build_management_context(self.state_dir, project["id"])
        flows = next(s for s in result["cognition"]["sections"] if s["id"] == "flows")
        self.assertEqual(flows["facts"], [])
        self.assertEqual(len(flows["pending"]), 4)
        self.assertEqual(result["cognition"]["todos"][0]["id"], uncertain)
        self.assertEqual(result["cognition"]["todos"][0]["action"], "inspect_saved_requests_before_explicit_recovery")
        self.assertEqual(before, self.snapshot())

    def test_legacy_and_local_records_and_index_are_visible_without_copying(self):
        project = self.project(workspace=False)
        notes = KnowledgeStore(self.state_dir)
        old = notes.add(project, "Old", "Original confirmed memory", scope="project")
        notes.confirm(old["id"])
        legacy_run = self.seed(project)
        RetrievalIndex(self.state_dir).build(project)
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        local = notes.add(project, "Local", "Current local memory", scope="project")
        notes.confirm(local["id"])
        local_run = self.seed(project)
        before = self.snapshot()
        result = build_management_context(self.state_dir, project["id"])
        self.assertEqual({n["id"] for n in result["memory"]["applicable"]}, {old["id"], local["id"]})
        self.assertEqual({r["id"] for r in result["cognition"]["current_runs"]}, {legacy_run, local_run})
        self.assertEqual(result["index"]["status"], "available")
        self.assertFalse((Path(project["resource_dir"]) / "index.sqlite3").exists())
        self.assertEqual(before, self.snapshot())

    def test_inventory_is_not_limited_by_history_100_or_context_three(self):
        project = self.project()
        notes = KnowledgeStore(self.state_dir)
        first = notes.add(project, "Original", "All entries remain visible", scope="project")
        notes.confirm(first["id"])
        directory = Path(project["resource_dir"])
        with closing(sqlite3.connect(directory / "knowledge.sqlite3")) as conn, conn:
            for number in range(105):
                conn.execute("""INSERT INTO notes SELECT ?,project_id,?,body,kind,scope,observed_commit,status,
                    created_at,updated_at,confirmed_at,invalidation_reason FROM notes WHERE id=?""",
                             (str(uuid4()), f"Memory {number}", first["id"]))
        run = self.seed(project)
        with CognitionStore(directory).open(write=True) as conn, conn:
            for _ in range(105):
                conn.execute("""INSERT INTO cognition_runs(id,project_id,commit_id,created_at,updated_at,status,envelope,envelope_sha256,plan,config_dir)
                    SELECT ?,project_id,commit_id,created_at,updated_at,status,envelope,envelope_sha256,plan,config_dir
                    FROM cognition_runs WHERE id=?""", (str(uuid4()), run))
        result = build_management_context(self.state_dir, project["id"])
        self.assertEqual(len(result["memory"]["applicable"]), 106)
        self.assertEqual(len(result["cognition"]["current_runs"]), 106)
        self.assertFalse(result["records_truncated"])

    def test_corrupt_or_duplicate_records_fail_without_repairing_sources(self):
        project = self.project()
        self.seed(project)
        database = Path(project["resource_dir"]) / "cognition.sqlite3"
        saved = database.read_bytes()
        (self.state_dir / "cognition.sqlite3").write_bytes(saved)
        before = self.snapshot()
        with self.assertRaises(FileReadError) as caught:
            build_management_context(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "resource_record_conflict")
        self.assertEqual(before, self.snapshot())
        (self.state_dir / "cognition.sqlite3").unlink()
        with CognitionStore(project["resource_dir"]).open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_runs SET envelope_sha256='broken'")
        before = self.snapshot()
        with self.assertRaises(FileReadError) as caught:
            build_management_context(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "cognition_record_corrupt")
        self.assertEqual(before, self.snapshot())

    def test_other_project_memory_findings_and_investigation_queue_are_excluded(self):
        first = self.project()
        other_root = self.root / "second-project"
        self.git("clone", str(self.repo), str(other_root))
        second = ProjectRegistry(self.state_dir).register(inspect_repository(other_root), workspace=True)
        notes = KnowledgeStore(self.state_dir)
        foreign = notes.add(second, "Second project", "DO_NOT_MIX_PROJECTS", scope="project")
        notes.confirm(foreign["id"])
        self.seed(second, status="awaiting_approval")
        before = self.snapshot()
        result = build_management_context(self.state_dir, first["id"])
        self.assertEqual(result["memory"]["applicable"], [])
        self.assertEqual(result["cognition"]["current_runs"], [])
        self.assertEqual(result["cognition"]["todos"], [])
        self.assertNotIn("DO_NOT_MIX_PROJECTS", json.dumps(result))
        self.assertEqual(before, self.snapshot())

    def test_index_corruption_and_sensitive_memory_are_not_silently_hidden(self):
        project = self.project()
        RetrievalIndex(self.state_dir).build(project)
        db = Path(project["resource_dir"]) / "index.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE indexes SET chunks='[]'")
        with self.assertRaises(FileReadError) as caught:
            build_management_context(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "index_corrupted")
        db.unlink()
        notes = KnowledgeStore(self.state_dir)
        notes.add(project, "Safe", "Original body")
        with closing(sqlite3.connect(Path(project["resource_dir"]) / "knowledge.sqlite3")) as conn, conn:
            conn.execute("UPDATE notes SET body=?", ("sk-" + "x" * 40,))
        with self.assertRaises(FileReadError) as caught:
            build_management_context(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "sensitive_knowledge")

    def test_changed_selected_version_is_rejected_before_output(self):
        project = self.project()
        before = self.snapshot()
        from zest import management_context
        original = management_context.selected_commit
        count = 0
        def moving(*args, **kwargs):
            nonlocal count
            count += 1
            return original(*args, **kwargs) if count == 1 else "f" * 40
        with patch("zest.management_context.selected_commit", side_effect=moving):
            with self.assertRaises(FileReadError) as caught:
                build_management_context(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "management_target_changed")
        self.assertEqual(before, self.snapshot())

    def test_cli_outputs_human_summary_json_and_real_phase_feedback(self):
        project = self.project()
        before = self.snapshot()
        code, out, err = self.cli("project", "context", project["id"], "--format", "text", "--progress")
        self.assertEqual(code, 0, err)
        self.assertIn("项目管理上下文（只读）", out)
        self.assertIn("尚无同版本自动核对事实", out)
        self.assertIn("读取项目记忆", err)
        code, out, err = self.cli("project", "context", project["id"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["target_commit"], project["git"]["head_commit"])
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
    def test_offline_cognition_production_records_are_usable_as_management_context(self):
        project = self.project()
        model = OfflineCognitionModel()
        settings = model.settings
        store = CognitionStore(self.state_dir)
        run = store.create(project["id"], target={"provider": settings.provider, "model": settings.name, "base_url": settings.base_url})
        store.decide(run["id"], approved=True, approval_sha256=run["approval_sha256"])
        self.assertEqual(CognitionEngine(self.state_dir, model=model).run(run["id"])["status"], "completed")
        before = self.snapshot()
        calls = len(model.calls)
        context = build_management_context(self.state_dir, project["id"])
        self.assertEqual(context["cognition"]["coverage"]["sections_with_facts"], 6)
        self.assertEqual(len(model.calls), calls)
        self.assertEqual(before, self.snapshot())
