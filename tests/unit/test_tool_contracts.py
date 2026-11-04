from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult
from kama_claude.core.tools.builtin import (
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
)
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


# 从真实调用入口执行工具，检查错误发生于审批前还是执行后
async def execute(
    tool: BaseTool, params: dict[str, Any], phases: list[str] | None = None,
    manager: PermissionManager | None = None, bus: EventBus | None = None,
) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    return await invoke_tool(
        registry, ToolCallBlock("test", tool.name, params), bus or EventBus(), "run",
        permission_manager=manager, session_id="session",
        checkpoint_hook=phases.append if phases is not None else None,
    )


# 功能：验证任务参数错误在审批与执行意图前返回 schema_error
# 设计：覆盖缺字段、错误类型、bool 冒充整数、非法状态、非法依赖及未知字段
@pytest.mark.parametrize("tool_type,params", [
    (TaskUpdateTool, {}),
    (TaskUpdateTool, {"task_id": "abc"}),
    (TaskUpdateTool, {"task_id": "1"}),
    (TaskUpdateTool, {"task_id": True}),
    (TaskUpdateTool, {"task_id": -1}),
    (TaskUpdateTool, {"task_id": 1, "status": "done"}),
    (TaskUpdateTool, {"task_id": 1, "add_blocked_by": "2"}),
    (TaskGetTool, {"task_id": 1.0}),
    (TaskGetTool, {"task_id": 1, "unexpected": True}),
    (TaskCreateTool, {"subject": ""}),
    (TaskCreateTool, {"subject": "x", "blocked_by": ["1"]}),
    (TaskCreateTool, {"subject": "x", "description": 123}),
    (TaskListTool, {"unexpected": True}),
])
async def test_bad_task_params_never_execute(
    tmp_path: Path, tool_type: Any, params: dict[str, Any],
) -> None:
    manager = PermissionManager(timeout_s=0.01)
    task_manager = TaskManager(tmp_path)
    phases: list[str] = []
    seen: list[str] = []
    bus = EventBus()

    # 记录审批和结果事件，错误输入不能先向用户请求审批
    async def collect(event: BaseModel) -> None:
        seen.append(str(getattr(event, "type", "")))

    bus.subscribe(collect)
    result = await execute(tool_type(task_manager), params, phases, manager, bus)
    assert result.is_error and result.error_type == "schema_error"
    assert not result.outcome_unknown
    assert phases == [] and "permission.requested" not in seen
    assert not list(tmp_path.glob("task_*.json")) and not manager._pending


# 功能：验证不存在的任务是明确业务错误，不是未知副作用
# 设计：读、写、依赖创建分别引用缺失 ID，检查磁盘不变及错误分类
@pytest.mark.parametrize("tool_type,params", [
    (TaskGetTool, {"task_id": 99}),
    (TaskUpdateTool, {"task_id": 99, "status": "completed"}),
    (TaskCreateTool, {"subject": "dependent", "blocked_by": [99]}),
])
async def test_missing_tasks_are_known_errors(tmp_path: Path, tool_type: Any, params: dict) -> None:
    result = await execute(tool_type(TaskManager(tmp_path)), params)
    assert result.error_type == "not_found" and not result.outcome_unknown
    assert not list(tmp_path.glob("task_*.json"))


# 功能：验证读取缺失文件或权限失败都不触发重复重试
# 设计：替换只读操作并记录实际调用次数，区分业务拒绝与临时运行故障
@pytest.mark.parametrize("error,kind", [
    (FileNotFoundError("missing"), "not_found"),
    (PermissionError("denied"), "permission_denied"),
    (IsADirectoryError("directory"), "invalid_operation"),
])
async def test_known_read_errors_do_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, kind: str,
) -> None:
    operation = AsyncMock(side_effect=error)
    monkeypatch.setattr(ReadFileTool, "_invoke_path", operation)
    result = await execute(ReadFileTool(tmp_path), {"path": "file.txt"})
    assert result.error_type == kind and not result.outcome_unknown
    assert operation.await_count == 1


# 功能：验证修改后的任意异常仍保留未知副作用，不会被误分为参数错误
# 设计：先真实写入再抛 ValueError/PermissionError，防止全局按异常名称误判
@pytest.mark.parametrize("error", [ValueError("late error"), PermissionError("late denied")])
async def test_late_write_failure_remains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    # 模拟操作已发生但结果丢失
    async def write_then_fail(self: WriteFileTool, params: dict, path: Path) -> ToolResult:
        path.write_text("already changed", encoding="utf-8")
        raise error

    monkeypatch.setattr(WriteFileTool, "_invoke_path", write_then_fail)
    result = await execute(WriteFileTool(tmp_path), {"path": "file.txt", "content": "x"})
    assert result.is_error and result.outcome_unknown
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "already changed"


# 功能：验证所有内置工具统一使用严格参数模型，声明与实际校验同源
# 设计：通过真实 Runner 注册表检查全部工具，避免单测遗漏新增工具
def test_builtin_schemas_have_single_source(tmp_path: Path) -> None:
    from kama_claude.core.config import KamaConfig
    from kama_claude.core.runner import AgentRunner

    runner = AgentRunner(KamaConfig(), provider=AsyncMock())
    registry = runner._build_registry(
        TaskManager(tmp_path / "tasks"), provider=AsyncMock(), bus=EventBus(), run_id="run",
        workspace=tmp_path,
    )
    assert len(registry.tool_schemas()) == 15
    for schema in registry.tool_schemas():
        tool = registry.get(str(schema["name"]))
        assert tool is not None and tool.params_model is not None
        assert issubclass(tool.params_model, ToolParams)
        assert tool.input_schema == tool.params_model.model_json_schema()
        assert tool.input_schema["additionalProperties"] is False


# 功能：验证缺失记忆条目也返回 not_found，而不是笼统运行异常
# 设计：只读取临时项目作用域，避免访问用户全局记忆
async def test_missing_memory_is_known_error(tmp_path: Path) -> None:
    from kama_claude.core.memory.store import MemoryStore
    from kama_claude.core.tools.builtin.memory import MemoryReadTool

    result = await execute(MemoryReadTool(MemoryStore(tmp_path)), {"key": "missing"})
    assert result.error_type == "not_found" and not result.outcome_unknown
