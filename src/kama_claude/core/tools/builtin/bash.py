from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult
from kama_claude.core.tools.command import CommandExecutor

_DEFAULT_TIMEOUT = 60
_SHELL = (
    f"Windows, using {os.environ.get('COMSPEC', 'cmd.exe')}"
    if os.name == "nt" else "POSIX, using /bin/sh"
)


class BashParams(ToolParams):
    command: str = Field(min_length=1, max_length=16_000)
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Each call starts in the fixed workspace; cd does not persist to the next call. "
        f"Current platform is {_SHELL}. "
        "Prefer short, focused commands. Output is truncated at 64 KiB."
    )
    input_schema = BashParams.model_json_schema()

    # 固定命令起始目录，这不是 shell 文件访问沙箱
    def __init__(
        self, workspace: Path | None = None, execution: ExecutionConfig | None = None,
    ) -> None:
        self.workspace = (workspace if workspace is not None else Path.cwd()).resolve()
        self._execution = execution

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        executor = CommandExecutor(self.workspace, self._execution)
        result = await executor.execute(p.command, p.timeout)
        if result.error:
            return ToolResult(
                content=result.error, is_error=True,
                error_type="timeout" if result.error.startswith("timeout") else "runtime_error",
                outcome_unknown=result.outcome_unknown,
            )
        if result.returncode:
            return ToolResult(
                content=f"[exit {result.returncode}]\n{result.output}",
                is_error=True, error_type="command_failed",
            )
        return ToolResult(content=result.output)
