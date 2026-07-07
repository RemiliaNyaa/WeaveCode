from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

from rich.markdown import Markdown
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Label, Static

from weavecode.core.transport.socket_client import IpcError, SocketClient


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
        self._finalized = False

    # 追加一个 token，流式期间只原地更新纯文本
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 块结束时才把累积文本渲染成 Markdown
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class WeaveTuiApp(App[None]):
    """WeaveCode TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "WeaveCode"
    BINDINGS = [Binding("ctrl+q", "quit", "quit")]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """

    # 初始化连接参数和 TUI 内部状态
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._current_llm: LLMStreamBlock | None = None

    def compose(self) -> ComposeResult:
        yield Label("● connecting...", id="header")
        yield VerticalScroll(id="log-view")

    # 挂载后启动连接守护进程的 worker
    def on_mount(self) -> None:
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")

    # 向单列滚动流里挂一个 widget 并滚到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块，下一个 token 会开启新块
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            self._current_llm.finalize_markdown()
            self._current_llm = None

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                header.update("● not connected — retrying in 2s")
                await asyncio.sleep(2)
                continue

            header.update(f"● connected  {self._host}:{self._port}")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                params: dict[str, Any] = {
                    "topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage"],
                    "scope": "global",
                }
                if self._replay_run_id is not None:
                    params["replay_from_run"] = self._replay_run_id
                await client.send_command("event.subscribe", params)
                await loop_task
            except IpcError as e:
                header.update(f"● subscribe error: {e}")
            finally:
                self._break_llm()
                if not loop_task.done():
                    loop_task.cancel()
                await client.close()

            header.update("● disconnected — retrying in 2s")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；单个事件渲染失败不会掀翻 socket loop
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "llm.token":
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "run.started":
            run_id = event.get("run_id", "")
            goal = event.get("goal", "")
            self._append(Static(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{_preview(goal, 96)}[/dim]",
                classes="run-header",
            ))

        elif t == "run.finished":
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(Static(
                    f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "step.started":
            self._append(Static(
                f"[dim]step {event.get('step')}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            summary = _param_summary(tool_name, params)
            summary_part = f"  [dim]{summary}[/dim]" if summary else ""
            self._append(Static(
                f"[dim]tool[/dim] [bold]{tool_name}[/bold]{summary_part}",
                classes="log-line",
            ))

        elif t == "tool.call_finished":
            tool_name = str(event.get("tool_name", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            self._append(Static(
                f"[green]tool ✓[/green] [bold]{tool_name}[/bold]"
                f"  [dim]{elapsed_ms}ms[/dim]",
                classes="log-line",
            ))

        elif t == "tool.call_failed":
            tool_name = str(event.get("tool_name", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error = str(event.get("error_message") or "")
            self._append(Static(
                f"[red]tool ✗[/red] [bold]{tool_name}[/bold]"
                f"  [dim]{_preview(error, 72)}[/dim]  [dim]{elapsed_ms}ms[/dim]",
                classes="log-line",
            ))

        elif t == "llm.usage":
            self._append(Static(
                f"[dim]  tokens  in={event.get('input_tokens')}"
                f" out={event.get('output_tokens')}"
                f" cache={event.get('cache_read_input_tokens')}[/dim]",
                classes="usage",
            ))
