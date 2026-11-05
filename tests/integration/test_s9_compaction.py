from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kama_claude.core.compact.budget import ARCHIVED_RESULT_MARKER
from kama_claude.core.compact.compactor import SUMMARY_MARKER
from kama_claude.core.session.store import SessionStore
from tests.integration.test_s8_recovery import Daemon, close, connect, wait_file, wait_status
from tests.unit.test_compactor import history
from tests.unit.test_layered_compaction import results, tool_history


@pytest.mark.parametrize("mode", ["llm", "programmatic"])
# 功能：验证真实 Core 在摘要或程序缩减提交后硬退出，TCP 恢复不重置 Step
# 设计：假模型在正式请求开始时等待文件信号，让测试精确结束进程并复用原磁盘状态
async def test_compaction_checkpoint_survives_real_process_restart(
    tmp_path: Path, free_port: int, mode: str,
) -> None:
    (tmp_path / ("s9-mode" if mode == "llm" else "s9-program-mode")).touch()
    (tmp_path / "release-b").touch()
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    waiter = None
    try:
        sid = (await client.send_command("session.create", {"mode": "chat"}))["session_id"]
        store = SessionStore(tmp_path / "sessions")
        original = history() if mode == "llm" else tool_history()
        for message in original:
            store.append_message(sid, message["role"], message["content"])
        await client.send_command("session.open", {"session_id": sid})
        waiter = asyncio.create_task(client.send_command(
            "session.send_message", {"session_id": sid, "content": "continue existing task"},
        ))
        await wait_file(tmp_path / "s9-model-waiting")
        saved = store.checkpoints.load(sid)
        if mode == "llm":
            assert saved.messages[0]["content"].startswith(SUMMARY_MARKER)
        else:
            assert any(b["content"].startswith(ARCHIVED_RESULT_MARKER) for b in results(saved.messages))
            assert not (tmp_path / "summary-calls.txt").exists()
        assert saved.run.step == 1 and saved.run.phase == "llm"
        full_history = store.read_history(sid)
        assert full_history == original + [{"role": "user", "content": "continue existing task"}]
        display_history = store.read_history(sid, include_metadata=True)
        daemon.kill()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(waiter, 3)
        await close(client, receiver)
        (tmp_path / "s9-release-model").touch()
        await daemon.start()
        client, receiver = await connect(free_port)
        opened = await client.send_command("session.open", {"session_id": sid})
        assert opened["run"]["status"] == "interrupted"
        assert opened["messages"] == display_history
        assert opened["messages"] != saved.messages
        await client.send_command("session.resume", {
            "session_id": sid, "expected_revision": opened["revision"], "action": "continue",
        })
        final = await wait_status(client, sid, "success")
        assert final["run"]["step"] == 2
        assert final["run"]["run_id"] == saved.run.run_id
        assert final["messages"][:len(display_history)] == display_history
        assert final["messages"] == store.read_history(sid, include_metadata=True)
        working = store.checkpoints.load(sid).messages
        if mode == "llm":
            assert working[0]["content"].startswith(SUMMARY_MARKER)
            assert (tmp_path / "summary-calls.txt").read_text(encoding="utf-8").splitlines() == ["summary"]
        else:
            assert any(b["content"].startswith(ARCHIVED_RESULT_MARKER) for b in results(working))
            assert not (tmp_path / "summary-calls.txt").exists()
        assert (tmp_path / "effects.txt").read_text(encoding="utf-8").splitlines() == ["a", "b"]
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await close(client, receiver)
        daemon.kill()
