from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, ListToolsResult, Tool

from kama_claude.core.config import KamaConfig
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.mcp.client import McpClient, McpServerUnavailableError
from kama_claude.core.mcp.server import McpServerManager


# 功能：验证 HTTP URL、请求头和 stdio 参数可从同一个配置文件读取
# 设计：两个真实配置小节并存，确认没有把 Core 的 TCP 参数当成 MCP 参数
def test_config_reads_standard_transports():
    config = KamaConfig()
    apply_toml(config, {"mcp": {"servers": [
        {"name": "remote", "transport": "streamable-http", "url": "https://example.com/mcp",
         "headers": {"Authorization": "Bearer test"}},
        {"name": "local", "command": "python", "args": ["server.py"], "env": {"KEY": "test"}},
    ]}})
    remote, local = config.mcp.servers
    assert remote.url == "https://example.com/mcp"
    assert remote.headers == {"Authorization": "Bearer test"}
    assert local.transport == "stdio" and local.command == "python"


@pytest.mark.parametrize("entry", [
    {"transport": "tcp", "host": "localhost", "port": 3000},
    {"transport": "sse", "url": "https://example.com/sse"},
    {"transport": "streamable-http"},
    {"transport": "streamable-http", "url": "file:///tmp/mcp"},
    {"transport": "streamable-http", "url": "https://example.com:bad/mcp"},
    {"transport": "streamable-http", "url": "https://user:secret@example.com/mcp"},
    {"transport": "streamable-http", "url": "https://example.com/mcp", "headers": {"Key": 1}},
    {"transport": "streamable-http", "url": "https://example.com/mcp", "host": "old"},
    {"transport": "stdio"},
    {"transport": "stdio", "command": "python", "args": [1]},
])
# 功能：验证旧裸 TCP 和错误 HTTP 配置被明确拒绝
# 设计：逐项覆盖 URL、请求头和传输混用，防止错误配置静默降级
def test_invalid_mcp_config_is_rejected(entry):
    with pytest.raises(SystemExit):
        apply_toml(KamaConfig(), {"mcp": {"servers": [{"name": "test", **entry}]}})


# 功能：验证重名服务不会在 Manager 中覆盖连接并遗留后台任务
# 设计：配置两份同名服务，在建立网络连接前直接拒绝
def test_duplicate_server_name_rejected():
    server = {"name": "same", "command": "python"}
    with pytest.raises(SystemExit, match="unique"):
        apply_toml(KamaConfig(), {"mcp": {"servers": [server, server]}})


# 功能：验证远程工具分页全部被发现，重复游标不会造成无限循环
# 设计：模拟 SDK 的强类型分页结果，仅测试项目的分页协调逻辑
async def test_all_tool_pages_and_repeated_cursor():
    sdk = AsyncMock()
    sdk.list_tools.side_effect = [
        ListToolsResult(tools=[Tool(name="first", input_schema={})], next_cursor="page2"),
        ListToolsResult(tools=[Tool(name="second", input_schema={})]),
    ]
    client = McpClient()
    client._client = sdk
    assert [tool.name for tool in await client.list_tools()] == ["first", "second"]
    sdk.list_tools.side_effect = [
        ListToolsResult(tools=[], next_cursor="same"),
        ListToolsResult(tools=[], next_cursor="same"),
    ]
    with pytest.raises(McpServerUnavailableError, match="repeated cursor"):
        await client.list_tools()


# 功能：验证工具发现失败时 Manager 会关闭已经建立的连接
# 设计：让连接成功而 tools/list 失败，覆盖初始化后的资源泄漏路径
async def test_manager_closes_connection_when_discovery_fails(monkeypatch):
    from kama_claude.core.config import McpServerConfig
    client = AsyncMock(spec=McpClient)
    client.list_tools.side_effect = McpServerUnavailableError("disconnected")
    manager = McpServerManager()
    monkeypatch.setattr(manager, "_connect", AsyncMock(return_value=client))
    await manager.start_all([McpServerConfig(name="remote", transport="streamable-http")])
    client.close.assert_awaited_once()
    assert manager.get_tools() == []


@pytest.mark.parametrize("code", [CONNECTION_CLOSED, REQUEST_TIMEOUT])
# 功能：验证 SDK 的连接中断和超时不会被误当作确定的工具失败
# 设计：注入 SDK 真实错误类型与错误码，保持 S8 的未知副作用语义
async def test_sdk_connection_errors_remain_unknown(code):
    from kama_claude.core.mcp.client import McpToolDef
    from kama_claude.core.mcp.tool import McpTool
    sdk = AsyncMock()
    sdk.call_tool.side_effect = MCPError(code, "connection lost")
    client = McpClient()
    client._client = sdk
    tool = McpTool(client, "remote", McpToolDef("write", "write remotely"))
    result = await tool.invoke({})
    assert result.is_error and result.outcome_unknown
