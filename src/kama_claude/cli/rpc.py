from __future__ import annotations

import asyncio
from typing import Any

import kama_claude
from kama_claude.core.bus.commands import PongResult
from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import SocketClient


# 短控制请求独立连接并限时；业务 Run 不能使用这个固定超时。
async def request(config: KamaConfig, method: str, params: dict[str, Any]) -> dict[str, Any]:
    client = SocketClient(config.host, config.port)
    reader: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(5):
            await client.connect()
            reader = asyncio.create_task(client.run_event_loop())
            return await client.send_command(method, params)
    finally:
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await client.close()


async def ping(config: KamaConfig) -> PongResult:
    return PongResult.model_validate(await request(config, "core.ping", {
        "client": f"cli/{kama_claude.__version__}",
    }))
