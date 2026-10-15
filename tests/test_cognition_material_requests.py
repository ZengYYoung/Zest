"""Package membership and read evidence are separate, with no automatic reads."""

import copy
import json
import unittest
from unittest.mock import patch

from tests import test_cognition as fixtures
from tests.test_cognition_capacity import materials
from tests.test_cognition_report_receipt import report
from zest.cognition_context import approved_materials, material_catalog, read_source_ids
from zest.cognition_contracts import partition_report, validate_report
from zest.cognition_tools import PackageTools
from zest.cognition_views import format_cognition, format_dossier
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.storage import ProjectRegistry


def request(source_id, path, reason="需要补齐该部分原文。"):
    return {"source_id": source_id, "path": path, "reason": reason}


class MaterialRequestTest(unittest.TestCase):
    def test_membership_is_resolved_locally_and_read_window_is_not_full_coverage(self):
        original = report([["R1"]])
        original["material_requests"] = [request("S1", "app.py"), request("", "./unread.py"),
                                         request("", "https://example.test/project/run.log")]
        frozen = copy.deepcopy(original)
        accepted, audit = partition_report(original, {"R1": {"text": "return a+b"}},
            [{"id": "S1", "path": "app.py"}, {"id": "S2", "path": "unread.py"}], ["S1"])
        self.assertEqual(accepted["status"], "partial")
        self.assertEqual([r["location"] for r in audit["material_request_resolutions"]],
                         ["approved_read", "approved_unread", "outside_package"])
        self.assertEqual([r["source_id"] for r in accepted["in_scope_requests"]], ["S1", "S2"])
        self.assertEqual(accepted["scope_requests"][0]["path"], "https://example.test/project/run.log")
        self.assertEqual(accepted["material_requests"], original["material_requests"])
        self.assertEqual(audit["original_report"], frozen)
        self.assertEqual(original, frozen)

    def test_id_path_mismatch_is_quarantined_without_losing_findings_or_valid_requests(self):
        original = report([["R1"]])
        original["material_requests"] = [request("S1", "outside.py"), request("invented", "app.py"),
                                         request("S1", "./app.py"), request("", "../app.py")]
        accepted, audit = partition_report(original, {"R1": {"text": "body"}}, [{"id": "S1", "path": "app.py"}])
        self.assertEqual(accepted["findings"], original["findings"])
        self.assertEqual(audit["accepted_indices"], [0])
        self.assertEqual([r["index"] for r in audit["rejected_material_requests"]], [0, 1])
        self.assertEqual([r["index"] for r in audit["material_request_resolutions"]], [2, 3])
        self.assertEqual(accepted["in_scope_requests"][0]["location"], "approved_unread")
        self.assertEqual(accepted["scope_requests"][0]["path"], "../app.py")
        with self.assertRaises(FileReadError): validate_report(original, {"R1": {"text": "body"}}, [{"id": "S1", "path": "app.py"}])

    def test_missing_catalog_cannot_turn_requests_into_outside_authorization(self):
        original = report([["R1"]]); original["material_requests"] = [request("", "app.py")]
        accepted, audit = partition_report(original, {"R1": {"text": "body"}})
        self.assertEqual(accepted["scope_requests"], [])
        self.assertEqual(accepted["in_scope_requests"], [])
        self.assertEqual(accepted["status"], "partial")
        self.assertEqual(len(audit["rejected_material_requests"]), 1)
        self.assertEqual(accepted["findings"], original["findings"])

    def test_malformed_or_sensitive_request_is_not_repaired_or_quarantined_as_safe_text(self):
        for bad in ("free text", {"path": "app.py", "reason": "missing source"},
                    request(None, "app.py"), {**request("S1", "app.py"), "approved": True},
                    request("", "run.log", "sk-" + "notARealSecretValue" * 2)):
            original = report([["R1"]]); original["material_requests"] = [bad]
            with self.subTest(bad=bad), self.assertRaises(FileReadError):
                partition_report(original, {"R1": {"text": "body"}}, [{"id": "S1", "path": "app.py"}])
        original = report([["R1"]]); original["scope_requests"] = original.pop("material_requests")
        with self.assertRaises(FileReadError): partition_report(original, {"R1": {"text": "body"}}, [])

    def test_complete_inventory_is_not_planning_catalog_and_empty_read_has_no_citation(self):
        envelope = materials(2, ""); envelope["policy"]["catalog_chars"] = 1
        self.assertEqual(material_catalog(envelope), [])
        self.assertEqual(len(approved_materials(envelope)), 2)
        empty = PackageTools(envelope).read("S1")
        reads = [{"tool": "read", "result": {"ok": True, "data": empty}},
                 {"tool": "read", "result": {"ok": False}},
                 {"tool": "search", "result": {"ok": True}}]
        self.assertEqual(read_source_ids(reads), ["S1"])
        self.assertFalse(empty["observation"]["can_cite_as_text"])


class MaterialRequestEngineTest(unittest.TestCase):
    setUpBase = fixtures.CognitionTest.setUpBase
    setUp = fixtures.CognitionTest.setUp
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def setup_requests(self):
        (self.repo / "unread.py").write_text("UNREAD = 1\n")
        (self.repo / "empty.py").write_text("")
        self.commit(); self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        complete = self.model.complete
        def respond(payload, *, purpose="qa"):
            if purpose not in ("project_research", "project_report"):
                return complete(payload, purpose=purpose)
            with self.model.lock: self.model.calls.append((purpose, copy.deepcopy(payload)))
            catalog = {item["path"]: item["id"] for item in payload["material_catalog"]}
            app = catalog["app.py"]
            if purpose != "project_report":
                value = {"actions": [{"tool": "read", "args": {"source_id": app, "end_line": 1}},
                                     {"tool": "read", "args": {"source_id": catalog["empty.py"]}}], "report": None}
            else:
                self.assertIn("unread.py", catalog)  # closing keeps the full approved scope
                self.assertEqual(set(payload["read_source_ids"]), {app, catalog["empty.py"]})
                self.assertEqual(payload["remaining_tool_actions"], 0)
                self.assertTrue(all(r["tool"] == "read" for r in payload["tool_results"]))
                value = {"actions": [], "report": report([["R1"]])}
                value["report"]["material_requests"] = [request(app, "app.py"), request(catalog["unread.py"], "unread.py"),
                    request("", "operations/run.log"), request(app, "mismatch.py")]
            return {"text": json.dumps(value, ensure_ascii=False), "reported_model": "offline-fixture"}
        self.model.complete = respond

    def test_report_round_classifies_requests_persists_audit_and_never_autoreads(self):
        self.setup_requests(); record = self.approved(); result = self.engine.run(record["id"])
        for job in result["jobs"]:
            saved = job["result"]; report_value = saved["report"]
            self.assertEqual([r["location"] for r in report_value["in_scope_requests"]], ["approved_read", "approved_unread"])
            self.assertEqual(len(report_value["scope_requests"]), 1)
            self.assertEqual(saved["report_validation"]["rejected_material_requests"][0]["index"], 3)
            self.assertEqual(len(saved["report_validation"]["original_report"]["material_requests"]), 4)
            self.assertEqual({r["result"]["data"]["evidence"]["path"] for r in saved["tool_results"]}, {"app.py", "empty.py"})
        self.assertEqual(len(self.model.calls), 10)  # plan + 2 * (3 research + report) + review
        self.assertEqual(result["call_count"], 10)
        self.assertTrue(all(f["status"] == "auto_reviewed" for f in result["findings"]))
        rendered = format_cognition(result) + format_dossier(self.store.dossier(record["id"]))
        for label in ("包内待补读（已有读取窗口）", "包内待补读（尚未读取）", "包外资料需另行批准", "资料需求未接收"):
            self.assertIn(label, rendered)
        calls = self.store.show(record["id"], include_calls=True)["calls"]
        for call in calls:
            if call["purpose"] == "project_report":
                self.assertEqual(len(json.loads(call["response"])["report"]["material_requests"]), 4)

    def test_checkpoint_reuse_preserves_classification_without_new_requests(self):
        self.setup_requests(); record = self.approved(); publish = self.engine.store.publish_job
        def fail_first(run, actor, result):
            if actor == "T1": raise FileReadError("cognition_storage_error", "offline interruption")
            return publish(run, actor, result)
        with patch.object(self.engine.store, "publish_job", side_effect=fail_first):
            result = self.engine.run(record["id"])
        before = len(self.model.calls)
        envelope = self.store.show(record["id"], include_envelope=True)["envelope"]
        spec = next(j["spec"] for j in result["jobs"] if j["id"] == "T1")
        self.engine.investigate(record["id"], spec, envelope, PackageTools(envelope), result["plan"])
        self.assertEqual(len(self.model.calls), before)
        saved = next(j["result"] for j in self.store.show(record["id"])["jobs"] if j["id"] == "T1")
        self.assertEqual(saved["report"]["in_scope_requests"][1]["location"], "approved_unread")
        self.assertEqual(saved["report_validation"]["rejected_material_requests"][0]["index"], 3)

    def test_legacy_free_text_is_read_only_and_not_treated_as_classified_expansion(self):
        record = self.approved()
        envelope = self.store.show(record["id"], include_envelope=True)["envelope"]
        self.store.set_status(record["id"], "planning")
        plan = self.engine.plan(record["id"], envelope, PackageTools(envelope))
        legacy = {"status": "partial", "findings": [], "unknowns": ["旧缺口"], "scope_requests": ["unread.py 需要资料"]}
        self.store.job_status(record["id"], plan["tasks"][0]["id"], "partial", {"report": legacy})
        before = copy.deepcopy(self.store.show(record["id"]))
        self.assertIn("历史资料需求（未分类）", format_cognition(before))
        dossier = self.store.dossier(record["id"])
        pending = [p for s in dossier["sections"] for p in s["pending"] if p.get("legacy_material_request")]
        self.assertTrue(pending)
        self.assertTrue(all(not p["scope_expansion"] for p in pending))
        self.assertEqual(self.store.show(record["id"]), before)
