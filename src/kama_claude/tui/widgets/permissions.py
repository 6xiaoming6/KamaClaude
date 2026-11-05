from __future__ import annotations

import logging

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static

log = logging.getLogger(__name__)


class PermissionSelect(Vertical):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    PermissionSelect Horizontal { height: 3; }
    PermissionSelect Button { min-width: 12; margin-right: 1; }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once", "允许本次", "y / 1"),
        ("always_allow", "本会话允许相同操作", "a / 2"),
        ("deny_once", "拒绝本次", "n / 3"),
        ("always_deny", "本会话拒绝此范围", "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",
        "1": "allow_once",
        "a": "always_allow",
        "2": "always_allow",
        "n": "deny_once",
        "3": "deny_once",
        "d": "always_deny",
        "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带审批请求 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、审批 ID 和决策
        def __init__(self, widget: PermissionSelect, request_id: str, decision: str) -> None:
            self.widget = widget
            self.request_id = request_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储审批 ID 与原始会话、Run（用于 IPC 回复）
    def __init__(
        self,
        request_id: str,
        allowed_decisions: list[str] | None = None,
        *,
        session_id: str = "",
        run_id: str = "",
        tool_name: str = "",
    ) -> None:
        super().__init__()
        self._request_id = request_id
        self.session_id = session_id
        self.run_id = run_id
        self._cursor = 0
        decisions = (
            allowed_decisions if allowed_decisions is not None else ["allow_once", "deny_once"]
        )
        choices = {choice[0]: choice for choice in self._CHOICES}
        self._choices = tuple(choices[value] for value in decisions if value in choices)
        if not self._choices:
            raise ValueError("permission request has no valid choices")
        allowed = {choice[0] for choice in self._choices}
        self._key_map = {key: value for key, value in self._KEY_MAP.items() if value in allowed}
        if tool_name in {"write_file", "edit_file"}:
            operation = "编辑" if tool_name == "edit_file" else "写入"
            self._choices = tuple(
                (value, f"本会话允许使用 {tool_name} {operation}此文件", hint)
                if value == "always_allow" else (value, label, hint)
                for value, label, hint in self._choices
            )

    # 按后端允许的决策生成按钮，按钮和键盘共用同一组选项
    def compose(self) -> ComposeResult:
        yield Static(self._render_ui(), classes="permission-options")
        with Horizontal():
            for decision, label, _ in self._choices:
                yield Button(label, id=f"permission-{decision}")

    # 更新方向键高亮，不重新创建按钮
    def update(self, content: str) -> None:
        self.query_one(".permission-options", Static).update(content)

    # 点击按钮与快捷键进入相同的决策校验入口
    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._pick((event.button.id or "").removeprefix("permission-"))

    # 挂载后将焦点交给审批控件
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
            self.has_focus,
            self.app.focused,
        )

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._choices):
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
            self._cursor = (self._cursor - 1) % len(self._choices)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._choices)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._choices[self._cursor][0])
        else:
            decision = self._key_map.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        if decision not in {choice[0] for choice in self._choices}:
            return
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(self.Decided(self, self._request_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once": "allowed (once)",
        "always_allow": "本会话允许相同操作",
        "deny_once": "denied",
        "always_deny": "denied for this scope in this session",
        "timeout": "⏱ timed out",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(
        self,
        tool_use_id: str,
        tool_name: str,
        param_preview: str,
        path_details: str = "",
    ) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._resolved = False
        self._path_details = path_details
        super().__init__(self._pending_text(), classes="log-line")

    # 审批时完整展示实际目标，路径中的方括号不作为 Rich 标记解释
    def _pending_text(self) -> str:
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""
        details = f"\n{escape(self._path_details)}" if self._path_details else ""
        return (
            f"[bold red]? permission[/bold red]  [bold]{self._tool_name}[/bold]{preview}{details}"
        )

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} permission  [bold]{self._tool_name}[/bold]{preview}  [dim]{label}[/dim]"
        )
        self.post_message(self.Resolved(self, decision))
