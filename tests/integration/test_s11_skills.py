from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills.context import render_skill
from kama_claude.core.skills.loader import SkillLoader
from tests.integration.test_s8_recovery import Daemon, close, connect, wait_file, wait_status


@pytest.mark.parametrize("skill_name", ["review", "github-issue-fix"])
# 功能：验证已加载 Issue Skill 在 Core 重启后沿用快照，不依赖磁盘文件。
# 设计：显式隔离 HOME，在等待模型时杀掉真实 Core 并删除测试 Skill 后恢复。
async def test_skill_snapshot_survives_core_restart(
    tmp_path: Path, free_port: int, monkeypatch: pytest.MonkeyPatch, skill_name: str,
) -> None:
    test_home = tmp_path / "test-home"
    test_home.mkdir()
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.setenv("USERPROFILE", str(test_home))
    skills_root = test_home / ".kama" / "skills"
    loader = SkillLoader(skills_root)
    loader.ensure_examples()
    (tmp_path / "s9-mode").touch()
    (tmp_path / "release-b").touch()
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    waiter = None
    try:
        sid = (await client.send_command("session.create", {"mode": "chat"}))["session_id"]
        await client.send_command("session.open", {"session_id": sid})
        waiter = asyncio.create_task(client.send_command(
            "session.send_message", {"session_id": sid, "content": f"/{skill_name} src"},
        ))
        await wait_file(tmp_path / "s9-model-waiting")
        store = SessionStore(tmp_path / "sessions")
        before = store.checkpoints.load(sid)
        assert before.run.active_skill.name == skill_name and before.run.step == 1
        assert before.run.active_skill.body not in before.run.system_prompt
        daemon.kill()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(waiter, 3)
        await close(client, receiver)
        # 仅删除本测试创建的临时示例，证明恢复不会重新读取文件。
        temporary_skill = skills_root / skill_name / "SKILL.md"
        assert temporary_skill.resolve().is_relative_to(tmp_path.resolve())
        temporary_skill.unlink()
        (tmp_path / "s9-release-model").touch()
        await daemon.start()
        client, receiver = await connect(free_port)
        opened = await client.send_command("session.open", {"session_id": sid})
        assert opened["run"]["status"] == "interrupted"
        await client.send_command("session.resume", {
            "session_id": sid, "expected_revision": opened["revision"], "action": "continue",
        })
        await wait_status(client, sid, "success")
        after = store.checkpoints.load(sid)
        assert after.run.run_id == before.run.run_id and after.run.step == 2
        assert after.run.active_skill == before.run.active_skill
        assert after.run.skill_catalog == before.run.skill_catalog
        assert after.run.system_prompt == before.run.system_prompt
        assert not temporary_skill.exists()
        assert sum(m.get("content") == render_skill(after.run.active_skill) for m in after.messages) == 1
        history = store.read_history(sid)
        assert history[0] == {"role": "user", "content": f"/{skill_name} src"}
        assert not any(m.get("content") == render_skill(after.run.active_skill) for m in history)
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await close(client, receiver)
        daemon.kill()
