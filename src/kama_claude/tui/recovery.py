from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static


class SessionPicker(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "返回"), ("ctrl+n", "new", "新会话")]
    DEFAULT_CSS = """
    SessionPicker { align: center middle; }
    SessionPicker > Vertical {
        width: 85%; height: 80%; border: round $accent;
        padding: 1 2; background: $surface;
    }
    SessionPicker ListView { height: 1fr; margin: 1 0; }
    SessionPicker ListItem { padding: 1; }
    SessionPicker Horizontal { height: 3; }
    """

    # 接收历史摘要，选择行为不启动任何模型调用
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__()
        self._sessions = sessions

    # 展示会话列表和明确的新建入口
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("历史会话：选择后查看；中断任务需要确认才能继续")
            items = []
            for session in self._sessions:
                title = session.get("title") or "未命名会话"
                text = f"{title}\n{session['id']}   [{session['status']}]"
                items.append(ListItem(Label(text, markup=False), name=session["id"]))
            yield ListView(*items, id="session-list")
            with Horizontal():
                yield Button("新建会话", id="new-session", variant="primary")
                yield Button("返回", id="cancel-session")

    # 将键盘焦点放到历史列表
    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    # 返回用户选择的会话身份
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)

    # 处理新建和返回按钮
    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("__new__" if event.button.id == "new-session" else None)

    # 快捷键返回
    def action_cancel(self) -> None:
        self.dismiss(None)

    # 快捷键创建新会话
    def action_new(self) -> None:
        self.dismiss("__new__")


class RecoveryActions(Vertical):
    DEFAULT_CSS = """
    RecoveryActions { height: auto; padding: 1 2; border: round $warning; }
    RecoveryActions Horizontal { height: 3; }
    """

    # 保存本次看到的修订号，决定提交时必须再次校验
    def __init__(self, revision: int, run: dict[str, Any]) -> None:
        super().__init__(id="recovery-actions")
        self.revision = revision
        self.run = run

    # 根据未知副作用和子任务状态提供可执行的恢复选项
    def compose(self) -> ComposeResult:
        unknown = [
            tool for tool in self.run.get("tools", []) if tool["status"] in ("unknown", "executing")
        ]
        child_blocked = any(
            state in ("running", "unknown", "background")
            for state in self.run.get("subagents", {}).values()
        ) or any(tool["name"] == "spawn_agent" for tool in unknown)
        yield Static(
            f"任务中断：{self.run['run_id']}  step={self.run['step']}\n"
            f"原因：{self.run.get('reason') or 'interrupted'}",
            markup=False,
        )
        if child_blocked:
            yield Static("存在未完成子 Agent；第一版不自动恢复任务树，可终止本轮。")
        elif unknown:
            import json

            tool = unknown[0]
            yield Static(
                f"工具结果未知：{tool['name']}\n"
                + json.dumps(tool["input"], ensure_ascii=False, indent=2)
                + "\n请先核实外部操作和进程是否已完成，再决定是否重试。",
                markup=False,
            )
        with Horizontal():
            if not child_blocked:
                yield Button(
                    "核实后重试" if unknown else "继续任务",
                    id="recovery-retry" if unknown else "recovery-continue",
                    variant="warning" if unknown else "primary",
                )
            yield Button("终止本轮", id="recovery-abort", variant="error")
