import json
import os
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.models import ModelSettings
from zest.qa import answer_context, prepare_context, validate_answer
from zest.storage import ProjectRegistry


class FakeModel:
    def __init__(self, text):
        self.text, self.payload = text, None

    def complete(self, payload):
        self.payload = payload
        return {"text": self.text, "requested_model": "offline-fixture", "usage": {}}


class QATest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def prepare(self):
        (self.repo / "README.md").write_text("# Demo\n使用 Python 实现 CLI。\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def raw(self, **kwargs):
        return json.dumps({"status": "answered", "answer": "项目使用 Python。[E1]",
                           "citations": ["E1"], "unknowns": [], **kwargs})

    def test_context_has_fixed_commit_and_no_absolute_paths_in_sources(self):
        project = self.prepare()
        context = prepare_context(project, "如何启动？", keywords=["Python"])
        self.assertEqual(context["commit"], project["git"]["head_commit"])
        self.assertEqual(context["sources"][0]["evidence"]["path"], "README.md")
        self.assertNotIn(str(self.repo), json.dumps(context["sources"]))

    def test_grounded_answer_contract(self):
        context = prepare_context(self.prepare(), "用什么语言？")
        model = FakeModel(self.raw())
        result = answer_context(context, allow_model=True, model=model)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["evidence"][0]["id"], "E1")
        self.assertEqual(set(model.payload), {"question", "sources", "limitations"})

    def test_unknown_and_missing_citations_rejected(self):
        context = prepare_context(self.prepare(), "语言？")
        for text in (self.raw(citations=["E999"]), self.raw(citations=[]), self.raw(answer="[E999]")):
            with self.subTest(text=text), self.assertRaises(FileReadError):
                validate_answer(text, context["sources"])

    def test_malformed_output_rejected(self):
        context = prepare_context(self.prepare(), "语言？")
        for text in ("not JSON", "[]", self.raw(unknowns="wrong"), self.raw(status="other")):
            with self.subTest(text=text), self.assertRaises(FileReadError):
                validate_answer(text, context["sources"])

    def test_permission_is_required_before_call(self):
        model = FakeModel(self.raw())
        with self.assertRaises(FileReadError):
            answer_context(prepare_context(self.prepare(), "语言？"), allow_model=False, model=model)
        self.assertIsNone(model.payload)

    def test_empty_evidence_does_not_call_model(self):
        project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        model = FakeModel(self.raw())
        result = answer_context(prepare_context(project, "语言？"), allow_model=True, model=model)
        self.assertFalse(result["model_called"])
        self.assertIsNone(model.payload)

    def test_preview_does_not_load_model_or_key(self):
        project = self.prepare()
        with patch("zest.qa.ModelSettings.from_env", side_effect=AssertionError("must not load")):
            code, stdout, stderr = self.cli("project", "ask", project["id"], "语言？", "--preview")
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["sources"])

    def test_context_budget_is_explicit(self):
        project = self.prepare()
        with patch("zest.qa.MAX_CONTEXT_CHARS", 1):
            context = prepare_context(project, "语言？")
        self.assertEqual(context["sources"], [])
        self.assertIn("context_budget", {item["code"] for item in context["warnings"]})

    def test_sensitive_question_is_not_accepted(self):
        with self.assertRaises(FileReadError):
            prepare_context(self.prepare(), "ghp_" + "a" * 30)

    def test_settings_key_not_in_repr(self):
        settings = ModelSettings("deepseek", "requested-name", "https://api.deepseek.com", "private-fixture")
        self.assertNotIn("private-fixture", repr(settings))

    def test_settings_validation_and_no_substitution(self):
        try:
            import dotenv
        except ImportError:
            self.skipTest("AI extra is not installed")
        values = {"ZEST_MODEL_PROVIDER": "deepseek", "ZEST_MODEL_NAME": "deepseek-v4-flash",
                  "ZEST_MODEL_BASE_URL": "https://api.deepseek.com", "ZEST_MODEL_API_KEY": "test-fixture"}
        with patch.dict(os.environ, values, clear=True):
            settings = ModelSettings.from_env(self.root / "missing.env")
            self.assertEqual(settings.name, "deepseek-v4-flash")
        with patch.dict(os.environ, {**values, "ZEST_MODEL_BASE_URL": "https://key@host/v1"}, clear=True):
            with self.assertRaises(FileReadError):
                ModelSettings.from_env(self.root / "missing.env")


class ModelTransportTest(unittest.TestCase):
    def test_real_langchain_adapter_with_offline_http_transport(self):
        try:
            import httpx
            from zest.models import DeepSeekModel
            import langchain_deepseek
        except ImportError:
            self.skipTest("AI extra is not installed")
        captured = []

        def handle(request):
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
                "model": "reported-fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content":
                '{"status":"insufficient_evidence","answer":"未知","citations":[],"unknowns":["缺少信息"]}'},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})

        settings = ModelSettings("deepseek", "deepseek-v4-flash", "https://provider.example.invalid", "fixture")
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            result = DeepSeekModel(settings, http_client=client).complete({"question": "test", "sources": []})
        self.assertEqual(captured[0]["model"], "deepseek-v4-flash")
        self.assertEqual(captured[0]["response_format"], {"type": "json_object"})
        self.assertEqual(result["reported_model"], "reported-fixture")


if __name__ == "__main__":
    unittest.main()
