from __future__ import annotations

import asyncio
from pathlib import Path

import psutil

from kama_claude.cli.commands import core
from kama_claude.core.config import ExecutionConfig, KamaConfig


async def test_real_background_start_identity_and_graceful_stop(tmp_path: Path, free_port, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pid_file = tmp_path / "kama-core.pid"
    monkeypatch.setattr(core, "_PID_FILE", pid_file)
    monkeypatch.setenv("KAMA_LOG_FILE", "")
    monkeypatch.setenv("KAMA_TRACE_ENABLED", "false")
    monkeypatch.setenv("KAMA_EXECUTION_BACKEND", "host")
    monkeypatch.setenv("KAMA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    config = KamaConfig(port=free_port, execution=ExecutionConfig(backend="host"))
    info = None
    try:
        await asyncio.to_thread(core.cmd_core_start, config)
        info = core._load_info()
        assert info is not None
        pong = await core._ping_check(config)
        assert info.pid == pong.pid and info.process() is not None
        # 实际服务 PID 必须匹配记录；Windows 启动器可能是它的父进程。
        await asyncio.to_thread(core.cmd_core_stop, config)
        assert not pid_file.exists()
        assert not psutil.pid_exists(info.pid)
        assert (tmp_path / "logs/core-startup.log").exists()
    finally:
        if info is not None and psutil.pid_exists(info.pid):
            process = info.process()
            if process is not None:
                process.terminate()
                await asyncio.to_thread(process.wait, 3)
