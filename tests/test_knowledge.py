import json
import unittest

from tests import test_projects as fixtures
from zest.files import FileReadError
from zest.git import inspect_repository
from zest.knowledge import KnowledgeStore
from zest.qa import prepare_context
from zest.storage import ProjectRegistry


class KnowledgeTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def project(self):
        (self.repo / "README.md").write_text("# Demo\n")
        self.commit()
        return ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))

    def test_draft_is_not_used_until_confirmed(self):
        p = self.project()
        store = KnowledgeStore(self.state_dir)
        note = store.add(p, "规则", "取消订单需要释放库存")
        self.assertEqual(store.for_context(p["id"], p["git"]["head_commit"]), [])
        store.confirm(note["id"])
        self.assertEqual(len(store.for_context(p["id"], p["git"]["head_commit"])), 1)

    def test_snapshot_scope_excludes_new_version(self):
        p = self.project()
        store = KnowledgeStore(self.state_dir)
        store.confirm(store.add(p, "规则", "旧版约束")["id"])
        (self.repo / "README.md").write_text("new")
        self.commit("new")
        new = inspect_repository(self.repo).head_commit
        self.assertEqual(store.for_context(p["id"], new), [])

    def test_project_scope_survives_version_change_until_invalidated(self):
        p = self.project()
        store = KnowledgeStore(self.state_dir)
        note = store.add(p, "规则", "长期约束", scope="project")
        store.confirm(note["id"])
        self.assertEqual(len(store.for_context(p["id"], "other-version")), 1)
        store.invalidate(note["id"], "已被替代")
        self.assertEqual(store.for_context(p["id"], "other-version"), [])
        self.assertEqual([e["action"] for e in store.get(note["id"])["events"]], ["created", "confirmed", "invalidated"])

    def test_confirmed_notes_enter_qa_with_distinct_provenance(self):
        p = self.project()
        store = KnowledgeStore(self.state_dir)
        note = store.add(p, "规则", "人工确认约束")
        store.confirm(note["id"])
        context = prepare_context(p, "约束？", state_dir=self.state_dir)
        source = context["sources"][0]
        self.assertEqual(source["evidence"]["kind"], "confirmed_knowledge")
        self.assertEqual(source["evidence"]["knowledge_id"], note["id"])
        self.assertEqual(source["body"], "人工确认约束")

    def test_invalidated_cannot_be_reconfirmed(self):
        p = self.project()
        store = KnowledgeStore(self.state_dir)
        note = store.add(p, "规则", "body")
        store.invalidate(note["id"], "错误")
        with self.assertRaises(FileReadError):
            store.confirm(note["id"])

    def test_secret_and_invalid_input_are_rejected(self):
        p = self.project()
        store = KnowledgeStore(self.state_dir)
        for title, body in (("", "body"), ("title", "x" * 8001), ("title", "ghp_" + "a" * 30)):
            with self.subTest(title=title), self.assertRaises(FileReadError):
                store.add(p, title, body)

    def test_new_store_reads_existing_notes(self):
        p = self.project()
        note = KnowledgeStore(self.state_dir).add(p, "title", "body")
        self.assertEqual(KnowledgeStore(self.state_dir).get(note["id"]), note)

    def test_missing_store_listing_does_not_create_database(self):
        store = KnowledgeStore(self.state_dir)
        self.assertEqual(store.list_notes("missing"), [])
        self.assertFalse(store.db_path.exists())

    def test_cli_add_confirm_invalidate(self):
        p = self.project()
        code, stdout, stderr = self.cli("knowledge", "add", p["id"], "--title", "规则", "--body", "body")
        self.assertEqual((code, stderr), (0, ""))
        note_id = json.loads(stdout)["id"]
        code, stdout, stderr = self.cli("knowledge", "confirm", note_id)
        self.assertEqual(json.loads(stdout)["status"], "confirmed")
        code, stdout, stderr = self.cli("knowledge", "invalidate", note_id, "--reason", "旧规则")
        self.assertEqual(json.loads(stdout)["status"], "invalidated")

    def test_corrupt_database_and_unknown_note(self):
        store = KnowledgeStore(self.state_dir)
        with self.assertRaises(FileReadError):
            store.get("unknown")
        self.state_dir.mkdir(exist_ok=True)
        store.db_path.write_bytes(b"not sqlite")
        with self.assertRaises(FileReadError):
            store.list_notes("any")

    def test_project_note_supports_unborn_development_project(self):
        p = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        store = KnowledgeStore(self.state_dir)
        note = store.add(p, "目标", "开发一个项目维护工具", scope="project")
        store.confirm(note["id"])
        context = prepare_context(p, "目标？", state_dir=self.state_dir)
        self.assertIsNone(context["commit"])
        self.assertEqual(context["sources"][0]["body"], "开发一个项目维护工具")


if __name__ == "__main__":
    unittest.main()
