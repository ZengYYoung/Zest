"""Scope-bound source lists and more useful reading/report windows, offline."""

import copy
import json
import unittest

try:
    import httpx
    import langgraph
except ImportError as exc:
    raise unittest.SkipTest("AI extras required") from exc

from tests import test_cognition as fixtures
from tests import test_model_outputs as outputs
from tests.test_model_transport import reply
from zest.cognition import CognitionEngine
from zest.cognition_context import material_catalog, scope_review
from zest.cognition_contracts import POLICY, SECTION_IDS, validate_plan, validate_report
from zest.cognition_tools import PackageTools
from zest.files import FileReadError
from zest.models import DeepSeekModel
from zest.storage import ProjectRegistry
from zest.git import inspect_repository


def materials(count=120, body="pass\n"):
    return {"policy": dict(POLICY), "survey_coverage": {"inventory_scan_finished": True}, "files": [
        {"id": f"S{i}", "roles": ["code_candidate"], "body": body,
         "evidence": {"path": f"module_{i}.py", "commit": "fixed", "content_sha256": f"hash-{i}"}}
        for i in range(1, count + 1)]}


class CapacityContractTest(unittest.TestCase):
    def plan(self):
        return outputs.ModelOutputContractTest().wire_examples()["project_plan"]

    def test_source_lists_follow_visible_catalog_instead_of_six_or_eight(self):
        plan = self.plan()
        ids = [f"S{i}" for i in range(1, 121)]
        plan["overview"][0]["source_ids"] = ids
        plan["tasks"][0]["source_ids"] = ids
        self.assertEqual(validate_plan(plan, set(ids)), plan)
        typed = outputs.model_for("project_plan").model_validate(plan)
        self.assertEqual(len(typed.tasks[0].source_ids), 120)
        self.assertEqual(len(material_catalog(materials())), 120)

    def test_duplicate_missing_or_out_of_scope_ids_still_fail_with_field_path(self):
        for field, values, code in [("overview", ["S1", "S1"], "invalid_cognition_reference"),
                                    ("tasks", ["S1", "outside"], "invalid_cognition_reference"),
                                    ("tasks", [], "invalid_cognition_output")]:
            plan = self.plan(); plan[field][0]["source_ids"] = values
            with self.subTest(field=field, values=values), self.assertRaises(FileReadError) as caught:
                validate_plan(plan, {"S1", "S2"})
            self.assertEqual(caught.exception.code, code)
            self.assertIn(f"{field}[0].source_ids", str(caught.exception))

    def test_task_count_and_six_section_contracts_remain_enforced(self):
        plan = self.plan()
        plan["tasks"] = [copy.deepcopy(plan["tasks"][0]) for _ in range(5)]
        with self.assertRaises(FileReadError) as caught:
            validate_plan(plan, {"S1"})
        self.assertIn("tasks 实际 5 项", str(caught.exception))
        plan = self.plan(); plan["tasks"][0]["sections"] = ["flows"]
        with self.assertRaises(FileReadError): validate_plan(plan, {"S1"})

    def test_large_or_incomplete_scope_requests_discussion_before_transmission(self):
        ordinary = materials()
        self.assertFalse(scope_review(ordinary)["requires_discussion"])
        large = materials(200)
        self.assertIn("large_material_package", scope_review(large)["reasons"])
        ordinary["survey_coverage"]["inventory_scan_finished"] = False
        self.assertIn("survey_incomplete", scope_review(ordinary)["reasons"])
        ordinary["policy"]["catalog_chars"] = 200
        summary = scope_review(ordinary)
        self.assertIn("planning_catalog_partial", summary["reasons"])
        self.assertLess(summary["catalog_count"], summary["source_count"])

    def test_wider_read_preserves_whole_lines_range_and_continuation(self):
        envelope = materials(1, "#" + "x" * 1999 + "\n" + "next_line\n" * 140)
        tools = PackageTools(envelope)
        proof = tools.read("S1")
        self.assertGreater(len(proof["text"]), 1200)
        self.assertLessEqual(len(proof["text"]), 3000)
        self.assertEqual(proof["text"], "\n".join(line["text"] for line in proof["lines"]))
        self.assertTrue(proof["more_after"])
        self.assertEqual(proof["evidence"]["end_line"], proof["lines"][-1]["number"])
        legacy = copy.deepcopy(envelope); legacy.pop("policy")
        with self.assertRaises(FileReadError) as caught: PackageTools(legacy).read("S1")
        self.assertEqual(caught.exception.code, "cognition_line_budget")
        short = PackageTools(materials(1, "line\n" * 450))
        self.assertEqual(len(short.read("S1")["lines"]), 120)
        self.assertEqual(len(short.read("S1", end_line=400)["lines"]), 400)
        with self.assertRaises(FileReadError): short.read("S1", end_line=401)

    def test_listing_search_and_symbols_use_the_full_approved_package(self):
        envelope = materials()
        for i, source in enumerate(envelope["files"]):
            source["body"] = f"def needle_{i}():\n    return 1\n"
        tools = PackageTools(envelope)
        self.assertEqual(len(tools.list_files()["matches"]), 50)
        self.assertEqual(tools.list_files()["next_offset"], 50)
        self.assertEqual(len(tools.search("return")["matches"]), 12)
        self.assertEqual(tools.search("return")["chunk_budget"], 8000)
        self.assertEqual(len(tools.symbols("needle")["matches"]), 30)
        self.assertEqual(len(tools.symbols("needle_119")["matches"]), 1)
        with self.assertRaises(FileReadError): tools.read("outside")

    def test_report_count_is_guidance_and_all_read_proofs_remain_validated(self):
        evidence = {f"R{i}": {"text": "read original"} for i in range(1, 13)}
        report = {"status": "partial", "findings": [{"section": "flows", "kind": "observed", "statement": f"发现 {i}",
            "citations": list(evidence)} for i in range(12)], "unknowns": [f"未知 {i}" for i in range(16)],
            "scope_requests": [f"包外需求 {i}" for i in range(8)]}
        self.assertEqual(validate_report(report, evidence), report)
        outputs.model_for("project_report").model_validate({"response": {"kind": "report", "report": report}})
        self.assertEqual(POLICY["recommended_report_findings"], 6)
        self.assertEqual(POLICY["max_response_chars"], 32000)
        for bad in (["R1", "R1"], ["R13"], []):
            changed = copy.deepcopy(report); changed["findings"][0]["citations"] = bad
            with self.assertRaises(FileReadError): validate_report(changed, evidence)

    def test_reserved_report_budget_is_larger_without_changing_other_calls(self):
        settings = fixtures.OfflineCognitionModel().settings
        seen = []
        def handle(request):
            body = json.loads(request.content); seen.append(body)
            return reply(body, outputs.ModelOutputContractTest().wire_examples()["project_report"])
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            result = DeepSeekModel(settings, http_client=client).complete({}, purpose="project_report")
        self.assertEqual(result["structured_status"], "valid")
        self.assertEqual(seen[0]["max_tokens"], 8192)
        self.assertEqual(POLICY["max_response_tokens"], 4096)
        self.assertEqual(POLICY["max_model_calls"], 32)
        self.assertEqual(POLICY["max_turns_per_task"], 4)
        self.assertEqual((POLICY["max_prompt_chars"], POLICY["max_extended_prompt_chars"]), (32000, 48000))


class CapacityEngineTest(unittest.TestCase):
    setUpBase = fixtures.CognitionTest.setUpBase
    setUp = fixtures.CognitionTest.setUp
    git = fixtures.CognitionTest.git
    commit = fixtures.CognitionTest.commit
    prepare = fixtures.CognitionTest.prepare
    approved = fixtures.CognitionTest.approved

    def test_review_repartitions_unsent_overlarge_batches_without_losing_proofs(self):
        (self.repo / "app.py").write_text("\n".join("#" + str(i) + "x" * 2650 for i in range(24)) + "\n")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        record = self.approved()
        envelope = self.store.show(record["id"], include_envelope=True)["envelope"]
        tools = PackageTools(envelope)
        source = next(source for source in envelope["files"] if source["evidence"]["path"] == "app.py")
        plan = outputs.ModelOutputContractTest().wire_examples()["project_plan"]
        plan["tasks"] = [copy.deepcopy(plan["tasks"][0]), copy.deepcopy(plan["tasks"][0])]
        for number, task in enumerate(plan["tasks"], 1): task["id"] = f"T{number}"
        self.store.save_plan(record["id"], plan)
        sections = sorted(SECTION_IDS)
        for job in range(2):
            evidence = {f"R{i + 1}": tools.read(source["id"], job * 12 + i + 1, job * 12 + i + 1) for i in range(12)}
            report = {"status": "completed", "findings": [{"section": sections[job * 3 + i], "kind": "observed",
                "statement": "观察到固定原文注释", "citations": [f"R{r + 1}" for r in range(i * 4, i * 4 + 4)]} for i in range(3)],
                "unknowns": [], "scope_requests": []}
            validate_report(report, evidence)
            self.store.publish_job(record["id"], f"T{job + 1}", {"report": report, "evidence": evidence})
        self.engine.review(record["id"], envelope)
        result = self.store.show(record["id"], include_calls=True)
        self.assertTrue(all(f["status"] == "auto_reviewed" for f in result["findings"]))
        self.assertTrue(any(e["action"] == "review_batch_reorganized" for e in result["events"]))
        self.assertTrue(all(call["request"]["chars"] <= 32000 for call in result["calls"]))
        self.assertEqual(len(self.model.calls), result["call_count"])
        self.assertEqual(len(result["context_requests"]), 0)


if __name__ == "__main__":
    unittest.main()
