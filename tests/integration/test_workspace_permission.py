from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.app import CoreApp
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer, get_connection_writer


# 功能：验证真实 TCP 下外部审批拒绝永久授权，重连后仍可单次批准
# 设计：使用真实 Core handler、广播器与文件读取；关闭客户端后业务等待保持，不调用付费模型
async def test_external_approval_over_tcp_and_reconnect(tmp_path: Path, free_port: int) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    archive = tmp_path / "outputs.txt"
    archive.write_text("archived result", encoding="utf-8")
    app = CoreApp()
    manager = PermissionManager(timeout_s=5)
    app._permission_manager = manager
    broadcaster = IpcEventBroadcaster()
    bus = EventBus()
    bus.subscribe(broadcaster.handle)
    server = SocketServer("127.0.0.1", free_port, broadcaster=broadcaster)
    server.register("permission.respond", app._permission_respond_handler)

    # 新连接绑定同一会话并返回仍有效的审批快照
    async def open_session(params: dict[str, Any]) -> dict[str, Any]:
        broadcaster.subscribe(get_connection_writer(), ["permission.*"], scope="session:sess-one")
        return {"pending_permissions": manager.pending_for_session("sess-one")}

    server.register("session.open", open_session)
    await server.start()
    client = SocketClient("127.0.0.1", free_port)
    await client.connect()
    reader = asyncio.create_task(client.run_event_loop())
    registry = ToolRegistry()
    registry.register(ReadFileTool(root))
    requested: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    # 收集经过真实 TCP 序列化后的请求内容
    async def event_received(event: dict[str, Any]) -> None:
        if event["type"] == "permission.requested" and not requested.done():
            requested.set_result(event)

    client.on_event(event_received)
    tool_task: asyncio.Task[Any] | None = None
    try:
        await client.send_command("session.open", {"session_id": "sess-one"})
        tool_task = asyncio.create_task(invoke_tool(
            registry, ToolCallBlock(id="external", name="read_file", input={"path": str(archive)}),
            bus, "run-one", permission_manager=manager, session_id="sess-one",
        ))
        event = await asyncio.wait_for(requested, 3)
        assert event["resolved_path"] == str(archive)
        assert event["allowed_decisions"] == ["allow_once", "deny_once"]
        with pytest.raises(IpcError) as invalid:
            await client.send_command("permission.respond", {
                "tool_use_id": "external", "decision": "always_allow",
            })
        assert invalid.value.code == -32602
        assert not tool_task.done()
        await client.close()
        await reader
        client = SocketClient("127.0.0.1", free_port)
        await client.connect()
        reader = asyncio.create_task(client.run_event_loop())
        snapshot = await client.send_command("session.open", {"session_id": "sess-one"})
        pending = snapshot["pending_permissions"][0]
        assert pending["resolved_path"] == str(archive)
        assert pending["allowed_decisions"] == ["allow_once", "deny_once"]
        await client.send_command("permission.respond", {
            "tool_use_id": "external", "decision": "allow_once",
        })
        result = await asyncio.wait_for(tool_task, 3)
        assert result.content.endswith("1 | archived result")
        assert manager._session_always == manager._persistent_always == {}
    finally:
        if tool_task is not None and not tool_task.done():
            tool_task.cancel()
            with suppress(asyncio.CancelledError):
                await tool_task
        await client.close()
        await reader
        await server.stop()
