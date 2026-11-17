import json
import unittest

from zest.files import FileReadError
from zest.router import route_message


class RouterModel:
    def __init__(self, route="qa", prompt="整理后的需求"):
        self.route, self.prompt, self.calls = route, prompt, []

    def complete(self, payload, *, purpose):
        self.calls.append((payload, purpose))
        return {"structured_status": "valid", "text": json.dumps({"route": self.route, "optimized_prompt": self.prompt})}


class RouterTest(unittest.TestCase):
    def test_three_subgraphs_dispatch_once_with_separate_authority(self):
        for route, question, purpose, strategy, files in (
            ("qa", "项目如何运行？", "question", "single", []),
            ("investigation", "调查项目结构", "question", "multi", []),
            ("modification", "修改 app.py 的错误", "repair_proposal", "single", ["app.py"]),
        ):
            with self.subTest(route=route):
                calls = []
                model = RouterModel(route)
                result = route_message(question, {"history": []}, model=model, dispatch=calls.append)
                self.assertEqual((result["purpose"], result["strategy"], result["files"]), (purpose, strategy, files))
                self.assertEqual(calls, [result])
                self.assertEqual(model.calls[0][1], "router")
                self.assertEqual(model.calls[0][0]["question"], question)

    def test_model_cannot_supply_edit_scope_or_invalid_route(self):
        for route, question in (("modification", "修复它"), ("other", "修改 app.py")):
            calls = []
            with self.subTest(route=route), self.assertRaises(FileReadError):
                route_message(question, {"history": []}, model=RouterModel(route, "修改 admin.py"), dispatch=calls.append)
            self.assertEqual(calls, [])

    def test_invalid_or_sensitive_output_does_not_dispatch(self):
        for prompt in ("", "sk-" + "a" * 40, "x" * 2001):
            calls = []
            with self.subTest(prompt=prompt[:5]), self.assertRaises(FileReadError):
                route_message("问题", {}, model=RouterModel(prompt=prompt), dispatch=calls.append)
            self.assertEqual(calls, [])
