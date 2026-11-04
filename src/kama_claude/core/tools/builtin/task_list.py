from __future__ import annotations

from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class TaskListParams(ToolParams):
    pass


class TaskListTool(BaseTool):
    retry_safe = True
    name = "task_list"
    description = (
        "List this Run's tasks with status and blocking dependencies. "
        "Use this to check what work remains and what can be started next."
    )
    params_model = TaskListParams
    input_schema = TaskListParams.model_json_schema()

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 返回格式化的任务列表摘要
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        TaskListParams.model_validate(params)
        return ToolResult(content=self._manager.format_list())
