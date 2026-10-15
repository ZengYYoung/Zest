"""Partial receipt is item-local; original responses and evidence stay intact."""

import copy
import json
import unittest
from unittest.mock import patch

from tests import test_cognition as fixtures
from tests.test_cognition_capacity import materials
from zest.cognition_context import compact_results
from zest.cognition_contracts import partition_report, validate_report
from zest.cognition_tools import PackageTools
from zest.cognition_views import format_cognition, format_dossier
from zest.files import FileReadError
from zest.storage import ProjectRegistry
from zest.git import inspect_repository


def report(citations):
    return {"status": "completed", "findings": [
        {"section": "flows", "kind": "observed", "statement": "固定代码返回加法。", "citations": refs}
        for refs in citations], "unknowns": [], "scope_requests": []}


class ReportReceiptTest(unittest.TestCase):
    def test_zero_byte_file_is_metadata_with_no_text_evidence(self):
        value = PackageTools(materials(1, "")).read("S1")
        self.assertEqual(value["observation"], {"kind": "empty_file", "file_size_bytes": 0, "can_cite_as_text": False})
        self.assertFalse(value["more_after"])
        self.assertIsNone(value["evidence"]["start_line"])
        raw = [{"tool": "read", "result": {"ok": True, "data": value}}] * 2
        view = compact_results(raw)
        self.assertEqual(len(view), 1)
        self.assertNotIn("citation_id", view[0])
        self.assertEqual(view[0]["result"]["data"]["observation"]["kind"], "empty_file")
        self.assertEqual(len(raw), 2)

    def test_blank_window_does_not_claim_whole_file_empty(self):
        value = PackageTools(materials(1, "\npass\n")).read("S1", 1, 1)
        self.assertEqual(value["observation"]["kind"], "empty_text_window")
        self.assertEqual(value["observation"]["file_size_bytes"], 6)
        self.assertTrue(value["more_after"])
        self.assertEqual(value["evidence"]["end_line"], 1)

    def test_local_rejection_preserves_other_candidate_and_original_order(self):
        original = report([["R2"], ["R1"]]); frozen = copy.deepcopy(original)
        accepted, audit = partition_report(original, {"R1": {"text": "return a+b"}, "R2": {"text": ""}})
        self.assertEqual(accepted["status"], "partial")
        self.assertEqual(accepted["findings"], [original["findings"][1]])
        self.assertEqual(audit["accepted_indices"], [1])
        self.assertEqual(audit["rejected_findings"][0]["field"], "report.findings[0]")
        self.assertIn("空原文", audit["rejected_findings"][0]["message"])
        self.assertEqual(audit["original_report"], frozen)
        self.assertEqual(original, frozen)
        with self.assertRaises(FileReadError): validate_report(original, {"R2": {"text": ""}})

    def test_all_rejected_stays_partial_and_no_claim_is_fabricated(self):
        original = report([["outside"], []])
        accepted, audit = partition_report(original, {})
        self.assertEqual(accepted["findings"], [])
        self.assertEqual(accepted["status"], "partial")
        self.assertEqual(len(audit["rejected_findings"]), 2)
        self.assertTrue(accepted["unknowns"])
        validate_report(accepted, {})

    def test_no_rejection_keeps_existing_report_behavior(self):
        original = report([["R1"]])
        accepted, audit = partition_report(original, {"R1": {"text": "body"}})
        self.assertEqual(accepted, original)
        self.assertEqual(audit["rejected_findings"], [])

    def test_whole_report_contract_is_never_repaired(self):
        missing = report([["R1"]]); missing.pop("scope_requests")
        bad_scopes = report([["R1"]]); bad_scopes["scope_requests"] = "not a list"
        bad_unknowns = report([["R1"]]); bad_unknowns["unknowns"] = "not a list"
        for value in (missing, bad_scopes, bad_unknowns):
            with self.subTest(value=value), self.assertRaises(FileReadError):
                partition_report(value, {"R1": {"text": "body"}})

    def test_sensitive_candidate_is_not_saved_in_quarantine_audit(self):
        value = report([["R1"], ["R1"]])
        value["findings"][1]["statement"] = "sk-" + "notARealSecretValue" * 2
        with self.assertRaises(FileReadError) as caught:
            partition_report(value, {"R1": {"text": "body"}})
        self.assertEqual(caught.exception.code, "sensitive_cognition_output")

    def test_many_findings_still_obey_full_response_size_budget(self):
        value = report([["R1"]] * 80)
        for finding in value["findings"]: finding["statement"] = "x" * 500
        with self.assertRaises(FileReadError) as caught:
            partition_report(value, {"R1": {"text": "body"}})
        self.assertIn("report 实际", str(caught.exception))
        self.assertIn("32000 字符", str(caught.exception))


class EmptyReadModel(fixtures.OfflineCognitionModel):
    def complete(self, payload, *, purpose="qa"):
        if purpose not in ("project_research", "project_report"):
            return super().complete(payload, purpose=purpose)
        with self.lock: self.calls.append((purpose, copy.deepcopy(payload)))
        if not payload["tool_results"]:
            empty = next(s["id"] for s in payload["material_catalog"] if s["path"] == "empty.py")
            app = next(s["id"] for s in payload["material_catalog"] if s["path"] == "app.py")
            value = {"actions": [{"tool": "read", "args": {"source_id": empty}},
                                  {"tool": "read", "args": {"source_id": app}}], "report": None}
        else:
            # Deliberately wrong citation precedes the valid candidate, testing
            # quarantine, stable finding IDs and original receipt preservation.
            value = {"actions": [], "report": report([["R2"], ["R1"]])}
        return {"text": json.dumps(value, ensure_ascii=False), "reported_model": "offline-fixture"}


class ReportReceiptEngineTest(unittest.TestCase):
    setUpBase = fixtures.CognitionTest.setUpBase
    setUp = fixtures.CognitionTest.setUp
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def setup_model(self):
        (self.repo / "empty.py").write_text("")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.model = EmptyReadModel(); self.engine.model = self.model

    def test_empty_read_and_bad_candidate_do_not_discard_valid_findings(self):
        self.setup_model(); record = self.approved()
        result = self.engine.run(record["id"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual({f["id"] for f in result["findings"]}, {"T1:F2", "T2:F2"})
        self.assertTrue(all(f["status"] == "auto_reviewed" for f in result["findings"]))
        for job in result["jobs"]:
            self.assertEqual(job["status"], "partial")
            self.assertEqual(list(job["result"]["evidence"]), ["R1"])
            empty = job["result"]["tool_results"][0]
            self.assertNotIn("citation_id", empty)
            self.assertEqual(empty["result"]["data"]["observation"]["kind"], "empty_file")
            self.assertEqual(job["result"]["report_validation"]["accepted_indices"], [1])
        for purpose, payload in self.model.calls:
            if purpose == "project_review":
                self.assertTrue(all(f["finding_id"].endswith(":F2") for f in payload["findings"]))
        calls = self.store.show(record["id"], include_calls=True)["calls"]
        for call in calls:
            if call["purpose"] == "project_research" and call["sequence"] == 1:
                self.assertEqual(json.loads(call["response"])["report"]["findings"][0]["citations"], ["R2"])
        self.assertIn("候选未接收", format_cognition(result))
        self.assertIn("报告候选未接收", format_dossier(self.store.dossier(record["id"])))
        self.assertEqual(len(self.model.calls), 6)  # no repair/retry model request

    def test_checkpoint_reuse_keeps_audit_and_does_not_request_again(self):
        self.setup_model(); record = self.approved(); original_publish = self.store.publish_job
        def fail_first(run, actor, result):
            if actor == "T1": raise FileReadError("cognition_storage_error", "offline interruption")
            return original_publish(run, actor, result)
        # Engine owns a separate store instance; patch that exact boundary.
        with patch.object(self.engine.store, "publish_job", side_effect=fail_first):
            result = self.engine.run(record["id"])
        before = len(self.model.calls)
        envelope = self.store.show(record["id"], include_envelope=True)["envelope"]
        spec = next(j["spec"] for j in result["jobs"] if j["id"] == "T1")
        self.engine.investigate(record["id"], spec, envelope, PackageTools(envelope), result["plan"])
        self.assertEqual(len(self.model.calls), before)
        job = next(j for j in self.store.show(record["id"])["jobs"] if j["id"] == "T1")
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["result"]["report_validation"]["accepted_indices"], [1])
        self.assertEqual(job["result"]["report_validation"]["rejected_findings"][0]["index"], 0)

    def test_over_six_candidates_reach_review_with_local_error_isolated(self):
        self.setup_model(); record = self.approved(); complete = self.model.complete
        def extended(payload, *, purpose="qa"):
            value = complete(payload, purpose=purpose)
            if purpose in ("project_research", "project_report"):
                parsed = json.loads(value["text"])
                if parsed["report"] is not None:
                    parsed["report"] = report([["R1"]] * 8 + [["outside"]])
                    for i, candidate in enumerate(parsed["report"]["findings"]):
                        candidate["statement"] = f"离线候选 {i}：观察到 add 返回 a+b。"
                    value["text"] = json.dumps(parsed, ensure_ascii=False)
            return value
        with patch.object(self.model, "complete", side_effect=extended):
            result = self.engine.run(record["id"])
        self.assertEqual(len(result["findings"]), 16)
        self.assertTrue(all(f["status"] == "auto_reviewed" for f in result["findings"]))
        for job in result["jobs"]:
            self.assertEqual(job["status"], "partial")
            audit = job["result"]["report_validation"]
            self.assertEqual(audit["accepted_indices"], list(range(8)))
            self.assertEqual(audit["rejected_findings"][0]["index"], 8)
            self.assertEqual(len(audit["original_report"]["findings"]), 9)
            self.assertNotIn(f"{job['id']}:F9", {f["id"] for f in result["findings"]})
        self.assertEqual(len(self.model.calls), 9)
        self.assertLessEqual(result["call_count"], result["policy"]["max_model_calls"])
