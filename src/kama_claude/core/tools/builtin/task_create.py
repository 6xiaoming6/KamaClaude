from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from kama_claude.core.task.manager import TaskManager, TaskNotFoundError
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class TaskCreateParams(ToolParams):
    subject: str = Field(min_length=1, max_length=200, description="Short task title.")
    description: str = Field(default="", max_length=4000)
    blocked_by: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=100)


class TaskCreateTool(BaseTool):
    name = "task_create"
    description = (
        "Create a task in this Run's task list, separate from other Runs and subagents. "
        "Use this to break down a complex goal into smaller, trackable steps. "
        "Returns the created task as JSON."
    )
    params_model = TaskCreateParams
    input_schema = TaskCreateParams.model_json_schema()

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 创建任务并返回 JSON 字符串
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = TaskCreateParams.model_validate(params)
        try:
            task = self._manager.create(parsed.subject, parsed.description, parsed.blocked_by)
            return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))
        except TaskNotFoundError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="not_found")
