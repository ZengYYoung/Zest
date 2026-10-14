import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zest.cli import main
from zest.doctor import readiness
from zest.files import FileReadError
from zest.models import ModelSettings


class DoctorTest(unittest.TestCase):
    def test_configured_key_is_never_printed_or_sent(self):
        key_fixture = "sk-" + "x" * 40
        settings = ModelSettings("deepseek", "fixture", "https://fixture.invalid", key_fixture)
        with patch("zest.doctor.ModelSettings.from_env", return_value=settings), \
                patch("zest.models.DeepSeekModel.complete", side_effect=AssertionError("must not call")):
            result = readiness()
        self.assertNotIn(key_fixture, json.dumps(result))
        self.assertTrue(result["checks"]["model"]["configured"])
        self.assertFalse(result["model_called"])
        self.assertFalse(result["checks"]["docker"]["checked"])

    def test_missing_git_model_and_docker_are_readiness_failures(self):
        with patch("zest.doctor.subprocess.run", side_effect=FileNotFoundError), \
                patch("zest.doctor.ModelSettings.from_env", side_effect=FileReadError("model_not_configured", "fixture")), \
                patch("zest.doctor.DockerEngine", side_effect=FileReadError("docker_unavailable", "fixture")):
            result = readiness(check_docker=True)
        self.assertFalse(result["capabilities"]["project_reads"])
        self.assertFalse(result["capabilities"]["grounded_questions"])
        self.assertFalse(result["capabilities"]["container_verification"])
        self.assertFalse(result["project_code_executed"])

    def test_image_resolution_does_not_mean_tests_passed(self):
        with patch("zest.doctor.DockerEngine") as engine:
            engine.return_value._control.return_value = b"fixture-docker-version"
            engine.return_value.resolve_image.return_value = "sha256:" + "a" * 64
            result = readiness(image="fixture")
        self.assertTrue(result["capabilities"]["requested_image_ready"])
        self.assertEqual(result["scope"], "environment_readiness_only")
        engine.return_value.resolve_image.assert_called_once_with("fixture")

    def test_cli_reports_json_without_creating_project_state(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "new-state"
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(state_dir), "doctor"]), 0)
            self.assertFalse(state_dir.exists())
        result = json.loads(output.getvalue())
        self.assertFalse(result["model_called"])
        self.assertIn("python", result["checks"])


if __name__ == "__main__":
    unittest.main()
