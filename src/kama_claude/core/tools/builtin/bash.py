from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.execution.factory import create_executor
from kama_claude.core.execution.model import CommandBackend
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult

_DEFAULT_TIMEOUT = 60
_SHELL = (
    f"Windows, using {os.environ.get('COMSPEC', 'cmd.exe')}"
    if os.name == "nt" else "POSIX, using /bin/sh"
)


class BashParams(ToolParams):
    command: str = Field(min_length=1, max_length=16_000)
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


class BashTool(BaseTool):
    timeout_s: ClassVar[float | None] = None  # 准备、命令和清理分别有界，仍受 Run 总预算约束。
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Each call starts in the fixed workspace; cd does not persist to the next call. "
        "Use dedicated file/search/edit tools when they support the operation; use this tool for "
        "programs, tests and operations without a dedicated tool. Prefer short, finite commands. "
        "Timeout is 1-120 seconds (default 60); output is truncated at 64 KiB. "
        "Do not launch persistent servers: inherited output pipes can keep the call open. "
    )
    input_schema = BashParams.model_json_schema()

    # 固定命令起始目录，这不是 shell 文件访问沙箱
    def __init__(
        self, workspace: Path | None = None, execution: ExecutionConfig | None = None,
        *, executor: CommandBackend | None = None,
    ) -> None:
        self.workspace = (workspace if workspace is not None else Path.cwd()).resolve()
        self._execution = execution or ExecutionConfig()
        self._executor = executor or create_executor(self.workspace, self._execution)
        self.platform = self._executor.platform
        self.execution_context = (
            f"Docker Linux Bash; cwd=/workspace; host workspace={self.workspace}"
            if self._execution.backend == "docker" else f"Host {_SHELL}; cwd={self.workspace}"
        )
        self.description = type(self).description + self.execution_context + ". " + (
            "Use Linux paths, never Windows cd /d. Writable user dependencies persist within "
            "this Run's container, but cd and exports do not. Network is enabled. "
            "Root filesystem is read-only; /workspace, /tmp and /home/kama are writable."
            if self._execution.backend == "docker" else
            "Windows cmd does not support POSIX heredocs or the interactive timeout command."
            if os.name == "nt" else "Use /bin/sh-compatible syntax."
        )

    # 独立使用工具时由调用方关闭；Runner 注入的执行器统一由 Run 收尾关闭。
    async def aclose(self) -> bool:
        return await self._executor.aclose()

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        result = await self._executor.execute(p.command, p.timeout)
        if result.error:
            content = result.error
            if result.output:
                content += f"\nCaptured output:\n{result.output}"
            return ToolResult(
                content=content,
                is_error=True,
                error_type=result.error_type or (
                    "timeout" if result.error.startswith("timeout") else "runtime_error"
                ),
                outcome_unknown=result.outcome_unknown,
            )
        if result.returncode:
            return ToolResult(
                content=f"[exit {result.returncode}]\n{result.output}",
                is_error=True, error_type="command_failed",
            )
        return ToolResult(content=result.output)
