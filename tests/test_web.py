import contextlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from urllib import request, error
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.models import ModelSettings
from zest.storage import ProjectRegistry
from zest.web import WebSettings, create_app


TOKEN = "test_web_" + "x" * 40


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "Web extra required")
class WebTest(unittest.TestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def setUp(self):
        from fastapi.testclient import TestClient
        self.setUpBase()
        (self.repo / "README.md").write_text("# Demo\n")
        (self.repo / "app.py").write_text("print('hello')\n")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.client = TestClient(create_app(self.state_dir, settings=WebSettings(TOKEN)))
        self.headers = {"Authorization": "Bearer " + TOKEN}

    def test_health_is_minimal_and_private_routes_require_token(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "mode": "local_read_only"})
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + TOKEN}):
            response = self.client.get("/api/projects", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(TOKEN, response.text)
        response = self.client.get("/api/projects", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], self.project["id"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_registered_overview_and_file_use_native_evidence(self):
        prefix = "/api/projects/" + self.project["id"]
        shown = self.client.get(prefix, headers=self.headers).json()["data"]
        self.assertEqual(shown["version"]["selected_commit"], self.project["git"]["head_commit"])
        overview = self.client.get(prefix + "/overview", headers=self.headers).json()["data"]
        self.assertEqual(overview["source"]["commit"], self.project["git"]["head_commit"])
        file = self.client.get(prefix + "/file", params={"path": "app.py", "end_line": 1}, headers=self.headers)
        self.assertEqual(file.json()["data"]["content"], "print('hello')\n")

    def test_private_traversal_and_bad_inputs_are_rejected_without_echo(self):
        endpoint = "/api/projects/" + self.project["id"] + "/file"
        for path in (".env", "../outside.py", "/etc/passwd"):
            response = self.client.get(endpoint, params={"path": path}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(path, response.text)
        response = self.client.get(endpoint, params={"path": "app.py", "start_line": -1}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(self.client.get("/api/projects/not-a-uuid", headers=self.headers).status_code, 422)

    def test_unknown_records_are_not_created(self):
        for route in ("projects", "tasks", "repairs", "verifications", "releases"):
            response = self.client.get(f"/api/{route}/{uuid4()}", headers=self.headers)
            self.assertEqual(response.status_code, 404, response.text)
        self.assertFalse((self.state_dir / "tasks.sqlite3").exists())
        self.assertFalse((self.state_dir / "repairs.sqlite3").exists())

    def test_no_mutation_or_model_routes(self):
        before = self.git("show-ref")
        with patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("no calls")):
            for route in ("/api/projects", "/api/tasks", "/api/repairs", "/api/releases"):
                response = self.client.post(route, json={"approved": True}, headers=self.headers)
                self.assertIn(response.status_code, (404, 405))
            self.assertEqual(self.client.get("/api/tasks", headers=self.headers).json(), {"data": []})
        self.assertEqual(self.git("show-ref"), before)
        self.assertFalse((self.state_dir / "tasks.sqlite3").exists())

    def test_host_and_cors_are_local_only(self):
        self.assertEqual(self.client.get("/api/projects", headers={**self.headers, "Host": "evil.example"}).status_code, 400)
        allowed = self.client.options("/api/projects", headers={"Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"})
        self.assertEqual(allowed.status_code, 200)
        denied = self.client.options("/api/projects", headers={"Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET"})
        self.assertEqual(denied.status_code, 400)
        self.assertNotIn("Access-Control-Allow-Origin", denied.headers)

    def test_task_preview_is_visible_without_key_or_execution(self):
        from zest.tasks import TaskRunner
        from tests.test_repair_agent import OfflineRepairModel
        model = OfflineRepairModel()
        task = TaskRunner(self.state_dir, model=model).create(self.project, "fix", files=["app.py"], purpose="repair_proposal")
        response = self.client.get("/api/tasks/" + task["task_id"], headers=self.headers)
        self.assertEqual(response.json()["data"]["status"], "awaiting_approval")
        self.assertNotIn(model.settings.api_key, response.text)
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual(model.calls, [])

    def test_task_list_filters_project_identity_without_calling_model(self):
        from zest.tasks import TaskRunner
        from tests.test_repair_agent import OfflineRepairModel
        model = OfflineRepairModel()
        task = TaskRunner(self.state_dir, model=model).create(self.project, "fix", files=["app.py"], purpose="repair_proposal")
        endpoint = "/api/tasks?project_id=" + self.project["id"]
        records = self.client.get(endpoint, headers=self.headers).json()["data"]
        self.assertEqual([record["task_id"] for record in records], [task["task_id"]])
        self.assertEqual(records[0]["project_id"], self.project["id"])
        self.assertEqual(self.client.get("/api/tasks?project_id=" + str(uuid4()), headers=self.headers).status_code, 404)
        # A registered repository with no tasks must not display another project's records.
        other = self.root / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(other)], capture_output=True, check=True)
        record = ProjectRegistry(self.state_dir).register(inspect_repository(other))
        self.assertEqual(self.client.get("/api/tasks?project_id=" + record["id"], headers=self.headers).json()["data"], [])
        self.assertEqual(model.calls, [])

    def test_verification_list_reads_linked_project_without_execution(self):
        import hashlib
        from zest.repairs import RepairStore
        from zest.verification import VerificationStore
        from tests.test_verification import OfflineVerificationEngine
        repairs = RepairStore(self.state_dir)
        repair = repairs.create(self.project, {"summary": "UI fixture", "changes": [{"path": "app.py",
            "expected_sha256": hashlib.sha256((self.repo / "app.py").read_bytes()).hexdigest(), "content": "print('hi')\n"}]})
        candidate = repairs.decide(repair["id"], approved=True, plan_sha256=repair["plan_sha256"])
        engine = OfflineVerificationEngine()
        record = VerificationStore(self.state_dir, engine=engine).create(candidate["id"], "offline-fixture")
        with patch("zest.verification.DockerEngine", side_effect=AssertionError("no Docker")):
            response = self.client.get("/api/projects/" + self.project["id"] + "/verifications", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], record["id"])
        self.assertEqual(response.json()["data"][0]["backend"], "offline_fixture")
        self.assertEqual(engine.calls, [])
        self.assertEqual(self.client.get("/api/projects/" + str(uuid4()) + "/verifications", headers=self.headers).status_code, 404)

    def test_web_token_is_independent_and_repr_private(self):
        self.assertNotIn(TOKEN, repr(WebSettings(TOKEN)))
        for token in ("", "short", "sk-" + "x" * 40, TOKEN + "\n"):
            with self.subTest(token=token), self.assertRaises(FileReadError):
                WebSettings(token)
        with patch.dict(os.environ, {"ZEST_WEB_API_TOKEN": TOKEN, "ZEST_MODEL_API_KEY": TOKEN}):
            with self.assertRaises(FileReadError):
                WebSettings.from_env()

    def test_real_loopback_http_has_auth_and_no_model_calls(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        program = ("import os,sys,uvicorn; from zest.web import create_app,WebSettings; "
                   "uvicorn.run(create_app(sys.argv[1],settings=WebSettings(os.environ['ZEST_TEST_WEB_TOKEN'])),"
                   "host='127.0.0.1',port=int(sys.argv[2]),access_log=False,log_level='error')")
        process = subprocess.Popen([sys.executable, "-c", program, str(self.state_dir), str(port)],
                                   env={**os.environ, "ZEST_TEST_WEB_TOKEN": TOKEN},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            endpoint = f"http://127.0.0.1:{port}"
            for _ in range(50):
                if process.poll() is not None:
                    self.fail("Loopback test server stopped before startup")
                try:
                    with request.urlopen(endpoint + "/health", timeout=1) as response:
                        self.assertEqual(json.load(response)["mode"], "local_read_only")
                    break
                except (OSError, error.URLError):
                    time.sleep(0.1)
            else:
                self.fail("Loopback test server startup timeout")
            with self.assertRaises(error.HTTPError) as caught:
                request.urlopen(endpoint + "/api/projects", timeout=2)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            with request.urlopen(request.Request(endpoint + "/api/projects", headers=self.headers), timeout=3) as response:
                self.assertEqual(json.load(response)["data"][0]["id"], self.project["id"])
            self.assertFalse((self.state_dir / "tasks.sqlite3").exists())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
