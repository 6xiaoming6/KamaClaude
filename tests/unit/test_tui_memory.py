from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, ListView, TextArea

from kama_claude.core.memory.store import MemoryStore
from kama_claude.tui.app import ChatTextArea, KamaTuiApp
from kama_claude.tui.memory import MemoryScreen
from tests.unit.test_checkpoint_recovery import Provider
from tests.unit.test_memory_workflow import isolate, setup_manager
from tests.unit.test_tui_recovery import idle_socket


class Client:
    # 只把查看请求交给真实管理器，记录是否误发消息或创建 Run
    def __init__(self, manager) -> None:
        self.manager = manager
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # 直接返回真实检查点和文件结果，不调用模型
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "memory.inspect":
            return self.manager.inspect_memory(
                params["session_id"], **{k: v for k, v in params.items() if k != "session_id"},
            )
        if method == "session.open":
            return await self.manager.open(params["session_id"])
        raise AssertionError(method)


# 功能：验证 TUI 的查看窗口分开呈现当前文件和原 Run 注入，读取详情不会修改会话
# 设计：使用真实 Manager、Markdown 和 Textual 生命周期，避免仅断言拼接的字符串
async def test_memory_screen_shows_current_files_and_frozen_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kama_claude.core.llm.types import LlmResponse

    isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    model = Provider([LlmResponse("end_turn", text="done")])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    memory = MemoryStore(tmp_path)
    memory.upsert("project", "python", summary="Python", content="old version", source="s / r")
    await manager.send_message(session.id, "goal")
    memory.upsert("project", "python", summary="Python", content="new version", source="s / r")
    client = Client(manager)
    app = KamaTuiApp("127.0.0.1", 1)
    before = store.checkpoints.path(session.id).read_bytes()
    async with app.run_test(size=(110, 42)) as pilot:
        app._client = client
        app._session_id = session.id
        await app._do_memory()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, MemoryScreen)
        frozen = screen.query_one("#memory-snapshot", TextArea)
        assert frozen.read_only and "old version" in frozen.text and "new version" not in frozen.text
        view = screen.query_one(ListView)
        view.focus()
        await pilot.press("enter")
        await pilot.pause()
        detail = screen.query_one("#memory-detail", TextArea)
        assert detail.read_only and "new version" in detail.text
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, MemoryScreen)
    assert store.checkpoints.path(session.id).read_bytes() == before
    assert len(model.calls) == 1 and all(method == "memory.inspect" for method, _ in client.calls)


# 功能：验证 /memory 被前端拦截，不作为真实用户消息发给 Agent
# 设计：通过实际输入提交事件打开窗口，检查 TCP 方法与输入框忙碌状态
async def test_memory_slash_command_does_not_create_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    _, manager = setup_manager(tmp_path, Provider([]))
    session = await manager.create("chat")
    client = Client(manager)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 42)) as pilot:
        app._client = client
        await app._open_session(session.id)
        prompt = app.query_one(ChatTextArea)
        prompt.text = "/memory"
        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        worker = next(worker for worker in app.workers if worker.name == "memory")
        await worker.wait()
        await pilot.pause()
        assert isinstance(app.screen, MemoryScreen)
        assert app._busy is False
        assert all(method != "session.send_message" for method, _ in client.calls)
        assert app.screen.query_one("#memory-next", Button).disabled


# 功能：验证真实列表分页、正文续读和作用域切换不会修改任何记忆
# 设计：创建 51 条记录触发下一页，在最后一页读取长正文，再切换到空会话层
async def test_memory_screen_pagination_and_scope_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual.widgets import Select

    isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    _, manager = setup_manager(tmp_path, Provider([]))
    session = await manager.create("chat")
    memory = MemoryStore(tmp_path)
    for index in range(51):
        memory.upsert(
            "project", f"item_{index:02}", summary="Item", content="head", source="s / r",
        )
    path = memory.directory("project") / "item_00.md"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("中" * 6_000)
    original = path.read_bytes()
    client = Client(manager)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 42)) as pilot:
        app._client, app._session_id = client, session.id
        await app._do_memory()
        await pilot.pause()
        screen = app.screen
        assert len(screen.query_one(ListView).children) == 50
        await pilot.click("#memory-next")
        await pilot.pause()
        assert len(screen.query_one(ListView).children) == 1
        assert not screen.query_one("#memory-prev", Button).disabled
        view = screen.query_one(ListView)
        view.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not screen.query_one("#memory-more", Button).disabled
        await pilot.click("#memory-more")
        await pilot.pause()
        assert "4000/" in screen.query_one("#memory-detail", TextArea).text
        assert screen.query_one("#memory-more", Button).disabled
        screen.query_one("#memory-scope", Select).value = "session"
        await pilot.pause()
        assert len(screen.query_one(ListView).children) == 0
        assert screen.query_one("#memory-prev", Button).disabled
        assert screen.query_one("#memory-next", Button).disabled
    assert path.read_bytes() == original
