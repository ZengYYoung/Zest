"""Optional terminal view for independent, durable project understanding."""

import asyncio
import json

from .cognition_store import CognitionStore
from .cognition_views import format_cognition, format_dossier
from .files import FileReadError
from .terminal import display_text


def cognition_screen(state_dir, project_id, *, store=None, launch=None):
    from textual import on, work
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, Checkbox, Label, Select, Static, TextArea
    from .cognition import start_worker

    store = store or CognitionStore(state_dir)
    launch = launch or start_worker

    class ScopePreview(ModalScreen[bool | None]):
        BINDINGS = [("escape", "close", "返回")]
        CSS = """
        ScopePreview { align: center middle; }
        #scope-box { width: 96%; height: 96%; padding: 1; border: solid #888888; background: $surface; }
        #scope-json { height: 1fr; }
        #scope-consent { height: auto; }
        #scope-actions { height: 3; }
        #scope-actions Button { width: 1fr; }
        """

        def __init__(self, record):
            super().__init__()
            self.record = record

        def compose(self):
            with Vertical(id="scope-box"):
                yield Label("完整调查授权包 · 固定版本 / 接口 / 预算")
                yield Static("包含所有允许读取的文件全文、共享认知和工作指令。仅授权模型调查；包外材料需重新批准。", markup=False)
                yield TextArea(display_text(json.dumps({"approval_sha256": self.record["approval_sha256"],
                    "envelope": self.record["envelope"]}, ensure_ascii=False, indent=2)), read_only=True, id="scope-json")
                yield Checkbox("我已审阅，允许在此资料包、目标和预算内后台调查", id="scope-consent")
                with Horizontal(id="scope-actions"):
                    yield Button("返回", id="scope-close")
                    yield Button("拒绝调查", id="scope-reject")
                    yield Button("批准并启动", disabled=True, id="scope-approve")

        @on(Checkbox.Changed, "#scope-consent")
        def consent(self, event):
            self.query_one("#scope-approve", Button).disabled = not event.value

        @on(Button.Pressed)
        def choose(self, event):
            if event.button.id == "scope-close":
                self.dismiss(None)
            elif event.button.id == "scope-reject":
                self.dismiss(False)
            elif event.button.id == "scope-approve" and self.query_one("#scope-consent", Checkbox).value:
                self.dismiss(True)

        def action_close(self):
            self.dismiss(None)

    class ContextPreview(ModalScreen[bool | None]):
        BINDINGS = [("escape", "close", "返回")]
        CSS = """
        ContextPreview { align: center middle; }
        #context-box { width: 96%; height: 96%; padding: 1; border: solid #888888; background: $surface; }
        #context-json { height: 1fr; }
        #context-consent { height: auto; }
        #context-actions { height: 3; }
        #context-actions Button { width: 1fr; }
        """

        def __init__(self, record):
            super().__init__()
            self.record = record

        def compose(self):
            record = self.record
            with Vertical(id="context-box"):
                yield Label("完整请求 · 本次字符扩展")
                yield Static(display_text(f"{record['actor']} / {record['sequence']}：{record['chars']} 字符；默认 {record['default_chars']}，超出 {record['over_percent']}%，最高 {record['max_chars']}。尚未外发；不扩大资料、调用或修改权限。"), markup=False)
                yield TextArea(display_text(json.dumps(record, ensure_ascii=False, indent=2)), read_only=True, id="context-json")
                yield Checkbox("我已审阅完整请求，批准本次字符扩展", id="context-consent")
                with Horizontal(id="context-actions"):
                    yield Button("返回", id="context-close")
                    yield Button("拒绝本次扩展", id="context-reject")
                    yield Button("批准并继续", disabled=True, id="context-approve")

        @on(Checkbox.Changed, "#context-consent")
        def consent(self, event):
            self.query_one("#context-approve", Button).disabled = not event.value

        @on(Button.Pressed)
        def choose(self, event):
            if event.button.id == "context-close":
                self.dismiss(None)
            elif event.button.id == "context-reject":
                self.dismiss(False)
            elif event.button.id == "context-approve" and self.query_one("#context-consent", Checkbox).value:
                self.dismiss(True)

        def action_close(self):
            self.dismiss(None)

    class CognitionScreen(ModalScreen):
        BINDINGS = [("escape", "close", "返回对话")]
        CSS = """
        CognitionScreen { align: center middle; }
        #cognition-box { width: 96%; height: 96%; padding: 1; border: solid #888888; background: $surface; }
        #cognition-actions { height: 3; }
        #cognition-actions Button { width: 1fr; min-width: 10; }
        #cognition-notice { height: auto; max-height: 4; }
        #cognition-result { height: 1fr; }
        """

        def __init__(self):
            super().__init__()
            self.record = None
            self.busy = False
            self.timer = None
            self.fingerprint = None

        def compose(self):
            with Vertical(id="cognition-box"):
                yield Label("项目认知 · 后台调查")
                yield Select([], prompt="当前项目最近 100 次调查", id="cognition-history")
                yield Static("准备资料后审阅批准。返回主对话或退出工作台，不会取消后台调查。", markup=False, id="cognition-notice")
                with Horizontal(id="cognition-actions"):
                    yield Button("新建调查", id="cognition-create")
                    yield Button("审阅资料包", id="cognition-preview", disabled=True)
                    yield Button("审阅超限请求", id="cognition-context", disabled=True)
                    yield Button("安全恢复", id="cognition-recover", disabled=True)
                    yield Button("返回对话", id="cognition-close")
                yield TextArea("正在读取历史…", read_only=True, id="cognition-result")

        def on_mount(self):
            self.refresh_history()
            self.timer = self.set_interval(0.5, self.poll)

        def on_unmount(self):
            if self.timer:
                self.timer.stop()

        @work(exclusive=True, group="cognition-history")
        async def refresh_history(self, selected=None):
            try:
                records = await asyncio.to_thread(store.list_runs, project_id)
                if not self.is_attached:
                    return
                selector = self.query_one("#cognition-history", Select)
                selector.set_options([(display_text(f"{record['created_at']} · {record['id'][:8]} · {record['status']}"), record["id"]) for record in records])
                if records:
                    selector.value = selected or records[0]["id"]
                else:
                    self.query_one("#cognition-result", TextArea).load_text("尚无调查记录。点击新建调查，Zest 会自主摸底并准备完整资料包。")
            except FileReadError as exc:
                self.notice(str(exc))

        def notice(self, message):
            if self.is_attached:
                self.query_one("#cognition-notice", Static).update(display_text(message))

        @on(Select.Changed, "#cognition-history")
        def selected(self, event):
            if event.value is not Select.NULL:
                self.read_record(str(event.value))

        def poll(self):
            if self.record and not self.busy and self.is_attached:
                self.read_record(self.record["id"])

        @work(exclusive=True, group="cognition-read")
        async def read_record(self, run_id):
            try:
                record = await asyncio.to_thread(store.show, run_id)
                dossier = await asyncio.to_thread(store.dossier, run_id)
                if not self.is_attached:
                    return
                self.record = record
                fingerprint = json.dumps([record, dossier], sort_keys=True)
                if fingerprint != self.fingerprint:
                    self.query_one("#cognition-result", TextArea).load_text(display_text(format_cognition(record) + "\n\n" + format_dossier(dossier)))
                    self.fingerprint = fingerprint
                self.query_one("#cognition-preview", Button).disabled = self.busy or record["status"] != "awaiting_approval"
                self.query_one("#cognition-context", Button).disabled = (self.busy or record["worker_active"] or
                    not any(item["status"] == "awaiting_approval" for item in record.get("context_requests", [])))
                self.query_one("#cognition-recover", Button).disabled = self.busy or record["worker_active"] or record["status"] not in ("planning", "investigating", "reviewing", "pending_execution")
            except FileReadError as exc:
                self.notice(str(exc))

        @on(Button.Pressed, "#cognition-create")
        def create(self):
            self.perform("create")

        @on(Button.Pressed, "#cognition-recover")
        def recover(self):
            if self.record:
                self.perform("recover", self.record["id"])

        @on(Button.Pressed, "#cognition-preview")
        def preview(self):
            if self.record:
                self.perform("preview", self.record["id"])

        @on(Button.Pressed, "#cognition-context")
        def context_preview(self):
            if self.record:
                self.perform("context", self.record["id"])

        @work(exclusive=True, group="cognition-action")
        async def perform(self, action, run_id=None):
            if self.busy:
                return
            self.busy = True
            self.query_one("#cognition-create", Button).disabled = True
            try:
                if action == "create":
                    self.notice("正在本地摸底并准备资料包；尚未调用模型。")
                    record = await asyncio.to_thread(store.create, project_id)
                    self.refresh_history(record["id"])
                    self.notice("资料已准备，点击审阅资料包后决定是否批准。")
                elif action == "preview":
                    record = await asyncio.to_thread(store.show, run_id, include_envelope=True)
                    decision = await self.app.push_screen_wait(ScopePreview(record))
                    if decision is not None:
                        await asyncio.to_thread(store.decide, run_id, approved=decision, approval_sha256=record["approval_sha256"])
                        if decision:
                            await asyncio.to_thread(launch, state_dir)
                        self.notice("已批准，后台 worker 已启动。可返回主对话。" if decision else "本次调查已拒绝。")
                elif action == "context":
                    requests = await asyncio.to_thread(store.context_requests, run_id)
                    for request in requests:
                        if request["status"] != "awaiting_approval":
                            continue
                        decision = await self.app.push_screen_wait(ContextPreview(request))
                        if decision is None:
                            break
                        record = await asyncio.to_thread(store.decide_context, run_id, request["actor"], request["sequence"],
                            approved=decision, request_sha256=request["request_sha256"])
                        if record["status"] == "queued":
                            await asyncio.to_thread(launch, state_dir)
                    self.notice("扩展决定已保存；尚未决定的请求继续等待。拒绝的请求不外发，已有证据保留。")
                else:
                    await asyncio.to_thread(store.recover, run_id)
                    await asyncio.to_thread(launch, state_dir)
                    self.notice("已安全排队恢复；已保存响应会复用，不重试未知请求。")
            except (FileReadError, OSError) as exc:
                self.notice(str(exc) if isinstance(exc, FileReadError) else "无法启动后台进程，可通过 CLI 核对并启动 worker。")
            finally:
                self.busy = False
                if self.is_attached:
                    self.query_one("#cognition-create", Button).disabled = False
                    if self.record:
                        self.read_record(self.record["id"])

        @on(Button.Pressed, "#cognition-close")
        def action_close(self):
            if self.busy:
                self.notice("正在保存本地操作，请稍候再返回。")
            else:
                self.dismiss()

    return CognitionScreen()
