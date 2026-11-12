from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import suppress

from kama_claude.core.process_env import build_process_env
from kama_claude.core.tools.command import CommandResult

OUTPUT_LIMIT = 64 * 1024


class DockerCli:
    # Docker 连接配置只供宿主 CLI 使用，不会原样带进容器。
    def __init__(self) -> None:
        self.env = build_process_env()
        self.env.update({key: value for key, value in os.environ.items()
                         if key.startswith("DOCKER_")})

    # 参数数组直接启动 CLI；始终排空输出，只有固定大小的内容进入内存。
    async def call(self, *args: str, timeout: float = 30) -> CommandResult:
        proc: asyncio.subprocess.Process | None = None
        reader: asyncio.Task[None] | None = None
        launch: asyncio.Task[asyncio.subprocess.Process] | None = None
        captured = bytearray()
        try:
            async with asyncio.timeout(timeout):
                launch = asyncio.create_task(asyncio.create_subprocess_exec(
                    "docker", *args, env=self.env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                ))
                proc = await asyncio.shield(launch)
                assert proc.stdout is not None
                reader = asyncio.create_task(self._read(proc.stdout, captured))
                await asyncio.shield(reader)
                returncode = await proc.wait()
            return CommandResult(self._decode(captured), returncode)
        except (OSError, TimeoutError) as exc:
            return CommandResult(
                self._decode(captured), None,
                f"Docker CLI timeout after {timeout:g}s" if isinstance(exc, TimeoutError)
                else f"Docker CLI unavailable: {exc}",
                error_type="timeout" if isinstance(exc, TimeoutError) else "configuration_error",
            )
        finally:
            # 创建窗口发生取消时仍取回进程；这里只回收 CLI，容器由所属 Run 单独删除。
            if proc is None and launch is not None:
                with suppress(OSError):
                    proc = await asyncio.shield(launch)
            if proc is not None and proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), 2)
            if reader is not None:
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    # Docker 输出约定 UTF-8，截断后继续读取，防止管道填满造成假死。
    async def _read(self, stream: asyncio.StreamReader, captured: bytearray) -> None:
        while block := await stream.read(16 * 1024):
            captured.extend(block[: max(0, OUTPUT_LIMIT + 1 - len(captured))])

    # 只解码保留部分，不把截断误当成完整输出。
    @staticmethod
    def _decode(captured: bytearray) -> str:
        return captured[:OUTPUT_LIMIT].decode("utf-8", errors="replace") + (
            "\n[truncated]" if len(captured) > OUTPUT_LIMIT else ""
        )

    # 准备步骤没有执行用户命令，失败应按已知配置错误报告。
    async def checked(self, *args: str, timeout: float = 30) -> str:
        result = await self.call(*args, timeout=timeout)
        if result.error or result.returncode != 0:
            raise RuntimeError(result.error or result.output or "Docker command failed")
        return result.output.strip()

    # 只允许本机 Docker 的 Linux 引擎，避免挂载到远程主机的同名路径。
    async def check_local(self) -> None:
        endpoint = self.env.get("DOCKER_HOST") if not self.env.get("DOCKER_CONTEXT") else None
        if not endpoint:
            import json

            context = self.env.get("DOCKER_CONTEXT") or await self.checked("context", "show")
            endpoint = json.loads(await self.checked(
                "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}",
            ))
        if not isinstance(endpoint, str) or not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
            raise RuntimeError("Sandbox requires a local Docker daemon (unix socket or named pipe)")
        if await self.checked("info", "--format", "{{.OSType}}") != "linux":
            raise RuntimeError("Sandbox requires Docker Linux containers")
