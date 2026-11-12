from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from kama_claude.core.config.models import ExecutionConfig
from kama_claude.core.execution.cli import DockerCli
from kama_claude.core.execution.owner import SandboxOwner
from kama_claude.core.tools.command import CommandResult

if TYPE_CHECKING:
    from kama_claude.core.events.bus import EventBus

log = logging.getLogger(__name__)
_RESERVED = {
    "PATH", "HOME", "USERPROFILE", "VIRTUAL_ENV", "COMSPEC", "PYTHONUSERBASE",
    "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
}


# --mount 使用 CSV 字段编码，Windows 路径中的空格、逗号不会变成额外挂载参数。
def bind_mount(source: Path, target: str, *, readonly: bool = False) -> str:
    output = io.StringIO()
    fields = ["type=bind", f"source={source}", f"target={target}"]
    if readonly:
        fields.append("readonly")
    csv.writer(output, lineterminator="").writerow(fields)
    return output.getvalue()


# 容器只接收显式名单中的值，宿主系统变量及模型密钥不会随 CLI 环境泄漏。
def container_env(config: ExecutionConfig) -> dict[str, str]:
    forwarded = {key.upper() for key in config.forward_env}
    values = {key: value for key, value in os.environ.items() if key.upper() in forwarded}
    values.update(config.env)
    return {key: value for key, value in values.items()
            if key.upper() not in _RESERVED and not key.upper().startswith(("DOCKER_", "KAMA_"))}


class DockerCommandExecutor:
    platform = "posix"

    # 一个实例只属于一次 Run 执行尝试；锁保护懒创建及逐命令执行。
    def __init__(
        self, workspace: Path, config: ExecutionConfig, *, run_id: str = "standalone",
        owner: SandboxOwner | None = None, bus: EventBus | None = None,
        cli: DockerCli | None = None,
    ) -> None:
        self.workspace, self.config = workspace.resolve(), config
        self.run_id, self.owner, self.bus = run_id, owner or SandboxOwner(), bus
        self.cli = cli or DockerCli()
        self.attempt = uuid4().hex
        self.name = f"kamaclaude-{self.attempt}"
        self._container = False
        self._ready = False
        self._closed = False
        self._cleanup_done = False
        self._cleanup_ok = True
        self._mask: tempfile.TemporaryDirectory[str] | None = None
        self._lock = asyncio.Lock()

    # 审批和 executing 已提交后才会进入此处，不提前创建容器或运行用户命令。
    async def execute(self, command: str, timeout: float) -> CommandResult:
        async with self._lock:
            if self._closed:
                return CommandResult("", None, "Run sandbox is closed; start a new Run",
                                     error_type="configuration_error")
            submitted = False
            try:
                if not self._ready:
                    async with asyncio.timeout(30):
                        await self._prepare()
                env_args = [item for key, value in container_env(self.config).items()
                            for item in ("--env", f"{key}={value}")]
                submitted = True
                result = await self.cli.call(
                    "exec", "--workdir", "/workspace", *env_args, self.name,
                    "/bin/bash", "--noprofile", "--norc", "-c", command, timeout=timeout,
                )
                if not result.error and result.returncode:
                    # Docker 失联和容器 OOM 退出不能冒充普通命令的已知非零结果。
                    state = await self.cli.call(
                        "inspect", "--format", "{{.State.Running}}", self.name, timeout=5,
                    )
                    if state.error or state.returncode or state.output.strip() != "true":
                        result.error = "Sandbox stopped or Docker connection was lost"
                        result.error_type = "runtime_error"
                if result.error:
                    result.outcome_unknown = True
                    await self._protected_close()
                return result
            except asyncio.CancelledError:
                await self._protected_close()
                raise
            except (OSError, RuntimeError, TimeoutError) as exc:
                # 这里只处理准备失败；用户命令尚未发出，不能标记未知副作用。
                await self._protected_close()
                return CommandResult(
                    "", None,
                    f"Sandbox {'execution' if submitted else 'preparation'} failed: {exc}",
                    outcome_unknown=submitted,
                    error_type="runtime_error" if submitted else "configuration_error",
                )

    # 只读检查引擎和镜像后启动无特权容器，禁止隐式下载或回退本机。
    async def _prepare(self) -> None:
        await self.cli.check_local()
        image = await self.cli.checked("image", "inspect", "--format", "{{.Id}}",
                                       self.config.docker.image)
        if not self.workspace.is_dir():
            raise RuntimeError("sandbox workspace must be an existing directory")
        host_uid = getattr(os, "getuid", lambda: 0)()
        uid = host_uid or 1000
        gid = getattr(os, "getgid", lambda: 1000)() if host_uid else 1000
        docker = self.config.docker
        args = [
            "create", "--name", self.name, "--pull", "never", "--init",
            "--network", "bridge", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", f"{uid}:{gid}",
            "--cpus", str(docker.cpus), "--memory", f"{docker.memory_mb}m",
            "--memory-swap", f"{docker.memory_mb}m", "--pids-limit", str(docker.pids_limit),
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs", f"/home/kama:rw,nosuid,nodev,size=512m,uid={uid},gid={gid},mode=700",
            "--env", "HOME=/home/kama", "--env", "PYTHONUSERBASE=/home/kama/.local",
            "--env", "NPM_CONFIG_PREFIX=/home/kama/.npm-global",
            "--workdir", "/workspace", "--mount", bind_mount(self.workspace, "/workspace"),
        ]
        for key, value in self.owner.labels(self.run_id, self.attempt).items():
            args += ["--label", f"{key}={value}"]
        for path in sorted(self.workspace.iterdir()):
            name = os.path.normcase(path.name)
            if name == ".env.example" or not (
                name == ".env" or name.startswith(".env.")
            ):
                continue
            if path.is_symlink() or path.is_junction() or not path.is_file():
                raise RuntimeError(f"cannot safely mask sandbox credential path: {path.name}")
            if self._mask is None:
                self._mask = tempfile.TemporaryDirectory(prefix="kamaclaude-mask-")
            empty = Path(self._mask.name) / "empty"
            empty.touch(exist_ok=True)
            empty.chmod(0o444)
            args += ["--mount", bind_mount(empty, f"/workspace/{path.name}", readonly=True)]
        # 预先登记名称，create/start 期间取消或失联也能按唯一名称回收。
        self._container = True
        await self.cli.checked(*args, "--entrypoint", "/bin/sleep", image, "infinity")
        await self.cli.checked("start", self.name)
        self._ready = True
        log.info("sandbox started run_id=%s container=%s image=%s", self.run_id, self.name, image)

    # 外部取消不能跳过容器清理，但清理自身有独立的五秒期限。
    async def _protected_close(self) -> bool:
        task = asyncio.create_task(self._close())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            return await asyncio.shield(task)

    # Runner 在 finally 调用，重复关闭不重复删除，也不执行任何业务命令。
    async def aclose(self) -> bool:
        async with self._lock:
            return await self._protected_close()

    # 清理失败保留诊断信息，不能把已提交的业务成功改成需要重跑。
    async def _close(self) -> bool:
        self._closed = True
        if self._cleanup_done:
            return self._cleanup_ok
        self._cleanup_done = True
        if self._container:
            try:
                result = await self.cli.call("rm", "-f", self.name, timeout=5)
                self._cleanup_ok = not result.error and (
                    result.returncode == 0 or "No such container" in result.output
                )
            except (OSError, RuntimeError):
                self._cleanup_ok = False
        if self._cleanup_ok and self._mask is not None:
            # 只清理 TemporaryDirectory 自己创建的空文件目录，不删除工作区。
            try:
                self._mask.cleanup()
                self._mask = None
            except OSError:
                self._cleanup_ok = False
        if not self._cleanup_ok:
            message = f"Sandbox cleanup incomplete: {self.name}; no business operation was retried"
            log.error(message)
            if self.bus is not None:
                from datetime import UTC, datetime

                from kama_claude.core.bus.events import LogLineEvent

                await self.bus.publish_safe(LogLineEvent(
                    run_id=self.run_id, level="ERROR", source=__name__, message=message,
                    ts=datetime.now(UTC).isoformat(),
                ))
        return self._cleanup_ok
