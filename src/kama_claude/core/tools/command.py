from __future__ import annotations

import asyncio
import locale
import logging
import os
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import psutil

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.process_env import build_process_env

log = logging.getLogger(__name__)
_OUTPUT_LIMIT = 64 * 1024


@dataclass
class CommandResult:
    output: str
    returncode: int | None
    error: str = ""
    outcome_unknown: bool = False


class CommandExecutor:
    # 绑定执行目录，进程树只在本次调用内管理，不创建后台守护进程
    def __init__(self, cwd: Path | None = None, execution: ExecutionConfig | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self._execution = execution or ExecutionConfig()

    # 逐块排空管道，只保留固定大小的字节缓冲，避免大输出耗尽内存
    async def _read_output(
        self, stream: asyncio.StreamReader, kept: bytearray | None = None,
    ) -> str:
        kept = kept if kept is not None else bytearray()
        while chunk := await stream.read(16 * 1024):
            # 多保留一个字节作为截断标志；异常时调用方也能取得已经读到的输出。
            room = _OUTPUT_LIMIT + 1 - len(kept)
            kept.extend(chunk[:room])
        return self._decode_output(kept)

    @staticmethod
    def _decode_output(kept: bytearray) -> str:
        truncated = len(kept) > _OUTPUT_LIMIT
        raw = kept[:_OUTPUT_LIMIT]
        try:
            output = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            partial_utf8 = truncated and exc.reason == "unexpected end of data"
            encoding = locale.getencoding() if os.name == "nt" and not partial_utf8 else "utf-8"
            output = raw.decode(encoding, errors="replace")
        return output + ("\n[truncated]" if truncated else "")

    # 运行中采集子进程身份，根 shell 先退出时仍能清理已经发现的后代
    async def _watch_children(
        self, root: psutil.Process, children: dict[int, psutil.Process],
    ) -> None:
        while True:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                for child in root.children(recursive=True):
                    children[child.pid] = child
            await asyncio.sleep(0.05)

    # 有界地终止受管进程，psutil 对象包含创建时间以防止 PID 复用误杀
    async def _cleanup(
        self, proc: asyncio.subprocess.Process, root: psutil.Process | None,
        children: dict[int, psutil.Process],
    ) -> bool:
        processes = list(children.values())
        ok = True
        if root is not None:
            try:
                processes.extend(root.children(recursive=True))
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                ok = False
                log.warning("cannot enumerate command children pid=%s", proc.pid)
            processes.append(root)
        if os.name != "nt":
            try:
                getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
            except OSError:
                ok = False
        for process in reversed(processes):
            try:
                if process.is_running():
                    process.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                ok = False
        try:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2)
            _, alive = await asyncio.to_thread(psutil.wait_procs, processes, timeout=1)
            ok = ok and not alive
        except (TimeoutError, ProcessLookupError):
            ok = False
        if not ok:
            log.error("command process cleanup incomplete pid=%s", proc.pid)
        return ok

    # 执行命令并统一处理超时与取消，已启动后的异常不能证明没有副作用
    async def execute(self, command: str, timeout: float) -> CommandResult:
        proc: asyncio.subprocess.Process | None = None
        root: psutil.Process | None = None
        children: dict[int, psutil.Process] = {}
        watcher: asyncio.Task[None] | None = None
        reader: asyncio.Task[str] | None = None
        launch: asyncio.Task[asyncio.subprocess.Process] | None = None
        captured = bytearray()
        try:
            # 启动也受保护，取消发生在创建窗口时仍能取回句柄并清理
            launch = asyncio.create_task(asyncio.create_subprocess_shell(
                command, cwd=self.cwd, stdin=asyncio.subprocess.DEVNULL,
                env=build_process_env(
                    forward_env=self._execution.forward_env, env=self._execution.env,
                ),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ))
            proc = await asyncio.shield(launch)
            with suppress(psutil.NoSuchProcess):
                root = psutil.Process(proc.pid)
            if root is not None:
                watcher = asyncio.create_task(self._watch_children(root, children))
            assert proc.stdout is not None
            reader = asyncio.create_task(self._read_output(proc.stdout, captured))
            async with asyncio.timeout(timeout):
                output = await asyncio.shield(reader)
                returncode = await proc.wait()
            return CommandResult(output or "[no output]", returncode)
        except asyncio.CancelledError:
            if proc is None and launch is not None:
                with suppress(OSError):
                    proc = await asyncio.shield(launch)
            if proc is not None:
                if root is None:
                    with suppress(psutil.NoSuchProcess):
                        root = psutil.Process(proc.pid)
                # 调用方取消不能打断清理；取消仍在清理完成后向上传递
                cleanup = asyncio.create_task(self._cleanup(proc, root, children))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup)
            raise
        except Exception as exc:
            cleaned = await self._cleanup(proc, root, children) if proc is not None else True
            reason = f"timeout after {timeout:g}s" if isinstance(exc, TimeoutError) else str(exc)
            if not cleaned:
                reason += "; process cleanup incomplete"
            return CommandResult(
                self._decode_output(captured), None, reason, outcome_unknown=proc is not None,
            )
        finally:
            for task in (watcher, reader):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (watcher, reader) if task is not None),
                return_exceptions=True,
            )
