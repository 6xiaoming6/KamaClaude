from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from kama_claude.core.config import McpServerConfig
from kama_claude.core.mcp.client import McpClient, McpServerUnavailableError, McpToolError
from kama_claude.core.mcp.server import McpServerManager

SCRIPT = Path(__file__).with_name("mcp_server.py")
HEADERS = {"x-test-key": "test-secret"}


@pytest.fixture(params=["json", "sse"])
# 功能：启动独立进程中的官方 SDK MCP 服务，分别返回 JSON 和 SSE
# 设计：通过真实 HTTP 端口测试协议，不用模拟传输冒充真实服务
async def mcp_endpoint(request: Any, free_port: int, tmp_path: Path):
    with (tmp_path / "mcp-server.log").open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-B", str(SCRIPT), "http", str(free_port), request.param],
            stdout=log, stderr=log,
        )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail((tmp_path / "mcp-server.log").read_text(encoding="utf-8"))
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", free_port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("MCP service did not start")
        yield f"http://127.0.0.1:{free_port}/mcp"
    finally:
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait, timeout=5)


# 功能：验证真实服务连接、并发工具调用、isError 及跨任务关闭
# 设计：同一个客户端由不同 asyncio Task 使用，覆盖 SDK 取消域的生命周期边界
async def test_real_http_service_roundtrip_and_close(mcp_endpoint):
    client = McpClient()
    await asyncio.create_task(client.connect_http(mcp_endpoint, HEADERS))
    try:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == {"add", "fail", "slow", "has_environment"}
        assert client.protocol_version
        output = await asyncio.gather(
            client.call_tool("add", {"a": 2, "b": 3}),
            client.call_tool("add", {"a": 7, "b": 8}),
        )
        assert [json.loads(text) for text in output] == [5, 15]
        with pytest.raises(McpToolError, match="expected failure"):
            await client.call_tool("fail", {})
        assert json.loads(await client.call_tool("add", {"a": 1, "b": 1})) == 2
    finally:
        await asyncio.create_task(client.close())
    await client.close()
    with pytest.raises(McpServerUnavailableError):
        await client.list_tools()


# 功能：验证鉴权失败不遗留会话，同一客户端可在清理后重新连接
# 设计：先缺少请求头得到真实 HTTP 401，再补齐请求头连接同一个端点
async def test_auth_failure_then_reconnect(mcp_endpoint):
    client = McpClient()
    with pytest.raises(McpServerUnavailableError):
        await client.connect_http(mcp_endpoint)
    assert client._task is None
    try:
        await client.connect_http(mcp_endpoint, HEADERS)
        assert await client.list_tools()
    finally:
        await client.close()


# 功能：验证 Manager 按配置发现并包装远程工具，关闭后不留下失效工具
# 设计：走项目真正的工具注册链路，不只直接调用 SDK
async def test_manager_registers_remote_tools(mcp_endpoint):
    manager = McpServerManager()
    await manager.start_all([McpServerConfig(
        name="remote", transport="streamable-http", url=mcp_endpoint, headers=HEADERS,
    )])
    try:
        tools = {tool.name: tool for tool in manager.get_tools()}
        result = await tools["remote__add"].invoke({"a": 10, "b": 20})
        assert not result.is_error and json.loads(result.content) == 30
        failed = await tools["remote__fail"].invoke({})
        assert failed.is_error and not failed.outcome_unknown
    finally:
        await manager.stop_all()
    assert manager.get_tools() == []


# 功能：验证取消单次远程工具调用后，会话仍可使用并能正常关闭
# 设计：对真实慢工具超时取消，再发送一次无副作用调用
async def test_cancel_http_call_keeps_session_usable(mcp_endpoint):
    client = McpClient()
    try:
        await client.connect_http(mcp_endpoint, HEADERS)
        await client.list_tools()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.call_tool("slow", {}), timeout=0.1)
        assert json.loads(await client.call_tool("add", {"a": 3, "b": 4})) == 7
    finally:
        await client.close()


# 功能：验证保留的 stdio 传输能运行官方 SDK 服务，并在关闭时结束子进程
# 设计：服务将 PID 写入临时文件，检查真实进程身份而非只断言 mock 被调用
async def test_real_stdio_service_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "s10-fake-key")
    monkeypatch.setenv("S10_MCP_FLAG", "explicitly-forwarded")
    client = McpClient()
    pid_path = tmp_path / "mcp.pid"
    process = None
    try:
        await client.connect_stdio(sys.executable, ["-X", "utf8", "-B", str(SCRIPT), "stdio"],
                                   {"MCP_TEST_PID": str(pid_path)}, forward_env=["S10_MCP_FLAG"])
        process = psutil.Process(int(pid_path.read_text(encoding="utf-8")))
        assert await client.list_tools()
        assert json.loads(await client.call_tool("add", {"a": 5, "b": 6})) == 11
        assert json.loads(await client.call_tool("has_environment", {"name": "OPENAI_API_KEY"})) is False
        assert json.loads(await client.call_tool("has_environment", {"name": "S10_MCP_FLAG"})) is True
    finally:
        await client.close()
    assert process is not None and not process.is_running()


# 功能：验证服务返回协议错误后映射为项目工具异常，客户端仍可清理
# 设计：使用真实会话先调用成功，再请求不存在工具验证协议错误传播
async def test_protocol_error_is_not_success(mcp_endpoint):
    client = McpClient()
    try:
        await client.connect_http(mcp_endpoint, HEADERS)
        await client.list_tools()
        with pytest.raises(McpToolError):
            await client.call_tool("missing", {})
    finally:
        await client.close()
