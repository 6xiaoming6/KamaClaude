from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from kama_claude.core.transport.socket_client import IpcError, SocketClient


class MemoryScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "返回")]
    DEFAULT_CSS = """
    MemoryScreen { align: center middle; }
    MemoryScreen > Vertical {
        width: 92%; height: 90%; border: round $accent;
        padding: 1 2; background: $surface;
    }
    MemoryScreen TabbedContent { height: 1fr; }
    MemoryScreen TabPane { padding: 0; }
    MemoryScreen #memory-current { height: 1fr; }
    MemoryScreen ListView { height: 10; border: solid $primary; }
    MemoryScreen TextArea { height: 1fr; min-height: 5; }
    MemoryScreen Horizontal { height: 3; }
    MemoryScreen Select { width: 1fr; }
    MemoryScreen #memory-status { height: auto; max-height: 4; }
    """

    # 保存查看时绑定的客户端和会话，后续请求始终使用同一会话身份
    def __init__(self, client: SocketClient, sid: str, data: dict[str, Any]) -> None:
        super().__init__()
        self._client = client
        self._sid = sid
        self._data = data
        self._scope = "all"
        self._offset = 0
        self._selected: tuple[str, str] | None = None
        self._body_offset: int | None = None

    # 当前文件和冻结快照分开展示，全部文本框都只读
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("记忆查看 · 当前文件与 Run 注入快照分别显示")
            yield Static("当前项目：" + self._data["workspace"], markup=False)
            with TabbedContent():
                with TabPane("当前文件", id="memory-current"):
                    yield Select(
                        [("全部作用域", "all"), ("全局", "global"),
                         ("项目", "project"), ("会话", "session")],
                        value="all", allow_blank=False, id="memory-scope",
                    )
                    yield Static("", id="memory-status", markup=False)
                    yield ListView(id="memory-list")
                    yield TextArea("", read_only=True, id="memory-detail")
                    with Horizontal():
                        yield Button("上一页", id="memory-prev", disabled=True)
                        yield Button("下一页", id="memory-next", disabled=not any(
                            layer["next_offset"] is not None for layer in self._data["layers"]
                        ))
                        yield Button("正文下一段", id="memory-more", disabled=True)
                with TabPane("Run 实际注入"):
                    yield TextArea(
                        self._snapshot_text(), read_only=True, id="memory-snapshot",
                    )
            yield Button("返回对话", id="memory-close")

    # 显示真正保存的快照，旧 Run 没有清单时不根据当前文件伪造
    def _snapshot_text(self) -> str:
        snapshot = self._data.get("snapshot")
        if snapshot is None:
            return "最近 Run 没有独立注入快照；此处不会重新读取记忆并冒充原输入。"
        lines = [
            f"Run：{self._data.get('run_id')}",
            f"记忆正文估算：{snapshot['estimated_tokens']} tokens（不含基础工具使用说明）",
        ]
        for layer in snapshot["layers"]:
            lines.append(
                f"{layer['scope']}：预算 {layer['budget']}，实际 {layer['estimated_tokens']}，"
                f"全文 {len(layer['included'])}，摘要 {len(layer['summarized'])}，"
                f"省略 {layer['omitted']}"
            )
            lines.extend(layer["warnings"])
        return "\n".join(lines) + "\n\n" + (snapshot["text"] or "本轮没有注入记忆正文。")

    # 初次挂载只显示已取得的索引，不启动额外 Run
    async def on_mount(self) -> None:
        await self._render_index()

    # 替换列表并显示各层数量、未读文件和分页状态
    async def _render_index(self) -> None:
        view = self.query_one("#memory-list", ListView)
        await view.clear()
        status = []
        items = []
        for layer in self._data["layers"]:
            status.append(f"{layer['scope']}: {layer['total']} 条")
            status.extend(layer["warnings"])
            for entry in layer["entries"]:
                items.append(ListItem(
                    Label(
                        f"[{layer['scope']}] {'★ ' if entry['pinned'] else ''}{entry['key']}"
                        f"  {entry['summary']}", markup=False,
                    ),
                    name=layer["scope"] + "/" + entry["key"],
                ))
        await view.extend(items)
        view.index = 0 if items else None
        self.query_one("#memory-status", Static).update("\n".join(status))
        self.query_one("#memory-prev", Button).disabled = self._offset == 0
        self.query_one("#memory-next", Button).disabled = not any(
            layer["next_offset"] is not None for layer in self._data["layers"]
        )
        self.query_one("#memory-more", Button).disabled = True
        self.query_one("#memory-detail", TextArea).load_text("选择条目查看原文；没有自动修改入口。")
        self._selected = None
        self._body_offset = None

    # 切换作用域只查询文件，不发送用户消息
    def on_select_changed(self, event: Select.Changed) -> None:
        scope = str(event.value)
        if scope != self._scope and scope in ("all", "global", "project", "session"):
            self._scope, self._offset = scope, 0
            self.run_worker(self._refresh(), group="memory-index", exclusive=True)

    # 获取当前作用域的新一页；取消旧请求，避免迟到响应覆盖新选择
    async def _refresh(self) -> None:
        try:
            params: dict[str, Any] = {"session_id": self._sid, "offset": self._offset, "limit": 50}
            if self._scope != "all":
                params["scope"] = self._scope
            self._data = await self._client.send_command("memory.inspect", params)
            await self._render_index()
        except (IpcError, RuntimeError, OSError) as exc:
            self.query_one("#memory-status", Static).update("读取失败：" + str(exc))

    # 选中 key 后读取正文，索引不会提前把全部正文加载到界面
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.name is not None:
            scope, key = event.item.name.split("/", 1)
            self._selected = (scope, key)
            self.run_worker(self._read_body(0), group="memory-body", exclusive=True)

    # 按字符位置获取下一段，读取错误保留在查看界面中
    async def _read_body(self, offset: int) -> None:
        selected = self._selected
        if selected is None:
            return
        try:
            scope, key = selected
            data = await self._client.send_command("memory.inspect", {
                "session_id": self._sid, "scope": scope, "key": key, "offset": offset,
            })
            if self._selected != selected:
                return
            entry = data["entry"]
            header = (
                f"{scope}/{key}\n摘要：{entry['summary']}\n来源：{entry['source']}\n"
                f"更新时间：{entry['updated_at']}\n正文位置：{offset}/{entry['total_chars']}\n\n"
            )
            self.query_one("#memory-detail", TextArea).load_text(header + entry["content"])
            self._body_offset = entry["next_offset"]
            self.query_one("#memory-more", Button).disabled = self._body_offset is None
        except (IpcError, RuntimeError, OSError) as exc:
            self.query_one("#memory-detail", TextArea).load_text("读取失败：" + str(exc))

    # 分页、读取后续正文和返回都是只读界面操作
    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button.id
        if button == "memory-close":
            self.dismiss(None)
        elif button == "memory-more" and self._body_offset is not None:
            self.run_worker(self._read_body(self._body_offset), group="memory-body", exclusive=True)
        elif button in ("memory-prev", "memory-next"):
            self._offset += -50 if button == "memory-prev" else 50
            self.run_worker(self._refresh(), group="memory-index", exclusive=True)

    # Escape 返回对话，不关闭会话或当前 TCP 连接
    def action_close(self) -> None:
        self.dismiss(None)
