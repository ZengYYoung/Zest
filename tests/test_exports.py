import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_releases as fixtures
from zest.cli import main
from zest.exports import SourceExporter, verify_export
from zest.files import FileReadError
from zest.repairs import digest


class ExportsTest(unittest.TestCase):
    setUp = fixtures.ReleasesTest.setUp
    git = fixtures.ReleasesTest.git
    make_repair = fixtures.ReleasesTest.make_repair
    prepare = fixtures.ReleasesTest.prepare
    approve = fixtures.ReleasesTest.approve

    def exported(self):
        self.applied = self.approve(self.prepare())
        destination = self.root / "export"
        result = SourceExporter(self.state).export(self.project["id"], destination)
        return destination, result

    def test_export_has_exact_selected_files_manifest_and_no_git(self):
        before = self.git("rev-parse", "HEAD")
        directory, result = self.exported()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["commit"], self.repair["candidate_commit"])
        self.assertFalse(result["runtime_deployed"])
        self.assertIn("a + b", (directory / "source/app.py").read_text())
        self.assertFalse((directory / "source/.git").exists())
        self.assertEqual(result["validation"]["backend"], "offline_fixture")  # Unit gate; never real verification evidence.
        self.assertEqual(verify_export(directory)["status"], "intact")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertIn("a - b", (self.repo / "app.py").read_text())

    def test_unselected_or_untracked_active_ref_cannot_export(self):
        with self.assertRaises(FileReadError) as caught:
            SourceExporter(self.state).export(self.project["id"], self.root / "new")
        self.assertEqual(caught.exception.code, "export_managed_version_required")
        self.git("update-ref", self.store.active(self.project["id"])["ref"], self.repair["candidate_commit"])
        with self.assertRaises(FileReadError) as caught:
            SourceExporter(self.state).export(self.project["id"], self.root / "new")
        self.assertEqual(caught.exception.code, "export_history_required")

    def test_existing_directory_and_project_destinations_are_rejected(self):
        self.approve(self.prepare())
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "user.txt").write_text("keep")
        for path in (existing, self.repo / "new-export"):
            with self.subTest(path=path), self.assertRaises(FileReadError):
                SourceExporter(self.state).export(self.project["id"], path)
        self.assertEqual((existing / "user.txt").read_text(), "keep")
        self.assertFalse((self.repo / "new-export").exists())

    def test_changed_or_extra_source_file_fails_verification(self):
        directory, _ = self.exported()
        source = directory / "source/app.py"
        original = source.read_bytes()
        source.write_text("changed\n")
        with self.assertRaises(FileReadError):
            verify_export(directory)
        source.write_bytes(original)
        extra = directory / "source/extra.py"
        extra.write_text("unexpected\n")
        with self.assertRaises(FileReadError):
            verify_export(directory)

    def test_corrupt_manifest_and_traversal_are_rejected_before_reading(self):
        directory, _ = self.exported()
        path = directory / "manifest.json"
        envelope = json.loads(path.read_text())
        envelope["payload"]["files"][0]["path"] = "../outside.py"
        envelope["sha256"] = digest(envelope["payload"])
        path.write_text(json.dumps(envelope))
        with self.assertRaises(FileReadError):
            verify_export(directory)
        path.write_text("not json")
        with self.assertRaises(FileReadError):
            verify_export(directory)

    def test_source_symlink_and_permission_change_are_rejected(self):
        directory, _ = self.exported()
        source = directory / "source/app.py"
        source.chmod(0o755)
        with self.assertRaises(FileReadError):
            verify_export(directory)
        source.unlink()
        source.symlink_to(self.repo / "app.py")
        with self.assertRaises(FileReadError):
            verify_export(directory)

    def test_partial_failure_preserves_directory_without_complete_manifest(self):
        self.approve(self.prepare())
        destination = self.root / "partial"
        with patch("zest.exports._export", side_effect=OSError("fixture")), self.assertRaises(FileReadError) as caught:
            SourceExporter(self.state).export(self.project["id"], destination)
        self.assertEqual(caught.exception.code, "export_incomplete")
        self.assertTrue(destination.exists())
        self.assertFalse((destination / "manifest.json").exists())

    def test_rollback_export_does_not_claim_runtime_or_candidate_validation(self):
        applied = self.approve(self.prepare())
        rollback = self.approve(self.store.rollback(applied["id"]))
        self.assertEqual(rollback["status"], "applied")
        result = SourceExporter(self.state).export(self.project["id"], self.root / "rollback-export")
        self.assertEqual(result["validation"]["kind"], "previous_recorded_source")
        self.assertFalse(result["validation"]["runtime_health_verified"])

    def test_cli_export_and_local_check(self):
        self.approve(self.prepare())
        destination = self.root / "cli-export"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.state), "release", "export", self.project["id"], str(destination)]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "complete")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["release", "verify-export", str(destination)]), 0)
        self.assertEqual(json.loads(output.getvalue())["scope"], "local_manifest_consistency")


if __name__ == "__main__":
    unittest.main()
