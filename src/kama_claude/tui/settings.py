from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from kama_claude.core.permissions.mode import PermissionMode


class PermissionModePicker(ModalScreen[PermissionMode | None]):
    BINDINGS = [("escape", "cancel", "返回")]
    DEFAULT_CSS = """
    PermissionModePicker { align: center middle; }
    PermissionModePicker > Vertical {
        width: 80%; max-width: 90; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    PermissionModePicker Button { width: 100%; margin-top: 1; }
    """

    def __init__(self, mode: PermissionMode) -> None:
        super().__init__()
        self._mode = mode

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"会话审批模式：当前 {self._mode.value.upper()}")
            yield Static("明确禁止始终有效；切换只影响后续调用，已弹出的审批仍需回答。")
            yield Button("ASK · 沿用现有逐工具审批", id="mode-ask")
            yield Button("APPROVE · 高危命令、外部写入仍询问", id="mode-approve")
            yield Button("FULL · 无需询问，命令仍在本机执行", id="mode-full")

    def on_mount(self) -> None:
        self.query_one(f"#mode-{self._mode.value}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        assert event.button.id is not None
        self.dismiss(PermissionMode(event.button.id.removeprefix("mode-")))

    def action_cancel(self) -> None:
        self.dismiss(None)
