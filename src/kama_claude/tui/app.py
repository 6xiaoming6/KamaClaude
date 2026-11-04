from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from rich.markdown import Markdown
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, Static, TextArea

from kama_claude.core.config import KamaConfig
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.tui.memory import MemoryScreen
from kama_claude.tui.recovery import RecoveryActions, SessionPicker

log = logging.getLogger(__name__)


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s




def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),
        "memory_read": ("scope", "key", "query"),
        "memory_write": ("scope", "key", "action"),
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

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary { color: $text-muted; }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="detail")

    # 生成摘要行文本
    def _summary(self) -> str:
        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{self._tool_name}[/bold]"
        if params_pre:
            line += f"  [dim]{params_pre}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params[/dim]\n{self._params_full}\n\n"
                f"[dim]output[/dim]\n{self._output}\n\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once",   "Allow once",   "y / 1"),
        ("always_allow", "Always allow", "a / 2"),
        ("deny_once",    "Deny",         "n / 3"),
        ("always_deny",  "Always deny",  "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）
    def __init__(self, tool_use_id: str) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._cursor = 0

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug(
            "PermissionSelect.on_focus  has_focus=%s  app.focused=%r",
            self.has_focus, self.app.focused,
        )

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once":   "allowed (once)",
        "always_allow": "always allowed",
        "deny_once":    "denied",
        "always_deny":  "always denied",
        "timeout":      "⏱ timed out",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(self, tool_use_id: str, tool_name: str, param_preview: str) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line")

    def _pending_text(self) -> str:
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        return f"[bold red]? permission[/bold red]  [bold]{self._tool_name}[/bold]{preview}"

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} permission  [bold]{self._tool_name}[/bold]{preview}  [dim]{label}[/dim]"
        )
        self.post_message(self.Resolved(self, decision))


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            desc_part = f"  [dim]{desc}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{name}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{name}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                popup.select_current()
                return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        await super()._on_key(event)


class KamaTuiApp(App[None]):
    """KamaClaude TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "KamaClaude"
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+o", "open_sessions", "历史会话", priority=True),
        Binding("ctrl+w", "close_session", "关闭会话", priority=True),
    ]
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
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """

    _BANNER = (
        '[bold cyan]██╗  ██╗ █████╗ ███╗   ███╗ █████╗  ██████╗'
        '██╗      █████╗ ██╗   ██╗██████╗ ███████╗[/bold cyan]\n'
        '[bold cyan]██║ ██╔╝██╔══██╗████╗ ████║██╔══██╗██╔════╝'
        '██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝[/bold cyan]\n'
        '[bold cyan]█████╔╝ ███████║██╔████╔██║███████║██║     '
        '██║     ███████║██║   ██║██║  ██║█████╗  [/bold cyan]\n'
        '[bold cyan]██╔═██╗ ██╔══██║██║╚██╔╝██║██╔══██║██║     '
        '██║     ██╔══██║██║   ██║██║  ██║██╔══╝  [/bold cyan]\n'
        '[bold cyan]██║  ██╗██║  ██║██║ ╚═╝ ██║██║  ██║╚██████╗'
        '███████╗██║  ██║╚██████╔╝██████╔╝███████╗[/bold cyan]\n'
        '[bold cyan]╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝'
        '╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝[/bold cyan]\n'
        "[dim]  输入消息开始对话  ·  键入 / 触发 skill  ·  "
        "Ctrl+Q 退出 · Ctrl+O 会话 · Ctrl+W 关闭会话[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._busy = False
        self._opening = False
        self._open_lock = asyncio.Lock()
        self._completed_run_ids: set[str] = set()
        self._recovering = False
        self._event_buffer: list[dict[str, Any]] = []
        self._last_event_seq = 0
        self._snapshot_revision = 0
        self._completed_tool_ids: set[str] = set()
        self._current_llm_run_id: str | None = None
        self._last_context_pct: float = 0.0
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

    def compose(self) -> ComposeResult:
        yield Label("[bold]KamaClaude[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("compact", "compress context window"),
            ("memory", "查看 Markdown 记忆与 Run 注入快照"),
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        # 模态查看窗口打开后，忽略旧聊天输入框迟到的补全事件
        if self._prompt() is None or isinstance(self.screen, MemoryScreen):
            return
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = PermissionSelect._KEY_MAP.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(PermissionSelect._CHOICES[select._cursor][0])
        except Exception:
            pass

    # 退出仅断开界面，保留可重新打开的会话
    async def action_quit(self) -> None:
        self.exit()

    # 打开历史选择器，不取消当前 Core 中的运行
    def action_open_sessions(self) -> None:
        if self._client is not None and not self._opening:
            self.run_worker(self._pick_session(), name="session_picker", exclusive=False)

    # 主动关闭会话与退出界面是两个不同动作
    def action_close_session(self) -> None:
        self.run_worker(self._close_session(), name="close_session", exclusive=False)

    # 向 Core 发送显式关闭请求
    async def _close_session(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            await self._client.send_command("session.close", {"session_id": self._session_id})
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]close error: {exc}[/red]"))

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        if content == "/memory":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None:
                self.run_worker(self._do_memory(), name="memory", exclusive=False)
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = "agent is working..."
        self._append(Static(f"[bold]>[/bold] {content}", classes="user-turn"))
        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 只读查看记忆，RPC 不产生用户消息或 Agent Run
    async def _do_memory(self) -> None:
        client, sid = self._client, self._session_id
        if client is None or sid is None:
            return
        try:
            data = await client.send_command("memory.inspect", {"session_id": sid, "limit": 50})
            if self._client is client and self._session_id == sid:
                await self.push_screen(MemoryScreen(client, sid, data))
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static("记忆读取失败：" + str(exc), markup=False, classes="run-err"))

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            # 进度与结果统一由会话事件展示，RPC 响应不再重复追加横幅
            await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {e}[/red]", classes="log-line"))

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        client, sid = self._client, self._session_id
        if client is None:
            return
        try:
            await client.send_command(
                "session.send_message", {"session_id": sid, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as exc:
            if self._client is not client or self._session_id != sid:
                return
            self._append(Static(f"[red]send error: {exc}[/red]", classes="log-line"))
            if isinstance(exc, ConnectionError):
                self._update_header("disconnected")
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
            else:
                await self._refresh_session()

    # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    # self.post_message(self.Decided(...))后，框架会自动调用on_permission_select_decided
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        log.info("permission decided tool_use_id=%s decision=%s", tool_use_id, decision)
        try:
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)
            if self._client is not None:
                try:
                    await self._client.send_command(
                        "permission.respond",
                        {"tool_use_id": tool_use_id, "decision": decision},
                    )
                except (IpcError, RuntimeError, OSError):
                    pass
            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    p.focus()
        except Exception:
            log.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
            "interrupted": "yellow",
            "closed": "dim",
        }.get(state, "dim")
        header.update(
            f"[bold]KamaClaude[/bold]  [dim]{self._host}:{self._port}[/dim]"
            f"{session}  [{color}]{state}[/{color}]"
        )

    # 选择历史或新建会话，仅用户明确操作才恢复中断任务，选择后并打开会话
    async def _pick_session(self) -> None:
        if self._client is None:
            return
        try:
            # 向 Core 发送请求获取会话列表
            result = await self._client.send_command("session.list", {})
            sessions = result["sessions"]
            # 显示选择框，等待用户选择
            selected: str | None
            # 没有历史会话默认创建新会话
            if not sessions:
                selected = "__new__"
            else:
                # 有历史会话，可以选择打开历史会话或者创建新的会话
                selected = await self.push_screen_wait(SessionPicker(sessions))
            # 创建新会话
            if selected == "__new__":
                created = await self._client.send_command("session.create", {"mode": "chat"})
                selected = str(created["session_id"])
            # 打开选中的会话
            if selected is not None:
                await self._open_session(selected)
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]open error: {exc}[/red]"))

    # 先缓冲推送，再用权威快照重建显示，最后应用水位之后的事件
    async def _open_session(self, sid: str) -> None:
        async with self._open_lock:
            await self._load_session(sid)

    # 在已持有打开锁时加载会话的历史快照并刷新组件，防止多个恢复通知交叉覆盖
    async def _load_session(self, sid: str) -> None:
        if self._client is None:
            return
        previous = self._session_id
        # 加载期间先缓冲事件，避免历史快照与实时更新交叉覆盖
        self._opening = True
        self._event_buffer = []
        self._session_id = sid
        try:
            data = await self._client.send_command("session.open", {"session_id": sid})
            await self.query_one("#log-view", VerticalScroll).remove_children()
            self._current_llm = None
            self._current_llm_run_id = None
            self._pending_tool_blocks.clear()
            self._pending_permission_blocks.clear()
            for select in list(self.query(PermissionSelect)):
                select.remove()
            for control in list(self.query(RecoveryActions)):
                control.remove()
            self._subagent_run_ids.clear()
            self._subagent_start_times.clear()
            self._snapshot_revision = int(data["revision"])
            # 水位过滤旧事件，完成 ID 再防止迟到通知重复显示
            self._last_event_seq = int(data.get("event_seq", 0))
            self._completed_tool_ids.clear()
            self._completed_run_ids.clear()
            # 后端返回 thread 完整原文；压缩后的工作上下文只供模型使用
            self._render_history(data["messages"])
            # 拿到该会话检查点的运行状态
            run = data.get("run")
            if run:
                self._completed_tool_ids = {
                    tool["id"] for tool in run["tools"] if tool["status"] == "completed"
                }
                # 状态为interrupted就需要用户决定是否从中断恢复运行
                if run["status"] == "interrupted":
                    self._append(RecoveryActions(self._snapshot_revision, run))
                # 状态为成功或者失败都说明上次的运行是完整的
                elif run["status"] in ("success", "failed"):
                    self._completed_run_ids.add(run["run_id"])
                    self._append(Static(
                        f"run {run['run_id']} — {run['status']} ({run['step']} steps)",
                        classes="run-ok" if run["status"] == "success" else "run-err",
                    ))
            # 拿到整个该会话的整体状态
            status = data["session"]["status"]
            self._busy = bool(data["running"]) or status == "interrupted"
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = self._busy or status == "closed"
                prompt.read_only = False
                prompt.border_title = (
                    "任务中断：请确认继续或终止" if status == "interrupted" else
                    "session closed — Ctrl+O 选择会话" if status == "closed" else
                    "agent is working..." if self._busy else "输入消息"
                )
                if not prompt.disabled:
                    prompt.focus()
            self._update_header(
                "interrupted" if status == "interrupted" else
                "closed" if status == "closed" else "running" if self._busy else "ready"
            )
            for permission in data.get("pending_permissions", []):
                self._handle_event_inner(permission)
        except BaseException:
            self._session_id = previous
            raise
        finally:
            self._opening = False
            buffered, self._event_buffer = self._event_buffer, []
            for event in buffered:
                self._handle_event(event)

    # 将已提交的消息与工具结果恢复成界面组件，不重放任何工具
    def _render_history(self, messages: list[dict[str, Any]]) -> None:
        blocks: dict[str, ToolCallBlock] = {}
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                if message["role"] == "user":
                    self._append(Static("> " + content, markup=False, classes="user-turn"))
                elif content:
                    block = LLMStreamBlock()
                    block.append_token(content)
                    block.finalize_markdown()
                    self._append(block)
                continue
            for item in content or []:
                if item.get("type") == "text":
                    block = LLMStreamBlock()
                    block.append_token(item.get("text", ""))
                    block.finalize_markdown()
                    self._append(block)
                elif item.get("type") == "tool_use":
                    tool = ToolCallBlock(item["name"], item.get("input", {}))
                    blocks[item["id"]] = tool
                    self._pending_tool_blocks[item["id"]] = tool
                    self._append(tool)
                elif item.get("type") == "tool_result":
                    tool_id = item["tool_use_id"]
                    result_block = blocks.get(tool_id)
                    if result_block is not None:
                        result_block.set_result(
                            str(item.get("content", "")), 0,
                            is_error=bool(item.get("is_error")),
                        )
                        self._pending_tool_blocks.pop(tool_id, None)

    # 恢复按钮只提交明确决定，不在重连后自动启动执行
    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "recovery-continue": "continue",
            "recovery-retry": "retry_tool",
            "recovery-abort": "abort",
        }
        action = actions.get(event.button.id or "")
        if action is not None and not self._recovering:
            self._recovering = True
            self.run_worker(self._resume_session(action), name="resume", exclusive=False)

    # 使用用户看到的修订提交决定，冲突后重新读取权威状态
    async def _resume_session(self, action: str) -> None:
        try:
            if self._client is None or self._session_id is None:
                return
            await self._client.send_command("session.resume", {
                "session_id": self._session_id,
                "expected_revision": self._snapshot_revision, "action": action,
            })
            await self._open_session(self._session_id)
        except (IpcError, RuntimeError, OSError) as exc:
            if self._client is not None and self._session_id is not None:
                try:
                    await self._open_session(self._session_id)
                except (IpcError, RuntimeError, OSError):
                    pass
            self._append(Static(f"[red]resume error: {exc}[/red]"))
        finally:
            self._recovering = False

    # 中断通知后刷新快照，按钮与未知工具信息来自持久化状态
    async def _refresh_session(self) -> None:
        if self._session_id is not None:
            try:
                await self._open_session(self._session_id)
            except (IpcError, RuntimeError, OSError):
                log.exception("session refresh failed")

    # 保留会话身份进行重连，Core 中的任务不随 TCP 连接取消
    async def _socket_loop(self) -> None:
        # 外层循环只在连接结束后创建新客户端，正常连接停在 receiver 等待处
        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue
            self._client = client

            # 初始化快照期间暂存实时事件
            async def on_event(event: dict[str, Any]) -> None:
                if self._opening:
                    self._event_buffer.append(event)
                else:
                    self._handle_event(event)

            client.on_event(on_event)
            # 客户端创建事件循环，开始接收服务端发来的内容
            receiver = asyncio.create_task(client.run_event_loop())
            try:
                if self._replay_run_id is not None:
                    await client.send_command("event.subscribe", {
                        "topics": ["*"], "scope": f"run:{self._replay_run_id}",
                        "replay_from_run": self._replay_run_id,
                    })
                    self._update_header("replay")
                # 如果是重连，还会保存上一次的_session_id，直接打开会话
                elif self._session_id is not None:
                    try:
                        await self._open_session(self._session_id)
                    except IpcError as exc:
                        self._append(Static(f"[red]open error: {exc}[/red]"))
                        self._session_id = None
                        await self._pick_session()
                # 如果是重启客户端后第一次连接，需要选择打开历史会话还是创建新会话
                else:
                    await self._pick_session()
                # 打开或创建会话后，就挂起持续接收服务端的消息，直到连接中断
                # 此时前端tui还可以响应其他的事情
                await receiver
            except (IpcError, RuntimeError, OSError):
                log.exception("socket session failed")
            finally:
                if not receiver.done():
                    receiver.cancel()
                try:
                    await receiver
                except asyncio.CancelledError:
                    pass
                self._client = None
                self._break_llm()
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.border_title = "disconnected, retrying..."
                await client.close()
            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        if self._replay_run_id is None and self._session_id is not None:
            if event.get("session_id") != self._session_id:
                return
        seq = event.get("event_seq")
        if isinstance(seq, int):
            if seq <= self._last_event_seq:
                return
            self._last_event_seq = seq
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "llm.token":
            if self._current_llm_run_id != event.get("run_id"):
                self._break_llm()
                self._current_llm_run_id = event.get("run_id")
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")

        elif t == "run.interrupted":
            self._append(Static(
                "任务已中断：" + str(event.get("reason", "interrupted")),
                markup=False, classes="run-err",
            ))
            self._busy = True
            self._update_header("interrupted")
            self.run_worker(self._refresh_session(), name="refresh", exclusive=False)

        elif t == "run.resumed":
            self._busy = True
            self._update_header("running")

        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("closed")

        elif t == "run.started":
            run_id = event.get("run_id", "")
            goal = event.get("goal", "")
            self._append(Static(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{_preview(goal, 96)}[/dim]",
                classes="run-header",
            ))

        elif t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self._append(Static(
                f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self._append(Static(
                f"[dim]┌─[/dim] [cyan]{_preview(description, 72)}[/cyan]  [dim]{short_id}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, event.get("description", ""))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
            if status == "success":
                self._append(Static(
                    f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(Static(
                    f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(Static(
                f"[dim]step {step}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            if tool_use_id in self._completed_tool_ids:
                return
            self._completed_tool_ids.add(tool_use_id)
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            if event.get("retrying"):
                return
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            run_id = str(event.get("run_id", ""))
            if run_id in self._completed_run_ids:
                return
            self._completed_run_ids.add(run_id)
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

        elif t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            ctx_bar = self._render_ctx_bar(pct)
            self._append(Static(
                f"[dim]  tokens  "
                f"in={event.get('input_tokens')} "
                f"out={event.get('output_tokens')} "
                f"cache={event.get('cache_read_input_tokens')}[/dim]"
                f"  {ctx_bar}",
                classes="usage",
            ))

        elif t == "context.compaction_started":
            self._append(Static(
                "[dim]⚡ 正在整理上下文，先尝试程序处理…[/dim]", classes="log-line",
            ))

        elif t == "context.compaction_failed":
            reasons = {
                "no_old_history": "没有需要压缩的旧历史",
                "incomplete_tool_batch": "工具消息尚未完整配对",
                "recent_context_too_large": "必须保留的近期原文过大",
                "summary_input_too_large": "旧历史超出单次摘要请求预算",
                "summary_request_failed": "摘要请求失败",
                "summary_incomplete": "摘要输出未完整结束",
                "empty_summary": "摘要为空",
                "summary_too_large": "摘要超过长度上限",
                "insufficient_reduction": "压缩后没有明显缩小",
                "context_budget_exceeded": "整理后仍超出输入预算",
                "required_context_too_large": "固定提示与必须保留的近期原文已超预算",
                "provider_unavailable": "需要摘要，但模型配置不可用",
                "below_threshold": "空间充足，无需继续压缩",
                "auto_disabled": "自动压力处理已关闭",
            }
            reason = reasons.get(str(event.get("reason")), str(event.get("reason", "")))
            retained = (
                "程序化缩减已保存，未采用新摘要" if event.get("applied_stages")
                else "未替换工作历史"
            )
            usage = "已尝试 LLM 摘要" if event.get("llm_used") else "未调用 LLM"
            self._append(Static(
                f"⚡ 上下文整理：{reason}；{retained}；{usage}。",
                classes="log-line", markup=False,
            ))

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            remaining = event.get("remaining_tokens", summary)
            stage_names = {
                "tool_preview": "长结果预览", "deduplicate": "重复结果去重",
                "tool_archive": "旧结果归档", "llm_summary": "LLM 摘要",
            }
            stages = " → ".join(stage_names.get(s, s) for s in event.get("applied_stages", []))
            label = (
                "已生成 LLM 摘要" if event.get("llm_used", True)
                else "程序整理完成（未调用 LLM）"
            )
            self._append(Static(
                f"⚡ {label}  history≈{orig} → {remaining} tokens; summary≈{summary}"
                + (f"  {stages}" if stages else ""),
                classes="log-line", markup=False,
            ))

        elif t == "permission.requested":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name, tool_use_id, _focused_repr,
            )
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview)
            self._pending_permission_blocks[tool_use_id] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(tool_use_id)
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )

        elif t == "permission.denied":
            # 处理超时或断连等非用户交互触发的 deny
            # 用户主动 deny 已由 on_permission_select_decided 处理
            tool_use_id = str(event.get("tool_use_id", ""))
            decision = str(event.get("decision", "denied"))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id)
                perm_block._resolve(decision)
                try:
                    select = self.query_one(PermissionSelect)
                    select.remove()
                except Exception:
                    pass
                if not self._pending_permission_blocks:
                    p = self._prompt()
                    if p is not None:
                        p.disabled = False
                        p.read_only = False
                        p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        p.focus()

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 KamaTuiApp
def run(config: KamaConfig, replay_run_id: str | None = None) -> None:
    app = KamaTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()
