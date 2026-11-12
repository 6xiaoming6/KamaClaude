from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import kama_claude.core.app as core_app
from kama_claude.core.config import KamaConfig
from kama_claude.core.config.models import McpServerConfig


@pytest.fixture(autouse=True)
# 功能：Core 单元测试不访问真实 Docker，启动清理在独立真实容器测试中验证。
# 设计：与已有服务器、MCP 替身保持同一边界，避免外部引擎耗时影响信号测试。
def isolated_orphan_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_app, "cleanup_orphans", AsyncMock())


@pytest.mark.parametrize(
    ("platform", "shutdown_mode"),
    [("win32", "cancel"), ("linux", "cancel"), ("linux", "signal")],
)
# 功能：验证 Windows 不注册 Unix 信号，取消或正常退出都会关闭服务器和 MCP 连接。
# 设计：注入模拟服务器、MCP 管理器和信号注册器，覆盖 Windows 取消及 Unix 信号、取消路径。
async def test_shutdown_closes_server(
    monkeypatch: pytest.MonkeyPatch, platform: str, shutdown_mode: str
) -> None:
    started = asyncio.Event()
    server = Mock()
    server.start = AsyncMock(side_effect=lambda: started.set() or "127.0.0.1:7437")
    server.stop = AsyncMock()
    mcp_manager = Mock()
    mcp_manager.start_all = AsyncMock()
    mcp_manager.stop_all = AsyncMock()
    register_signal = Mock()
    if platform == "win32":
        register_signal.side_effect = NotImplementedError

    monkeypatch.setattr(core_app, "sys", SimpleNamespace(platform=platform), raising=False)
    monkeypatch.setattr(core_app, "get_config", lambda: KamaConfig())
    monkeypatch.setattr(core_app, "setup_logging", Mock())
    monkeypatch.setattr(core_app, "SocketServer", Mock(return_value=server))
    monkeypatch.setattr(core_app, "McpServerManager", Mock(return_value=mcp_manager))
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", register_signal)

    task = asyncio.create_task(core_app.CoreApp().run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        if shutdown_mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            callback = register_signal.call_args_list[1].args[1]
            callback()
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    server.stop.assert_awaited_once()
    mcp_manager.stop_all.assert_awaited_once()
    if platform == "win32":
        register_signal.assert_not_called()
    else:
        assert [call.args[0] for call in register_signal.call_args_list] == [
            signal.SIGINT,
            signal.SIGTERM,
        ]


@pytest.mark.parametrize("failure", [None, "sessions", "mcp", "server"])
async def test_shutdown_gates_requests_first_and_keeps_cleanup_on_failure(monkeypatch, failure):
    order = []
    started = asyncio.Event()

    def record(name):
        order.append(name)
        if name == failure:
            raise RuntimeError(f"controlled {name} failure")

    server = Mock()
    server.start = AsyncMock(side_effect=lambda: started.set() or "local")
    server.begin_shutdown.side_effect = lambda: record("gate")
    server.stop = AsyncMock(side_effect=lambda: record("server"))
    sessions = Mock()
    sessions.shutdown = AsyncMock(side_effect=lambda: record("sessions"))
    mcp = Mock()
    mcp.start_all = AsyncMock()
    mcp.stop_all = AsyncMock(side_effect=lambda: record("mcp"))
    trace = Mock()
    trace.start = AsyncMock()
    trace.stop = AsyncMock(side_effect=lambda: record("trace"))
    monkeypatch.setattr(core_app, "get_config", lambda: KamaConfig())
    monkeypatch.setattr(core_app, "setup_logging", Mock())
    monkeypatch.setattr(core_app, "SocketServer", Mock(return_value=server))
    monkeypatch.setattr(core_app, "SessionManager", Mock(return_value=sessions))
    monkeypatch.setattr(core_app, "McpServerManager", Mock(return_value=mcp))
    monkeypatch.setattr(core_app, "TraceWriter", Mock(return_value=trace))
    monkeypatch.setattr(core_app, "sys", SimpleNamespace(platform="win32"))
    task = asyncio.create_task(core_app.CoreApp().run())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(RuntimeError if failure else asyncio.CancelledError):
        await task
    assert order == ["gate", "sessions", "mcp", "server", "trace"]
    controls = {call.args[0] for call in server.register.call_args_list if call.kwargs.get("control")}
    assert controls == {"core.ping", "core.shutdown", "permission.respond",
                        "session.set_permission_mode", "session.interrupt"}


async def test_startup_log_excludes_configured_credentials(monkeypatch, caplog):
    config = KamaConfig()
    config.trace.enabled = False
    secrets = ["fake-execution-secret", "fake-header-secret", "fake-mcp-secret",
               "fake-argument-secret", "fake-url-secret"]
    config.execution.env = {"CUSTOM_TOKEN": secrets[0]}
    config.mcp.servers = [McpServerConfig(
        "remote", transport="streamable-http", headers={"Authorization": secrets[1]},
        env={"CUSTOM_TOKEN": secrets[2]}, args=["--token=" + secrets[3]],
        url="https://example.invalid/mcp?key=" + secrets[4],
    )]
    started = asyncio.Event()
    server = Mock()
    server.start = AsyncMock(side_effect=lambda: started.set() or "local")
    server.stop = AsyncMock()
    mcp = Mock(start_all=AsyncMock(), stop_all=AsyncMock())
    monkeypatch.setattr(core_app, "get_config", lambda: config)
    monkeypatch.setattr(core_app, "setup_logging", Mock())
    monkeypatch.setattr(core_app, "SocketServer", Mock(return_value=server))
    monkeypatch.setattr(core_app, "McpServerManager", Mock(return_value=mcp))
    monkeypatch.setattr(core_app, "sys", SimpleNamespace(platform="win32"))
    with caplog.at_level("INFO", logger=core_app.__name__):
        task = asyncio.create_task(core_app.CoreApp().run())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert "config" in caplog.text
    assert not any(secret in caplog.text for secret in secrets)
