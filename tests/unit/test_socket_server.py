from __future__ import annotations

import asyncio
import socket
from unittest.mock import Mock

import pytest

from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_server import SocketServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 功能：验证客户端断开后 SocketServer 调用 broadcaster.unsubscribe(writer) 清理订阅
# 设计：用内联 MockBroadcaster 捕获 unsubscribe 调用并设置 asyncio.Event，避免 sleep 轮询；
#       等待 Event 而非断言调用次数，确保时序正确性而不依赖竞态假设
async def test_broadcaster_unsubscribe_called_on_disconnect() -> None:
    unsubscribed = asyncio.Event()

    class MockBroadcaster:
        def unsubscribe(self, writer: object) -> None:
            unsubscribed.set()

    port = _free_port()
    server = SocketServer("127.0.0.1", port, broadcaster=MockBroadcaster())  # type: ignore[arg-type]
    await server.start()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()

        await asyncio.wait_for(unsubscribed.wait(), timeout=2.0)
    finally:
        await server.stop()


# 功能：验证不传入 broadcaster 时 SocketServer 仍可正常启动和停止（backward-compatible 默认值）
# 设计：直接实例化 SocketServer(host, port)（无 broadcaster），start/stop 不抛异常即为通过；
#       回归测试确保新参数的默认值 None 不破坏现有调用方
async def test_no_broadcaster_server_starts_and_stops() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port)
    await server.start()
    await server.stop()


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError(22, "peer reset (WinError 64)"),
        ConnectionAbortedError("peer aborted"),
        BrokenPipeError("peer closed"),
    ],
)
# 功能：验证客户端连接重置或中止时正常退出连接协程，并清理 writer 和事件订阅
# 设计：用真实 StreamReader 注入网络异常，模拟 Windows 断开行为，检查 Task 不再以异常结束
async def test_connection_loss_is_handled_and_cleans_up(error: ConnectionError) -> None:
    broadcaster = IpcEventBroadcaster()
    server = SocketServer("127.0.0.1", 1, broadcaster=broadcaster)
    reader = asyncio.StreamReader()
    reader.set_exception(error)
    writer = Mock(spec=asyncio.StreamWriter)
    broadcaster.subscribe(writer, ["*"])

    task = asyncio.create_task(server._handle_connection(reader, writer))
    await task

    assert task.exception() is None
    assert writer not in server._active_writers
    assert not broadcaster._subscriptions
    writer.close.assert_called_once_with()


@pytest.mark.parametrize("error", [RuntimeError("bug"), asyncio.CancelledError()])
# 功能：验证连接处理不吞掉程序错误和取消信号，异常传播时仍清理连接
# 设计：对相同读取入口注入非网络异常，约束修复仅覆盖预期断线而非宽泛屏蔽错误
async def test_other_connection_errors_still_propagate(error: BaseException) -> None:
    broadcaster = IpcEventBroadcaster()
    server = SocketServer("127.0.0.1", 1, broadcaster=broadcaster)
    reader = asyncio.StreamReader()
    reader.set_exception(error)
    writer = Mock(spec=asyncio.StreamWriter)
    broadcaster.subscribe(writer, ["*"])

    with pytest.raises(type(error)):
        await server._handle_connection(reader, writer)

    assert writer not in server._active_writers
    assert not broadcaster._subscriptions
    writer.close.assert_called_once_with()
