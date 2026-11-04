from __future__ import annotations

import json

from pydantic import Field

from kama_claude.core.task.manager import TaskManager, TaskNotFoundError
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class TaskGetParams(ToolParams):
    task_id: int = Field(gt=0)


class TaskGetTool(BaseTool):
    retry_safe = True
    name = "task_get"
    description = "Get a task by integer ID from this Run's task list. Returns JSON."
    params_model = TaskGetParams
    input_schema = TaskGetParams.model_json_schema()

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 获取任务详情并返回 JSON 字符串
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = TaskGetParams.model_validate(params)
        try:
            task = self._manager.get(parsed.task_id)
            return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))
        except TaskNotFoundError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="not_found")
