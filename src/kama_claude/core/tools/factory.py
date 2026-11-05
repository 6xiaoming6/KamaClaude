from __future__ import annotations

from pathlib import Path

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import BaseTool
from kama_claude.core.tools.builtin import (
    BashTool,
    EditFileTool,
    FindFilesTool,
    ListDirTool,
    ReadFileTool,
    SearchTextTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)


# 这里只列出共同工具；记忆、MCP、子任务及白名单仍由各自装配处明确处理。
def builtin_tools(
    workspace: Path, execution: ExecutionConfig, tasks: TaskManager,
) -> list[BaseTool]:
    return [
        ReadFileTool(workspace), BashTool(workspace, execution),
        WriteFileTool(workspace), ListDirTool(workspace),
        FindFilesTool(workspace), SearchTextTool(workspace), EditFileTool(workspace),
        TaskCreateTool(tasks), TaskUpdateTool(tasks), TaskListTool(tasks), TaskGetTool(tasks),
    ]
