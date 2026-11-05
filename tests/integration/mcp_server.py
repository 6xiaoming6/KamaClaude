from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import uvicorn
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

server = MCPServer("KamaClaude MCP integration", log_level="ERROR")


@server.tool(description="Add two numbers using a real MCP service")
# 返回确定性结果，供 stdio 和 HTTP 两种真实传输复用
def add(a: int, b: int) -> int:
    return a + b


@server.tool(description="Return a real MCP application error")
# 模拟工具已明确返回错误，而不是传输层断线
def fail() -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text="expected failure")], is_error=True)


@server.tool(description="Wait before returning, for cancellation tests")
# 等待任务允许测试主动取消客户端请求后继续使用同一会话
async def slow() -> str:
    await asyncio.sleep(30)
    return "finished"


@server.tool(description="Report environment presence without exposing values")
def has_environment(name: str) -> bool:
    return name in os.environ


# 启动独立 SDK 服务，HTTP 模式支持 JSON/SSE 响应和固定测试请求头
def main() -> None:
    if path := os.environ.get("MCP_TEST_PID"):
        Path(path).write_text(str(os.getpid()), encoding="utf-8")
    if sys.argv[1] == "stdio":
        server.run(transport="stdio")
        return
    port = int(sys.argv[2])
    app = server.streamable_http_app(json_response=sys.argv[3] == "json")

    # 对所有 MCP HTTP 请求检查请求头，验证初始化、调用及关闭均携带鉴权
    async def authenticated(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and dict(scope["headers"]).get(b"x-test-key") != b"test-secret":
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await app(scope, receive, send)

    uvicorn.run(authenticated, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    main()
