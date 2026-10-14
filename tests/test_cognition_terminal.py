import asyncio
import importlib.util
import json
import threading
import unittest

from tests import test_projects as fixtures
from tests.test_cognition import OfflineCognitionModel
from zest.cognition import CognitionEngine
from zest.cognition_store import CognitionStore
from zest.git import inspect_repository
from zest.storage import ProjectRegistry
from zest.terminal import create_workspace


@unittest.skipUnless(importlib.util.find_spec("textual") and importlib.util.find_spec("fastmcp"), "tui/mcp extras required")
class CognitionTerminalTest(unittest.IsolatedAsyncioTestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def setUp(self):
        self.setUpBase()
        (self.repo / "README.md").write_text("# Offline fixture\n")
        (self.repo / "app.py").write_text("def add(a,b):\n    return a+b\n")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.model = OfflineCognitionModel(block=True)
        target = {"provider": "deepseek", "model": self.model.settings.name, "base_url": self.model.settings.base_url}
        class FixtureStore(CognitionStore):
            def create(store, project_id, **kwargs):
                return super().create(project_id, target=target, **kwargs)
        self.store = FixtureStore(self.state_dir)
        self.threads = []
        def launch(state_dir):
            thread = threading.Thread(target=CognitionEngine(state_dir, model=self.model).run, args=(self.store.list_runs(self.project["id"])[0]["id"],))
            self.threads.append(thread)
            thread.start()
        self.launch = launch
        self.addCleanup(self.cleanup_threads)

    def cleanup_threads(self):
        self.model.release.set()
        for thread in self.threads:
            thread.join(timeout=15)

    async def wait_for(self, pilot, predicate):
        for _ in range(200):
            await pilot.pause(0.03)
            if predicate():
                return
        self.fail("Cognition UI did not reach expected state")

    async def test_preview_consent_background_progress_and_return_to_main_conversation(self):
        from textual.widgets import Button, TextArea, Input
        app = create_workspace(self.state_dir, cognition_store=self.store, cognition_launch=self.launch)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.click("#cognition")
            await pilot.click("#cognition-create")
            await self.wait_for(pilot, lambda: getattr(app.screen, "record", None) is not None)
            record = app.screen.record
            self.assertEqual(record["status"], "awaiting_approval")
            self.assertEqual(self.model.calls, [])
            await pilot.click("#cognition-preview")
            await self.wait_for(pilot, lambda: app.screen.__class__.__name__ == "ScopePreview")
            preview = json.loads(app.screen.query_one("#scope-json", TextArea).text)
            self.assertEqual(preview["approval_sha256"], record["approval_sha256"])
            self.assertIn("return a+b", json.dumps(preview))
            self.assertTrue(app.screen.query_one("#scope-approve", Button).disabled)
            await pilot.click("#scope-consent")
            await pilot.click("#scope-approve")
            await self.wait_for(pilot, lambda: self.model.both_entered.is_set())
            await self.wait_for(pilot, lambda: app.screen.record["status"] == "investigating")
            self.assertIn("正在调查", app.screen.query_one("#cognition-result", TextArea).text)
            await pilot.click("#cognition-close")
            await pilot.pause()
            self.assertEqual(app.screen.__class__.__name__, "Screen")
            self.assertFalse(app.busy)
            app.query_one("#question", Input).value = "可以继续讨论项目"
            self.assertFalse(app.query_one("#submit", Button).disabled)
            self.model.release.set()
            await self.wait_for(pilot, lambda: self.store.show(record["id"])["status"] == "completed")
            await pilot.click("#cognition")
            await self.wait_for(pilot, lambda: getattr(app.screen, "record", None) and app.screen.record["status"] == "completed")
            self.assertIn("add", app.screen.query_one("#cognition-result", TextArea).text)

    async def test_escape_from_preview_does_not_decide_and_reject_does_not_launch(self):
        app = create_workspace(self.state_dir, cognition_store=self.store, cognition_launch=self.launch)
        record = self.store.create(self.project["id"])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+u")
            await self.wait_for(pilot, lambda: getattr(app.screen, "record", None) is not None)
            await pilot.click("#cognition-preview")
            await self.wait_for(pilot, lambda: app.screen.__class__.__name__ == "ScopePreview")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.store.show(record["id"])["status"], "awaiting_approval")
            await pilot.click("#cognition-preview")
            await self.wait_for(pilot, lambda: app.screen.__class__.__name__ == "ScopePreview")
            await pilot.click("#scope-reject")
            await self.wait_for(pilot, lambda: self.store.show(record["id"])["status"] == "rejected")
            self.assertEqual(self.threads, [])
            self.assertEqual(self.model.calls, [])
