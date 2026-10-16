"""Budgeted known-format retries with actual SDK responses and durable recovery."""

import copy
import json
import unittest
from unittest.mock import patch

try:
    import httpx
    import langgraph
except ImportError as exc:
    raise unittest.SkipTest("AI extras required") from exc

from tests import test_cognition as fixtures
from tests import test_model_outputs as outputs
from tests.test_model_transport import reply
from zest.cognition import CognitionEngine
from zest.cognition_contracts import POLICY, partition_report, validate_plan
from zest.cognition_views import format_cognition
from zest.files import FileReadError
from zest.models import DeepSeekModel


class CognitionRetryTest(unittest.TestCase):
    setUp = fixtures.CognitionTest.setUp
    setUpBase = fixtures.CognitionTest.setUpBase
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def active(self):
        record = self.approved()
        self.store.set_status(record["id"], "planning")
        return record["id"], self.store.show(record["id"], include_envelope=True)["envelope"]

    def invoke(self, run, envelope, *, purpose="project_plan", payload=None):
        return self.engine.call(run, "coordinator", 0, purpose, payload or {}, envelope)

    def record(self, run):
        value = self.store.show(run, include_calls=True)
        self.assertEqual(len(value["calls"]), 1)
        return value, value["calls"][0]

    def install(self, handler):
        client = self.enterContext(httpx.Client(transport=httpx.MockTransport(handler)))
        self.engine = CognitionEngine(self.state_dir, model=DeepSeekModel(self.model.settings, http_client=client))

    def test_bad_json_or_schema_then_success_preserves_both_attempts_and_reuses_result(self):
        good = outputs.ModelOutputContractTest().wire_examples()["project_plan"]
        missing_sections = copy.deepcopy(good)
        del missing_sections["tasks"][0]["sections"]
        for bad in ("{} }", "{}", json.dumps(missing_sections)):
            with self.subTest(bad=bad):
                run, envelope = self.active()
                sent = []

                def handle(request):
                    sent.append(request.content.decode())
                    return reply(json.loads(request.content), good, argument_text=bad if len(sent) == 1 else None)

                self.install(handle)
                result = self.invoke(run, envelope)
                self.assertEqual(result, good)
                record, call = self.record(run)
                self.assertEqual(record["call_count"], 2)
                self.assertEqual(sent, [call["request"]["body_text"]] * 2)
                self.assertEqual([a["status"] for a in call["attempts"]], ["invalid", "completed"])
                self.assertEqual(call["attempts"][0]["receipt"]["raw_response"]["provider_tool_calls"][0]
                                 ["function"]["arguments"], bad)
                self.assertEqual(call["receipt"], call["attempts"][1]["receipt"])
                self.assertIn("格式补试", format_cognition(record))
                self.assertEqual(self.invoke(run, envelope), result)
                self.assertEqual(len(sent), 2)

    def test_retry_exhaustion_is_terminal_without_third_request(self):
        run, envelope = self.active()
        sent = []

        def handle(request):
            sent.append(request)
            return reply(json.loads(request.content), {}, argument_text="{} }")

        self.install(handle)
        with self.assertRaises(FileReadError):
            self.invoke(run, envelope)
        record, call = self.record(run)
        self.assertEqual(call["status"], "invalid")
        self.assertEqual([a["status"] for a in call["attempts"]], ["invalid", "invalid"])
        with self.assertRaises(FileReadError) as caught:
            self.invoke(run, envelope)
        self.assertEqual(caught.exception.code, "cognition_response_invalid")
        self.assertEqual(len(sent), 2)
        self.assertEqual(record["call_count"], 2)

    def test_plan_extras_use_one_attempt_keep_raw_receipt_and_never_grant_sources(self):
        run, envelope = self.active()
        good = outputs.ModelOutputContractTest().wire_examples()["project_plan"]
        source = envelope["files"][0]["id"]
        good["overview"][0]["source_ids"] = [source]
        good["tasks"][0]["source_ids"] = [source]
        wire = copy.deepcopy(good)
        wire["approved"] = True
        wire["tasks"][0]["accepectance_placeholder"] = "unused"
        wire["tasks"][0]["editable_source_ids"] = ["outside"]
        raw = json.dumps(wire, ensure_ascii=False)
        sent = []

        def handle(request):
            sent.append(request.content.decode())
            return reply(json.loads(request.content), wire, argument_text=raw)

        self.install(handle)
        result = self.invoke(run, envelope)
        self.assertEqual(validate_plan(result, {source}), good)
        record, call = self.record(run)
        self.assertEqual(record["call_count"], 1)
        self.assertEqual([a["status"] for a in call["attempts"]], ["completed"])
        self.assertEqual(sent, [call["request"]["body_text"]])
        self.assertEqual(call["receipt"]["raw_response"]["provider_tool_calls"][0]["function"]["arguments"], raw)
        self.assertEqual(self.invoke(run, envelope), good)
        self.assertEqual(len(sent), 1)
        result["tasks"][0]["source_ids"] = ["outside"]
        with self.assertRaises(FileReadError):
            validate_plan(result, {source})

    def test_plan_reception_contract_change_blocks_old_envelope_before_sending(self):
        run, envelope = self.active()
        del envelope["structured_outputs"]["project_plan"]["local_reception"]
        self.install(lambda request: self.fail("an old approval cannot send"))
        with self.assertRaises(FileReadError):
            self.invoke(run, envelope)
        self.assertEqual(self.store.show(run, include_calls=True)["call_count"], 0)

    def test_sensitive_extra_plan_metadata_is_still_rejected_without_retry(self):
        run, envelope = self.active()
        wire = outputs.ModelOutputContractTest().wire_examples()["project_plan"]
        secret = "sk-" + "z" * 32
        wire["unused"] = secret
        sent = []

        def handle(request):
            sent.append(request)
            return reply(json.loads(request.content), wire)

        self.install(handle)
        with self.assertRaises(FileReadError):
            self.invoke(run, envelope)
        _, call = self.record(run)
        self.assertEqual(len(sent), 1)
        self.assertEqual(call["receipt"]["structured_error"], "sensitive_model_output")
        self.assertNotIn(secret, json.dumps(call))

    def test_timeouts_never_retry_including_on_second_attempt(self):
        for first_invalid in (False, True):
            with self.subTest(first_invalid=first_invalid):
                run, envelope = self.active()
                sent = []

                def handle(request):
                    sent.append(request)
                    if first_invalid and len(sent) == 1:
                        return reply(json.loads(request.content), {}, argument_text="{} }")
                    raise httpx.ReadTimeout("offline", request=request)

                self.install(handle)
                with self.assertRaises(FileReadError) as caught:
                    self.invoke(run, envelope)
                self.assertEqual(caught.exception.code, "cognition_execution_unknown")
                _, call = self.record(run)
                self.assertEqual(call["status"], "uncertain")
                self.assertEqual(call["attempts"][-1]["status"], "uncertain")
                with self.assertRaises(FileReadError):
                    self.store.recover(run)
                with self.assertRaises(FileReadError):
                    self.invoke(run, envelope)
                self.assertEqual(len(sent), 2 if first_invalid else 1)

    def test_sensitive_and_oversized_output_do_not_retry(self):
        for statement, code in (("sk-" + "a" * 32, "sensitive_model_output"),
                                ("a" * (270 * 1024), "model_output_budget")):
            with self.subTest(code=code):
                run, envelope = self.active()
                good = outputs.ModelOutputContractTest().wire_examples()["project_plan"]
                good["overview"][0]["statement"] = statement
                sent = []

                def handle(request):
                    sent.append(request)
                    return reply(json.loads(request.content), good)

                self.install(handle)
                with self.assertRaises(FileReadError):
                    self.invoke(run, envelope)
                _, call = self.record(run)
                self.assertEqual(len(sent), 1)
                self.assertEqual(call["receipt"]["structured_error"], code)
                self.assertNotIn(statement, json.dumps(call))

    def test_business_reference_rejection_does_not_retry_structurally_valid_output(self):
        run, envelope = self.active()
        good = outputs.ModelOutputContractTest().wire_examples()["project_report"]
        good["response"]["report"]["findings"] = [
            {"section": "flows", "kind": "observed", "statement": "缺少证据的候选。", "citations": []}]
        sent = []

        def handle(request):
            sent.append(request)
            return reply(json.loads(request.content), good)

        self.install(handle)
        value = self.invoke(run, envelope, purpose="project_report")
        _, audit = partition_report(value["report"], {})
        self.assertTrue(audit["rejected_findings"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.record(run)[1]["status"], "completed")

    def test_budget_prevents_extra_attempt_even_after_known_format_failure(self):
        run, envelope = self.active()
        with self.store.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_runs SET call_count=? WHERE id=?", (POLICY["max_model_calls"] - 1, run))
        sent = []

        def handle(request):
            sent.append(request)
            return reply(json.loads(request.content), {}, argument_text="{} }")

        self.install(handle)
        with self.assertRaises(FileReadError) as caught:
            self.invoke(run, envelope)
        self.assertEqual(caught.exception.code, "cognition_call_budget")
        record, call = self.record(run)
        self.assertEqual(record["call_count"], POLICY["max_model_calls"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(call["status"], "retry_ready")

    def test_old_approval_without_retry_policy_is_not_reinterpreted(self):
        run, envelope = self.active()
        old = copy.deepcopy(envelope)
        del old["policy"]["max_format_retries"]
        with patch.object(self.model, "complete", side_effect=AssertionError("no model")):
            with self.assertRaises(FileReadError) as caught:
                self.invoke(run, old)
        self.assertEqual(caught.exception.code, "cognition_contract_changed")
        self.assertEqual(self.store.show(run)["call_count"], 0)

    def interrupted(self, action):
        run, envelope = self.active()
        good = outputs.ModelOutputContractTest().wire_examples()["project_plan"]
        sent = []

        def handle(request):
            sent.append(request)
            return reply(json.loads(request.content), good, argument_text="{} }" if len(sent) == 1 else None)

        self.install(handle)
        original = self.engine.store.event

        def event(run_id, actor, name, detail):
            original(run_id, actor, name, detail)
            if name == action:
                raise KeyboardInterrupt

        with patch.object(self.engine.store, "event", side_effect=event):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke(run, envelope)
        return run, envelope, sent

    def test_crash_after_known_failure_can_resume_only_the_reserved_retry(self):
        run, envelope, sent = self.interrupted("model_format_retry_ready")
        self.assertEqual(self.record(run)[1]["status"], "retry_ready")
        self.store.recover(run)
        self.store.set_status(run, "planning")
        self.invoke(run, envelope)
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.record(run)[1]["status"], "completed")

    def test_crash_after_retry_admission_stays_unknown_and_is_not_repeated(self):
        run, envelope, sent = self.interrupted("model_format_retry_started")
        record, call = self.record(run)
        self.assertEqual(record["call_count"], 2)
        self.assertEqual([a["status"] for a in call["attempts"]], ["invalid", "pending"])
        with self.assertRaises(FileReadError):
            self.store.recover(run)
        with self.assertRaises(FileReadError):
            self.invoke(run, envelope)
        self.assertEqual(len(sent), 1)

    def test_retry_rechecks_elapsed_workflow_budget(self):
        run, envelope, sent = self.interrupted("model_format_retry_ready")
        with self.store.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_runs SET started_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", run))
        with self.assertRaises(FileReadError) as caught:
            self.invoke(run, envelope)
        self.assertEqual(caught.exception.code, "cognition_time_budget")
        self.assertEqual(len(sent), 1)

    def test_retry_cannot_change_payload_contract_or_frozen_wire(self):
        for change in ("payload", "policy", "wire"):
            with self.subTest(change=change):
                run, envelope, sent = self.interrupted("model_format_retry_ready")
                if change == "policy":
                    envelope = copy.deepcopy(envelope)
                    envelope["policy"]["max_format_retries"] += 1
                from zest.cognition import prepare_model_request
                prepare = prepare_model_request

                def compile(*args, **kwargs):
                    value = prepare(*args, **kwargs)
                    return {**value, "body_sha256": "changed"} if change == "wire" else value

                with patch("zest.cognition.prepare_model_request", side_effect=compile):
                    with self.assertRaises(FileReadError) as caught:
                        self.invoke(run, envelope, payload={"changed": True} if change == "payload" else {})
                self.assertEqual(caught.exception.code, "cognition_contract_changed" if change == "policy" else "cognition_request_changed")
                self.assertEqual(len(sent), 1)
