from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client import Transport
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, Implementation, TextContent

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


class McpToolError(Exception):
    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


# 展开 SDK 任务组异常，保留真正的连接失败原因
def _error_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_error_message(item) for item in exc.exceptions)
    message = str(exc) or type(exc).__name__
    if exc.__cause__ is not None:
        message += ": " + _error_message(exc.__cause__)
    return message


# 两种传输共用官方 MCP 会话，工具包装层不需要知道底层协议
class McpClient:
    # SDK 的进入与退出固定在同一后台任务，避免跨任务关闭 AnyIO 取消域
    def __init__(self) -> None:
        self._client: Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._failure = "MCP client is not connected"

    # 启动本地 stdio 服务，由 SDK 完成初始化与消息分发
    async def connect_stdio(
        self, command: str, args: list[str], env: dict[str, str] | None = None,
    ) -> None:
        await self._connect(StdioServerParameters(
            command=command, args=args, env={**os.environ, **(env or {})},
        ))

    # 连接标准 Streamable HTTP 端点，SDK 同时处理 JSON 和 SSE 响应
    async def connect_http(self, url: str, headers: dict[str, str] | None = None) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.fragment
        ):
            raise ValueError(
                "MCP Streamable HTTP requires an http(s) URL without userinfo or fragment"
            )
        await self._connect(url, headers)

    # 连接建立后立即返回，后台任务继续持有会话直到 close
    async def _connect(
        self, endpoint: str | StdioServerParameters, headers: dict[str, str] | None = None,
    ) -> None:
        if self._task is not None:
            raise RuntimeError("MCP client already started; close it before reconnecting")
        self._stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(endpoint, headers, ready))
        try:
            await asyncio.wait_for(ready, timeout=30)
        except BaseException:
            await self.close()
            raise

    # 在一个任务内管理 HTTP 客户端、MCP 会话与关闭，业务请求可由其他任务发起
    async def _run(
        self, endpoint: str | StdioServerParameters, headers: dict[str, str] | None,
        ready: asyncio.Future[None],
    ) -> None:
        try:
            async with AsyncExitStack() as stack:
                transport: Transport | StdioServerParameters
                if isinstance(endpoint, str):
                    http = await stack.enter_async_context(httpx2.AsyncClient(
                        headers=headers, timeout=httpx2.Timeout(30, read=300),
                    ))
                    transport = streamable_http_client(endpoint, http_client=http)
                else:
                    transport = endpoint
                client = await stack.enter_async_context(Client(
                    transport, client_info=Implementation(name="kama-claude", version="0.1"),
                    read_timeout_seconds=30, cache=None,
                ))
                self._client = client
                self._failure = ""
                if not ready.done():
                    ready.set_result(None)
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = _error_message(exc)
            if not ready.done():
                ready.set_exception(McpServerUnavailableError(self._failure))
            else:
                log.warning("mcp connection stopped: %s", self._failure)
        finally:
            self._client = None

    # 只返回已完成初始化且仍由后台任务持有的会话
    def _connected(self) -> Client:
        if self._client is None or self._stop.is_set():
            raise McpServerUnavailableError(self._failure or "MCP client closed")
        return self._client

    @property
    # 暴露协商结果，便于真实服务验证与排障
    def protocol_version(self) -> str:
        return self._connected().protocol_version

    # 获取全部工具页，防止远程服务分页时只注册第一批工具
    async def list_tools(self) -> list[McpToolDef]:
        client = self._connected()
        tools: list[McpToolDef] = []
        cursor: str | None = None
        seen: set[str] = set()
        try:
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(McpToolDef(
                    name=tool.name, description=tool.description or "",
                    input_schema=tool.input_schema,
                ) for tool in page.tools)
                cursor = page.next_cursor # 还有下一页就继续读取下一页的工具列表
                if not cursor:
                    return tools
                if cursor in seen:
                    raise McpServerUnavailableError("MCP tools/list returned a repeated cursor")
                seen.add(cursor)
        except MCPError as exc:
            if exc.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT):
                raise McpServerUnavailableError(str(exc)) from exc
            raise McpToolError(str(exc)) from exc
        except McpServerUnavailableError:
            raise
        except Exception as exc:
            raise McpServerUnavailableError(_error_message(exc)) from exc

    # 保留工具错误语义；纯结构化结果转换成 JSON 文本供原工具链使用
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        client = self._connected()
        try:
            result = await client.call_tool(name, arguments)
        except MCPError as exc:
            if exc.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT):
                raise McpServerUnavailableError(str(exc)) from exc
            raise McpToolError(str(exc)) from exc
        except Exception as exc:
            raise McpServerUnavailableError(_error_message(exc)) from exc
        text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
        if not text and result.structured_content is not None:
            text = json.dumps(result.structured_content, ensure_ascii=False)
        if result.is_error:
            raise McpToolError(text or f"MCP tool '{name}' returned isError=true")
        return text

    # 通知所有者任务退出，使 SDK 有机会结束 HTTP 会话或清理 stdio 子进程
    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            self._client = None
            self._task = None
            self._failure = "MCP client closed"
