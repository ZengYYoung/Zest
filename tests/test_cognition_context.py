"""Offline full-request budgets, exact approvals, closing turns and evidence."""

import copy
import json
import unittest
from unittest.mock import patch

try:
    import httpx
    import langgraph
except ImportError as exc:
    raise unittest.SkipTest("AI extra required") from exc

from tests import test_cognition as fixtures
from tests import test_model_outputs as outputs
from tests.test_model_transport import reply
from zest.cognition_context import compact_proof, compact_results, review_payload
from zest.cognition_store import canonical
from zest.files import FileReadError
from zest.models import DeepSeekModel, PROMPTS, prepare_model_request


class FullRequestTest(unittest.TestCase):
    def test_compilation_matches_entire_actual_http_body_without_credentials(self):
        settings = fixtures.OfflineCognitionModel().settings
        payload = {"question": "中文原文", "sources": []}
        prepared = prepare_model_request(settings, payload, purpose="qa")
        seen = []
        def handle(request):
            seen.append(request.content.decode())
            return reply(json.loads(request.content), outputs.ModelOutputContractTest().wire_examples()["qa"])
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            DeepSeekModel(settings, http_client=client).complete(payload, expected_request=prepared)
        self.assertEqual(seen, [prepared["body_text"]])
        self.assertEqual(prepared["chars"], len(seen[0]))
        self.assertNotIn(settings.api_key, canonical(prepared))
        body = json.loads(seen[0])
        self.assertIn("tools", body)
        self.assertEqual(body["messages"][0]["content"], PROMPTS["qa"])
        self.assertGreater(prepared["chars"], len(canonical(payload)))

    def test_wire_drift_is_blocked_before_transport(self):
        settings = fixtures.OfflineCognitionModel().settings
        prepared = prepare_model_request(settings, {}, purpose="qa")
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(500)
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(FileReadError) as caught:
                DeepSeekModel(settings, http_client=client).complete({"changed": True}, expected_request=prepared)
            with self.assertRaises(FileReadError):
                DeepSeekModel(settings, http_client=client).complete({}, expected_request={**prepared, "url": "https://changed.invalid/chat/completions"})
        self.assertEqual(caught.exception.code, "model_request_changed")
        self.assertEqual(seen, [])

    def test_compact_views_keep_original_text_identity_and_shared_proofs(self):
        proof = {"source_id": "S1", "evidence": {"commit": "fixed", "path": "a.py", "start_line": 3, "end_line": 4},
                 "text": "alpha\nbeta", "lines": [{"number": 3, "text": "alpha"}, {"number": 4, "text": "beta"}], "more_after": False}
        original = copy.deepcopy(proof)
        raw = [{"tool": "read", "citation_id": "R1", "result": {"ok": True, "data": proof}}] * 2
        compact = compact_results(raw)
        self.assertEqual(len(compact), 1)
        self.assertEqual(compact[0]["result"]["data"], compact_proof(proof))
        batch = [{"id": f"T1:F{i}", "finding": {"statement": "alpha", "evidence": [proof]}} for i in (1, 2)]
        reviewed = review_payload("fixed", batch, [])
        self.assertEqual(len(reviewed["evidence"]), 1)
        self.assertTrue(all(item["evidence_ids"] == ["P1"] for item in reviewed["findings"]))
        self.assertEqual(proof, original)
        self.assertEqual(reviewed["evidence"]["P1"]["text"], original["text"])


class ContextApprovalTest(unittest.TestCase):
    setUpBase = fixtures.CognitionTest.setUpBase
    setUp = fixtures.CognitionTest.setUp
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    cli = fixtures.CognitionTest.cli
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def active(self):
        record = self.approved()
        self.store.set_status(record["id"], "planning")
        return record["id"], self.store.show(record["id"], include_envelope=True)["envelope"]

    def padded(self, chars):
        empty = {"padding": ""}
        overhead = prepare_model_request(self.model.settings, empty, purpose="project_plan")["chars"]
        return {"padding": "x" * (chars - overhead)}

    def call(self, run, envelope, payload, actor="coordinator"):
        return self.engine.call(run, actor, 0, "project_plan", payload, envelope)

    def pending(self, chars=33000):
        run, envelope = self.active()
        payload = self.padded(chars)
        with self.assertRaises(FileReadError) as caught:
            self.call(run, envelope, payload)
        self.assertEqual(caught.exception.code, "cognition_context_approval_required")
        return run, envelope, payload, self.store.context_requests(run)[0]

    def test_exact_default_and_fifty_percent_boundaries_do_not_send_early(self):
        for chars in (32000, 32001, 48000, 48001):
            with self.subTest(chars=chars):
                run, envelope = self.active()
                payload = self.padded(chars)
                with patch.object(self.model, "complete", return_value={"text": "{}"}) as provider:
                    if chars == 32000:
                        self.assertEqual(self.call(run, envelope, payload), {})
                        self.assertEqual(provider.call_count, 1)
                        self.assertEqual(self.store.show(run, include_calls=True)["calls"][0]["request"]["chars"], chars)
                    else:
                        with self.assertRaises(FileReadError) as caught:
                            self.call(run, envelope, payload)
                        self.assertEqual(caught.exception.code, "cognition_prompt_budget" if chars > 48000 else "cognition_context_approval_required")
                        self.assertEqual(provider.call_count, 0)
                        self.assertEqual(self.store.show(run)["call_count"], 0)
                        self.assertEqual(len(self.store.context_requests(run)), 0 if chars > 48000 else 1)

    def test_exact_approval_then_saved_response_reuse_and_no_double_decision(self):
        run, envelope, payload, request = self.pending()
        self.assertIn("tools", json.loads(request["authorization"]["request"]["body_text"]))
        with self.assertRaises(FileReadError):
            self.store.decide_context(run, "coordinator", 0, approved=True, request_sha256="wrong")
        result = self.store.decide_context(run, "coordinator", 0, approved=True, request_sha256=request["request_sha256"])
        self.assertEqual(result["status"], "queued")
        with self.assertRaises(FileReadError):
            self.store.decide_context(run, "coordinator", 0, approved=True, request_sha256=request["request_sha256"])
        self.store.set_status(run, "planning")
        with patch.object(self.model, "complete", return_value={"text": "{}"}) as provider:
            self.call(run, envelope, payload)
            self.call(run, envelope, payload)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(self.store.show(run)["call_count"], 1)

    def test_changed_request_cannot_use_previous_extension(self):
        run, envelope, payload, request = self.pending()
        self.store.decide_context(run, "coordinator", 0, approved=True, request_sha256=request["request_sha256"])
        self.store.set_status(run, "planning")
        with self.assertRaises(FileReadError) as caught:
            self.call(run, envelope, {"padding": payload["padding"] + "different"})
        self.assertEqual(caught.exception.code, "cognition_request_changed")
        self.assertEqual(self.model.calls, [])

    def test_compiled_output_budget_cannot_exceed_fixed_workflow_approval(self):
        import hashlib
        run, envelope = self.active()
        prepared = prepare_model_request(self.model.settings, {}, purpose="project_plan")
        body = json.loads(prepared["body_text"])
        body["max_tokens"] = 8192
        raw = json.dumps(body, ensure_ascii=False)
        changed = {**prepared, "body_text": raw, "chars": len(raw), "body_sha256": hashlib.sha256(raw.encode()).hexdigest()}
        with patch("zest.cognition.prepare_model_request", return_value=changed):
            with self.assertRaises(FileReadError) as caught:
                self.call(run, envelope, {})
        self.assertEqual(caught.exception.code, "cognition_contract_changed")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.store.show(run)["call_count"], 0)

    def test_rejected_extension_is_never_sent_or_bypassed_by_recovery(self):
        run, envelope, payload, request = self.pending()
        with self.assertRaises(FileReadError) as caught:
            self.store.recover(run)
        self.assertEqual(caught.exception.code, "cognition_context_approval_required")
        self.store.decide_context(run, "coordinator", 0, approved=False, request_sha256=request["request_sha256"])
        self.store.set_status(run, "planning")
        with self.assertRaises(FileReadError) as caught:
            self.call(run, envelope, payload)
        self.assertEqual(caught.exception.code, "cognition_context_rejected")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.store.show(run)["call_count"], 0)

    def test_unknown_call_blocks_extension_approval(self):
        run, envelope, _, request = self.pending()
        with patch.object(self.model, "complete", side_effect=TimeoutError):
            with self.assertRaises(FileReadError):
                self.call(run, envelope, {}, actor="T2")
        with self.assertRaises(FileReadError) as caught:
            self.store.decide_context(run, "coordinator", 0, approved=True, request_sha256=request["request_sha256"])
        self.assertEqual(caught.exception.code, "cognition_execution_unknown")
        self.assertEqual(self.store.context_requests(run)[0]["status"], "awaiting_approval")

    def test_cli_preview_and_hash_bound_decision_do_not_start_without_flag(self):
        run, _, _, request = self.pending()
        code, stdout, stderr = self.cli("cognition", "context-preview", run)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)[0]["authorization"], request["authorization"])
        with patch("zest.cognition.start_worker", side_effect=AssertionError("must not launch")):
            code, stdout, stderr = self.cli("cognition", "context-approve", run, "coordinator", "0",
                "--request-sha256", request["request_sha256"], "--no-start")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "queued")
        self.assertEqual(self.model.calls, [])

    def test_corrupted_pending_body_cannot_be_previewed_or_approved(self):
        run, _, _, request = self.pending()
        changed = copy.deepcopy(request["authorization"])
        changed["request"]["body_text"] += "changed"
        with self.store.open(write=True) as conn, conn:
            conn.execute("UPDATE cognition_context_requests SET authorization=? WHERE run_id=?", (canonical(changed), run))
        with self.assertRaises(FileReadError) as caught:
            self.store.context_requests(run)
        self.assertEqual(caught.exception.code, "cognition_record_corrupt")
        with self.assertRaises(FileReadError):
            self.store.decide_context(run, "coordinator", 0, approved=True, request_sha256=request["request_sha256"])
        self.assertEqual(self.model.calls, [])

    def test_overlarge_unsent_candidate_reorganizes_into_typed_report(self):
        from zest.cognition_contracts import COGNITION_PROMPTS
        original = self.model.complete
        def report_gaps(payload, *, purpose):
            if purpose == "project_report":
                self.model.calls.append((purpose, payload))
                return {"text": json.dumps({"actions": [], "report": {"status": "partial", "findings": [],
                    "unknowns": ["没有已读原文，保留调查缺口。"], "scope_requests": []}})}
            return original(payload, purpose=purpose)
        extended = COGNITION_PROMPTS["project_research"] + "x" * 45000
        with patch.dict(COGNITION_PROMPTS, {"project_research": extended}), patch.dict(PROMPTS, {"project_research": extended}):
            record = self.approved()
            with patch.object(self.model, "complete", side_effect=report_gaps):
                result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "partial")
        self.assertFalse(any(purpose == "project_research" for purpose, _ in self.model.calls))
        self.assertEqual(sum(purpose == "project_report" for purpose, _ in self.model.calls), 2)
        self.assertEqual(len(result["context_requests"]), 0)
        self.assertTrue(all(call["request"]["chars"] <= 32000 for call in self.store.show(record["id"], include_calls=True)["calls"]))
        self.assertEqual(sum(event["action"] == "context_reorganized" for event in result["events"]), 2)

    def test_parallel_pauses_preserve_proofs_and_resume_each_exact_request_once(self):
        from zest.cognition_contracts import COGNITION_PROMPTS
        extended = COGNITION_PROMPTS["project_research"] + "\n" + "x" * 26000
        with patch.dict(COGNITION_PROMPTS, {"project_research": extended}), patch.dict(PROMPTS, {"project_research": extended}):
            record = self.approved()
            result = self.engine.run(record["id"])
            self.assertEqual(result["status"], "awaiting_context_approval", result.get("error"))
            self.assertEqual(result["call_count"], 1)
            self.assertEqual(len(result["context_requests"]), 2)
            for _ in range(4):
                pending = [item for item in self.store.context_requests(record["id"]) if item["status"] == "awaiting_approval"]
                for index, request in enumerate(pending):
                    result = self.store.decide_context(record["id"], request["actor"], request["sequence"],
                        approved=True, request_sha256=request["request_sha256"])
                    self.assertEqual(result["status"], "queued" if index == len(pending) - 1 else "awaiting_context_approval")
                result = self.engine.run(record["id"])
                if result["status"] != "awaiting_context_approval":
                    break
                self.assertTrue(all(job["result"]["evidence"] for job in result["jobs"]))
            self.assertEqual(result["status"], "completed", result.get("error"))
            self.assertEqual(sum(purpose == "project_plan" for purpose, _ in self.model.calls), 1)
            self.assertEqual(len(self.model.calls), result["call_count"])

    def test_last_turn_is_report_only_and_repeated_reads_reuse_proof_identity(self):
        original = self.model.complete
        def keep_reading(payload, *, purpose):
            response = original(payload, purpose=purpose)
            if purpose == "project_research":
                source = next(item["id"] for item in payload["material_catalog"] if item["path"] == "app.py")
                return {"text": json.dumps({"actions": [{"tool": "read", "args": {"source_id": source}}], "report": None})}
            return response
        record = self.approved()
        with patch.object(self.model, "complete", side_effect=keep_reading):
            result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(sum(purpose == "project_report" for purpose, _ in self.model.calls), 2)
        for job in result["jobs"]:
            self.assertEqual(job["result"]["turns"], 4)
            self.assertEqual(len(job["result"]["evidence"]), 1)
            self.assertEqual(len(job["result"]["tool_results"]), 3)
            self.assertTrue(job["result"]["report"]["findings"])
        for purpose, payload in self.model.calls:
            if purpose == "project_report":
                self.assertTrue(payload["report_required"])
                self.assertEqual(len(payload["tool_results"]), 1)


if __name__ == "__main__":
    unittest.main()
