from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.session.store import SessionStore
from tests.integration.test_s8_recovery import Daemon, close, connect, wait_file


# 功能：验证真实 Core 硬重启后，TCP 查看能区分最新 Markdown 和原 Run 冻结记忆
# 设计：工具等待时结束进程，再修改文件并启动新进程，检查查看请求不改检查点或重执行工具
async def test_memory_inspection_over_tcp_after_core_restart(tmp_path: Path, free_port: int) -> None:
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    waiter = None
    try:
        sid = (await client.send_command("session.create", {"mode": "chat"}))["session_id"]
        memory = MemoryStore(tmp_path, tmp_path / "sessions" / sid)
        memory.upsert("session", "decision", summary="Decision", content="original memory", source="s / r")
        await client.send_command("session.open", {"session_id": sid})
        waiter = asyncio.create_task(client.send_command(
            "session.send_message", {"session_id": sid, "content": "controlled goal"},
        ))
        await wait_file(tmp_path / "b-started")
        original = SessionStore(tmp_path / "sessions").checkpoints.load(sid).run.memory_snapshot
        daemon.kill()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(waiter, 3)
        await close(client, receiver)
        memory.upsert("session", "decision", summary="Decision", content="changed memory", source="s / r")
        await daemon.start()
        client, receiver = await connect(free_port)
        path = tmp_path / "sessions" / sid / "checkpoint.json"
        before, effects = path.read_bytes(), (tmp_path / "effects.txt").read_bytes()
        result = await client.send_command("memory.inspect", {
            "session_id": sid, "scope": "session", "key": "decision",
        })
        assert result["entry"]["content"] == "changed memory"
        assert result["snapshot"] == original.model_dump(mode="json")
        assert "original memory" in result["snapshot"]["text"]
        assert path.read_bytes() == before and (tmp_path / "effects.txt").read_bytes() == effects
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await close(client, receiver)
        daemon.kill()
