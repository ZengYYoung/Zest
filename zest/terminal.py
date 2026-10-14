"""Interactive local question workspace, using the existing durable task service."""

import asyncio
from collections import deque
import json
from pathlib import Path
import re
import sys
import time

from .files import CREDENTIAL_PATTERNS, FileReadError
from .qa import model_payload
from .storage import ProjectRegistry
from .tasks import TaskRunner


def display_text(value):
    """Do not interpret terminal controls, Rich markup, or credential-like text."""
    raw = str(value)
    if re.search(CREDENTIAL_PATTERNS[-1], raw.encode("utf-8")):
        return "[已隐藏含私钥的内容]"
    for pattern in CREDENTIAL_PATTERNS:
        raw = re.sub(pattern.decode(), "[已隐藏凭据]", raw)
    return "".join(char if ord(char) >= 32 or char in "\n\t" else f"\\x{ord(char):02x}" for char in raw).replace("\x7f", "\\x7f")


def create_workspace(state_dir, *, runner=None):
    # Optional dependency: importing the base CLI must not require Textual.
    try:
        from rich.text import Text
        from textual import on, work
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.screen import ModalScreen
        from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RichLog, Select, Static, TextArea
    except ImportError as exc:
        raise FileReadError("tui_dependencies_missing", "请执行 uv sync --extra ai --extra tui。") from exc

    class RequestPreview(ModalScreen[bool]):
        BINDINGS = [("escape", "reject", "拒绝并返回")]
        CSS = """
        RequestPreview { align: center middle; }
        #preview-box { width: 96%; height: 95%; border: solid #888888; padding: 1; background: $surface; }
        #preview-title { height: 2; text-style: bold; }
        #request-info { height: auto; max-height: 6; margin-bottom: 1; }
        #request-json { height: 1fr; }
        #consent { height: auto; margin: 1 0; }
        #preview-actions { height: 3; }
        #preview-actions Button { width: 1fr; }
        """

        def __init__(self, record):
            super().__init__()
            self.record = record

        def compose(self) -> ComposeResult:
            context = self.record["context"]
            envelope = {"task_id": self.record["task_id"], "payload_sha256": self.record["payload_sha256"],
                        "target": self.record["target"], "payload": model_payload(context)}
            with Vertical(id="preview-box"):
                yield Label("审阅本次模型数据请求", id="preview-title")
                yield Static(Text(f"来源版本：{context['commit']}\n批准仅覆盖以下 target / payload；不授予修改或测试权限。"), id="request-info")
                yield TextArea(json.dumps(envelope, ensure_ascii=False, indent=2), read_only=True, id="request-json")
                yield Checkbox("我已检查完整来源和目标，允许发送这份请求", id="consent")
                with Horizontal(id="preview-actions"):
                    yield Button("拒绝并返回", id="reject")
                    yield Button("批准本次请求", id="approve", disabled=True)

        @on(Checkbox.Changed, "#consent")
        def consent_changed(self, event):
            self.query_one("#approve", Button).disabled = not event.value

        @on(Button.Pressed)
        def choose(self, event):
            if event.button.id == "reject":
                self.dismiss(False)
            elif event.button.id == "approve" and self.query_one("#consent", Checkbox).value:
                self.dismiss(True)

        def action_reject(self):
            self.dismiss(False)

    class Workspace(App):
        TITLE = "Zest · 项目维护工作台"
        BINDINGS = [("ctrl+q", "quit_workspace", "退出"), ("ctrl+c", "quit_workspace", "退出"),
                    ("ctrl+l", "focus_question", "输入问题")]
        CSS = """
        Screen { background: $background; }
        #project { margin: 0 1; height: 3; }
        #body { height: 1fr; margin: 0 1; }
        #conversation { width: 1fr; border: round #cccccc; padding: 0 1; }
        #progress { width: 30; border: round #cccccc; padding: 1; margin-left: 1; }
        #stage { height: auto; }
        #notice { height: auto; min-height: 1; max-height: 4; margin: 0 1; }
        #files { margin: 0 1; height: 3; }
        #question-row { height: 3; margin: 0 1; }
        #question { width: 1fr; }
        #submit { width: 12; margin-left: 1; }
        """

        def __init__(self):
            super().__init__()
            self.theme = "textual-light"
            self.registry = ProjectRegistry(state_dir)
            self.runner = runner or TaskRunner(state_dir)
            self.projects = self.registry.list_projects()
            self.project = self.projects[0] if self.projects else None
            self.pending = None
            self.busy = False
            self.stage = "等待问题"
            self.step = 0
            self.started = None
            self.elapsed = 0.0
            self.messages = deque(maxlen=40)
            self.last_record = None
            self.outcome = None

        def compose(self) -> ComposeResult:
            yield Header()
            options = [(Text(display_text(Path(project["root_path"]).name)), project["id"]) for project in self.projects]
            yield Select(options, value=self.project["id"] if self.project else Select.NULL,
                         allow_blank=not bool(options), prompt="选择已登记项目", id="project", disabled=not bool(options))
            with Horizontal(id="body"):
                yield RichLog(wrap=True, min_width=20, markup=False, highlight=False, max_lines=3000, id="conversation")
                with Vertical(id="progress"):
                    yield Static("阶段时间线", markup=False)
                    yield Static("", markup=False, id="stage")
            yield Static("只生成调查回答。修复、测试与源码选择仍使用现有子命令。", markup=False, id="notice")
            yield Input(placeholder="来源文件：相对路径，逗号分隔；留空使用现有默认来源", max_length=5120, id="files", disabled=not bool(options))
            with Horizontal(id="question-row"):
                yield Input(placeholder="向这个项目提问（最多 2000 字符）", id="question", max_length=2000, disabled=not bool(options))
                yield Button("准备问题", id="submit", disabled=not bool(options))
            yield Footer()

        def on_mount(self):
            self.set_interval(0.25, self.update_progress)
            self.append("Zest", "选择项目后持续提问。每次先在本地准备完整来源，再由你单独批准请求。")
            if not self.project:
                self.append("Zest", "尚未登记项目。请先用 project init 或 project import 接入，然后重新启动工作台。")
            self.update_progress()

        def append(self, role, message):
            value = display_text(message)
            self.messages.append((role, value))
            self.query_one("#conversation", RichLog).write(Text(f"{role}\n{value}\n"))

        def update_progress(self):
            if self.busy and self.started is not None:
                self.elapsed = time.monotonic() - self.started
            phases = ["本地准备来源", "审阅 / 批准请求", "分析 / 核对结果", "展示回答与引用"]
            lines = [f"当前：{self.stage}", f"工作耗时：{self.elapsed:.1f} 秒", ""]
            for n, label in enumerate(phases):
                marker = '✓' if n < self.step else '▶' if n == self.step else '○'
                if self.outcome == "rejected" and n >= 1:
                    marker = '✕' if n == 1 else '—'
                elif self.outcome == "failed" and n >= self.step:
                    marker = '✕' if n == self.step else '—'
                lines.append(f"{marker} {label}")
            if self.last_record:
                lines.extend(["", "任务 ID", self.last_record["task_id"]])
            self.query_one("#stage", Static).update(Text("\n".join(lines)))

        def controls(self):
            blocked = self.busy or self.pending is not None or self.project is None
            for selector in ("#project", "#files", "#question", "#submit"):
                self.query_one(selector).disabled = blocked

        @on(Select.Changed, "#project")
        def select_project(self, event):
            if not self.busy and self.pending is None:
                self.project = self.registry.get_project(str(event.value)) if event.value is not Select.NULL else None
                self.controls()

        @on(Input.Submitted, "#question")
        @on(Button.Pressed, "#submit")
        def prepare_question(self):
            if self.busy or self.pending or not self.project:
                return
            question = self.query_one("#question", Input).value
            if not question.strip():
                self.query_one("#notice", Static).update("请先输入问题。")
                return
            if any(re.search(pattern, question.encode("utf-8")) for pattern in CREDENTIAL_PATTERNS):
                self.query_one("#question", Input).value = ""
                self.query_one("#notice", Static).update("问题含已识别凭据，已隐藏；未创建任务或调用模型。")
                return
            files = [path.strip() for path in self.query_one("#files", Input).value.split(",") if path.strip()]
            self.append("你", question)
            self.outcome = None
            self.query_one("#notice", Static).update("本地准备不调用模型；完成后会展开完整请求预览。")
            self.started = time.monotonic()
            self.elapsed = 0
            self.busy, self.step, self.stage = True, 0, "准备固定版本上下文"
            self.controls()
            self.update_progress()
            self.prepare_worker(dict(self.project), question, files)

        @work(group="tasks", exit_on_error=False)
        async def prepare_worker(self, project, question, files):
            try:
                record = await asyncio.to_thread(self.runner.create, project, question, files=files)
                self.last_record = record
                self.busy = False
                self.elapsed = time.monotonic() - self.started
                if record["status"] == "awaiting_approval":
                    self.pending = record
                    self.step, self.stage = 1, "等待本次数据批准"
                    self.controls()
                    self.push_screen(RequestPreview(record), self.decision)
                else:
                    self.finish(record)
            except Exception as exc:
                self.failure(exc)
            self.update_progress()

        def decision(self, approved):
            record = self.pending
            if record is None or self.busy:
                return
            self.busy = True
            self.started = time.monotonic() - self.elapsed
            self.step = 2 if approved else 1
            self.stage = "执行已批准请求与核对结果" if approved else "记录拒绝"
            self.controls()
            self.update_progress()
            self.decide_worker(record["task_id"], approved is True, record["payload_sha256"])

        @work(group="tasks", exit_on_error=False)
        async def decide_worker(self, task_id, approved, digest):
            try:
                record = await asyncio.to_thread(self.runner.decide, task_id, approved=approved, payload_sha256=digest if approved else None)
                self.finish(record)
            except Exception as exc:
                self.failure(exc)

        def finish(self, record):
            self.last_record = record
            self.pending, self.busy = None, False
            if self.started is not None:
                self.elapsed = time.monotonic() - self.started
            status = record["status"]
            self.outcome = status
            result = record.get("result") or {}
            if status == "completed":
                self.step = 4
                self.stage = "完成（证据不足）" if result.get("status") == "insufficient_evidence" else "回答已生成"
                self.append("Zest", result.get("answer", "没有可展示的回答。"))
                if result.get("unknowns"):
                    self.append("待补充", "\n".join(result["unknowns"]))
                for source in result.get("evidence", []):
                    evidence = source["evidence"]
                    location = f"{evidence.get('path', source.get('title', '知识记录'))} · {evidence.get('commit', evidence.get('observed_commit'))}"
                    content = source.get("body") or "\n".join(f"{line['number']:>4}  {line['text']}" for line in source.get("lines", []))
                    self.append(f"来源 {source['id']} · {location}", content)
                self.append("Zest", "引用可定位原文，仍需核对结论。" + ("本次未调用模型。" if result.get("model_called") is False else ""))
            elif status == "rejected":
                self.stage = "已拒绝 · 未调用模型"
                self.append("Zest", "已拒绝本次请求，可以修改问题或来源后重新准备。")
            else:
                self.stage = "待核对执行" if status == "pending_execution" else "任务失败"
                self.append("Zest", json.dumps(record.get("error") or {"status": status}, ensure_ascii=False))
            self.query_one("#notice", Static).update("本次状态已保存，可继续提问；完整记录仍可用 task show 查看。")
            self.controls()
            self.update_progress()
            self.query_one("#question", Input).value = ""
            self.query_one("#question", Input).focus()

        def failure(self, exc):
            self.busy, self.pending = False, None
            self.stage = "操作未完成 · 未自动重试"
            self.outcome = "failed"
            if self.started is not None:
                self.elapsed = time.monotonic() - self.started
            self.append("Zest", f"{exc.code}: {exc}" if isinstance(exc, FileReadError) else "终端操作异常。请核对保存的任务状态；未回显原始错误。")
            self.controls()
            self.update_progress()
            self.query_one("#notice", Static).update("请核对任务 ID 的保存状态；没有自动重试请求。")

        def action_focus_question(self):
            if not self.busy and self.pending is None:
                self.query_one("#question", Input).focus()

        def action_quit_workspace(self):
            if self.busy:
                self.query_one("#notice", Static).update("后台步骤仍在执行，退出不能等同取消。请等待有限超时；强制结束后需核对任务，不能自动重试。")
                return
            if self.pending is not None:
                self.append("Zest", "等待批准的任务已保存，可用 task show 查看。")
            self.exit()

    return Workspace()


def launch_workspace(state_dir):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise FileReadError("tui_requires_terminal", "交互工作台需要真实终端；脚本请继续使用原 JSON 子命令。")
    create_workspace(state_dir).run()
