from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from kama_claude.core.task.manager import TaskManager, TaskNotFoundError
from kama_claude.core.task.model import TaskStatus
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class TaskUpdateParams(ToolParams):
    task_id: int = Field(gt=0)
    status: TaskStatus | None = None
    add_blocked_by: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=100)
    remove_blocked_by: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=list, max_length=100,
    )


class TaskUpdateTool(BaseTool):
    name = "task_update"
    description = (
        "Update a task in this Run's task list. "
        "Set status to 'in_progress' when starting work on a task, "
        "'completed' when finished (automatically clears it from other tasks' blocked_by). "
        "Returns the updated task as JSON."
    )
    params_model = TaskUpdateParams
    input_schema = TaskUpdateParams.model_json_schema()

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 更新任务并返回 JSON 字符串
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = TaskUpdateParams.model_validate(params)
        try:
            task = self._manager.update(
                parsed.task_id,
                status=parsed.status,
                add_blocked_by=parsed.add_blocked_by or None,
                remove_blocked_by=parsed.remove_blocked_by or None,
            )
            return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))
        except TaskNotFoundError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="not_found")
