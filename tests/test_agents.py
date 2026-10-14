import json
import unittest

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.models import ModelSettings
from zest.qa import prepare_context
from zest.storage import ProjectRegistry

try:
    import fastmcp
    import langgraph
    HAS_TEAM = True
except ImportError:
    HAS_TEAM = False


class TeamFixtureModel:
    settings = ModelSettings("deepseek", "fixture", "https://fixture.invalid", "fixture")

    def __init__(self):
        self.calls = []

    def complete(self, payload):
        self.calls.append(payload)
        citation = payload["sources"][0]["id"]
        return {"text": json.dumps({"status": "answered", "answer": f"分析 [{citation}]",
                                    "citations": [citation], "unknowns": []}), "usage": {}}


@unittest.skipUnless(HAS_TEAM, "AI/MCP extras are not installed")
class AgentsTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def context(self, code=True):
        (self.repo / "README.md").write_text("# Demo\n使用 Python\n")
        if code:
            (self.repo / "main.py").write_text("print('hello')\n")
        self.commit()
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        return p, prepare_context(p, "解释项目", files=["main.py"] if code else [], state_dir=self.state_dir)

    def test_two_specialists_and_coordinator_use_only_approved_sources(self):
        from zest.agents import answer_multi_context
        p, context = self.context()
        model = TeamFixtureModel()
        result = answer_multi_context(context, allow_model=True, state_dir=self.state_dir, model=model)
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(len(result["model_responses"]), 3)
        self.assertEqual({r["actor"] for r in result["model_responses"]},
                         {"code", "documentation", "coordinator"})
        self.assertTrue(all(r["status"] == "received" and r["response"]["text"]
                            for r in result["model_responses"]))
        self.assertEqual({r["role"] for r in result["specialists"]}, {"code", "documentation"})
        allowed = {s["id"] for s in context["sources"]}
        self.assertTrue(all({s["id"] for s in payload["sources"]} <= allowed for payload in model.calls))

    def test_no_code_evidence_skips_code_model_call(self):
        from zest.agents import answer_multi_context
        p, context = self.context(code=False)
        result = answer_multi_context(context, allow_model=True, state_dir=self.state_dir, model=TeamFixtureModel())
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(result["model_responses"]), 2)

    def test_permission_denial_prevents_model_call(self):
        from zest.agents import answer_multi_context
        p, context = self.context()
        model = TeamFixtureModel()
        with self.assertRaises(FileReadError):
            answer_multi_context(context, allow_model=False, state_dir=self.state_dir, model=model)
        self.assertEqual(model.calls, [])

    def test_tampered_source_is_denied_before_model(self):
        from zest.agents import answer_multi_context
        p, context = self.context()
        context["sources"][0]["lines"][0]["text"] = "tampered"
        model = TeamFixtureModel()
        with self.assertRaises(FileReadError):
            answer_multi_context(context, allow_model=True, state_dir=self.state_dir, model=model)
        self.assertEqual(model.calls, [])

    def test_persistent_task_binds_multi_call_policy(self):
        from zest.tasks import TaskRunner
        p, context = self.context()
        model = TeamFixtureModel()
        runner = TaskRunner(self.state_dir, model=model)
        task = runner.create(p, "解释项目", files=["main.py"], strategy="multi")
        from zest.models import output_contract
        self.assertEqual(task["context"]["execution_policy"], {"strategy": "multi", "max_model_calls": 3,
                                                             "structured_output": output_contract("qa")})
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["model_calls"], 3)
        self.assertEqual(len(runner.show(task["task_id"])["result"]["model_responses"]), 3)

    def test_failed_multi_task_retains_received_specialist_responses(self):
        from zest.tasks import TaskRunner
        class InvalidTeamModel(TeamFixtureModel):
            def complete(self, payload):
                super().complete(payload)
                return {"text": "invalid-json", "raw_response": {"content": "received-invalid"},
                        "structured_status": "invalid"}
        p, _ = self.context()
        model = InvalidTeamModel()
        runner = TaskRunner(self.state_dir, model=model)
        task = runner.create(p, "解释项目", files=["main.py"], strategy="multi")
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "failed")
        saved = runner.show(task["task_id"])["model_responses"]
        self.assertEqual(len(saved), len(model.calls))
        self.assertGreater(len(saved), 0)
        self.assertTrue(all(r["response"]["raw_response"]["content"] == "received-invalid" for r in saved))


if __name__ == "__main__":
    unittest.main()
