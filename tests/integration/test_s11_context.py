from __future__ import annotations

from pathlib import Path

from kama_claude.core.session.store import SessionStore
from tests.integration.test_s8_recovery import Daemon, close, connect, wait_status


# 真实 Core/TCP 重启：归档失败发生在完成事实之后，恢复不再执行第一个工具。
async def test_completed_output_preparation_recovers_after_core_restart(
    tmp_path: Path, free_port: int,
) -> None:
    (tmp_path / "long-output").touch()
    (tmp_path / "archive-fail").touch()
    (tmp_path / "release-b").touch()
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    store = SessionStore(tmp_path / "sessions")
    try:
        sid = (await client.send_command("session.create", {"mode": "chat"}))["session_id"]
        await client.send_command("session.open", {"session_id": sid})
        await client.send_command("session.send_message", {"session_id": sid, "content": "goal"})
        await wait_status(client, sid, "interrupted")
        before = store.checkpoints.load(sid)
        assert before.run.phase == "tools" and before.run.tool_index == 1
        assert before.run.tools[0].status == "completed"
        assert not before.run.tools[0].output_prepared
        assert before.run.tools[0].result["content"] == "HEAD" + "x" * 10_000 + "TAIL"
        assert (tmp_path / "effects.txt").read_text(encoding="utf-8").splitlines() == ["a"]
        daemon.kill()
        await close(client, receiver)
        (tmp_path / "archive-fail").unlink()
        await daemon.start()
        client, receiver = await connect(free_port)
        opened = await client.send_command("session.open", {"session_id": sid})
        await client.send_command("session.resume", {
            "session_id": sid, "expected_revision": opened["revision"], "action": "continue",
        })
        final = await wait_status(client, sid, "success")
        assert final["run"]["run_id"] == before.run.run_id and final["run"]["step"] == 2
        assert (tmp_path / "effects.txt").read_text(encoding="utf-8").splitlines() == ["a", "b"]
        working = store.checkpoints.load(sid).messages
        work_result = next(block for message in working if isinstance(message["content"], list)
                           for block in message["content"] if block.get("type") == "tool_result"
                           and block.get("tool_use_id") == "tool-a")
        assert "chars omitted" in work_result["content"]
        history = store.read_history(sid)
        full_result = next(block for message in history if isinstance(message["content"], list)
                           for block in message["content"] if block.get("type") == "tool_result"
                           and block.get("tool_use_id") == "tool-a")
        assert full_result["content"] == "HEAD" + "x" * 10_000 + "TAIL"
        archive = next(store.runs_dir(sid).glob("*/outputs/*.txt"))
        assert archive.read_text(encoding="utf-8") == full_result["content"]
        assert final["messages"] == store.read_history(sid, include_metadata=True)
    finally:
        await close(client, receiver)
        daemon.kill()
