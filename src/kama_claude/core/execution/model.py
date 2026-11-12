from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.config.models import DockerConfig, ExecutionConfig

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
    from kama_claude.core.tools.command import CommandResult


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["host", "docker"]
    docker: DockerConfig = Field(default_factory=DockerConfig)


class CommandBackend(Protocol):
    platform: str

    # 两种后端返回相同结果，不由 BashTool 推测副作用是否发生。
    async def execute(self, command: str, timeout: float) -> CommandResult: ...

    # Run 收尾统一关闭；本机后端没有需要跨命令保留的资源。
    async def aclose(self) -> bool: ...


# 只冻结后端及资源，不把密钥、环境变量和容器运行句柄写进检查点。
def snapshot_execution(config: ExecutionConfig) -> ExecutionSnapshot:
    return ExecutionSnapshot.model_validate({
        "backend": config.backend, "docker": replace(config.docker),
    })


# 老检查点原来在宿主机执行，不能因升级默认值而静默切换环境。
def execution_for_run(run: RunCheckpoint, config: ExecutionConfig) -> ExecutionConfig:
    snapshot = run.execution
    return replace(
        config, backend=snapshot.backend if snapshot is not None else "host",
        docker=replace(snapshot.docker if snapshot is not None else config.docker),
    )
