from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import kama_claude.core.app as core_app
from kama_claude.core.config import KamaConfig


@pytest.mark.parametrize(
    ("platform", "shutdown_mode"),
    [("win32", "cancel"), ("linux", "cancel"), ("linux", "signal")],
)
# 功能：验证 Windows 启动不注册 Unix 信号，取消或正常退出都会关闭服务器。
# 设计：注入模拟服务器和信号注册器，覆盖 Windows 取消及 Unix 信号、取消三条路径。
async def test_shutdown_closes_server(
    monkeypatch: pytest.MonkeyPatch, platform: str, shutdown_mode: str
) -> None:
    started = asyncio.Event()
    server = Mock()
    server.start = AsyncMock(side_effect=lambda: started.set() or "127.0.0.1:7437")
    server.stop = AsyncMock()
    register_signal = Mock()
    if platform == "win32":
        register_signal.side_effect = NotImplementedError

    monkeypatch.setattr(core_app, "sys", SimpleNamespace(platform=platform), raising=False)
    monkeypatch.setattr(core_app, "get_config", lambda: KamaConfig())
    monkeypatch.setattr(core_app, "setup_logging", Mock())
    monkeypatch.setattr(core_app, "SocketServer", Mock(return_value=server))
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
    if platform == "win32":
        register_signal.assert_not_called()
    else:
        assert [call.args[0] for call in register_signal.call_args_list] == [
            signal.SIGINT,
            signal.SIGTERM,
        ]
