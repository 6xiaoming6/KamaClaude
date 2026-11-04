from __future__ import annotations

import asyncio
import json
from typing import Any

from kama_claude.cli.commands.run import _run_async
from kama_claude.core.config import KamaConfig


# 功能：验证 CLI 忽略其他 Run 的终态，并把本次中断作为失败返回
# 设计：通过真实 TCP 在 agent.run 响应前推送事件，覆盖初始化缓冲和按 Run 过滤
async def test_cli_interrupted_run_is_not_confused_with_other_run(capsys: Any) -> None:
    # 以固定消息模拟 Core，不调用真实模型
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while line := await reader.readline():
            request = json.loads(line)
            if request["method"] == "event.subscribe":
                result = {}
            else:
                result = {"run_id": "target"}
                for event in [
                    {"type": "run.finished", "run_id": "other", "status": "success"},
                    {"type": "run.started", "run_id": "target"},
                    {"type": "run.interrupted", "run_id": "target", "reason": "core_restarted"},
                ]:
                    writer.write((json.dumps({"kind": "event", "event": event}) + "\n").encode())
            writer.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": result,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    config = KamaConfig(port=server.sockets[0].getsockname()[1])
    async with server:
        assert await asyncio.wait_for(_run_async("goal", config), 3) == 1
    captured = capsys.readouterr()
    assert "[run] target" in captured.out
    assert "interrupted" in captured.err
    assert "other" not in captured.out


# 功能：验证任务没有终态但 Core 断开时 CLI 能结束等待
# 设计：返回 Run ID 后立刻关闭 TCP，用超时断言排除 finished.wait 永久挂起
async def test_cli_disconnect_does_not_wait_forever(capsys: Any) -> None:
    # 完成订阅和接受任务后主动断开
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        for _ in range(2):
            request = json.loads(await reader.readline())
            writer.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {"run_id": "target"},
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    config = KamaConfig(port=server.sockets[0].getsockname()[1])
    async with server:
        assert await asyncio.wait_for(_run_async("goal", config), 3) == 1
    assert "disconnected" in capsys.readouterr().err
