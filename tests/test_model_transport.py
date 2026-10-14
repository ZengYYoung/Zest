"""Real LangChain transport with offline HTTP; no model service requests."""

import json
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

try:
    import httpx
    from langchain_deepseek import ChatDeepSeek
except ImportError as exc:
    raise unittest.SkipTest("AI extra is not installed") from exc

from tests import test_projects as fixtures
from tests import test_model_outputs as output_fixtures
from tests.test_tasks import FixtureModel
from zest.cognition import CognitionEngine
from zest.cognition_store import CognitionStore
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.models import DeepSeekModel, ModelSettings, model_target, output_contract
from zest.storage import ProjectRegistry
from zest.tasks import TaskRunner


def reply(body, wire, *, reported="offline-reported", argument_text=None, copies=1):
    name = body["tools"][0]["function"]["name"]
    return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
        "model": reported, "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"offline-{i}", "type": "function", "function": {"name": name,
                "arguments": argument_text if argument_text is not None else json.dumps(wire, ensure_ascii=False)}}
                for i in range(copies)]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})


class StructuredTransportTest(unittest.TestCase):
    def settings(self, url="https://fixture.invalid"):
        return ModelSettings("deepseek", "offline-model", url, "offline-private-key")

    def test_every_purpose_uses_described_strict_schema_and_one_request(self):
        examples = output_fixtures.ModelOutputContractTest().wire_examples()
        for purpose, wire in examples.items():
            with self.subTest(purpose=purpose):
                seen = []
                def handle(request):
                    body = json.loads(request.content); seen.append(body)
                    return reply(body, wire)
                with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                    result = DeepSeekModel(self.settings(), http_client=client).complete({}, purpose=purpose)
                self.assertEqual(len(seen), 1)
                self.assertEqual(result["structured_status"], "valid", result)
                function = seen[0]["tools"][0]["function"]
                self.assertTrue(function["strict"])
                self.assertEqual(seen[0]["tool_choice"]["function"]["name"], function["name"])
                self.assertTrue(function["parameters"]["properties"])
                self.assertNotIn("response_format", seen[0])
                self.assertEqual(result["output_contract"], output_contract(purpose))
                self.assertEqual(result["usage"]["total_tokens"], 5)
                self.assertEqual(result["reported_model"], "offline-reported")
                self.assertEqual(json.loads(result["raw_response"]["provider_tool_calls"][0]["function"]["arguments"]), wire)

    def test_beta_endpoint_is_resolved_before_approval_and_matches_http(self):
        settings = self.settings("https://api.deepseek.com")
        self.assertEqual(model_target(settings)["base_url"], "https://api.deepseek.com/beta")
        seen = []
        def handle(request):
            seen.append(str(request.url)); return reply(json.loads(request.content), output_fixtures.ModelOutputContractTest().wire_examples()["qa"])
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            result = DeepSeekModel(settings, http_client=client).complete({})
        self.assertEqual(seen, ["https://api.deepseek.com/beta/chat/completions"])
        self.assertEqual(result["target"], model_target(settings))

    def test_missing_extra_wrong_type_and_malformed_arguments_are_known_invalid(self):
        good = output_fixtures.ModelOutputContractTest().wire_examples()["qa"]
        bad = [{k: v for k, v in good.items() if k != "citations"},
               {**good, "type": "json_object"}, {**good, "answer": 1}, None]
        for wire in bad:
            seen = []
            def handle(request):
                body = json.loads(request.content); seen.append(body)
                return reply(body, wire, argument_text="not-json" if wire is None else None)
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                result = DeepSeekModel(self.settings(), http_client=client).complete({})
            self.assertEqual(len(seen), 1)
            self.assertEqual(result["structured_status"], "invalid")
            self.assertEqual(result["text"], "")
            self.assertEqual(result["structured_error"], "invalid_structured_output")
            self.assertIsNotNone(result["raw_response"])
            self.assertNotIn("parsing_error", result)  # Exception repr must not echo input.

    def test_sensitive_content_and_reported_model_are_not_returned(self):
        good = output_fixtures.ModelOutputContractTest().wire_examples()["qa"]
        secret = "sk-" + "a" * 32
        for wire, reported in [({**good, "answer": secret}, "offline"), (good, secret)]:
            def handle(request): return reply(json.loads(request.content), wire, reported=reported)
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                result = DeepSeekModel(self.settings(), http_client=client).complete({})
            self.assertNotIn(secret, json.dumps(result))
            self.assertEqual(result["structured_status"], "invalid")
            self.assertIsNone(result["raw_response"])

    def test_received_body_survives_sdk_message_conversion_failure(self):
        for changes in ({"choices": []}, {"choices": [{"index": 0, "message": {
            "role": "assistant", "content": {"bad": "content shape"}}, "finish_reason": "stop"}]},
            {"choices": [], "model": "sk-" + "a" * 32}):
            with self.subTest(changes=changes):
                seen = []
                body = {"id": "offline", "object": "chat.completion", "created": 1,
                        "model": "offline-reported", **changes}
                def handle(request):
                    seen.append(request)
                    return httpx.Response(200, json=body)
                with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                    result = DeepSeekModel(self.settings(), http_client=client).complete({})
                self.assertEqual(len(seen), 1)
                self.assertEqual(result["structured_status"], "invalid")
                self.assertEqual(result["text"], "")
                if changes.get("model", "").startswith("sk-"):
                    self.assertIsNone(result["raw_response"])
                    self.assertNotIn(changes["model"], json.dumps(result))
                else:
                    self.assertEqual(result["raw_response"]["provider_response"], body)

    def test_multiple_outputs_are_rejected_and_http_failure_never_retries(self):
        good = output_fixtures.ModelOutputContractTest().wire_examples()["qa"]
        def handle(request): return reply(json.loads(request.content), good, copies=2)
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(DeepSeekModel(self.settings(), http_client=client).complete({})["structured_status"], "invalid")
        calls = []
        def failed(request): calls.append(request); return httpx.Response(503, json={"error": {"message": "offline error"}})
        with httpx.Client(transport=httpx.MockTransport(failed)) as client:
            with self.assertRaises(FileReadError): DeepSeekModel(self.settings(), http_client=client).complete({})
        self.assertEqual(len(calls), 1)


class StructuredApprovalTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def project(self):
        (self.repo / "README.md").write_text("# Demo\nPython CLI\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def test_schema_change_blocks_pending_question_before_model_request(self):
        model = FixtureModel(); runner = TaskRunner(self.state_dir, model=model)
        task = runner.create(self.project(), "语言？")
        changed = {**output_contract("qa"), "version": 2}
        with patch("zest.models.output_contract", return_value=changed):
            result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["error"]["code"], "model_contract_changed")
        self.assertEqual(model.calls, [])

    def test_known_schema_failure_remains_in_failed_question_checkpoint(self):
        wire = {"status": "answered"}  # Missing required fields.
        def handle(request): return reply(json.loads(request.content), wire)
        settings = ModelSettings("deepseek", "offline-model", "https://fixture.invalid", "offline-private-key")
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            runner = TaskRunner(self.state_dir, model=DeepSeekModel(settings, http_client=client))
            task = runner.create(self.project(), "语言？")
            result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["model_response"]["structured_status"], "invalid")
        self.assertIsNotNone(TaskRunner(self.state_dir).show(task["task_id"])["model_response"]["raw_response"])

    def test_cognition_receipt_and_classification_survive_node_crash_without_recall(self):
        from tests.test_cognition import OfflineCognitionModel
        model = OfflineCognitionModel(); store = CognitionStore(self.state_dir)
        target = model_target(model.settings)
        task = store.create(self.project()["id"], target=target)
        store.decide(task["id"], approved=True, approval_sha256=task["approval_sha256"])
        engine = CognitionEngine(self.state_dir, model=model)
        with patch.object(engine.store, "save_plan", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): engine.run(task["id"])
        calls = store.show(task["id"], include_calls=True)["calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "completed")
        self.assertIsNotNone(calls[0]["receipt"])
        store.recover(task["id"])
        engine.run(task["id"])
        self.assertEqual(sum(purpose == "project_plan" for purpose, _ in model.calls), 1)

    def test_cognition_schema_change_is_blocked_and_known_invalid_receipt_is_preserved(self):
        from tests.test_cognition import OfflineCognitionModel
        model = OfflineCognitionModel(); store = CognitionStore(self.state_dir)
        task = store.create(self.project()["id"], target=model_target(model.settings))
        store.decide(task["id"], approved=True, approval_sha256=task["approval_sha256"])
        engine = CognitionEngine(self.state_dir, model=model)
        with patch("zest.cognition.output_contract", return_value={"changed": True}):
            result = engine.run(task["id"])
        self.assertEqual(result["error"]["code"], "cognition_contract_changed")
        self.assertEqual(model.calls, [])
        # A separate fresh run has a received, known-invalid structured response.
        other = store.create(task["project_id"], target=model_target(model.settings))
        store.decide(other["id"], approved=True, approval_sha256=other["approval_sha256"])
        invalid = {"text": "", "structured_status": "invalid", "raw_response": {"content": "known invalid"}}
        with patch.object(model, "complete", return_value=invalid) as complete:
            engine.run(other["id"])
        self.assertEqual(complete.call_count, 1)
        call = store.show(other["id"], include_calls=True)["calls"][0]
        self.assertEqual(call["status"], "invalid")
        self.assertEqual(call["receipt"], invalid)

    def test_crash_before_receipt_transaction_commit_is_unknown_and_not_replayed(self):
        from tests.test_cognition import OfflineCognitionModel
        model = OfflineCognitionModel(); store = CognitionStore(self.state_dir)
        task = store.create(self.project()["id"], target=model_target(model.settings))
        store.decide(task["id"], approved=True, approval_sha256=task["approval_sha256"])
        connect = sqlite3.connect
        class InterruptedConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("UPDATE cognition_calls SET status='completed'"):
                    raise KeyboardInterrupt
                return super().execute(sql, *args, **kwargs)
        with patch("sqlite3.connect", side_effect=lambda *a, **k: connect(*a, **k, factory=InterruptedConnection)):
            with self.assertRaises(KeyboardInterrupt): CognitionEngine(self.state_dir, model=model).run(task["id"])
        calls = store.show(task["id"], include_calls=True)["calls"]
        self.assertEqual(calls[0]["status"], "pending")
        self.assertIsNone(calls[0]["receipt"])
        self.assertEqual(len(model.calls), 1)
        with self.assertRaises(FileReadError) as caught: store.recover(task["id"])
        self.assertEqual(caught.exception.code, "cognition_execution_unknown")
        self.assertEqual(len(model.calls), 1)

    def test_sensitive_fixture_response_cannot_enter_receipt_storage(self):
        from tests.test_cognition import OfflineCognitionModel
        model = OfflineCognitionModel(); store = CognitionStore(self.state_dir)
        task = store.create(self.project()["id"], target=model_target(model.settings))
        store.decide(task["id"], approved=True, approval_sha256=task["approval_sha256"])
        secret = "sk-" + "a" * 32
        with patch.object(model, "complete", return_value={"text": secret, "raw_response": secret}):
            CognitionEngine(self.state_dir, model=model).run(task["id"])
        calls = store.show(task["id"], include_calls=True)["calls"]
        self.assertNotIn(secret, json.dumps(calls))
        self.assertEqual(calls[0]["receipt"]["structured_error"], "sensitive_model_output")


if __name__ == "__main__":
    unittest.main()
