from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.app import CoreApp
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.mode import PermissionMode
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer
from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.settings import PermissionModePicker


# 两个真实连接观察同一会话；切换、广播与重连均不创建业务 Run。
async def test_permission_mode_over_tcp(tmp_path: Path, free_port: int) -> None:
    app, bus, broadcaster = CoreApp(), EventBus(), IpcEventBroadcaster()
    bus.subscribe(broadcaster.handle)
    permissions = PermissionManager()
    app._sessions = SessionManager(
        SessionStore(tmp_path), lambda: None, bus, permission_manager=permissions,  # type: ignore[arg-type,return-value]
    )
    app._broadcaster = broadcaster
    server = SocketServer("127.0.0.1", free_port, broadcaster=broadcaster)
    server.register("session.create", app._session_create_handler)
    server.register("session.open", app._session_open_handler)
    server.register("session.set_permission_mode", app._session_permission_mode_handler, control=True)
    await server.start()
    clients: list[SocketClient] = []
    readers: list[asyncio.Task[None]] = []
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async def received(event: dict[str, Any]) -> None:
        events.put_nowait(event)
    async def connect() -> SocketClient:
        client = SocketClient("127.0.0.1", free_port)
        await client.connect()
        clients.append(client)
        readers.append(asyncio.create_task(client.run_event_loop()))
        return client
    try:
        first, second = await connect(), await connect()
        second.on_event(received)
        sid = (await first.send_command("session.create", {}))["session_id"]
        for client in (first, second):
            assert (await client.send_command("session.open", {"session_id": sid}))["permission_mode"] == "ask"
        changed = await first.send_command("session.set_permission_mode", {"session_id": sid, "mode": "approve"})
        assert changed["permission_mode"] == "approve"
        event = await asyncio.wait_for(events.get(), 2)
        assert event["type"] == "session.permission_mode_changed"
        assert event["session_id"] == sid and event["permission_mode"] == "approve"
        with pytest.raises(IpcError) as invalid:
            await first.send_command("session.set_permission_mode", {"session_id": sid, "mode": "invalid"})
        assert invalid.value.code == -32602
        assert permissions.get_mode(sid) == PermissionMode.APPROVE
        await first.close()
        first = await connect()
        assert (await first.send_command("session.open", {"session_id": sid}))["permission_mode"] == "approve"
        assert app._sessions._tasks == {}
    finally:
        for client in clients:
            await client.close()
        await asyncio.gather(*readers, return_exceptions=True)
        await server.stop()


# 实际文件调用：放宽审批仍经过路径复核和工具自身只读保护，模型参数不能选择模式。
async def test_modes_with_real_file_tools(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("original", encoding="utf-8")
    registry, bus, permissions = ToolRegistry(), EventBus(), PermissionManager()
    registry.register(ReadFileTool(root))
    registry.register(WriteFileTool(root))
    requested: list[Any] = []
    async def deny(event: Any) -> None:
        if event.type == "permission.requested":
            requested.append(event)
            permissions.respond(event.request_id, "deny_once", session_id="s", run_id="r")
    bus.subscribe(deny)
    async def call(name: str, params: dict[str, Any]):
        return await invoke_tool(registry, ToolCallBlock(id="t", name=name, input=params), bus, "r",
                                 permission_manager=permissions, session_id="s")
    permissions.set_mode("s", PermissionMode.APPROVE)
    assert not (await call("read_file", {"path": str(external)})).is_error
    assert not (await call("write_file", {"path": "internal.txt", "content": "ok"})).is_error
    assert (await call("write_file", {"path": str(external), "content": "changed"})).error_type == "permission_denied"
    assert external.read_text(encoding="utf-8") == "original" and len(requested) == 1
    permissions.set_mode("s", PermissionMode.FULL)
    assert not (await call("write_file", {"path": str(external), "content": "changed"})).is_error
    assert external.read_text(encoding="utf-8") == "changed" and len(requested) == 1
    assert (await call("write_file", {"path": "AGENTS.md", "content": "forbidden"})).error_type == "permission_denied"
    assert not (root / "AGENTS.md").exists()
    permissions.set_mode("s", PermissionMode.ASK)
    # 伪造 mode 字段在参数校验阶段失败，不会自动放宽审批。
    result = await call("write_file", {"path": "other.txt", "content": "x", "mode": "full"})
    assert result.is_error and not (root / "other.txt").exists()


# 使用真实连接 Worker 和 TCP，不再用空协程替代连接而遗漏 exclusive 的取消行为。
async def test_tui_mode_picker_keeps_connection(tmp_path: Path, free_port: int) -> None:
    core, bus, broadcaster = CoreApp(), EventBus(), IpcEventBroadcaster()
    bus.subscribe(broadcaster.handle)
    core._sessions = SessionManager(
        SessionStore(tmp_path), lambda: None, bus,  # type: ignore[arg-type,return-value]
        permission_manager=PermissionManager(),
    )
    core._broadcaster = broadcaster
    server = SocketServer("127.0.0.1", free_port, broadcaster=broadcaster)
    for method, handler in {
        "session.list": core._session_list_handler,
        "session.create": core._session_create_handler,
        "session.open": core._session_open_handler,
        "session.set_permission_mode": core._session_permission_mode_handler,
    }.items():
        server.register(method, handler)
    await server.start()
    ui = KamaTuiApp("127.0.0.1", free_port)
    try:
        async with ui.run_test(size=(120, 50)) as pilot:
            async def ready() -> None:
                while (
                    ui._session_id is None or ui._opening or ui._client is None
                    or ui._prompt() is None or ui._prompt().disabled
                ):
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(ready(), 5)
            client = ui._client
            socket_worker = next(worker for worker in ui.workers if worker.name == "socket")
            for mode in ("approve", "full", "ask"):
                await pilot.press("ctrl+p")
                assert isinstance(ui.screen, PermissionModePicker)
                assert socket_worker.is_running and ui._client is client
                await pilot.click(f"#mode-{mode}")
                await pilot.pause()
                assert ui._permission_mode.value == mode
                assert socket_worker.is_running and ui._client is client
                assert not ui._prompt().disabled
                assert "disconnected" not in ui._prompt().border_title
            await pilot.press("ctrl+p", "escape")
            assert socket_worker.is_running and ui._client is client
            assert core._sessions._tasks == {}
    finally:
        await server.stop()
