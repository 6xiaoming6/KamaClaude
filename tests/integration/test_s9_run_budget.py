from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from tests.integration.test_s8_recovery import Daemon, close, connect, wait_file


# 功能：验证真实 Core 硬退出后，未取得 Usage 的请求预留仍留在检查点
# 设计：复用假模型 TCP 守护进程，在工具副作用发生后强杀并重新打开会话
async def test_unknown_usage_reservation_survives_core_restart(tmp_path: Path, free_port: int):
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    waiter = None
    try:
        created = await client.send_command("session.create", {"mode": "chat"})
        sid = created["session_id"]
        waiter = asyncio.create_task(client.send_command(
            "session.send_message", {"session_id": sid, "content": "goal"},
        ))
        await wait_file(tmp_path / "b-started")
        daemon.kill()
        with pytest.raises(ConnectionError):
            await waiter
        await close(client, receiver)
        await daemon.start()
        client, receiver = await connect(free_port)
        opened = await client.send_command("session.open", {"session_id": sid})
        run = RunCheckpoint.model_validate(opened["run"])
        assert run.estimated_tokens > 0 and not run.usage_complete
        assert run.status == "interrupted" and run.step == 1
        # 恢复出的累计值直接参与下一次预算检查，不重新获得额度
        run.max_total_tokens = run.estimated_tokens
        with pytest.raises(BudgetExceeded):
            RunBudget(run).reserve(1)
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await close(client, receiver)
        daemon.kill()
