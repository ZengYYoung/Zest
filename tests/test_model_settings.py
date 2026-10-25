"""Local credentials never need an external model call or a durable command."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from zest.files import FileReadError
from zest.model_connection import ModelConnectionStore
from zest.model_settings import ModelSettingsStore, model_settings_scope, worker_environment
from zest.models import ModelSettings
from zest.web import create_app
from zest.web_commands import execute


class ModelSettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zest-model-settings-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "catalog"
        self.client = TestClient(create_app(self.directory))
        self.headers = {"Origin": "http://127.0.0.1:5173"}
        self.key = "offline-fixture-credential-123456"

    def save(self, key=None, client=None):
        return (client or self.client).post("/api/model/settings", headers=self.headers,
                                           json={"api_key": self.key if key is None else key})

    def test_save_and_restart_are_private_without_calling_the_model(self):
        with patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("no network")):
            response = self.save()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(self.key, response.text)
            self.assertFalse(response.json()["data"]["connectivity_tested"])
            restarted = TestClient(create_app(self.directory)).get("/api/model")
        state = restarted.json()["data"]
        self.assertTrue(state["configuration"]["configured"])
        self.assertEqual(state["configuration"]["name"], "deepseek-flash")
        self.assertFalse(state["connected"])
        self.assertNotIn(self.key, restarted.text)
        self.assertEqual(set(p.name for p in self.directory.iterdir()), {"model-settings.json"})
        self.assertEqual((self.directory / "model-settings.json").stat().st_mode & 0o777, 0o600)

    def test_invalid_input_and_foreign_origins_never_echo_credentials(self):
        for body in ({"api_key": self.key, "base_url": "https://example.invalid"},
                     {"api_key": self.key + "\n"}, {"api_key": "short"},
                     {"api_key": [self.key]}, {"api_key": None}, self.key):
            response = self.client.post("/api/model/settings", headers=self.headers, json=body)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(self.key, response.text)
        for origin in (None, "https://foreign.invalid"):
            response = self.client.post("/api/model/settings", headers={"Origin": origin} if origin else {},
                                        json={"api_key": self.key})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.directory.exists())

    def test_saved_settings_override_legacy_env_without_changing_it(self):
        legacy = {"ZEST_MODEL_NAME": "custom-old-model", "ZEST_MODEL_API_KEY": "old-fixture-key",
                  "ZEST_MODEL_BASE_URL": "https://fixture.invalid", "ZEST_MODEL_PROVIDER": "deepseek"}
        with patch.dict(os.environ, legacy), model_settings_scope(self.directory):
            self.assertEqual(ModelSettings.from_env().base_url, "https://fixture.invalid")
            self.save()
            settings = ModelSettings.from_env()
            self.assertEqual(settings.api_key, self.key)
            self.assertEqual(settings.base_url, "https://api.deepseek.com")
            self.assertEqual(settings.name, "deepseek-flash")
            self.assertEqual(os.environ["ZEST_MODEL_API_KEY"], "old-fixture-key")
            self.assertNotIn(self.key, repr(settings))

    def test_apps_and_nested_scopes_do_not_mix_keys(self):
        other = self.root / "other"
        self.save()
        other_client = TestClient(create_app(other))
        self.save("other-offline-fixture-key", other_client)
        with model_settings_scope(self.directory):
            self.assertEqual(ModelSettings.from_env().api_key, self.key)
            with model_settings_scope(other):
                self.assertEqual(ModelSettings.from_env().api_key, "other-offline-fixture-key")
            self.assertEqual(ModelSettings.from_env().api_key, self.key)
        first = self.client.get("/api/model").json()["data"]["configuration"]
        second = other_client.get("/api/model").json()["data"]["configuration"]
        self.assertNotEqual(first["revision"], second["revision"])

    def test_durable_worker_reads_new_configuration_without_restart(self):
        self.save()
        with patch.dict(os.environ, {"ZEST_MODEL_NAME": "wrong-model", "ZEST_MODEL_API_KEY": "old-fixture-key"}):
            prepared = execute(self.directory, "model.prepare", {})
        self.assertEqual(prepared["envelope"]["target"]["model"], "deepseek-flash")
        self.assertNotIn(self.key, json.dumps(prepared))
        self.assertEqual(prepared["envelope"]["configuration_revision"],
                         self.client.get("/api/model").json()["data"]["configuration"]["revision"])

    def test_child_project_worker_keeps_the_catalog_configuration(self):
        self.save()
        project_state = self.root / "project" / ".zest" / "state"
        with model_settings_scope(self.directory):
            environment = worker_environment()
        self.assertEqual(environment["ZEST_MODEL_SETTINGS_DIRECTORY"], str(self.directory))
        self.assertNotIn(self.key, json.dumps(environment))
        child = subprocess.run([sys.executable, "-m", "zest", "--state-dir", str(project_state), "doctor"],
                               env=environment, capture_output=True, timeout=30, check=True)
        model = json.loads(child.stdout)["checks"]["model"]
        self.assertEqual(model["name"], "deepseek-flash")
        self.assertTrue(model["configured"])
        self.assertNotIn(self.key.encode(), child.stdout + child.stderr)

    def test_rotated_key_invalidates_previous_success_and_pending_probe(self):
        self.save()
        prepared = execute(self.directory, "model.prepare", {})
        store = ModelConnectionStore(self.directory)
        store.finish(prepared["id"], "passed", result={"fixture": True})
        self.assertTrue(self.client.get("/api/model").json()["data"]["connected"])
        pending = execute(self.directory, "model.prepare", {})
        self.save("rotated-offline-fixture-key")
        state = self.client.get("/api/model").json()["data"]
        self.assertFalse(state["connected"])
        self.assertFalse(state["check_matches_target"])
        with patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("no network")):
            with self.assertRaises(FileReadError) as caught:
                execute(self.directory, "model.decide", {"id": pending["id"], "approved": True,
                        "sha256": pending["approval_sha256"]})
        self.assertEqual(caught.exception.code, "model_check_changed")

    def test_unsafe_or_corrupt_files_fail_closed_without_env_fallback(self):
        self.directory.mkdir()
        store = ModelSettingsStore(self.directory)
        outside = self.root / "outside"
        outside.write_text(self.key)
        store.path.symlink_to(outside)
        for action in (store.read, lambda: store.save(self.key)):
            with self.assertRaises(FileReadError) as caught:
                action()
            self.assertNotIn(self.key, str(caught.exception))
        self.assertEqual(outside.read_text(), self.key)
        store.path.unlink()
        store.path.write_text("{corrupt " + self.key)
        with model_settings_scope(self.directory), self.assertRaises(FileReadError):
            ModelSettings.from_env()
        response = self.client.get("/api/model")
        self.assertFalse(response.json()["data"]["configuration"]["configured"])
        self.assertNotIn(self.key, response.text)
        store.save(self.key)
        self.assertEqual(store.read()["api_key"], self.key)

    def test_key_cannot_be_submitted_to_the_durable_operation_queue(self):
        from uuid import uuid4
        response = self.client.post("/api/operations", headers=self.headers, json={
            "request_id": str(uuid4()), "action": "model.settings", "parameters": {"api_key": self.key}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(self.key, response.text)
        self.assertFalse((self.directory / "web-operations.sqlite3").exists())


class ConversationModelSettingsTest(unittest.TestCase):
    from tests.test_projects import ProjectsTest
    setUp = ProjectsTest.setUp
    git = ProjectsTest.git
    commit = ProjectsTest.commit

    def test_saved_key_reaches_real_adapter_through_conversation_and_graph_worker(self):
        import httpx
        from uuid import uuid4
        from tests.test_model_transport import reply
        from zest.git import inspect_repository
        from zest.models import DeepSeekModel
        from zest.storage import ProjectRegistry
        from zest.web_operations import WebOperations

        (self.repo / "README.md").write_text("# Demo\nPython CLI\n")
        self.commit()
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        client = TestClient(create_app(self.state_dir))
        key = "offline-conversation-fixture-key"
        saved = client.post("/api/model/settings", headers={"Origin": "http://127.0.0.1:5173"}, json={"api_key": key})
        self.assertEqual(saved.status_code, 200)
        conversation = execute(self.state_dir, "conversation.create", {"project_id": project["id"]})
        turn = execute(self.state_dir, "conversation.send", {"id": conversation["id"],
                       "question": "项目使用什么语言？", "message_id": str(uuid4()), "automatic_answer": False})
        task = turn["task"]
        self.assertEqual(task["target"]["model"], "deepseek-flash")
        seen = []
        def handle(request):
            seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
            return reply(seen[-1][2], {"status": "answered", "answer": "使用 Python。[E1]",
                                      "citations": ["E1"], "unknowns": []})
        with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
            with patch("zest.tasks.DeepSeekModel", side_effect=lambda settings: DeepSeekModel(settings, http_client=transport)):
                operations = WebOperations(self.state_dir)
                operation, _ = operations.submit(str(uuid4()), "task.decide", {
                    "id": task["task_id"], "approved": True, "sha256": task["payload_sha256"]})
                operations.run(operation["id"])
                receipt = operations.show(operation["id"])
        self.assertEqual(receipt["status"], "completed", receipt)
        self.assertEqual(receipt["result"]["status"], "completed", receipt)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "https://api.deepseek.com/beta/chat/completions")
        self.assertEqual(seen[0][1], "Bearer " + key)
        self.assertEqual(seen[0][2]["model"], "deepseek-flash")
        self.assertNotIn(key, json.dumps(receipt))
        self.assertNotIn(key.encode(), operations.path.read_bytes())
