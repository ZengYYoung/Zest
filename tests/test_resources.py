"""Project-local storage, native tree preservation and legacy recovery boundaries."""

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from tests.test_cognition import OfflineCognitionModel
from zest.cognition import CognitionEngine, worker
from zest.cognition_refresh import build_refresh_preview, prepare_refresh
from zest.cognition_store import CognitionStore
from zest.files import FileReadError, read_project_file
from zest.git import inspect_repository
from zest.index import RetrievalIndex
from zest.knowledge import KnowledgeStore
from zest.resources import project_states, record_state
from zest.storage import ProjectRegistry, StorageError
from zest.survey import SurveyStore
from zest.tasks import TaskRunner


class ResourceTest(unittest.TestCase):
    setUp = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit
    cli = fixtures.ProjectsTest.cli

    def populated(self):
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        (self.repo / "README.md").write_text("# Example project\n")
        return self.commit()

    def imported(self):
        self.populated()
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def target(self):
        settings = OfflineCognitionModel().settings
        return {"provider": settings.provider, "model": settings.name, "base_url": settings.base_url}

    def snapshot(self, directory):
        return {str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                for path in directory.rglob("*") if path.is_file()}

    def test_reinitialization_is_byte_and_timestamp_noop_for_existing_management(self):
        project = self.imported()
        folder = self.repo / ".zest"
        (folder / ".gitignore").write_text("# custom control\n*\n!AGENTS.md\n")
        (folder / "AGENTS.md").write_text("# User-maintained management guidance\n")
        (folder / "skills" / "review").mkdir(parents=True)
        (folder / "skills" / "review" / "SKILL.md").write_text("Preserve this skill\n")
        note = KnowledgeStore(self.state_dir).add(project, "Existing memory", "Do not reset", scope="project")
        KnowledgeStore(self.state_dir).confirm(note["id"])
        record = CognitionStore(self.state_dir).create(project["id"], target=self.target())
        catalog = self.snapshot(self.state_dir)
        previous = self.snapshot(folder)
        head = self.git("rev-parse", "HEAD")
        # A new observation of the worktree must not replace saved management state.
        (self.repo / "README.md").write_text("User work in progress\n")
        dirty = self.git("status", "--porcelain")
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), project)
        self.assertEqual(previous, self.snapshot(folder))
        self.assertEqual(catalog, self.snapshot(self.state_dir))
        self.assertEqual(head, self.git("rev-parse", "HEAD"))
        self.assertEqual(dirty, self.git("status", "--porcelain"))
        new_catalog = self.root / "fresh-catalog"
        reattached = ProjectRegistry(new_catalog).register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(reattached, project)
        self.assertEqual(previous, self.snapshot(folder))
        self.assertEqual(CognitionStore(new_catalog).show(record["id"])["approval_sha256"], record["approval_sha256"])
        self.assertEqual(KnowledgeStore(new_catalog).get(note["id"])["status"], "confirmed")

    def test_unknown_existing_management_folder_is_not_taken_over(self):
        self.populated()
        folder = self.repo / ".zest"
        folder.mkdir()
        (folder / "AGENTS.md").write_text("Existing controller\n")
        previous = self.snapshot(folder)
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("resource_directory_conflict", err)
        self.assertEqual(previous, self.snapshot(folder))
        self.assertFalse(self.state_dir.exists())

    def test_missing_local_registration_or_conflicting_catalog_does_not_reinitialize(self):
        project = self.imported()
        database = Path(project["resource_dir"]) / "state.sqlite3"
        saved = database.read_bytes()
        database.unlink()
        before = self.snapshot(self.repo / ".zest")
        with self.assertRaises(FileReadError) as caught:
            ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(caught.exception.code, "resource_identity_mismatch")
        self.assertEqual(before, self.snapshot(self.repo / ".zest"))
        database.write_bytes(saved)
        conflicting = ProjectRegistry(self.root / "conflicting-catalog")
        conflicting.register(inspect_repository(self.repo))  # A different legacy identity.
        before = self.snapshot(self.repo / ".zest")
        catalog = self.snapshot(conflicting.state_dir)
        with self.assertRaises(StorageError):
            conflicting.register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(before, self.snapshot(self.repo / ".zest"))
        self.assertEqual(catalog, self.snapshot(conflicting.state_dir))
        # Partial routing metadata is a conflict, not permission to repair it.
        with closing(sqlite3.connect(self.state_dir / "state.sqlite3")) as conn, conn:
            conn.execute("UPDATE projects SET legacy_state_dir=NULL WHERE id=?", (project["id"],))
        before = self.snapshot(self.repo / ".zest")
        catalog = self.snapshot(self.state_dir)
        with self.assertRaises(StorageError):
            ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(before, self.snapshot(self.repo / ".zest"))
        self.assertEqual(catalog, self.snapshot(self.state_dir))

    def test_legacy_catalog_in_root_folder_retains_identity_and_original_memories(self):
        self.populated()
        registry = ProjectRegistry(self.repo / ".zest")
        project = registry.register(inspect_repository(self.repo))
        note = KnowledgeStore(registry.state_dir).add(project, "Legacy", "Original memory")
        original = (registry.state_dir / "knowledge.sqlite3").read_bytes()
        attached = registry.register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(attached["id"], project["id"])
        self.assertEqual(original, (registry.state_dir / "knowledge.sqlite3").read_bytes())
        self.assertEqual(KnowledgeStore(registry.state_dir).get(note["id"])["body"], "Original memory")
        before = self.snapshot(registry.state_dir)
        registry.register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(before, self.snapshot(registry.state_dir))

    def test_legacy_custom_ignore_control_requires_resolution_without_automatic_edits(self):
        self.populated()
        registry = ProjectRegistry(self.repo / ".zest")
        registry.register(inspect_repository(self.repo))
        (registry.state_dir / ".gitignore").write_text("# Existing custom rules\ncache/\n")
        before = self.snapshot(registry.state_dir)
        with self.assertRaises(FileReadError) as caught:
            registry.register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(caught.exception.code, "resource_ignore_conflict")
        self.assertEqual(before, self.snapshot(registry.state_dir))

    def test_generated_guide_is_navigation_and_explicit_bootstrap_never_overwrites(self):
        self.imported()
        folder = self.repo / ".zest"
        guide = folder / "AGENTS.md"
        self.assertIn("原生 README/docs 无需拷贝", guide.read_text())
        self.assertFalse((folder / "README.md").exists())
        self.assertFalse((folder / "docs").exists())
        project = ProjectRegistry(self.state_dir).list_projects_readonly()[0]
        guide.unlink()
        ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        self.assertFalse(guide.exists())  # Initialization does not silently backfill.
        code, out, err = self.cli("project", "guide", project["id"])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["created"])
        guide.write_text("Custom guide\n")
        before = self.snapshot(folder)
        code, out, err = self.cli("project", "guide", project["id"])
        self.assertEqual(code, 0, err)
        self.assertFalse(json.loads(out)["created"])
        self.assertEqual(before, self.snapshot(folder))

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
    def test_management_hints_are_frozen_approved_separately_from_native_evidence(self):
        project = self.imported()
        guide = self.repo / ".zest" / "AGENTS.md"
        guide.write_text("# Management hints v1\nRead native app.py to verify behavior.\n")
        store = CognitionStore(self.state_dir)
        run = store.create(project["id"], target=self.target())
        envelope = store.show(run["id"], include_envelope=True)["envelope"]
        hint = envelope["management_guidance"]
        self.assertEqual(hint["body"], guide.read_text())
        self.assertNotIn(".zest/AGENTS.md", [s["evidence"]["path"] for s in envelope["files"]])
        guide.write_text("# Changed after preparation, must not enter the old request\n")
        newer = store.create(project["id"], target=self.target())
        self.assertNotEqual(run["approval_sha256"], newer["approval_sha256"])
        store.decide(run["id"], approved=True, approval_sha256=run["approval_sha256"])
        model = OfflineCognitionModel()
        result = CognitionEngine(self.state_dir, model=model).run(run["id"])
        self.assertEqual(result["status"], "completed")
        for purpose, payload in model.calls:
            if purpose != "project_review":
                self.assertEqual(payload["management_guidance"], hint)
        self.assertEqual(store.show(newer["id"])["status"], "awaiting_approval")

    def test_sensitive_or_linked_management_guidance_is_not_packaged(self):
        project = self.imported()
        guide = self.repo / ".zest" / "AGENTS.md"
        guide.write_text("sk-" + "x" * 40)
        store = CognitionStore(self.state_dir)
        with self.assertRaises(FileReadError) as caught:
            store.create(project["id"], target=self.target())
        self.assertEqual(caught.exception.code, "sensitive_cognition_output")
        guide.unlink()
        guide.symlink_to(self.repo / "README.md")
        with self.assertRaises(FileReadError) as caught:
            store.create(project["id"], target=self.target())
        self.assertEqual(caught.exception.code, "unsafe_resource_path")

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
    def test_existing_approval_without_guide_does_not_gain_new_management_content(self):
        project = self.imported()
        guide = self.repo / ".zest" / "AGENTS.md"
        guide.unlink()
        store = CognitionStore(self.state_dir)
        run = store.create(project["id"], target=self.target())
        self.assertNotIn("management_guidance", store.show(run["id"], include_envelope=True)["envelope"])
        code, out, err = self.cli("project", "guide", project["id"])
        self.assertEqual(code, 0, err)
        store.decide(run["id"], approved=True, approval_sha256=run["approval_sha256"])
        model = OfflineCognitionModel()
        self.assertEqual(CognitionEngine(self.state_dir, model=model).run(run["id"])["status"], "completed")
        self.assertTrue(model.calls)
        self.assertTrue(all("management_guidance" not in payload for _, payload in model.calls))

    def test_import_creates_ignored_private_resources_and_preserves_native_files(self):
        self.populated()
        (self.repo / "zest").mkdir()
        (self.repo / "zest" / "business.py").write_text("BUSINESS = True\n")
        self.commit("native zest package")
        before = {name: (self.repo / name).read_bytes() for name in ("app.py", "README.md", "zest/business.py")}
        head = self.git("rev-parse", "HEAD")
        index = self.git("ls-files", "--stage")
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 0, err)
        project = json.loads(out)
        directory = self.repo.resolve() / ".zest" / "state"
        self.assertEqual(project["resource_dir"], str(directory))
        marker = json.loads((self.repo / ".zest" / "workspace.json").read_text())
        self.assertEqual(marker["project_id"], project["id"])
        self.assertTrue((directory / "state.sqlite3").is_file())
        self.assertEqual(ProjectRegistry(directory).get_project(project["id"])["id"], project["id"])
        self.assertEqual(before, {name: (self.repo / name).read_bytes() for name in before})
        self.assertEqual(head, self.git("rev-parse", "HEAD"))
        self.assertEqual(index, self.git("ls-files", "--stage"))
        self.git("check-ignore", "--", ".zest/state/state.sqlite3")
        self.assertNotIn(b".zest/", self.git("status", "--porcelain", "--untracked-files=all"))
        self.assertFalse(project["git"]["dirty"])
        self.assertFalse((self.repo / ".gitignore").exists())
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_reimport_preserves_ignore_bytes_identity_and_saved_notes(self):
        self.populated()
        original = b"# existing\r\nlogs/\r\n!logs/example.log"
        (self.repo / ".gitignore").write_bytes(original)
        registry = ProjectRegistry(self.state_dir)
        project = registry.register(inspect_repository(self.repo), workspace=True)
        note = KnowledgeStore(self.state_dir).add(project, "Local note", "Keep this memory")
        after = (self.repo / ".gitignore").read_bytes()
        self.assertEqual(after, original)
        repeated = registry.register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(repeated["id"], project["id"])
        self.assertEqual((self.repo / ".gitignore").read_bytes(), after)
        self.assertEqual(KnowledgeStore(self.state_dir).get(note["id"])["body"], "Keep this memory")
        other_catalog = self.root / "other-catalog"
        reattached = ProjectRegistry(other_catalog).register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(reattached["id"], project["id"])
        self.assertEqual(KnowledgeStore(other_catalog).get(note["id"])["id"], note["id"])

    def test_symlink_directories_and_ignore_file_are_rejected_without_outside_writes(self):
        self.populated()
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / ".zest").symlink_to(outside, target_is_directory=True)
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 1)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.state_dir.exists())
        (self.repo / ".zest").unlink()
        target = outside / "ignore"
        target.write_text("ORIGINAL\n")
        (self.repo / ".zest").mkdir()
        (self.repo / ".zest" / ".gitignore").symlink_to(target)
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(), "ORIGINAL\n")
        self.assertFalse(self.state_dir.exists())

    def test_tracked_resources_and_unowned_state_are_not_overwritten(self):
        self.populated()
        directory = self.repo / ".zest"
        directory.mkdir()
        (directory / "old.md").write_text("tracked native content\n")
        self.commit("tracked .zest")
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("resources_already_tracked", err)
        self.assertFalse((self.repo / ".gitignore").exists())
        self.git("rm", "-r", ".zest")
        self.commit("remove tracked .zest")
        (directory / "state").mkdir(parents=True)
        (directory / "state" / "user.txt").write_text("unowned\n")
        code, out, err = self.cli("project", "import", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn("resource_directory_conflict", err)
        self.assertEqual((directory / "state" / "user.txt").read_text(), "unowned\n")

    def test_memory_index_and_survey_write_to_project_and_not_catalog(self):
        project = self.imported()
        directory = Path(project["resource_dir"])
        notes = KnowledgeStore(self.state_dir)
        note = notes.add(project=project, title="Goal", body="Addition example", scope="project")
        notes.confirm(note_id=note["id"])
        self.assertEqual(notes.for_context(project["id"], project["git"]["head_commit"])[0]["id"], note["id"])
        RetrievalIndex(self.state_dir).build(project)
        self.assertTrue(RetrievalIndex(self.state_dir).search(project, "add")["matches"])
        survey = SurveyStore(self.state_dir).build(project)
        self.assertEqual(SurveyStore(self.state_dir).show(survey["id"])["id"], survey["id"])
        for name in ("knowledge.sqlite3", "index.sqlite3", "surveys.sqlite3"):
            self.assertTrue((directory / name).is_file(), name)
            self.assertFalse((self.state_dir / name).exists(), name)

    def test_two_projects_keep_separate_databases_and_contexts(self):
        first = self.imported()
        other_root = self.root / "second"
        self.git("clone", str(self.repo), str(other_root))
        second = ProjectRegistry(self.state_dir).register(inspect_repository(other_root), workspace=True)
        store = KnowledgeStore(self.state_dir)
        first_note = store.add(first, "First", "FIRST_PROJECT_ONLY", scope="project")
        second_note = store.add(second, "Second", "SECOND_PROJECT_ONLY", scope="project")
        store.confirm(first_note["id"])
        store.confirm(second_note["id"])
        self.assertNotEqual(first["resource_dir"], second["resource_dir"])
        self.assertEqual([n["id"] for n in store.list_notes(first["id"])], [first_note["id"]])
        self.assertEqual([n["id"] for n in store.list_notes(second["id"])], [second_note["id"]])
        first_bytes = (Path(first["resource_dir"]) / "knowledge.sqlite3").read_bytes()
        self.assertNotIn(b"SECOND_PROJECT_ONLY", first_bytes)
        self.assertEqual(record_state(self.state_dir, "knowledge", second_note["id"]), Path(second["resource_dir"]))
        with self.assertRaises(FileReadError) as caught:
            KnowledgeStore(first["resource_dir"]).add(second, "Wrong destination", "Do not mix")
        self.assertEqual(caught.exception.code, "resource_project_mismatch")

    def test_legacy_notes_and_index_are_read_without_copy_or_reconfirmation(self):
        self.populated()
        registry = ProjectRegistry(self.state_dir)
        project = registry.register(inspect_repository(self.repo))  # Historical layout.
        store = KnowledgeStore(self.state_dir)
        old = store.add(project, "Old", "Original confirmed decision", scope="project")
        store.confirm(old["id"])
        RetrievalIndex(self.state_dir).build(project)
        original = (self.state_dir / "knowledge.sqlite3").read_bytes()
        attached = registry.register(inspect_repository(self.repo), workspace=True)
        self.assertEqual(attached["id"], project["id"])
        self.assertEqual(store.for_context(project["id"], project["git"]["head_commit"])[0]["id"], old["id"])
        repeated = store.add(project, "Old", "Original confirmed decision", scope="project", note_id=old["id"])
        self.assertEqual(repeated["status"], "confirmed")
        self.assertTrue(RetrievalIndex(self.state_dir).search(project, "add")["matches"])
        new = store.add(project, "New", "New local decision", scope="project")
        self.assertEqual({n["id"] for n in store.list_notes(project["id"])}, {old["id"], new["id"]})
        self.assertEqual(original, (self.state_dir / "knowledge.sqlite3").read_bytes())
        store.invalidate(old["id"], "Withdraw old decision")
        self.assertEqual(store.for_context(project["id"], project["git"]["head_commit"]), [])
        with closing(sqlite3.connect(Path(attached["resource_dir"]) / "knowledge.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM notes WHERE id=?", (old["id"],)).fetchone()[0], 0)

    def test_missing_workspace_is_not_silently_recreated_by_reads(self):
        project = self.imported()
        marker = self.repo / ".zest" / "workspace.json"
        saved = marker.read_bytes()
        marker.unlink()
        with self.assertRaises(FileReadError) as caught:
            KnowledgeStore(self.state_dir).list_notes(project["id"])
        self.assertEqual(caught.exception.code, "resource_identity_mismatch")
        self.assertFalse(marker.exists())
        marker.write_bytes(saved)
        self.assertEqual(project_states(self.state_dir, project["id"])[0], Path(project["resource_dir"]))

    def test_duplicate_record_copies_are_rejected_instead_of_reused(self):
        project = self.imported()
        store = KnowledgeStore(self.state_dir)
        note = store.add(project, "Original", "Canonical memory")
        database = Path(project["resource_dir"]) / "knowledge.sqlite3"
        (self.state_dir / "knowledge.sqlite3").write_bytes(database.read_bytes())
        for operation in (lambda: store.get(note["id"]), lambda: store.list_notes(project["id"])):
            with self.assertRaises(FileReadError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "resource_record_conflict")

    def test_shared_store_can_write_two_project_memories_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.imported()
        other_root = self.root / "concurrent-project"
        self.git("clone", str(self.repo), str(other_root))
        second = ProjectRegistry(self.state_dir).register(inspect_repository(other_root), workspace=True)
        store = KnowledgeStore(self.state_dir)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.add, project, title, title) for project, title in
                       ((first, "Concurrent first"), (second, "Concurrent second"))]
            notes = [future.result() for future in futures]
        self.assertEqual(store.state_dir, self.state_dir.resolve())
        for project, note in zip((first, second), notes):
            with closing(sqlite3.connect(Path(project["resource_dir"]) / "knowledge.sqlite3")) as conn:
                rows = conn.execute("SELECT project_id,id FROM notes").fetchall()
            self.assertEqual(rows, [(project["id"], note["id"])])

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
    def test_task_history_and_exact_approval_survive_resource_attachment(self):
        self.populated()
        registry = ProjectRegistry(self.state_dir)
        project = registry.register(inspect_repository(self.repo))
        model = OfflineCognitionModel()
        runner = TaskRunner(self.state_dir, model=model)
        old = runner.create(project, "What is add?", files=["app.py"])
        attached = registry.register(inspect_repository(self.repo), workspace=True)
        new = runner.create(attached, "Explain add", files=["app.py"])
        self.assertEqual({r["task_id"] for r in runner.list_tasks()}, {old["task_id"], new["task_id"]})
        self.assertEqual(runner.show(old["task_id"])["payload_sha256"], old["payload_sha256"])
        self.assertEqual(record_state(self.state_dir, "task", old["task_id"]), self.state_dir.resolve())
        self.assertEqual(record_state(self.state_dir, "task", new["task_id"]), Path(attached["resource_dir"]))
        self.assertEqual(runner.decide(old["task_id"], approved=False)["status"], "rejected")
        self.assertEqual(runner.decide(new["task_id"], approved=False)["status"], "rejected")
        self.assertEqual(model.calls, [])

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
    def test_cognition_worker_uses_project_ledger_locks_and_checkpoints(self):
        project = self.imported()
        store = CognitionStore(self.state_dir)
        record = store.create(project["id"], target=self.target())
        directory = Path(project["resource_dir"])
        self.assertEqual(record_state(self.state_dir, "cognition", record["id"]), directory)
        store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        model = OfflineCognitionModel()
        worker(self.state_dir, once=True, model=model)
        result = store.show(record["id"])
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertTrue(list((directory / "cognition-checkpoints").glob("*.sqlite3")))
        self.assertTrue((directory / "cognition-locks").is_dir())
        self.assertFalse((self.state_dir / "cognition.sqlite3").exists())
        self.assertFalse((self.state_dir / "cognition-checkpoints").exists())
        self.assertEqual(len(store.for_context(project["id"], result["commit"])), 3)

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
    def test_refresh_uses_legacy_baseline_but_new_package_is_project_local(self):
        from tests import test_cognition_refresh as refresh_fixtures
        project, old = refresh_fixtures.CognitionRefreshTest.project(self)
        base_run = refresh_fixtures.CognitionRefreshTest.baseline(self, project, old)
        attached = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo), workspace=True)
        (self.repo / "README.md").write_text("# New version\n")
        new = self.commit("updated docs")
        preview = build_refresh_preview(self.state_dir, project["id"])
        self.assertEqual(preview["baseline"]["run_id"], base_run)
        record = prepare_refresh(self.state_dir, project["id"], target=self.target())
        self.assertEqual(record["commit"], new)
        self.assertEqual(record_state(self.state_dir, "cognition", record["id"]), Path(attached["resource_dir"]))
        self.assertEqual(record_state(self.state_dir, "cognition", base_run), self.state_dir.resolve())
        model = OfflineCognitionModel()
        store = CognitionStore(self.state_dir)
        store.decide(record["id"], approved=True, approval_sha256=record["approval_sha256"])
        result = CognitionEngine(self.state_dir, model=model).run(record["id"])
        self.assertEqual(result["status"], "completed", result["error"])

    def test_zest_resources_are_never_source_evidence_even_if_force_committed(self):
        project = self.imported()
        (self.repo / ".zest" / "note.md").write_text("Generated memory must not become project source\n")
        self.git("add", "-f", ".zest/note.md")
        self.git("commit", "-m", "test forced resource")
        with self.assertRaises(FileReadError) as caught:
            read_project_file(project, ".zest/note.md")
        self.assertEqual(caught.exception.code, "blocked_path")
        survey = SurveyStore(self.state_dir).build(project)
        self.assertTrue(all(not s["evidence"]["path"].startswith(".zest/") for s in survey["sources"]))

    def test_repair_verification_and_source_selection_use_project_artifacts(self):
        from tests.test_verification import OfflineVerificationEngine
        from zest.repairs import RepairStore
        from zest.verification import VerificationStore
        from zest.releases import ReleaseStore
        project = self.imported()
        directory = Path(project["resource_dir"])
        source = read_project_file(project, "app.py")
        repairs = RepairStore(self.state_dir)
        pending = repairs.create(project, {"summary": "Offline storage fixture", "changes": [{
            "path": "app.py", "expected_sha256": source["content_sha256"],
            "content": "def add(a, b):\n    return a + b + 0\n"}]})
        repair = repairs.decide(pending["id"], approved=True, plan_sha256=pending["plan_sha256"])
        verification_store = VerificationStore(self.state_dir, engine=OfflineVerificationEngine())
        pending_verification = verification_store.create(repair["id"], "offline_fixture")
        verification = verification_store.decide(pending_verification["id"], approved=True,
                                                 plan_sha256=pending_verification["plan_sha256"])
        # Explicit offline gate: this test does not certify Docker or a release.
        with patch("zest.releases._validated_verification", return_value=(verification, repair)):
            release = ReleaseStore(self.state_dir).promote(verification["id"])
        self.assertEqual(record_state(self.state_dir, "release", release["id"]), directory)
        for filename in ("repairs.sqlite3", "verifications.sqlite3", "releases.sqlite3"):
            self.assertTrue((directory / filename).is_file(), filename)
            self.assertFalse((self.state_dir / filename).exists(), filename)
        self.assertTrue((directory / "repair-artifacts" / repair["id"]).is_dir())
        self.assertTrue((directory / "verification-artifacts" / verification["id"]).is_dir())
        self.assertEqual(verification["status"], "passed")
        self.assertEqual(release["status"], "awaiting_approval")

    @unittest.skipUnless(importlib.util.find_spec("fastapi"), "web extra required")
    def test_readonly_web_reads_project_memory_without_creating_catalog_ledgers(self):
        from fastapi.testclient import TestClient
        from zest.web import create_app
        project = self.imported()
        note = KnowledgeStore(self.state_dir).add(project, "Web note", "Project local memory")
        database = Path(project["resource_dir"]) / "knowledge.sqlite3"
        before = database.read_bytes()
        with TestClient(create_app(self.state_dir)) as client:
            response = client.get(f"/api/projects/{project['id']}/knowledge")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], note["id"])
        self.assertEqual(database.read_bytes(), before)
        self.assertFalse((self.state_dir / "knowledge.sqlite3").exists())

    @unittest.skipUnless(importlib.util.find_spec("fastmcp"), "mcp extra required")
    def test_readonly_mcp_uses_local_index_and_confirmed_memory(self):
        import asyncio
        from fastmcp import Client
        from zest.mcp_server import create_server
        project = self.imported()
        note = KnowledgeStore(self.state_dir).add(project, "MCP note", "Project local rule", scope="project")
        KnowledgeStore(self.state_dir).confirm(note["id"])
        RetrievalIndex(self.state_dir).build(project)
        async def query():
            async with Client(create_server(project["id"], self.state_dir)) as client:
                knowledge = await client.call_tool("confirmed_knowledge", {})
                index = await client.call_tool("indexed_search", {"query": "add"})
            return knowledge.data, index.data
        knowledge, index = asyncio.run(query())
        self.assertEqual(knowledge["data"][0]["id"], note["id"])
        self.assertTrue(index["data"]["matches"])
        self.assertFalse((self.state_dir / "knowledge.sqlite3").exists())
        self.assertFalse((self.state_dir / "index.sqlite3").exists())

    @unittest.skipUnless(importlib.util.find_spec("langgraph") and importlib.util.find_spec("fastmcp"), "ai/mcp extras required")
    def test_cli_approval_launches_worker_at_record_owner(self):
        project = self.imported()
        record = CognitionStore(self.state_dir).create(project["id"], target=self.target())
        with patch("zest.cognition.start_worker", return_value={"status": "fixture_not_started"}) as launch:
            code, out, err = self.cli("cognition", "approve", record["id"], "--approval-sha256", record["approval_sha256"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "queued")
        launch.assert_called_once_with(Path(project["resource_dir"]))


if __name__ == "__main__":
    unittest.main()
