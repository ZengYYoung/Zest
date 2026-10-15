"""Offline diagnostics: actual LangChain spans, no model or LangSmith traffic."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import httpx
    import langsmith
except ImportError as exc:
    raise unittest.SkipTest("AI extra is not installed") from exc

from zest.files import FileReadError
from zest.model_diagnostics import LangSmithSettings, LocalModelTrace, publish_trace
from zest.models import DeepSeekModel, ModelSettings, prepare_model_request, safe_model_receipt
from tests.test_model_transport import reply
from tests.test_model_outputs import ModelOutputContractTest


class DiagnosticsTest(unittest.TestCase):
    def capture(self, *, arguments=None):
        settings = ModelSettings("deepseek", "offline-model", "https://fixture.invalid", "offline-private-key")
        request = prepare_model_request(settings, {}, purpose="qa")
        trace = LocalModelTrace()
        calls = []

        def handle(req):
            calls.append(req.content.decode())
            return reply(json.loads(req.content), ModelOutputContractTest().wire_examples()["qa"],
                         argument_text=arguments)

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            response = DeepSeekModel(settings, http_client=client, diagnostics=trace).complete(
                {}, expected_request=request)
        self.assertEqual(calls, [request["body_text"]])
        return trace, request, safe_model_receipt(response)

    def test_real_nested_spans_collected_locally_and_request_unchanged(self):
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "offline-key"}), \
                patch.object(langsmith.Client, "create_run", side_effect=AssertionError("no upload")):
            trace, request, receipt = self.capture()
            export = trace.export(request, receipt)
        self.assertEqual(receipt["structured_status"], "valid")
        root = export["runs"][0]
        self.assertIsNone(root["error"])
        self.assertEqual(root["inputs"]["request"], json.loads(request["body_text"]))
        self.assertTrue(any(r["run_type"] == "llm" for r in export["runs"]))
        self.assertTrue(any(r["run_type"] == "parser" for r in export["runs"]))
        ids = {r["id"] for r in export["runs"]}
        self.assertTrue(all(r["parent_run_id"] in ids for r in export["runs"][1:]))
        with self.assertRaises(FileReadError) as caught:
            trace.invoke(None, {})
        self.assertEqual(caught.exception.code, "diagnostic_already_used")

    def test_invalid_arguments_remain_unchanged_and_root_is_marked_invalid(self):
        original = '{"response": {}}}'
        trace, request, receipt = self.capture(arguments=original)
        export = trace.export(request, receipt)
        root = export["runs"][0]
        self.assertEqual(root["error"], "invalid_structured_output")
        diagnostic = root["outputs"]["diagnostic"]["tool_calls"][0]
        self.assertFalse(diagnostic["json_valid"])
        self.assertEqual(diagnostic["offset"], len(original) - 1)
        self.assertEqual(root["outputs"]["receipt"]["raw_response"]["provider_tool_calls"][0]
                         ["function"]["arguments"], original)
        self.assertEqual(receipt["text"], "")

    def test_secrets_suppressed_responses_and_changed_request_cannot_export(self):
        trace, request, receipt = self.capture()
        for unsafe in ("sensitive_model_output", "model_output_budget"):
            with self.assertRaises(FileReadError):
                trace.export(request, {**receipt, "structured_error": unsafe})
        with self.assertRaises(FileReadError) as caught:
            trace.export({**request, "body_text": "{}"}, receipt)
        self.assertEqual(caught.exception.code, "diagnostic_request_changed")
        with self.assertRaises(FileReadError):
            trace.export(request, receipt, secrets=("offline-model",))

    def test_explicit_upload_failure_preserves_export_and_never_calls_model(self):
        trace, request, receipt = self.capture()
        export = trace.export(request, receipt)
        original = json.dumps(export, sort_keys=True)

        class OfflineClient:
            def __init__(self):
                self.calls = []

            def create_run(self, **kwargs):
                self.calls.append(kwargs)

        client = OfflineClient()
        settings = LangSmithSettings("offline-langsmith-key")
        with patch.object(DeepSeekModel, "complete", side_effect=AssertionError("no model")):
            result = publish_trace(export, settings, client=client)
            self.assertEqual(result["run_ids"], [r["id"] for r in export["runs"]])
            self.assertTrue(all(c["api_url"] == settings.endpoint for c in client.calls))
            with patch.object(client, "create_run", side_effect=RuntimeError("do not expose this")):
                with self.assertRaises(FileReadError) as caught:
                    publish_trace(export, settings, client=client)
            self.assertEqual(caught.exception.code, "langsmith_upload_failed")
        self.assertEqual(original, json.dumps(export, sort_keys=True))

    def test_configuration_keeps_key_private_and_rejects_credential_urls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("LANGSMITH_API_KEY=offline-langsmith-key\n")
            settings = LangSmithSettings.from_env(path)
            self.assertNotIn(settings.api_key, repr(settings))
            path.write_text("LANGSMITH_API_KEY=offline-langsmith-key\n"
                            "LANGSMITH_ENDPOINT=https://user:password@invalid.test\n")
            with self.assertRaises(FileReadError):
                LangSmithSettings.from_env(path)
            path.write_text("")
            with self.assertRaises(FileReadError) as caught:
                LangSmithSettings.from_env(path)
            self.assertEqual(caught.exception.code, "langsmith_not_configured")

    def test_actual_langsmith_sdk_serializes_spans_without_runtime_or_headers(self):
        import requests
        trace, request, receipt = self.capture()
        export = trace.export(request, receipt)
        original = json.dumps(export, sort_keys=True)
        sent = []

        class OfflineSession(requests.Session):
            def send(self, request, **kwargs):
                sent.append(request)
                response = requests.Response()
                response.status_code = 200
                response._content = b'{}'
                response.request = request
                response.url = request.url
                return response

        settings = LangSmithSettings("offline-langsmith-key", "https://trace.fixture.invalid")
        with OfflineSession() as session:
            client = langsmith.Client(api_url=settings.endpoint, api_key=settings.api_key,
                                      session=session, auto_batch_tracing=False,
                                      omit_traced_runtime_info=True, hide_inputs=False,
                                      hide_outputs=False, hide_metadata=False)
            try:
                publish_trace(export, settings, client=client)
            finally:
                client.close()
        posts = [r for r in sent if r.method == "POST"]
        self.assertEqual(len(posts), len(export["runs"]))
        for req in posts:
            self.assertEqual(req.url, settings.endpoint + "/runs")
            body = json.loads(req.body)
            self.assertNotIn(settings.api_key, json.dumps(body))
            self.assertNotIn("runtime", body.get("extra", {}))
        self.assertEqual(original, json.dumps(export, sort_keys=True))
