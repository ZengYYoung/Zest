import json
import unittest

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.models import ModelSettings
from zest.storage import ProjectRegistry

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
    HAS_GRAPH = True
except ImportError:
    HAS_GRAPH = False


class FixtureModel:
    def __init__(self, name="fixture"):
        self.settings = ModelSettings("deepseek", name, "https://fixture.invalid", "private-fixture")
        self.calls = []

    def complete(self, payload):
        self.calls.append(payload)
        return {"text": json.dumps({"status": "answered", "answer": "Python [E1]",
                                    "citations": ["E1"], "unknowns": []}), "usage": {}}


@unittest.skipUnless(HAS_GRAPH, "LangGraph AI extra is not installed")
class TasksTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def project(self):
        (self.repo / "README.md").write_text("# Demo\nPython CLI\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def runner(self, model=None):
        from zest.tasks import TaskRunner
        return TaskRunner(self.state_dir, model=model or FixtureModel())

    def test_create_pauses_without_model_call(self):
        runner = self.runner()
        task = runner.create(self.project(), "语言？")
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertEqual(runner.model.calls, [])
        self.assertTrue(task["payload_sha256"])

    def test_resume_in_new_runner_uses_saved_context(self):
        project = self.project()
        task = self.runner().create(project, "语言？")
        (self.repo / "README.md").write_text("changed")
        self.commit("new head")
        runner = self.runner()
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["context"]["commit"], project["git"]["head_commit"])
        self.assertIn("Python", json.dumps(runner.model.calls[0]))

    def test_wrong_hash_does_not_call_model(self):
        runner = self.runner()
        task = runner.create(self.project(), "语言？")
        with self.assertRaises(FileReadError):
            runner.decide(task["task_id"], approved=True, payload_sha256="wrong")
        self.assertEqual(runner.model.calls, [])
        self.assertEqual(runner.show(task["task_id"])["status"], "awaiting_approval")

    def test_reject_does_not_call_model(self):
        runner = self.runner()
        task = runner.create(self.project(), "语言？")
        result = runner.decide(task["task_id"], approved=False)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(runner.model.calls, [])

    def test_changed_model_target_is_rejected(self):
        task = self.runner().create(self.project(), "语言？")
        runner = self.runner(FixtureModel("changed"))
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "model_target_changed")
        self.assertEqual(runner.model.calls, [])

    def test_finished_task_cannot_be_approved_twice(self):
        runner = self.runner()
        task = runner.create(self.project(), "语言？")
        runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        with self.assertRaises(FileReadError):
            runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(len(runner.model.calls), 1)

    def test_active_task_lock_blocks_second_executor(self):
        runner = self.runner()
        task = runner.create(self.project(), "语言？")
        with runner._lock(task["task_id"]):
            with self.assertRaises(FileReadError) as exc:
                self.runner().decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(exc.exception.code, "task_busy")

    def test_checkpoint_contains_no_key_and_list_has_no_source_body(self):
        runner = self.runner()
        runner.create(self.project(), "语言？")
        self.assertNotIn(b"private-fixture", runner.db_path.read_bytes())
        self.assertNotIn("context", runner.list_tasks()[0])

    def test_invalid_task_id_and_unknown_task(self):
        runner = self.runner()
        with self.assertRaises(FileReadError):
            runner.show("../../escape")
        with self.assertRaises(FileReadError):
            runner.show("00000000-0000-0000-0000-000000000000")

    def test_invalid_question_marks_failed_task(self):
        task = self.runner().create(self.project(), "")
        self.assertEqual(task["status"], "failed")

    def test_corrupted_checkpoint_storage_is_reported(self):
        self.project()
        runner = self.runner()
        runner.db_path.write_bytes(b"not sqlite")
        with self.assertRaises(FileReadError) as exc:
            runner.show("00000000-0000-0000-0000-000000000000")
        self.assertEqual(exc.exception.code, "task_storage_error")

    def test_invalidated_knowledge_blocks_pending_approved_context(self):
        from zest.knowledge import KnowledgeStore
        project = self.project()
        store = KnowledgeStore(self.state_dir)
        note = store.add(project, "规则", "项目约束", scope="project")
        store.confirm(note["id"])
        runner = self.runner()
        task = runner.create(project, "规则？")
        store.invalidate(note["id"], "规则已替代")
        result = runner.decide(task["task_id"], approved=True, payload_sha256=task["payload_sha256"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "knowledge_changed")
        self.assertEqual(runner.model.calls, [])


if __name__ == "__main__":
    unittest.main()
