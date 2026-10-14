import asyncio
from dataclasses import replace
import importlib.util
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_projects as fixtures
from zest.git import inspect_repository
from zest.models import ModelSettings
from zest.storage import ProjectRegistry
from zest.tasks import TaskRunner
from zest.terminal import create_workspace, display_text


class OfflineQuestionModel:
    def __init__(self, *, wait=False):
        self.settings = ModelSettings("deepseek", "terminal-fixture", "https://fixture.invalid", "fixture-key")
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not wait:
            self.release.set()

    def complete(self, payload, *, purpose="qa"):
        self.calls.append(payload)
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("fixture did not release")
        return {"text": json.dumps({"status": "answered", "answer": "这是一个示例项目。[E1] [link=bad]作为文本显示[/link]",
                                    "citations": ["E1"], "unknowns": ["尚无运行日志"]}), "reported_model": "offline-fixture"}


@unittest.skipUnless(importlib.util.find_spec("textual") and importlib.util.find_spec("langgraph"), "tui/ai extras required")
class TerminalTest(unittest.IsolatedAsyncioTestCase):
    setUpBase = fixtures.ProjectsTest.setUp
    git = fixtures.ProjectsTest.git
    commit = fixtures.ProjectsTest.commit

    def setUp(self):
        self.setUpBase()
        (self.repo / "README.md").write_text("# Demo\n这是一个示例。\n")
        self.commit()
        self.project = ProjectRegistry(self.state_dir).register(inspect_repository(self.repo))
        self.model = OfflineQuestionModel()
        self.runner = TaskRunner(self.state_dir, model=self.model)

    async def wait_for(self, pilot, predicate):
        for _ in range(150):
            await pilot.pause(0.02)
            if predicate():
                return
        self.fail("UI did not reach expected state")

    async def prepare(self, app, pilot):
        from textual.widgets import Input
        app.query_one("#question", Input).value = "这个项目是什么？"
        await pilot.click("#submit")
        await self.wait_for(pilot, lambda: app.pending is not None)
        await pilot.pause()

    async def approve(self, app, pilot):
        await pilot.click("#consent")
        await pilot.pause()
        await pilot.click("#approve")

    async def test_explicit_consent_and_complete_loop_preserve_payload_and_sources(self):
        from textual.widgets import Button, TextArea
        app = create_workspace(self.state_dir, runner=self.runner)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.prepare(app, pilot)
            record = app.pending
            preview = json.loads(app.screen.query_one("#request-json", TextArea).text)
            self.assertEqual(preview["payload_sha256"], record["payload_sha256"])
            self.assertNotIn("fixture-key", json.dumps(preview))
            self.assertTrue(app.screen.query_one("#approve", Button).disabled)
            await pilot.click("#approve")
            self.assertEqual(self.model.calls, [])
            await self.approve(app, pilot)
            await self.wait_for(pilot, lambda: app.last_record and app.last_record["status"] == "completed")
            self.assertEqual(len(self.model.calls), 1)
            self.assertEqual(self.model.calls[0], preview["payload"])
            self.assertEqual(app.step, 4)
            self.assertIn("[link=bad]", str(app.messages))
            self.assertIn("来源 E1", str(app.messages))
            self.assertFalse((self.state_dir / "repairs.sqlite3").exists())

    async def test_reject_then_new_question_has_no_implicit_approval(self):
        app = create_workspace(self.state_dir, runner=self.runner)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.prepare(app, pilot)
            first = app.pending["task_id"]
            await pilot.click("#reject")
            await self.wait_for(pilot, lambda: not app.busy and app.pending is None)
            self.assertEqual(self.runner.show(first)["status"], "rejected")
            self.assertEqual(self.model.calls, [])
            await self.prepare(app, pilot)
            self.assertNotEqual(app.pending["task_id"], first)
            self.assertEqual(self.model.calls, [])
            await pilot.press("escape")
            await self.wait_for(pilot, lambda: not app.busy and app.pending is None)

    async def test_active_request_stays_responsive_and_cannot_duplicate_or_pretend_cancel(self):
        from textual.widgets import Static
        model = OfflineQuestionModel(wait=True)
        self.addCleanup(model.release.set)
        app = create_workspace(self.state_dir, runner=TaskRunner(self.state_dir, model=model))
        async with app.run_test(size=(110, 40)) as pilot:
            await self.prepare(app, pilot)
            await self.approve(app, pilot)
            await self.wait_for(pilot, model.entered.is_set)
            self.assertTrue(app.busy)
            app.prepare_question()
            app.decision(True)
            await pilot.press("ctrl+q")
            await pilot.pause(0.3)
            self.assertTrue(app.is_running)
            self.assertTrue(app.busy)
            self.assertGreater(app.elapsed, 0)
            self.assertIn("退出不能等同取消", str(app.query_one("#notice", Static).render()))
            self.assertEqual(len(model.calls), 1)
            model.release.set()
            await self.wait_for(pilot, lambda: not app.busy)

    async def test_changed_target_fails_without_model_call(self):
        app = create_workspace(self.state_dir, runner=self.runner)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.prepare(app, pilot)
            self.model.settings = replace(self.model.settings, name="changed-target")
            await self.approve(app, pilot)
            await self.wait_for(pilot, lambda: not app.busy and app.pending is None)
            self.assertEqual(app.last_record["status"], "failed")
            self.assertEqual(app.last_record["error"]["code"], "model_target_changed")
            self.assertEqual(self.model.calls, [])

    async def test_known_credentials_are_not_echoed_saved_or_sent(self):
        from textual.widgets import Input
        app = create_workspace(self.state_dir, runner=self.runner)
        secret = "sk-" + "x" * 24
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#question", Input).value = secret
            app.prepare_question()
            await pilot.pause()
            self.assertEqual(app.query_one("#question", Input).value, "")
            self.assertNotIn(secret, str(app.messages))
            self.assertFalse(self.runner.db_path.exists())
            self.assertEqual(self.model.calls, [])

    async def test_empty_workspace_is_read_only_and_non_tty_cli_is_rejected(self):
        from textual.widgets import Input
        from zest.cli import main
        import contextlib
        import io
        absent = self.root / "absent"
        app = create_workspace(absent, runner=self.runner)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#question", Input).disabled)
            self.assertFalse(absent.exists())
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(main(["--state-dir", str(absent), "ui"]), 1)
        self.assertIn("tui_requires_terminal", output.getvalue())
        self.assertFalse(absent.exists())

    def test_plain_terminal_text_does_not_interpret_escape_or_markup(self):
        self.assertEqual(display_text("\x1b[31m[bold]raw[/bold]"), "\\x1b[31m[bold]raw[/bold]")
        self.assertNotIn("sk-", display_text("sk-" + "x" * 24))
