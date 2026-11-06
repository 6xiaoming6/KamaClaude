from __future__ import annotations

import logging
from pathlib import Path

from kama_claude.core.config import McpServerConfig, ToolSearchConfig
from kama_claude.core.discovery.model import CatalogTool, server_identity
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.discovery.store import CatalogError, CatalogStore
from kama_claude.core.mcp.client import McpClient
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


# 管理所有 MCP server 连接的生命周期：启动、工具发现、注册、关闭
class McpServerManager:
    # 只缓存成功连接及其工具，不保留启动失败的半成品
    def __init__(self, config: ToolSearchConfig | None = None) -> None:
        self._clients: dict[str, McpClient] = {}
        self._tools: list[McpTool] = []
        self.config = config or ToolSearchConfig()
        self.catalog = CatalogStore(Path(self.config.db_path))
        self.retriever = ToolRetriever(self.catalog, self.config)

    # 依次连接每个 MCP server，发现工具后缓存供后续 registry 使用；失败时记录日志并跳过
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        for cfg in servers:
            client: McpClient | None = None
            try:
                # 重连先撤销旧连接；新列表完整提交之前不暴露半份工具。
                old = self._clients.pop(cfg.name, None)
                self._tools = [tool for tool in self._tools if tool.server_name != cfg.name]
                if old is not None:
                    await old.close()
                client = await self._connect(cfg)
                tool_defs = await client.list_tools()
                identity = server_identity(cfg, str(Path.cwd().resolve()))
                records = [CatalogTool.from_definition(identity, cfg.name, tool.definition())
                           for tool in tool_defs]
                existing = {tool.name for tool in self._tools}
                if any(record.public_name in existing for record in records):
                    raise CatalogError("MCP tool public names collide across servers")
                changed = self.catalog.sync(identity, records)
                self._tools.extend(
                    McpTool(client, cfg.name, definition, record=record, catalog=self.catalog)
                    for definition, record in zip(tool_defs, records, strict=True)
                )
                self._clients[cfg.name] = client
                log.info(
                    "mcp: server '%s' connected, %d tool(s) discovered, %d catalog changes",
                    cfg.name, len(tool_defs), changed,
                )
            except BaseException as exc:
                if client is not None:
                    await client.close()
                if not isinstance(exc, Exception):
                    raise
                log.exception("mcp: server '%s' failed to start, skipping", cfg.name)

    # 将所有已发现的 MCP 工具注册到指定 registry
    def register_tools(self, registry: ToolRegistry) -> None:
        for tool in self._tools:
            registry.register(tool)

    # 返回已发现的 MCP 工具列表（用于 runner 每次 run 时注入新 registry）
    def get_tools(self) -> list[McpTool]:
        return list(self._tools)

    # 关闭所有 MCP 连接并终止 stdio 子进程
    async def stop_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                await client.close()
                log.info("mcp: server '%s' closed", name)
            except Exception:
                log.warning("mcp: error closing server '%s'", name)
        self._clients.clear()
        self._tools.clear()

    # 根据 transport 类型建立连接
    async def _connect(self, cfg: McpServerConfig) -> McpClient:
        client = McpClient()
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError(f"mcp server '{cfg.name}': stdio transport requires 'command'")
            await client.connect_stdio(
                cfg.command, cfg.args, cfg.env or None, forward_env=cfg.forward_env,
            )
        elif cfg.transport == "streamable-http":
            await client.connect_http(cfg.url, cfg.headers or None)
        else:
            raise ValueError(f"mcp server '{cfg.name}': unknown transport '{cfg.transport}'")
        return client
