"""Offline incremental package/approval/evidence tests, not model quality tests."""

import importlib.util
import json
import unittest
from unittest.mock import patch

from tests import test_cognition_refresh as refresh_fixtures
from tests.test_cognition import OfflineCognitionModel
from zest.cognition import CognitionEngine
from zest.cognition_context import material_catalog
from zest.cognition_refresh import prepare_refresh
from zest.cognition_store import CognitionStore
from zest.cognition_tools import PackageTools
from zest.files import FileReadError
from zest.survey import SurveyStore


@unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
class RefreshPrepareTest(unittest.TestCase):
    setUp = refresh_fixtures.CognitionRefreshTest.setUp
    git = refresh_fixtures.CognitionRefreshTest.git
    commit = refresh_fixtures.CognitionRefreshTest.commit
    cli = refresh_fixtures.CognitionRefreshTest.cli
    project = refresh_fixtures.CognitionRefreshTest.project
    baseline = refresh_fixtures.CognitionRefreshTest.baseline
    mutate = refresh_fixtures.CognitionRefreshTest.mutate

    def changed(self):
        project, old = self.project()
        run = self.baseline(project, old, proofs=("app.py", "README.md", "old.txt"))
        (self.repo / "README.md").write_text("# Example\nNew documentation.\n")
        (self.repo / "old.txt").unlink()
        (self.repo / "new.py").write_text("NEW = True\n")
        new = self.commit("new version")
        return project, old, new, run

    def target(self):
        settings = OfflineCognitionModel().settings
        return {"provider": settings.provider, "model": settings.name, "base_url": settings.base_url}

    def prepare(self, project, **kwargs):
        return prepare_refresh(self.state_dir, project["id"], target=self.target(), **kwargs)

    def test_preparation_freezes_new_package_without_model_or_knowledge_migration(self):
        project, old, new, base_run = self.changed()
        (self.repo / "app.py").write_text("DIRTY = True\n")
        model = OfflineCognitionModel()
        phases = []
        store = CognitionStore(self.state_dir)
        with patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("no transmission")):
            record = self.prepare(project, progress=lambda e: phases.append(e["phase"]))
        full = store.show(record["id"], include_envelope=True, include_calls=True)
        envelope = full["envelope"]
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertEqual(record["commit"], new)
        self.assertEqual(full["calls"], [])
        self.assertEqual(record["jobs"], [])
        self.assertEqual(record["findings"], [])
        self.assertEqual(envelope["shared_knowledge"], [])
        self.assertEqual(record["refresh"]["baseline_commit"], old)
        context = envelope["refresh_context"]
        self.assertEqual(context["baseline"]["run_id"], base_run)
        self.assertEqual(len(context["historical_leads"]), 3)
        self.assertNotIn('"text":', json.dumps(context))
        self.assertTrue(all(f["needs_review"] for f in context["historical_leads"]))
        self.assertTrue(all(s["evidence"]["commit"] == new for s in envelope["files"]))
        self.assertNotIn("DIRTY", json.dumps(envelope))
        self.assertEqual({s["evidence"]["path"] for s in envelope["files"]}, {"README.md", "app.py", "new.py"})
        self.assertEqual([f["path"] for f in context["focus"] if f["availability"] == "deleted"], ["old.txt"])
        self.assertEqual(material_catalog(envelope)[0]["path"], "README.md")
        self.assertIn("survey_inventory", phases)
        self.assertEqual(phases[-1], "prepared")
        with self.assertRaises(FileReadError):
            CognitionEngine(self.state_dir, model=model).run(record["id"])
        self.assertEqual(model.calls, [])
        self.assertEqual(store.for_context(project["id"], new), [])
        self.assertEqual(len(store.for_context(project["id"], old)), 3)

    def test_approval_hash_includes_historical_context_and_cannot_use_old_approval(self):
        project, old, new, base_run = self.changed()
        record = self.prepare(project)
        store = CognitionStore(self.state_dir)
        with store.open() as conn:
            old_hash = conn.execute("SELECT envelope_sha256 FROM cognition_runs WHERE id=?", (base_run,)).fetchone()[0]
        with self.assertRaises(FileReadError) as caught:
            store.decide(record["id"], approved=True, approval_sha256=old_hash)
        self.assertEqual(caught.exception.code, "approval_mismatch")
        store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        self.assertEqual(store.show(record["id"])["status"], "queued")

    def test_missing_or_same_version_baseline_does_not_create_new_run(self):
        project, old = self.project()
        with self.assertRaises(FileReadError):
            self.prepare(project)
        self.assertFalse((self.state_dir / "cognition.sqlite3").exists())
        self.baseline(project, old)
        with self.assertRaises(FileReadError) as caught:
            self.prepare(project)
        self.assertEqual(caught.exception.code, "cognition_refresh_not_needed")
        with CognitionStore(self.state_dir).open() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM cognition_runs").fetchone()[0], 1)

    def test_target_movement_during_survey_does_not_create_approval(self):
        project, old, new, base_run = self.changed()
        original = SurveyStore.build
        def moving(store, *args, **kwargs):
            survey = original(store, *args, **kwargs)
            (self.repo / "extra.py").write_text("X = 1\n")
            self.commit("moved")
            return survey
        with patch.object(SurveyStore, "build", moving):
            with self.assertRaises(FileReadError) as caught:
                self.prepare(project)
        self.assertEqual(caught.exception.code, "cognition_refresh_target_changed")
        with CognitionStore(self.state_dir).open() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM cognition_runs").fetchone()[0], 1)

    def test_changed_binary_is_visible_but_remains_outside_package(self):
        project, old, new, base_run = self.changed()
        (self.repo / "binary.dat").write_bytes(b"\x00private binary\x00")
        self.commit("binary")
        record = self.prepare(project)
        envelope = CognitionStore(self.state_dir).show(record["id"], include_envelope=True)["envelope"]
        focus = next(f for f in envelope["refresh_context"]["focus"] if f["path"] == "binary.dat")
        self.assertEqual(focus["availability"], "outside_package")
        self.assertIsNone(focus["source_id"])
        self.assertEqual(record["refresh"]["outside_package"], 1)
        self.assertIn("changed_material_outside_package", record["scope_review"]["reasons"])
        with self.assertRaises(FileReadError):
            PackageTools(envelope).read("binary.dat")

    def assert_stale_baseline_blocks(self, change):
        project, old, new, base_run = self.changed()
        record = self.prepare(project)
        store = CognitionStore(self.state_dir)
        store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        if change == "invalidated":
            with store.open(write=True) as conn, conn:
                conn.execute("UPDATE cognition_findings SET status='invalidated' WHERE run_id=?", (base_run,))
        else:
            self.mutate(base_run, "T1:F1", lambda value: value.update(statement="Changed old claim"))
        model = OfflineCognitionModel()
        result = CognitionEngine(self.state_dir, model=model).run(record["id"])
        self.assertEqual(result["status"], "failed", result["error"])
        self.assertEqual(result["error"]["code"], "cognition_refresh_baseline_changed")
        self.assertEqual(model.calls, [])

    def test_invalidated_baseline_blocks_first_call(self):
        self.assert_stale_baseline_blocks("invalidated")

    def test_edited_baseline_blocks_first_call(self):
        self.assert_stale_baseline_blocks("statement")

    def test_approved_flow_passes_leads_to_planner_and_workers_but_rereads_new_commit(self):
        project, old, new, base_run = self.changed()
        store = CognitionStore(self.state_dir)
        record = self.prepare(project)
        store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        model = OfflineCognitionModel()
        result = CognitionEngine(self.state_dir, model=model).run(record["id"])
        self.assertEqual(result["status"], "completed", result["error"])
        for purpose, payload in model.calls:
            if purpose != "project_review":
                self.assertEqual(payload["refresh_context"]["baseline"]["commit"], old)
                self.assertEqual(payload["shared_knowledge"], [])
        self.assertEqual(len(result["findings"]), 6)
        for finding in result["findings"]:
            self.assertEqual(finding["status"], "auto_reviewed")
            self.assertTrue(all(p["evidence"]["commit"] == new for p in finding["finding"]["evidence"]))
        self.assertEqual(len(store.for_context(project["id"], new)), 3)

    def test_baseline_invalidation_after_planning_blocks_subsequent_calls(self):
        project, old, new, base_run = self.changed()
        store = CognitionStore(self.state_dir)
        record = self.prepare(project)
        store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        model = OfflineCognitionModel()
        original = model.complete
        def invalidate_after_plan(payload, *, purpose="qa"):
            response = original(payload, purpose=purpose)
            if purpose == "project_plan":
                with store.open(write=True) as conn, conn:
                    conn.execute("UPDATE cognition_findings SET status='invalidated' WHERE run_id=?", (base_run,))
            return response
        model.complete = invalidate_after_plan
        result = CognitionEngine(self.state_dir, model=model).run(record["id"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["call_count"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "cognition_refresh_baseline_changed")
        self.assertEqual(result["findings"], [])

    def test_historical_lead_does_not_supply_body_citation(self):
        project, old, new, base_run = self.changed()
        store = CognitionStore(self.state_dir)
        record = self.prepare(project)
        store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        model = OfflineCognitionModel()
        original = model.complete
        def fake_old_citation(payload, *, purpose="qa"):
            if purpose in ("project_research", "project_report"):
                # A historical statement cannot manufacture a current R1.
                return {"text": json.dumps({"actions": [], "report": {"status": "completed", "findings": [
                    {"section": "flows", "kind": "observed", "statement": "旧结论直接迁移。", "citations": ["R1"]}],
                    "unknowns": [], "material_requests": []}})}
            return original(payload, purpose=purpose)
        model.complete = fake_old_citation
        result = CognitionEngine(self.state_dir, model=model).run(record["id"])
        self.assertEqual(result["status"], "partial", result["error"])
        self.assertEqual(result["findings"], [])
        self.assertTrue(all(job["result"]["report_validation"]["rejected_findings"] for job in result["jobs"]))
        self.assertEqual(store.for_context(project["id"], new), [])

    def test_cli_progress_and_readable_summary_do_not_start_worker(self):
        project, old, new, base_run = self.changed()
        with patch("zest.models.ModelSettings.from_env", return_value=OfflineCognitionModel().settings), \
             patch("zest.cognition.start_worker", side_effect=AssertionError("no worker")):
            code, out, err = self.cli("cognition", "refresh-create", project["id"], "--progress")
        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(record["status"], "awaiting_approval")
        self.assertIn("等待批准", err)
        self.assertEqual(record["call_count"], 0)
        from zest.cognition_views import format_cognition
        self.assertIn("增量调查", format_cognition(record))
        self.assertIn("旧结论不直接迁移", format_cognition(record))


if __name__ == "__main__":
    unittest.main()
