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

    proc = subprocess.Popen([sys.executable, "-m", "kama_claude.core"], env=env)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail("Daemon did not start within 3 seconds")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
