"""Initialization orchestration with offline research; no external model calls."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from tests import test_projects as fixtures
from tests.test_cognition import OfflineCognitionModel
from zest.cognition_store import CognitionStore
from zest.conversations import ConversationStore
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.initialization import InitializationStore
from zest.model_settings import ModelSettingsStore, model_settings_scope
from zest.models import ModelSettings
from zest.resources import project_states
from zest.storage import ProjectRegistry
from zest.web import create_app
from zest.web_commands import execute


class InitializationTest(unittest.TestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def setUp(self):
        self.setUpBase()
        (self.repo / "README.md").write_text("# Addition project\n用于离线初始化测试。\n")
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        self.store = InitializationStore(self.state_dir)
        self.model = OfflineCognitionModel()
        self.configuration = patch("zest.models.ModelSettings.from_env", return_value=self.model.settings)
        self.configuration.start()
        self.addCleanup(self.configuration.stop)

    def prepare(self):
        return self.store.prepare(self.project["id"])

    def approve(self):
        ready = self.prepare()
        with patch.object(InitializationStore, "launch"):
            return self.store.decide(self.project["id"], approved=True, sha256=ready["research"]["approval_sha256"])

    def finish(self):
        self.approve()
        return self.store.run(self.project["id"], model=self.model)

    def test_prepare_is_local_repeatable_and_builds_docs_index_and_exact_scope(self):
        ready = self.prepare()
        self.assertEqual(ready["status"], "awaiting_approval")
        self.assertTrue((self.repo / ".zest/docs/index.md").is_file())
        self.assertEqual(ready["index"]["commit"], ready["commit"])
        full = CognitionStore(self.state_dir).show(ready["run_id"], include_envelope=True)
        self.assertTrue(any("return a + b" in f["body"] for f in full["envelope"]["files"]))
        self.assertIn("参数", full["envelope"]["initialization_goals"]["flows"])
        self.assertEqual(self.prepare()["run_id"], ready["run_id"])
        self.assertEqual(len(CognitionStore(self.state_dir).list_runs(self.project["id"])), 1)
        self.assertEqual(self.model.calls, [])
        self.assertNotIn(self.model.settings.api_key, json.dumps(ready))
        directory = project_states(self.state_dir, self.project["id"])[0]
        self.assertEqual((directory / "initialization.json").stat().st_mode & 0o777, 0o600)

    def test_full_initialization_publishes_seven_docs_and_feeds_first_natural_question(self):
        finished = self.finish()
        self.assertEqual(finished["status"], "completed", finished.get("research", {}).get("error"))
        self.assertEqual(finished["summary"]["documents"], 7)
        self.assertEqual(finished["summary"]["reviewed_findings"], 6)
        api = (self.repo / ".zest/docs/initialization/api.md").read_text()
        self.assertIn("app.py:", api)
        self.assertIn(finished["commit"], api)
        self.assertIn("合并和线上状态未知", api)
        self.assertNotIn(self.model.settings.api_key, api)
        for purpose, payload in self.model.calls:
            if purpose in ("project_plan", "project_research", "project_report"):
                self.assertIn("initialization_goals", payload)
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), finished["commit"])
        conversation = ConversationStore(self.state_dir, model=self.model).create(self.project)
        turn = ConversationStore(self.state_dir, model=self.model).send(conversation["id"], "add 如何实现？", message_id=str(uuid4()))
        context = turn["task"]["context"]
        self.assertTrue(any(s["evidence"]["kind"] == "auto_reviewed_project_knowledge" for s in context["sources"]))
        self.assertTrue(any(s["evidence"].get("path") == "app.py" for s in context["sources"]))
        before = len(self.model.calls)
        self.store.run(self.project["id"], model=self.model)
        self.prepare()
        self.assertEqual(len(self.model.calls), before)

    def test_scope_rejection_is_local_and_can_be_prepared_again(self):
        ready = self.prepare()
        result = self.store.decide(self.project["id"], approved=False)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.model.calls, [])
        self.assertNotEqual(self.prepare()["run_id"], ready["run_id"])

    def test_wrong_identity_changed_version_and_rotated_configuration_never_launch(self):
        ready = self.prepare()
        with patch.object(InitializationStore, "launch") as launch:
            with self.assertRaises(FileReadError):
                self.store.decide(self.project["id"], approved=True, sha256="0" * 64)
            (self.repo / "app.py").write_text("def add(a, b):\n    return b + a\n")
            self.commit("new version")
            with self.assertRaises(FileReadError) as caught:
                self.store.decide(self.project["id"], approved=True, sha256=ready["research"]["approval_sha256"])
            self.assertEqual(caught.exception.code, "initialization_version_changed")
            self.assertTrue(self.store.show(self.project["id"])["stale"])
            fresh = self.store.prepare(self.project["id"], replace_pending=True)
            with patch("zest.models.ModelSettings.from_env", return_value=replace(self.model.settings, revision=str(uuid4()))):
                with self.assertRaises(FileReadError) as caught:
                    self.store.decide(self.project["id"], approved=True, sha256=fresh["research"]["approval_sha256"])
            self.assertEqual(caught.exception.code, "initialization_model_changed")
            launch.assert_not_called()

    def test_preparation_crash_reuses_intended_research_id(self):
        original = CognitionStore.create
        def crash(store, *args, **kwargs):
            original(store, *args, **kwargs)
            raise RuntimeError("offline interruption after research save")
        with patch.object(CognitionStore, "create", crash), self.assertRaises(RuntimeError):
            self.prepare()
        saved = self.store.show(self.project["id"])
        restored = self.prepare()
        self.assertEqual(restored["run_id"], saved["run_id"])
        self.assertEqual(len(CognitionStore(self.state_dir).list_runs(self.project["id"])), 1)
        self.assertEqual(self.model.calls, [])

    def test_publication_failure_resumes_locally_without_model_replay(self):
        self.approve()
        with patch("zest.initialization.publish_documents", side_effect=FileReadError("fixture_disk_error", "fixture")):
            stopped = self.store.run(self.project["id"], model=self.model)
        self.assertEqual(stopped["status"], "publication_failed")
        calls = len(self.model.calls)
        restored = self.store.run(self.project["id"], model=self.model)
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(len(self.model.calls), calls)
        self.assertNotIn("error", restored)

    def test_structured_plan_gaps_are_published_as_readable_text(self):
        original = self.model.complete
        def with_gap(payload, *, purpose='qa'):
            result = original(payload, purpose=purpose)
            if purpose == 'project_plan':
                value = json.loads(result['text'])
                value['gaps'] = [{'section':'constraints', 'question':'团队负责人和历史设计理由未提供。'}]
                result = {**result, 'text':json.dumps(value, ensure_ascii=False)}
            return result
        with patch.object(self.model, 'complete', side_effect=with_gap):
            finished = self.finish()
        self.assertEqual(finished['stage'], 'published')
        self.assertEqual(finished['summary']['documents'], 7)
        self.assertEqual(finished['summary']['unresolved_gaps'], 1)
        self.assertIn('团队负责人和历史设计理由未提供。', (self.repo/'.zest/docs/initialization/index.md').read_text())

    def test_unexpected_publication_error_is_visible_and_can_resume_without_model_replay(self):
        self.approve()
        with patch('zest.initialization.publish_documents', side_effect=TypeError('private fixture implementation detail')):
            failed = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(failed['status'], 'publication_failed')
        self.assertFalse(failed['worker_active'])
        self.assertNotIn('private fixture', failed['error']['message'])
        count = len(self.model.calls)
        restored = self.store.run(self.project['id'], model=self.model)
        self.assertEqual(restored['stage'], 'published')
        self.assertEqual(len(self.model.calls), count)

    def test_failed_research_with_saved_facts_can_publish_partial_without_running_models(self):
        from zest.cognition import CognitionEngine
        from zest.resources import record_state
        prepared = self.approve()
        CognitionEngine(self.state_dir, model=self.model).run(prepared['run_id'])
        store = CognitionStore(record_state(self.state_dir, 'cognition', prepared['run_id']))
        store.set_status(prepared['run_id'], 'failed', error={'code':'invalid_cognition_reference','message':'fixture invalid review'})
        waiting = self.store.show(self.project['id'])
        self.assertEqual(waiting['status'], 'publication_pending')
        self.assertFalse(waiting['worker_active'])
        calls = len(self.model.calls)
        with patch.object(CognitionEngine, 'run', side_effect=AssertionError('never replay models')):
            restored = self.store.run(self.project['id'], check_updates=False)
        self.assertEqual(restored['stage'], 'published')
        self.assertEqual(restored['status'], 'partial')
        self.assertEqual(restored['research']['status'], 'failed')
        self.assertEqual(restored['summary']['documents'], 7)
        self.assertEqual(len(self.model.calls), calls)

    def test_existing_team_document_is_preserved_and_conflict_reported(self):
        self.approve()
        destination = self.repo / ".zest/docs/initialization/api.md"
        destination.parent.mkdir(parents=True)
        destination.write_text("# Team API contract\nDo not replace.\n")
        result = self.store.run(self.project["id"], model=self.model)
        self.assertEqual(result["status"], "partial")
        self.assertIn(".zest/docs/initialization/api.md", result["conflicts"])
        self.assertIn("Do not replace", destination.read_text())

    def test_partial_understanding_keeps_unconfirmed_claims_out_of_fact_sections(self):
        self.approve()
        result = self.store.run(self.project["id"], model=OfflineCognitionModel(reject=True))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["summary"]["reviewed_findings"], 0)
        text = (self.repo / ".zest/docs/initialization/architecture.md").read_text()
        self.assertIn("尚无可发布的自动核对发现", text)
        self.assertIn("待补充的项目知识", text)

    def test_unknown_call_never_restarts_or_pretends_initialization_completed(self):
        self.approve()
        class UnknownModel(OfflineCognitionModel):
            def complete(self, payload, **kwargs):
                self.calls.append(payload)
                raise FileReadError("model_request_failed", "offline uncertain outcome")
        model = UnknownModel()
        result = self.store.run(self.project["id"], model=model)
        self.assertEqual(result["status"], "pending_execution")
        with self.assertRaises(FileReadError):
            self.store.launch(self.project["id"])
        with self.assertRaises(FileReadError):
            self.store.recover(self.project["id"])
        self.assertEqual(len(model.calls), 1)
        self.assertFalse((self.repo / ".zest/docs/initialization/index.md").exists())
        with self.assertRaises(FileReadError):
            self.store.prepare(self.project["id"], replace_pending=True)
        from zest.resources import record_state
        CognitionStore(record_state(self.state_dir, "cognition", result["run_id"])).set_status(result["run_id"], "failed")
        with self.assertRaises(FileReadError) as caught:
            self.store.prepare(self.project["id"], replace_pending=True)
        self.assertEqual(caught.exception.code, "initialization_execution_unknown")
        self.assertEqual(len(model.calls), 1)

    def test_status_and_preview_do_not_write_or_call_models(self):
        client = TestClient(create_app(self.state_dir))
        path = f"/api/projects/{self.project['id']}/initialization"
        self.assertEqual(client.get(path).json()["data"]["status"], "not_started")
        ready = self.prepare()
        directory = project_states(self.state_dir, self.project["id"])[0]
        def snapshot():
            return {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in directory.rglob("*") if p.is_file()}
        before = snapshot()
        with patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("no calls")):
            for _ in range(2):
                self.assertEqual(client.get(path).json()["data"]["run_id"], ready["run_id"])
                self.assertEqual(client.get(f"/api/cognition/{ready['run_id']}/preview").status_code, 200)
        self.assertEqual(snapshot(), before)

    def test_missing_model_does_not_prevent_local_documents(self):
        with patch("zest.models.ModelSettings.from_env", side_effect=FileReadError("model_not_configured", "fixture")):
            result = self.prepare()
        self.assertEqual(result["status"], "needs_model")
        self.assertNotIn("run_id", result)
        self.assertTrue((self.repo / ".zest/docs/index.md").exists())
        self.assertEqual(self.prepare()["status"], "awaiting_approval")

    def test_unborn_repository_creates_local_layout_without_model_calls(self):
        other = self.root / "empty"
        other.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-b", "main", str(other)], check=True, capture_output=True)
        project = ProjectRegistry(self.state_dir).register(inspect_repository(other), workspace=True)
        result = self.store.prepare(project["id"])
        self.assertEqual(result["status"], "needs_commit")
        self.assertTrue((other / ".zest/docs/index.md").exists())
        self.assertEqual(self.model.calls, [])

    def test_web_import_prepares_initialization_and_duplicate_import_reuses_run(self):
        first = execute(self.state_dir, "project.import", {"path": str(self.repo)})
        second = execute(self.state_dir, "project.import", {"path": str(self.repo)})
        self.assertEqual(first["initialization"]["status"], "awaiting_approval")
        self.assertEqual(first["initialization"]["run_id"], second["initialization"]["run_id"])
        self.assertEqual(self.model.calls, [])

    def test_duplicate_starts_and_finished_run_cannot_send_again(self):
        self.approve()
        result = self.store.run(self.project["id"], model=self.model)
        calls = len(self.model.calls)
        with self.assertRaises(FileReadError):
            self.store.decide(self.project["id"], approved=True, sha256=result["research"]["approval_sha256"])
        with self.assertRaises(FileReadError):
            self.store.launch(self.project["id"])
        self.store.run(self.project["id"], model=self.model)
        self.assertEqual(len(self.model.calls), calls)

    def test_parallel_research_inherits_private_model_configuration(self):
        self.configuration.stop()
        credential = "offline-isolated-catalog-key"
        ModelSettingsStore(self.state_dir).save(credential)
        threads = set()
        class ScopedModel(OfflineCognitionModel):
            def complete(self, payload, **kwargs):
                if ModelSettings.from_env().api_key != credential:
                    raise AssertionError("incorrect configuration scope")
                threads.add(threading.current_thread().name)
                return super().complete(payload, **kwargs)
        with model_settings_scope(self.state_dir):
            model = ScopedModel()
            model.settings = ModelSettings.from_env()
            self.approve()
            result = self.store.run(self.project["id"], model=model)
        self.assertEqual(result["status"], "completed", result.get("research", {}).get("error"))
        self.assertTrue(any("ThreadPoolExecutor" in name for name in threads))

    def test_background_launcher_detaches_and_passes_only_configuration_location(self):
        from unittest.mock import Mock
        from zest.model_settings import worker_environment
        self.approve()
        child = Mock(pid=4321)
        with model_settings_scope(self.state_dir), patch("zest.initialization.subprocess.Popen", return_value=child) as popen:
            self.store.launch(self.project["id"])
        args, kwargs = popen.call_args
        self.assertIn("zest.initialization", args[0])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["ZEST_MODEL_SETTINGS_DIRECTORY"], str(self.state_dir))
        self.assertNotIn(self.model.settings.api_key, json.dumps(args))
        self.assertNotIn(self.model.settings.api_key, json.dumps(kwargs["env"]))

    def test_explicit_reinitialization_prepares_a_new_approval_without_replaying(self):
        previous = self.finish()
        calls = len(self.model.calls)
        fresh = self.store.prepare(self.project["id"], replace_pending=True)
        self.assertNotEqual(fresh["run_id"], previous["run_id"])
        self.assertEqual(fresh["status"], "awaiting_approval")
        self.assertEqual(fresh["previous_initialization"]["run_id"], previous["run_id"])
        self.assertEqual(fresh["document_hashes"], previous["document_hashes"])
        self.assertEqual(len(self.model.calls), calls)

    def test_finished_worker_in_a_separate_process_publishes_durable_documents(self):
        import subprocess
        import sys
        self.approve()
        program = ("import sys; from zest.initialization import InitializationStore; "
                   "from tests.test_cognition import OfflineCognitionModel; "
                   "r=InitializationStore(sys.argv[1]).run(sys.argv[2],model=OfflineCognitionModel()); "
                   "assert r['status']=='completed',r.get('error')")
        child = subprocess.run([sys.executable, "-c", program, str(self.state_dir), self.project["id"]],
                               capture_output=True, timeout=60, check=True)
        self.assertEqual(self.store.show(self.project["id"])["status"], "completed")
        self.assertTrue((self.repo / ".zest/docs/initialization/index.md").exists())
        self.assertNotIn(self.model.settings.api_key.encode(), child.stdout + child.stderr)
