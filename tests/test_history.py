import contextlib
import io
import json
import unittest

from tests import test_releases as fixtures
from zest.cli import main
from zest.files import FileReadError
from zest.history import capture_maintenance
from zest.knowledge import KnowledgeStore
from zest.qa import prepare_context


class HistoryTest(unittest.TestCase):
    setUp = fixtures.ReleasesTest.setUp
    git = fixtures.ReleasesTest.git
    make_repair = fixtures.ReleasesTest.make_repair
    prepare = fixtures.ReleasesTest.prepare
    approve = fixtures.ReleasesTest.approve

    def capture(self):
        self.applied = self.approve(self.prepare())
        return capture_maintenance(self.state, self.applied["id"])

    def test_capture_is_draft_and_distinguishes_claim_from_evidence(self):
        result = self.capture()
        note = result["note"]
        self.assertEqual(note["status"], "draft")
        self.assertEqual(note["kind"], "maintenance")
        self.assertEqual(note["scope"], "project")
        self.assertEqual(note["observed_commit"], self.repair["candidate_commit"])
        body = json.loads(note["body"])
        self.assertEqual(body["scope"], "managed_source_only")
        self.assertEqual(body["candidate"]["tests_run"], 1)
        self.assertEqual(body["candidate"]["backend"], "offline_fixture")  # Explicit unit gate, not live evidence.
        self.assertIn("proposal_summary_unverified", body)
        self.assertFalse(result["model_called"])
        self.assertEqual(KnowledgeStore(self.state).for_context(self.project["id"], self.repair["candidate_commit"]), [])

    def test_confirmation_enables_historical_retrieval_after_version_change(self):
        note = self.capture()["note"]
        store = KnowledgeStore(self.state)
        store.confirm(note["id"])
        rollback = self.store.rollback(self.applied["id"])
        self.approve(rollback)
        context = prepare_context(self.project, "以前如何处理？", state_dir=self.state)
        sources = [source for source in context["sources"] if source["evidence"].get("kind") == "confirmed_knowledge"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["evidence"]["observed_commit"], self.repair["candidate_commit"])
        self.assertNotEqual(sources[0]["evidence"]["observed_commit"], context["commit"])
        self.assertIn("历史案例", sources[0]["body"])

    def test_repeat_capture_preserves_confirmed_and_invalidated_state(self):
        note = self.capture()["note"]
        store = KnowledgeStore(self.state)
        confirmed = store.confirm(note["id"])
        repeat = capture_maintenance(self.state, self.applied["id"])["note"]
        self.assertEqual(repeat, confirmed)
        invalidated = store.invalidate(note["id"], "不再使用")
        self.assertEqual(capture_maintenance(self.state, self.applied["id"])["note"], invalidated)

    def test_historical_capture_uses_target_commit_even_after_rollback(self):
        applied = self.approve(self.prepare())
        self.approve(self.store.rollback(applied["id"]))
        note = capture_maintenance(self.state, applied["id"])["note"]
        self.assertEqual(note["observed_commit"], self.repair["candidate_commit"])
        self.assertNotEqual(note["observed_commit"], self.store.active(self.project["id"])["selected_commit"])

    def test_pending_and_rollback_are_not_new_verified_maintenance(self):
        pending = self.prepare()
        with self.assertRaises(FileReadError):
            capture_maintenance(self.state, pending["id"])
        applied = self.approve(pending)
        rollback = self.approve(self.store.rollback(applied["id"]))
        with self.assertRaises(FileReadError):
            capture_maintenance(self.state, rollback["id"])

    def test_changed_history_or_offline_production_gate_is_rejected(self):
        applied = self.approve(self.prepare())
        self.gate.stop()
        with self.assertRaises(FileReadError) as caught:
            capture_maintenance(self.state, applied["id"])
        self.assertEqual(caught.exception.code, "release_verification_required")
        self.gate.start()
        self.git("update-ref", self.store._history_ref(applied["id"], "after"), applied["plan"]["previous_commit"])
        with self.assertRaises(FileReadError) as caught:
            capture_maintenance(self.state, applied["id"])
        self.assertEqual(caught.exception.code, "maintenance_history_changed")

    def test_knowledge_fixed_id_conflict_and_foreign_version_are_rejected(self):
        note = self.capture()["note"]
        with self.assertRaises(FileReadError):
            KnowledgeStore(self.state).add(self.project, "different", "body", scope="project", note_id=note["id"])
        with self.assertRaises(FileReadError):
            KnowledgeStore(self.state).add(self.project, "history", "body", observed_commit="0" * 40)

    def test_cli_capture_does_not_confirm(self):
        applied = self.approve(self.prepare())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state), "release", "capture", applied["id"]]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["note"]["status"], "draft")
        self.assertEqual(result["next_step"], "review_knowledge_then_confirm")


if __name__ == "__main__":
    unittest.main()
