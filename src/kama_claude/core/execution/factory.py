from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kama_claude.core.config.models import ExecutionConfig
from kama_claude.core.execution.docker import DockerCommandExecutor
from kama_claude.core.execution.model import CommandBackend
from kama_claude.core.execution.owner import SandboxOwner
from kama_claude.core.tools.command import CommandExecutor

if TYPE_CHECKING:
    from kama_claude.core.events.bus import EventBus


# 只选择并构造资源，不启动 Docker；调用方负责在 Run 退出时关闭。
def create_executor(
    workspace: Path, config: ExecutionConfig, *, run_id: str = "standalone",
    owner: SandboxOwner | None = None, bus: EventBus | None = None,
) -> CommandBackend:
    if config.backend == "host":
        return CommandExecutor(workspace, config)
    return DockerCommandExecutor(workspace, config, run_id=run_id, owner=owner, bus=bus)
