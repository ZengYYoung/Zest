"""Offline response-schema contracts, separate from evidence/authority checks."""

from copy import deepcopy
import json
import unittest

try:
    from pydantic import ValidationError
except ImportError as exc:
    raise unittest.SkipTest("AI extra is not installed") from exc

from zest.model_outputs import (
    OUTPUT_MODELS, InvestigationTask, ProjectResearchResponse,
    get_output_schema, model_for, output_to_protocol,
)


class ModelOutputContractTest(unittest.TestCase):
    def wire_examples(self):
        return {
            "qa": {"status": "answered", "answer": "使用 Python。[E1]", "citations": ["E1"], "unknowns": []},
            "repair_proposal": {"status": "proposed", "summary": "修复边界判断。", "changes": [
                {"source_id": "E1", "content": "value = 1\n"},
                {"source_id": "E2", "old_text": "return 0", "new_text": "return 1"}], "unknowns": []},
            "project_plan": {"overview": [{"statement": "存在 CLI 入口。", "kind": "observed", "source_ids": ["S1"]}],
                "tasks": [{"id": "T1", "title": "调查入口", "goal": "追踪业务流程", "reason": "建立模块认知",
                    "source_ids": ["S1"], "sections": ["purpose", "architecture", "flows", "dependencies", "development", "constraints"],
                    "deliverable": "有原文依据的报告", "acceptance": ["引用入口原文"], "depends_on": [], "priority": 1}], "gaps": []},
            "project_research": {"response": {"kind": "actions", "actions": [
                {"tool": "list_files", "args": {"query": "", "offset": 0}},
                {"tool": "search", "args": {"query": "main"}},
                {"tool": "read", "args": {"source_id": "S1", "start_line": 1, "end_line": 0}},
                {"tool": "symbols", "args": {"query": "main"}}]}},
            "project_review": {"judgments": [{"finding_id": "T1-F1", "supported": True, "reason": "原文直接支持。"}]},
        }

    def test_all_model_purposes_preserve_protocol_shape(self):
        examples = self.wire_examples()
        self.assertEqual(set(examples), set(OUTPUT_MODELS))
        for purpose, wire in examples.items():
            with self.subTest(purpose=purpose):
                parsed = model_for(purpose).model_validate_json(json.dumps(wire))
                expected = deepcopy(wire)
                if purpose == "project_research":
                    expected = {"actions": expected["response"]["actions"], "report": None}
                    expected["actions"][2]["args"] = {"source_id": "S1", "start_line": 1}
                self.assertEqual(output_to_protocol(parsed), expected)

    def test_report_preserves_evidence_and_partial_status(self):
        report = {"status": "partial", "findings": [
            {"section": "flows", "kind": "observed", "statement": "入口调用导入。", "citations": ["R1"]}],
            "unknowns": ["未运行验证。"], "scope_requests": []}
        wire = {"response": {"kind": "report", "report": report}}
        parsed = ProjectResearchResponse.model_validate(wire)
        self.assertEqual(output_to_protocol(parsed), {"actions": [], "report": report})

    def test_every_schema_object_is_required_closed_and_described(self):
        def check(node):
            if isinstance(node, list):
                for item in node:
                    check(item)
            elif isinstance(node, dict):
                self.assertFalse(set(node) & {"$ref", "$defs", "$def", "const", "default", "minItems", "maxItems", "minLength", "maxLength"})
                self.assertNotEqual(node.get("type"), "null")
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                    self.assertTrue(node.get("description"))
                    for field in node["properties"].values():
                        self.assertTrue(field.get("description"))
                for item in node.values():
                    check(item)
        for purpose in OUTPUT_MODELS:
            with self.subTest(purpose=purpose):
                check(get_output_schema(purpose))

    def test_missing_required_or_extra_top_level_fields_are_rejected(self):
        for purpose, wire in self.wire_examples().items():
            for key in wire:
                with self.subTest(purpose=purpose, missing=key), self.assertRaises(ValidationError):
                    model_for(purpose).model_validate({name: value for name, value in wire.items() if name != key})
            with self.subTest(purpose=purpose, extra="type"), self.assertRaises(ValidationError):
                model_for(purpose).model_validate({**wire, "type": "json_object"})

    def test_nested_types_and_authority_shape_are_not_coerced(self):
        invalid_actions = [
            {"tool": "execute", "args": {"query": "main"}},
            {"tool": "search", "args": {"query": "main", "path": "/tmp/outside"}},
            {"tool": "read", "args": {"source_id": "S1", "start_line": "1", "end_line": 0}},
            {"tool": "read", "args": {"source_id": "S1", "start_line": True, "end_line": 0}},
            {"tool": "read", "args": {"source_id": "S1", "start_line": None, "end_line": 0}},
            {"tool": "list_files", "args": {}},
        ]
        for action in invalid_actions:
            wire = {"response": {"kind": "actions", "actions": [action]}}
            with self.subTest(action=action), self.assertRaises(ValidationError):
                ProjectResearchResponse.model_validate_json(json.dumps(wire))
        review = self.wire_examples()["project_review"]
        review["judgments"][0]["supported"] = "true"
        with self.assertRaises(ValidationError):
            model_for("project_review").model_validate_json(json.dumps(review))
        task = self.wire_examples()["project_plan"]["tasks"][0]
        for priority in (True, "1", 4):
            with self.subTest(priority=priority), self.assertRaises(ValidationError):
                InvestigationTask.model_validate_json(json.dumps({**task, "priority": priority}))

    def test_file_change_variants_cannot_mix_full_and_anchor_content(self):
        wire = self.wire_examples()["repair_proposal"]
        wire["changes"] = [{"source_id": "E1", "content": "body", "old_text": "old", "new_text": "new"}]
        with self.assertRaises(ValidationError):
            model_for("repair_proposal").model_validate(wire)

    def test_research_response_variants_are_exclusive_and_required(self):
        wire = self.wire_examples()["project_research"]
        for bad in ({"actions": [], "report": None}, {"response": {"kind": "actions"}},
                    {"response": {"kind": "report", "report": None}},
                    {"response": {**wire["response"], "report": None}}):
            with self.subTest(wire=bad), self.assertRaises(ValidationError):
                ProjectResearchResponse.model_validate(bad)

    def test_explicit_end_line_is_preserved_by_protocol_conversion(self):
        wire = {"response": {"kind": "actions", "actions": [
            {"tool": "read", "args": {"source_id": "S1", "start_line": 20, "end_line": 40}}]}}
        self.assertEqual(output_to_protocol(ProjectResearchResponse.model_validate(wire)),
                         {"actions": wire["response"]["actions"], "report": None})

    def test_schema_copies_cannot_mutate_future_approval_contracts(self):
        schema = get_output_schema("project_review")
        schema["properties"]["judgments"]["items"]["properties"]["supported"]["type"] = "string"
        self.assertEqual(get_output_schema("project_review")["properties"]["judgments"]["items"]["properties"]["supported"]["type"], "boolean")

    def test_provider_tool_conversion_preserves_approved_schema_and_descriptions(self):
        try:
            from langchain_core.utils.function_calling import convert_to_openai_tool
        except ImportError:
            self.skipTest("AI extra is not installed")

        def without_titles(value):
            if isinstance(value, list):
                return [without_titles(item) for item in value]
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key == "properties":
                        result[key] = {name: without_titles(field) for name, field in item.items()}
                    elif key != "title":
                        result[key] = without_titles(item)
                return result
            return value

        for purpose in OUTPUT_MODELS:
            with self.subTest(purpose=purpose):
                approved = without_titles(get_output_schema(purpose))
                description = approved.pop("description")
                actual = convert_to_openai_tool(model_for(purpose), strict=True)["function"]
                self.assertIs(actual["strict"], True)
                self.assertEqual(actual["name"], model_for(purpose).__name__)
                self.assertEqual(actual["description"], description)
                self.assertEqual(without_titles(actual["parameters"]), approved)


if __name__ == "__main__":
    unittest.main()
