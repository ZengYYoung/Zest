"""Read-only committed-version comparisons; no AI dependencies or model calls."""

import hashlib
import json
import sqlite3
from contextlib import closing
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_projects as fixtures
from zest.cognition_refresh import build_refresh_preview, format_refresh_preview
from zest.cognition_store import CognitionStore, canonical, digest
from zest.files import FileReadError, _find_file, load_text_blob
from zest.git import compare_commits, inspect_repository, managed_ref
from zest.storage import ProjectRegistry


class CognitionRefreshTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def project(self):
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        (self.repo / "README.md").write_text("# Example\n")
        (self.repo / "old.txt").write_text("old content\n")
        commit = self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        return project, commit

    def baseline(self, project, commit, *, status="completed", proofs=("app.py",), created="2026-10-15T01:00:00+00:00"):
        sources = []
        for index, path in enumerate(("app.py", "README.md", "old.txt"), 1):
            entry = _find_file(self.repo, commit, path.split("/"))
            raw = load_text_blob(self.repo, entry)
            sources.append({"id": f"S{index}", "body": raw.decode(), "evidence": {
                "path": path, "commit": commit, "blob_id": entry.object_id,
                "content_sha256": hashlib.sha256(raw).hexdigest()}})
        run = str(uuid4())
        envelope = {"project_id": project["id"], "commit": commit, "files": sources}
        store = CognitionStore(self.state_dir)
        with store.open(write=True) as conn, conn:
            conn.execute("INSERT INTO cognition_runs(id,project_id,commit_id,created_at,updated_at,status,envelope,envelope_sha256,config_dir) VALUES (?,?,?,?,?,?,?,?,?)",
                         (run, project["id"], commit, created, created, status, canonical(envelope), digest(envelope), str(self.root)))
            for index, path in enumerate(proofs, 1):
                source = next(s for s in sources if s["evidence"]["path"] == path)
                finding = {"section": "flows", "kind": "observed", "statement": f"固定版本的 {path} 观察。",
                           "evidence": [{"source_id": source["id"], "text": source["body"],
                                         "evidence": {**source["evidence"], "start_line": 1, "end_line": 2}}]}
                conn.execute("INSERT INTO cognition_findings VALUES (?,?,?,?,?,?)",
                             (run, f"T1:F{index}", "T1", canonical(finding), "auto_reviewed", "{}"))
        return run

    def mutate(self, run, finding_id, transform):
        with CognitionStore(self.state_dir).open(write=True) as conn, conn:
            value = json.loads(conn.execute("SELECT finding FROM cognition_findings WHERE run_id=? AND id=?", (run, finding_id)).fetchone()[0])
            transform(value)
            conn.execute("UPDATE cognition_findings SET finding=? WHERE run_id=? AND id=?", (canonical(value), run, finding_id))

    def test_no_baseline_and_unknown_project_never_initialize_cognition_or_state(self):
        missing = self.root / "missing-state"
        with self.assertRaises(FileReadError):
            build_refresh_preview(missing, str(uuid4()))
        self.assertFalse(missing.exists())
        project, commit = self.project()
        result = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(result["status"], "no_baseline")
        self.assertEqual(result["target_commit"], commit)
        self.assertFalse((self.state_dir / "cognition.sqlite3").exists())
        self.assertEqual(result["model_calls"], 0)

    def test_unborn_repository_has_no_target_and_no_cognition_database(self):
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        result = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(result["status"], "no_committed_target")
        self.assertFalse((self.state_dir / "cognition.sqlite3").exists())

    def test_added_changed_deleted_files_map_evidence_without_rewriting_state(self):
        project, old = self.project()
        self.baseline(project, old, proofs=("app.py", "README.md", "old.txt"))
        (self.repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "old.txt").unlink()
        (self.repo / "new.txt").write_text("new\n")
        new = self.commit("changed")
        before = {p.name: p.read_bytes() for p in self.state_dir.glob("*.sqlite3")}
        phases = []
        with patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("no model")):
            result = build_refresh_preview(self.state_dir, project["id"], progress=lambda e: phases.append(e["phase"]))
        self.assertEqual(result["status"], "refresh_needed")
        self.assertEqual(result["target_commit"], new)
        self.assertEqual({c["path"]: c["change"] for c in result["changes"]},
                         {"app.py": "modified", "old.txt": "deleted", "new.txt": "added"})
        self.assertEqual(result["counts"]["directly_affected"], 2)
        self.assertEqual(result["counts"]["evidence_unchanged"], 1)
        self.assertEqual([c["path"] for c in result["uncovered_changes"]], ["new.txt"])
        self.assertTrue(all(f["needs_review"] for f in result["findings"]))
        self.assertEqual(phases, ["snapshot", "baseline", "compare", "evidence", "completed"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state_dir.glob("*.sqlite3")})
        self.assertNotIn("return a - b", json.dumps(result))
        self.assertEqual(CognitionStore(self.state_dir).for_context(project["id"], new), [])

    def test_managed_version_takes_precedence_and_dirty_content_is_excluded(self):
        project, old = self.project()
        self.baseline(project, old)
        (self.repo / "app.py").write_text("print('new')\n")
        new = self.commit("new")
        self.git("update-ref", managed_ref(project["id"]), old)
        (self.repo / "README.md").write_text("DIRTY BODY MUST NOT BE READ")
        result = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(result["status"], "same_version")
        self.assertEqual(result["target_commit"], old)
        self.assertEqual(result["head_commit"], new)
        self.assertTrue(result["working_tree_dirty"])
        self.assertNotIn("DIRTY BODY", json.dumps(result))
        self.assertFalse(result["findings"][0]["needs_review"])

    def test_all_finished_runs_at_baseline_included_pending_failed_and_invalidated_excluded(self):
        project, old = self.project()
        first = self.baseline(project, old)
        second = self.baseline(project, old, status="partial", proofs=("README.md",))
        self.baseline(project, old, status="failed")
        self.baseline(project, old, status="investigating")
        with CognitionStore(self.state_dir).open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_findings SET status='invalidated' WHERE run_id=?", (first,))
            conn.execute("UPDATE cognition_findings SET status='needs_investigation' WHERE run_id=?", (second,))
        result = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(len(result["baseline_runs"]), 2)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["run_id"], second)
        self.assertTrue(result["findings"][0]["needs_review"])
        self.assertIn("baseline_contains_partial_investigations", result["warnings"])

    def test_missing_or_mismatched_proof_is_unresolved(self):
        project, old = self.project()
        run = self.baseline(project, old, proofs=("app.py", "README.md", "old.txt", "app.py"))
        self.mutate(run, "T1:F1", lambda f: f.update(evidence=[]))
        self.mutate(run, "T1:F2", lambda f: f["evidence"][0]["evidence"].update(content_sha256="wrong"))
        self.mutate(run, "T1:F3", lambda f: f["evidence"][0].update(text=""))
        self.mutate(run, "T1:F4", lambda f: f["evidence"][0]["evidence"].pop("end_line"))
        result = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(result["counts"]["unresolved"], 4)
        self.assertTrue(all(f["needs_review"] for f in result["findings"]))

    def test_explicit_baseline_scope_validation_and_current_version_preference(self):
        project, old = self.project()
        first = self.baseline(project, old)
        (self.repo / "app.py").write_text("new\n")
        new = self.commit("new")
        self.git("update-ref", managed_ref(project["id"]), old)
        # Later investigation of another commit must not hide an existing baseline
        # of the currently selected version.
        self.baseline(project, new, created="2026-10-15T02:00:00+00:00")
        default = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(default["baseline"]["run_id"], first)
        self.assertEqual(default["status"], "same_version")
        other = self.baseline({**project, "id": str(uuid4())}, old)
        for run, code in ((other, "cognition_scope_mismatch"),
                          (self.baseline(project, old, status="queued"), "cognition_baseline_not_ready")):
            with self.assertRaises(FileReadError) as caught:
                build_refresh_preview(self.state_dir, project["id"], base_run_id=run)
            self.assertEqual(caught.exception.code, code)

    def test_deleted_commit_is_reported_without_inventing_a_diff(self):
        project, old = self.project()
        run = self.baseline(project, old)
        with CognitionStore(self.state_dir).open(write=True) as conn, conn:
            row = conn.execute("SELECT envelope FROM cognition_runs WHERE id=?", (run,)).fetchone()
            envelope = json.loads(row[0])
            envelope["commit"] = "f" * 40
            conn.execute("UPDATE cognition_runs SET commit_id=?,envelope=?,envelope_sha256=? WHERE id=?",
                         (envelope["commit"], canonical(envelope), digest(envelope), run))
        result = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(result["status"], "baseline_unavailable")
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["counts"]["unresolved"], 1)

    def test_rename_mode_and_symlink_changes_use_metadata_without_reading_targets(self):
        project, old = self.project()
        self.baseline(project, old, proofs=("old.txt", "app.py"))
        self.git("mv", "old.txt", "renamed\nfile.txt")
        (self.repo / "app.py").unlink()
        outside = self.root / "outside-secret"
        outside.write_text("PRIVATE TARGET BODY")
        (self.repo / "app.py").symlink_to(outside)
        (self.repo / "README.md").chmod(0o755)
        new = self.commit("metadata")
        changes = {c["path"]: c for c in compare_commits(self.repo, old, new)}
        self.assertEqual(changes["app.py"]["change"], "type_changed")
        self.assertEqual(changes["old.txt"]["change"], "deleted")
        self.assertEqual(changes["renamed\nfile.txt"]["change"], "added")
        self.assertEqual(changes["README.md"]["before_object"], changes["README.md"]["after_object"])
        result = build_refresh_preview(self.state_dir, project["id"])
        self.assertNotIn("PRIVATE TARGET BODY", json.dumps(result))
        self.assertEqual(result["counts"]["directly_affected"], 2)

    def test_diff_drivers_are_not_executed_and_large_change_sets_are_complete(self):
        project, old = self.project()
        self.baseline(project, old)
        marker = self.root / "driver-ran"
        self.git("config", "diff.external", f"touch '{marker}'")
        for index in range(3):
            (self.repo / f"new-{index}.txt").write_text("new\n")
        self.commit("many")
        with patch.dict("zest.cognition_refresh.POLICY", {"large_scope_files": 2}):
            result = build_refresh_preview(self.state_dir, project["id"])
        self.assertFalse(marker.exists())
        self.assertTrue(result["requires_discussion"])
        self.assertEqual(len(result["changes"]), 3)

    def test_moving_selected_version_is_detected_before_returning(self):
        project, old = self.project()
        self.baseline(project, old)

        def moving(*args):
            result = compare_commits(*args)
            (self.repo / "new.txt").write_text("new\n")
            self.commit("moved")
            return result

        with patch("zest.cognition_refresh.compare_commits", side_effect=moving):
            with self.assertRaises(FileReadError) as caught:
                build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "cognition_refresh_target_changed")

    def test_cli_json_progress_and_text_are_readonly_and_need_no_model_config(self):
        project, old = self.project()
        self.baseline(project, old)
        (self.repo / "app.py").write_text("new\n")
        self.commit("new")
        before = {p.name: p.read_bytes() for p in self.state_dir.glob("*.sqlite3")}
        code, out, err = self.cli("cognition", "refresh-preview", project["id"], "--progress")
        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(record["status"], "refresh_needed")
        self.assertIn("关联已有原文证据", err)
        code, out, err = self.cli("cognition", "refresh-preview", project["id"], "--format", "text")
        self.assertEqual(code, 0, err)
        self.assertIn("跨版本", format_refresh_preview(record))
        self.assertIn("证据文件变化", out)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state_dir.glob("*.sqlite3")})

    def test_legacy_registration_is_read_without_schema_migration_or_model_settings(self):
        project, _ = self.project()
        path = ProjectRegistry(self.state_dir).db_path
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("ALTER TABLE projects DROP COLUMN mode")
        before = path.read_bytes()
        with patch("zest.models.ModelSettings.from_env", side_effect=AssertionError("no credentials")):
            result = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(result["status"], "no_baseline")
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as conn:
            self.assertNotIn("mode", [row[1] for row in conn.execute("PRAGMA table_info(projects)")])

    def test_corrupt_or_sensitive_finding_fails_without_printing_partial_preview(self):
        project, old = self.project()
        run = self.baseline(project, old)
        self.mutate(run, "T1:F1", lambda f: f.update(statement="sk-" + "a" * 32))
        with self.assertRaises(FileReadError) as caught:
            build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "sensitive_refresh_preview")
        self.mutate(run, "T1:F1", lambda f: f.update(statement=None))
        with self.assertRaises(FileReadError) as caught:
            build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(caught.exception.code, "cognition_record_corrupt")
