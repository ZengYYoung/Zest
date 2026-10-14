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

    async def open_history(self, app, pilot, task_id):
        from textual.widgets import Input
        await pilot.click("#history")
        await self.wait_for(pilot, lambda: len(app.screen_stack) > 1)
        app.screen.query_one("#history-id", Input).value = task_id
        await pilot.click("#history-open")
        await self.wait_for(pilot, lambda: not app.busy and len(app.screen_stack) == 1)

    async def test_restart_reads_completed_result_without_call_or_new_task(self):
        record = self.runner.create(self.project, "这个项目是什么？")
        self.runner.decide(record["task_id"], approved=True, payload_sha256=record["payload_sha256"])
        model = OfflineQuestionModel()
        restarted = TaskRunner(self.state_dir, model=model)
        app = create_workspace(self.state_dir, runner=restarted)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.open_history(app, pilot, record["task_id"])
            self.assertEqual(app.last_record["status"], "completed")
            self.assertIn("来源 E1", str(app.messages))
            self.assertIn(record["context"]["commit"], str(app.messages))
            self.assertEqual(model.calls, [])
            self.assertEqual(len(restarted.list_tasks()), 1)
            failed = restarted.create(self.project, "")
            await self.open_history(app, pilot, failed["task_id"])
            self.assertEqual(app.last_record["status"], "failed")
            self.assertEqual(app.step, 0)
            self.assertEqual(model.calls, [])

    async def test_restart_pending_requires_separate_review_and_preserves_original_version(self):
        from textual.widgets import Button, TextArea
        record = self.runner.create(self.project, "这个项目是什么？")
        (self.repo / "README.md").write_text("# Changed\nNew version\n")
        self.commit("change after preparing")
        model = OfflineQuestionModel()
        restarted = TaskRunner(self.state_dir, model=model)
        app = create_workspace(self.state_dir, runner=restarted)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.open_history(app, pilot, record["task_id"])
            self.assertIsNone(app.pending)
            self.assertFalse(app.query_one("#review-saved", Button).disabled)
            self.assertEqual(model.calls, [])
            await pilot.click("#review-saved")
            await self.wait_for(pilot, lambda: app.pending is not None)
            await pilot.pause()
            preview = json.loads(app.screen.query_one("#request-json", TextArea).text)
            self.assertEqual(preview["payload_sha256"], record["payload_sha256"])
            self.assertEqual(app.pending["context"]["commit"], record["context"]["commit"])
            self.assertTrue(app.screen.query_one("#approve", Button).disabled)
            await self.approve(app, pilot)
            await self.wait_for(pilot, lambda: not app.busy and app.pending is None)
            self.assertEqual(app.last_record["task_id"], record["task_id"])
            self.assertEqual(model.calls, [preview["payload"]])
            self.assertEqual(len(restarted.list_tasks()), 1)

    async def test_history_never_resumes_unknown_execution_or_repair_or_other_project(self):
        from textual.widgets import Button
        unknown = self.runner.create(self.project, "这个项目是什么？")
        with self.runner._open() as (_, graph):
            graph.update_state(self.runner._config(unknown["task_id"]), {"status": "approved"}, as_node="approval")
        self.assertEqual(self.runner.show(unknown["task_id"])["status"], "pending_execution")
        other = dict(self.project, id="00000000-0000-0000-0000-000000000001")
        foreign = self.runner.create(other, "FOREIGN BODY MUST NOT APPEAR")
        repair = self.runner.create(self.project, "REPAIR BODY MUST NOT APPEAR", files=["README.md"], purpose="repair_proposal")
        app = create_workspace(self.state_dir, runner=self.runner)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.open_history(app, pilot, unknown["task_id"])
            self.assertEqual(app.last_record["status"], "pending_execution")
            self.assertTrue(app.query_one("#review-saved", Button).disabled)
            self.assertIn("执行结果未知", str(app.messages))
            for record in (foreign, repair):
                await self.open_history(app, pilot, record["task_id"])
                self.assertIsNone(app.last_record)
                self.assertIn("history_task_scope_mismatch", str(app.messages))
            self.assertNotIn("BODY MUST NOT APPEAR", str(app.messages))
            self.assertEqual(self.model.calls, [])
            self.assertEqual(self.runner.show(unknown["task_id"])["status"], "pending_execution")

    async def test_history_filters_picker_and_rechecks_status_before_review(self):
        from textual.widgets import Button, Select
        record = self.runner.create(self.project, "这个项目是什么？")
        other = dict(self.project, id="00000000-0000-0000-0000-000000000001")
        self.runner.create(other, "another project")
        self.runner.create(self.project, "repair", files=["README.md"], purpose="repair_proposal")
        app = create_workspace(self.state_dir, runner=self.runner)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.click("#history")
            await self.wait_for(pilot, lambda: len(app.screen_stack) > 1)
            self.assertEqual([r["task_id"] for r in app.screen.records], [record["task_id"]])
            app.screen.query_one("#history-task", Select).value = record["task_id"]
            await pilot.click("#history-open")
            await self.wait_for(pilot, lambda: not app.busy and len(app.screen_stack) == 1)
            self.runner.decide(record["task_id"], approved=False)
            await pilot.click("#review-saved")
            await self.wait_for(pilot, lambda: not app.busy)
            self.assertIsNone(app.pending)
            self.assertEqual(app.last_record["status"], "rejected")
            self.assertEqual(app.step, 1)
            self.assertTrue(app.query_one("#review-saved", Button).disabled)
            self.assertEqual(self.model.calls, [])

    async def test_close_history_and_invalid_id_never_mutate_pending_task(self):
        record = self.runner.create(self.project, "这个项目是什么？")
        app = create_workspace(self.state_dir, runner=self.runner)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+h")
            await self.wait_for(pilot, lambda: len(app.screen_stack) > 1)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.runner.show(record["task_id"])["status"], "awaiting_approval")
            await self.open_history(app, pilot, "../../bad")
            self.assertIn("invalid_task_id", str(app.messages))
            self.assertEqual(self.runner.show(record["task_id"])["status"], "awaiting_approval")
            self.assertEqual(self.model.calls, [])

    async def test_project_switch_clears_old_history_and_older_id_still_opens(self):
        from textual.widgets import Button, Select
        record = self.runner.create(self.project, "这个项目是什么？")
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        import subprocess
        from zest.git import git_environment
        subprocess.run(["git", "-C", str(other_repo), "init", "-b", "main"], env=git_environment(), capture_output=True, check=True)
        subprocess.run(["git", "-C", str(other_repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--allow-empty", "-m", "other"], env=git_environment(), capture_output=True, check=True)
        other = ProjectRegistry(self.state_dir).register(inspect_repository(other_repo))
        app = create_workspace(self.state_dir, runner=self.runner)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#project", Select).value = self.project["id"]
            await pilot.pause()
            with patch.object(self.runner, "list_tasks", return_value=[]):
                await self.open_history(app, pilot, record["task_id"])
            self.assertFalse(app.query_one("#review-saved", Button).disabled)
            app.query_one("#project", Select).value = other["id"]
            await pilot.pause()
            self.assertIsNone(app.last_record)
            self.assertTrue(app.query_one("#review-saved", Button).disabled)
            self.assertEqual(list(app.messages), [])
            self.assertEqual(self.runner.show(record["task_id"])["status"], "awaiting_approval")
            self.assertEqual(self.model.calls, [])

    def test_plain_terminal_text_does_not_interpret_escape_or_markup(self):
        self.assertEqual(display_text("\x1b[31m[bold]raw[/bold]"), "\\x1b[31m[bold]raw[/bold]")
        self.assertNotIn("sk-", display_text("sk-" + "x" * 24))
