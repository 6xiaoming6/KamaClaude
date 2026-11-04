from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys

import psutil
import pytest

from kama_claude.core.tools.command import CommandExecutor


# 功能：将测试 Python 代码转换成当前平台可执行的命令
# 设计：使用当前解释器与平台转义，不依赖 bash 或用户 PATH
def python_command(code: str) -> str:
    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


# 功能：验证超大输出被持续读取但只保留固定大小，并正确处理非零退出
# 设计：输出远超管道容量，能够结束说明截断后仍在排空管道
async def test_output_is_bounded_and_exit_code_preserved(tmp_path):
    result = await CommandExecutor(tmp_path).execute(
        python_command("import sys; print('x'*1000000); sys.exit(7)"), 10,
    )
    assert result.returncode == 7 and not result.outcome_unknown
    assert len(result.output) <= 65536 + 20
    assert result.output.endswith("[truncated]")


# 功能：验证命令产生副作用后超时仍返回未知，不能误称安全失败
# 设计：先创建文件再长时间等待，用文件证明副作用确实发生
async def test_timeout_keeps_unknown_side_effect(tmp_path):
    result = await CommandExecutor(tmp_path).execute(
        python_command("from pathlib import Path; import time; Path('effect').touch(); time.sleep(30)"),
        0.5,
    )
    assert (tmp_path / "effect").exists()
    assert result.outcome_unknown and result.error.startswith("timeout")


# 功能：验证取消会清理实际命令进程并继续传播 CancelledError
# 设计：命令写入自己的 PID，等待就绪后再取消，避免只取消启动前的协程
async def test_cancel_cleans_command_process(tmp_path):
    command = python_command(
        "from pathlib import Path; import os,time; Path('pid').write_text(str(os.getpid())); time.sleep(30)"
    )
    task = asyncio.create_task(CommandExecutor(tmp_path).execute(command, 60))
    for _ in range(200):
        if (tmp_path / "pid").exists():
            break
        await asyncio.sleep(0.02)
    assert (tmp_path / "pid").exists()
    process = psutil.Process(int((tmp_path / "pid").read_text()))
    try:
        # 给采集任务一次机会记录 shell 子进程
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not process.is_running()
    finally:
        if process.is_running():
            process.kill()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# 功能：验证取消发生在子进程已经创建、句柄尚未返回的窗口时也能清理
# 设计：人为延迟创建函数返回，用事件固定取消时机而不是依赖 sleep 猜测
async def test_cancel_during_process_creation(monkeypatch, tmp_path):
    original = asyncio.create_subprocess_shell
    created = asyncio.Event()
    release = asyncio.Event()
    processes = []

    # 进程已存在但执行器还没取得返回句柄
    async def delayed_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append(proc)
        created.set()
        await release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", delayed_spawn)
    task = asyncio.create_task(CommandExecutor(tmp_path).execute(
        python_command("import time; time.sleep(30)"), 60,
    ))
    await asyncio.wait_for(created.wait(), 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
