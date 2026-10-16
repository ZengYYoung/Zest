import copy
import json
import os
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository, managed_ref
from zest.index import RetrievalIndex
from zest.qa import context_budget, prepare_context
from zest.storage import ProjectRegistry


class QAContextSelectionTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def project(self):
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        RetrievalIndex(self.state_dir).build(project)
        return project

    def test_larger_configurable_budget_recovers_evidence_without_a_model(self):
        (self.repo / "README.md").write_text("project information " * 2000)
        project = self.project()
        with patch.dict(os.environ, {"ZEST_QA_CONTEXT_CHARS": "128000"}):
            self.assertEqual(context_budget(), 128000)
            context = prepare_context(project, "说明项目")
        self.assertTrue(context["sources"])
        self.assertGreater(len(json.dumps(context["sources"], ensure_ascii=False)), 24000)
        with patch.dict(os.environ, {"ZEST_QA_CONTEXT_CHARS": "24000"}):
            small = prepare_context(project, "说明项目")
        self.assertEqual(small["sources"], [])
        self.assertIn("context_budget", {w["code"] for w in small["warnings"]})
        for value in ("0", "-1", "1000001", "not-a-number", "1.5"):
            with self.subTest(value=value), patch.dict(os.environ, {"ZEST_QA_CONTEXT_CHARS": value}):
                with self.assertRaises(FileReadError) as caught:
                    prepare_context(project, "说明项目")
                self.assertEqual(caught.exception.code, "invalid_qa_context_budget")

    def test_dotenv_budget_does_not_load_model_settings_or_interpolate(self):
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.dotenv_values", return_value={"ZEST_QA_CONTEXT_CHARS": "64000"}) as values:
            with patch("zest.qa.ModelSettings.from_env", side_effect=AssertionError("no model configuration")):
                self.assertEqual(context_budget(), 64000)
            self.assertFalse(values.call_args.kwargs["interpolate"])

    def test_compact_cognition_keeps_original_windows_and_binding_without_mutation(self):
        (self.repo / "app.py").write_text("def compute():\n    return 7\n")
        project = self.project()
        proof = {"source_id": "S1", "evidence": {"path": "app.py", "commit": project["git"]["head_commit"], "blob_id": "fixed-blob", "content_sha256": "fixed-content", "start_line": 1, "end_line": 2},
                 "lines": [{"number": 1, "text": "def compute():"}, {"number": 2, "text": "    return 7"}], "text": "def compute():\n    return 7", "more_after": False}
        note = {"id": "T1:F1", "run_id": "saved-run", "commit": project["git"]["head_commit"], "finding_sha256": "finding-and-review-hash", "body": "compute返回7。",
                "evidence": [proof, {**proof, "source_id": "S2"}], "review": {"reason": "long audit reason " * 1000}}
        original = copy.deepcopy(note)
        with patch("zest.cognition_store.CognitionStore.for_context", return_value=[note]):
            context = prepare_context(project, "返回什么？", state_dir=self.state_dir)
        source = context["sources"][0]
        self.assertEqual(source["body"], note["body"])
        self.assertEqual(source["evidence"]["finding_sha256"], note["finding_sha256"])
        self.assertNotIn("review", source)
        self.assertEqual(len(source["provenance"]), 1)
        self.assertEqual(source["provenance"][0]["lines"], proof["lines"])
        self.assertEqual(source["provenance"][0]["evidence"], proof["evidence"])
        self.assertNotIn("text", source["provenance"][0])
        self.assertEqual(note, original)

    def test_exact_implementation_precedes_repeated_examples_and_merges_windows(self):
        marker = self.root / "must-not-execute"
        (self.repo / "views.py").write_text(f"open({str(marker)!r}, 'w').write('bad')\n@route('/article')\ndef make_article():\n    save_article()\n    return 'created'\n")
        (self.repo / "examples.py").write_text("# make_article() example\n" * 100)
        project = self.project()
        index_before = (self.state_dir / "index.sqlite3").read_bytes()
        context = prepare_context(project, "make_article 怎样保存和返回？", state_dir=self.state_dir, retrieval="lexical")
        native = [s for s in context["sources"] if "kind" not in s["evidence"]]
        self.assertEqual(native[0]["evidence"]["path"], "views.py")
        self.assertTrue(any(line["text"] == "    save_article()" for line in native[0]["lines"]))
        self.assertTrue(any(line["text"].startswith("@route") for line in native[0]["lines"]))
        self.assertEqual(sum(s["evidence"]["path"] == "examples.py" for s in native), 1)
        self.assertEqual(index_before, (self.state_dir / "index.sqlite3").read_bytes())
        self.assertFalse(marker.exists())

    def test_qualified_method_uses_selected_version_and_reports_long_windows(self):
        (self.repo / "profile.py").write_text("class Profile:\n    def follow(self):\n" + "        value = 1\n" * 210 + "        return 'old'\n")
        project = self.project()
        original = project["git"]["head_commit"]
        (self.repo / "profile.py").write_text("class Profile:\n    def follow(self): return 'new'\n")
        self.commit("new version")
        self.git("update-ref", managed_ref(project["id"]), original)
        context = prepare_context(project, "Profile.follow 如何处理？", state_dir=self.state_dir, retrieval="lexical")
        source = context["sources"][0]
        self.assertEqual(context["commit"], original)
        self.assertEqual(source["evidence"]["commit"], original)
        self.assertLessEqual(len(source["lines"]), 200)
        self.assertNotIn("'new'", json.dumps(context["sources"]))
        self.assertIn("symbol_context_partial", {w["code"] for w in context["warnings"]})

    def test_missing_index_is_not_bypassed_by_symbol_lookup(self):
        (self.repo / "app.py").write_text("def compute(): return 7\n")
        project = self.project()
        (self.state_dir / "index.sqlite3").unlink()
        with self.assertRaises(FileReadError) as caught:
            prepare_context(project, "compute如何计算？", state_dir=self.state_dir, retrieval="lexical")
        self.assertEqual(caught.exception.code, "index_missing")

    def test_prepared_approval_keeps_exact_saved_sources_after_budget_change(self):
        from tests.test_tasks import FixtureModel
        from zest.tasks import TaskRunner
        (self.repo / "app.py").write_text("def compute(): return 7\n")
        project = self.project()
        model = FixtureModel()
        runner = TaskRunner(self.state_dir, model=model)
        with patch.dict(os.environ, {"ZEST_QA_CONTEXT_CHARS": "128000"}):
            task = runner.create(project, "compute返回什么？", retrieval="lexical")
        with patch.dict(os.environ, {"ZEST_QA_CONTEXT_CHARS": "1"}):
            result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["context"], task["context"])
        self.assertEqual(model.calls[0]["sources"], task["context"]["sources"])
