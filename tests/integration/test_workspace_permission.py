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
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer, get_connection_sender


# 功能：验证真实 TCP 下外部读取两选项、内部写入三选项，重连后仍能批准原请求
# 设计：复用真实 handler、广播器和文件工具，检查非法回答不消费请求及断线不取消调用
@pytest.mark.parametrize("internal_write", [False, True])
async def test_file_approval_over_tcp_and_reconnect(
    tmp_path: Path, free_port: int, internal_write: bool,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    archive = (root if internal_write else tmp_path) / "outputs.txt"
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
        broadcaster.subscribe(get_connection_sender(), ["permission.*"], scope="session:sess-one")
        return {"pending_permissions": manager.pending_for_session("sess-one")}

    server.register("session.open", open_session)
    await server.start()
    client = SocketClient("127.0.0.1", free_port)
    await client.connect()
    reader = asyncio.create_task(client.run_event_loop())
    registry = ToolRegistry()
    registry.register(WriteFileTool(root) if internal_write else ReadFileTool(root))
    choices = ["allow_once", "deny_once", "always_allow"] if internal_write else ["allow_once", "deny_once"]
    requested: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    # 收集经过真实 TCP 序列化后的请求内容
    async def event_received(event: dict[str, Any]) -> None:
        if event["type"] == "permission.requested" and not requested.done():
            requested.set_result(event)

    client.on_event(event_received)
    tool_task: asyncio.Task[Any] | None = None
    try:
        await client.send_command("session.open", {"session_id": "sess-one"})
        call = ToolCallBlock(
            id="file-call", name="write_file" if internal_write else "read_file",
            input={"path": "./outputs.txt", "content": "updated"} if internal_write else {"path": str(archive)},
        )
        tool_task = asyncio.create_task(invoke_tool(
            registry, call,
            bus, "run-one", permission_manager=manager, session_id="sess-one",
        ))
        event = await asyncio.wait_for(requested, 3)
        assert event["resolved_path"] == str(archive)
        assert event["allowed_decisions"] == choices
        for session_id, run_id in (("other", "run-one"), ("sess-one", "other")):
            with pytest.raises(IpcError) as wrong_owner:
                await client.send_command("permission.respond", {
                    "request_id": event["request_id"], "decision": "allow_once",
                    "session_id": session_id, "run_id": run_id,
                })
            assert wrong_owner.value.code == -32602
        assert len(manager.pending_for_session("sess-one")) == 1
        with pytest.raises(IpcError) as invalid:
            await client.send_command("permission.respond", {
                "request_id": event["request_id"],
                "decision": "always_deny" if internal_write else "always_allow",
                "session_id": "sess-one", "run_id": "run-one",
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
        assert pending["allowed_decisions"] == choices
        assert pending["request_id"] == event["request_id"]
        await client.send_command("permission.respond", {
            "request_id": pending["request_id"],
            "decision": "always_allow" if internal_write else "allow_once",
            "session_id": "sess-one", "run_id": "run-one",
        })
        result = await asyncio.wait_for(tool_task, 3)
        if internal_write:
            assert not result.is_error
            assert archive.read_text(encoding="utf-8") == "updated"
            assert len(manager._file_grants) == 1
            # 同一目标的下一次调用无需再经 TCP 审批，也没有新增等待对象。
            repeated = await asyncio.wait_for(invoke_tool(
                registry, call, bus, "run-two", permission_manager=manager, session_id="sess-one",
            ), 3)
            assert not repeated.is_error and not manager._pending
            manager.cancel_session("sess-one")
            assert not manager._file_grants
        else:
            assert result.content.endswith("1 | archived result")
        assert manager._session_grants == {}
        with pytest.raises(IpcError) as replay:
            await client.send_command("permission.respond", {
                "request_id": pending["request_id"], "decision": "allow_once",
                "session_id": "sess-one", "run_id": "run-one",
            })
        assert replay.value.code == -32602
    finally:
        if tool_task is not None and not tool_task.done():
            tool_task.cancel()
            with suppress(asyncio.CancelledError):
                await tool_task
        await client.close()
        await reader
        await server.stop()
