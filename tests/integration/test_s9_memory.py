from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kama_claude.core.session.store import SessionStore
from kama_claude.core.transport.socket_client import IpcError
from tests.integration.test_s8_recovery import Daemon, close, connect, wait_file


# 功能：真实 Core 重启保留冻结规则，移除的 memory.inspect 返回方法不存在。
# 设计：工具副作用后强制结束进程，再改规则重启，分别核对快照和 RPC 零副作用。
async def test_frozen_rules_and_removed_rpc_after_restart(tmp_path: Path, free_port: int) -> None:
    rules = Path.home() / ".kama" / "AGENTS.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("ORIGINAL_GLOBAL_RULE", encoding="utf-8")
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    waiter = None
    try:
        sid = (await client.send_command("session.create", {"mode": "chat"}))["session_id"]
        await client.send_command("session.open", {"session_id": sid})
        waiter = asyncio.create_task(client.send_command(
            "session.send_message", {"session_id": sid, "content": "controlled goal"},
        ))
        await wait_file(tmp_path / "b-started")
        store = SessionStore(tmp_path / "sessions")
        original = store.checkpoints.load(sid).run.memory_snapshot
        assert "ORIGINAL_GLOBAL_RULE" in original.text
        daemon.kill()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(waiter, 3)
        await close(client, receiver)
        rules.write_text("CHANGED_GLOBAL_RULE", encoding="utf-8")
        await daemon.start()
        client, receiver = await connect(free_port)
        await client.send_command("session.open", {"session_id": sid})
        path = tmp_path / "sessions" / sid / "checkpoint.json"
        before, effects = path.read_bytes(), (tmp_path / "effects.txt").read_bytes()
        with pytest.raises(IpcError) as error:
            await client.send_command("memory.inspect", {"session_id": sid})
        assert error.value.code == -32601
        assert store.checkpoints.load(sid).run.memory_snapshot == original
        assert path.read_bytes() == before and (tmp_path / "effects.txt").read_bytes() == effects
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await close(client, receiver)
        daemon.kill()
