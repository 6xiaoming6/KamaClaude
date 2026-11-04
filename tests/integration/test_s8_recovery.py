from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.transport.socket_client import IpcError, SocketClient


class Daemon:
    # 记录只属于本测试的子进程与临时路径
    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.proc: subprocess.Popen[bytes] | None = None

    # 启动假模型驱动的真实 TCP 守护进程
    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "ready.json").unlink(missing_ok=True)
        with (self.root / "daemon.log").open("ab") as log:
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    "-B",
                    str(Path(__file__).with_name("recovery_daemon.py")),
                    str(self.root),
                    str(self.port),
                ],
                stdout=log,
                stderr=log,
            )
        for _ in range(300):
            if self.proc.poll() is not None:
                pytest.fail((self.root / "daemon.log").read_text(encoding="utf-8"))
            if (self.root / "ready.json").exists():
                return
            await asyncio.sleep(0.02)
        pytest.fail("test daemon startup timed out")

    # 只结束本测试创建的进程，模拟没有 Python finally 的硬退出
    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


# 创建真实客户端与单一读循环
async def connect(port: int) -> tuple[SocketClient, asyncio.Task[None]]:
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    return client, asyncio.create_task(client.run_event_loop())


# 回收客户端与读取任务，不把故意断线作为未处理异常
async def close(client: SocketClient, receiver: asyncio.Task[None]) -> None:
    with suppress(OSError):
        await client.close()
    await asyncio.wait_for(receiver, timeout=3)


# 等待受控文件副作用到达，不用固定 sleep 推测工具阶段
async def wait_file(path: Path) -> None:
    for _ in range(300):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"expected file not created: {path}")


# 持续查询权威状态，直到 Run 进入预期状态
async def wait_status(client: SocketClient, sid: str, status: str) -> dict[str, Any]:
    for _ in range(200):
        state = await client.send_command("session.open", {"session_id": sid})
        if state["run"] and state["run"]["status"] == status:
            return state
        await asyncio.sleep(0.02)
    pytest.fail(f"run did not reach {status}")


# 功能：验证真实进程硬退出后工具结果未知，确认重试只影响当前工具
# 设计：使用文件副作用和真实 TCP，强制结束 Core 后复用磁盘检查点恢复
async def test_hard_restart_requires_confirmation(tmp_path: Path, free_port: int) -> None:
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    waiter: asyncio.Task[dict[str, Any]] | None = None
    try:
        created = await client.send_command("session.create", {"mode": "chat"})
        sid = created["session_id"]
        await client.send_command("session.open", {"session_id": sid})
        waiter = asyncio.create_task(
            client.send_command(
                "session.send_message",
                {"session_id": sid, "content": "goal"},
            )
        )
        await wait_file(tmp_path / "b-started")
        original = json.loads(
            (tmp_path / "sessions" / sid / "checkpoint.json").read_text(encoding="utf-8")
        )
        daemon.kill()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(waiter, 3)
        await close(client, receiver)
        await daemon.start()
        client, receiver = await connect(free_port)
        opened = await client.send_command("session.open", {"session_id": sid})
        assert opened["run"]["tools"][1]["status"] == "unknown"
        with pytest.raises(IpcError, match="outcome unknown"):
            await client.send_command(
                "session.resume",
                {
                    "session_id": sid,
                    "expected_revision": opened["revision"],
                    "action": "continue",
                },
            )
        (tmp_path / "release-b").touch()
        await client.send_command(
            "session.resume",
            {
                "session_id": sid,
                "expected_revision": opened["revision"],
                "action": "retry_tool",
            },
        )
        final = await wait_status(client, sid, "success")
        assert final["run"]["run_id"] == original["run"]["run_id"]
        assert final["run"]["step"] == 2
        effects = (tmp_path / "effects.txt").read_text(encoding="utf-8").splitlines()
        assert effects.count("a") == 1
        assert effects.count("b") == 2
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await close(client, receiver)
        daemon.kill()


# 功能：验证 TCP 客户端断开不取消 Core 任务，重新打开会话不重复执行
# 设计：第二个客户端在同一 Core 上查看存活任务，然后释放受控工具完成执行
async def test_client_reconnect_attaches_to_existing_run(tmp_path: Path, free_port: int) -> None:
    daemon = Daemon(tmp_path, free_port)
    await daemon.start()
    client, receiver = await connect(free_port)
    waiter: asyncio.Task[dict[str, Any]] | None = None
    try:
        sid = (await client.send_command("session.create", {"mode": "chat"}))["session_id"]
        await client.send_command("session.open", {"session_id": sid})
        waiter = asyncio.create_task(
            client.send_command(
                "session.send_message",
                {"session_id": sid, "content": "goal"},
            )
        )
        await wait_file(tmp_path / "b-started")
        await close(client, receiver)
        with pytest.raises(ConnectionError):
            await waiter
        client, receiver = await connect(free_port)
        opened = await client.send_command("session.open", {"session_id": sid})
        assert opened["running"] is True
        assert opened["run"]["tools"][1]["status"] == "executing"
        (tmp_path / "release-b").touch()
        await wait_status(client, sid, "success")
        assert (tmp_path / "effects.txt").read_text(encoding="utf-8").splitlines() == ["a", "b"]
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await close(client, receiver)
        daemon.kill()
