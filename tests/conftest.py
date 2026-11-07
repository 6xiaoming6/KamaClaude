from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
# 每项测试绑定独立用户目录，记忆和子进程测试都不能读写真实用户数据。
def isolated_user_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path_factory.mktemp("user-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


@pytest.fixture
async def running_daemon(free_port: int, tmp_path: Path) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    env = os.environ.copy()
    env["KAMA_PORT"] = str(free_port)
    env["KAMA_SESSIONS_DIR"] = str(tmp_path / "sessions")
    env["KAMA_LOG_FILE"] = ""
    env["KAMA_LOG_LEVEL"] = "WARNING"
    env["DEEPSEEK_API_KEY"] = ""
    env["ANTHROPIC_API_KEY"] = ""

    stderr_path = tmp_path / "daemon.stderr.log"
    with stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-m", "kama_claude.core"], env=env, stderr=stderr,
        )
        try:
            await _wait_for_daemon(proc, free_port, stderr_path)
            yield proc
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def _daemon_diagnostics(proc: subprocess.Popen[bytes], stderr_path: Path) -> str:
    # 只读取日志尾部，启动失败也能看到退出状态，不用无限积累 PIPE 输出。
    with stderr_path.open("rb") as stderr:
        size = stderr.seek(0, 2)
        stderr.seek(max(0, size - 8_192))
        tail = stderr.read().decode("utf-8", errors="replace")
    return f"pid={proc.pid}, exit_code={proc.poll()}\nstderr tail:\n{tail}"


async def _wait_for_daemon(
    proc: subprocess.Popen[bytes], port: int, stderr_path: Path, timeout_s: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("Daemon exited before listening: " + _daemon_diagnostics(proc, stderr_path))
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except (ConnectionRefusedError, OSError):
            pass
    pytest.fail(f"Daemon did not start within {timeout_s:g} seconds: "
                + _daemon_diagnostics(proc, stderr_path))
