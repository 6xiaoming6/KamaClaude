from __future__ import annotations

import asyncio

import pytest

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.run_budget import RunBudget
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from tests.unit.test_subagent_collaboration import Model
from tests.unit.test_subagent_runtime import spawn_tool


@pytest.mark.parametrize("role", ["explorer", "executor"])
async def test_foreground_child_outlives_regular_tool_deadline(tmp_path, monkeypatch, role):
    monkeypatch.setattr(BaseTool, "timeout_s", 0.02)
    model = Model(blocked=True)
    tool = spawn_tool(tmp_path, model)
    registry = ToolRegistry()
    registry.register(tool)
    task = asyncio.create_task(invoke_tool(registry, ToolCallBlock("spawn", "spawn_agent", {
        "description": "inspect", "prompt": "inspect", "subagent_type": role,
    }), EventBus(), "parent"))
    try:
        await asyncio.wait_for(model.started.wait(), timeout=2)
        await asyncio.sleep(0.06)
        assert not task.done(), "foreground child inherited the regular tool timeout"
        model.release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert not result.is_error and not result.outcome_unknown
    finally:
        model.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await tool._task_registry.cancel()


async def test_foreground_child_respects_parent_remaining_time(tmp_path, monkeypatch):
    parent = RunBudget(RunCheckpoint(run_id="parent", goal="g", model="test", max_steps=5,
                                     workspace=str(tmp_path)))
    monkeypatch.setattr(parent, "remaining_seconds", lambda: 0.03)
    tool = spawn_tool(tmp_path, Model(blocked=True), parent_budget=parent)
    registry = ToolRegistry()
    registry.register(tool)
    result = await asyncio.wait_for(invoke_tool(registry, ToolCallBlock("spawn", "spawn_agent", {
        "description": "inspect", "prompt": "inspect",
    }), EventBus(), "parent"), timeout=2)
    assert result.is_error and "run_time_budget_exceeded" in result.content
    assert not result.outcome_unknown
    await tool._task_registry.cancel()


async def test_regular_tool_keeps_default_deadline(monkeypatch):
    monkeypatch.setattr(BaseTool, "timeout_s", 0.02)

    class SlowTool(BaseTool):
        name = "slow"
        description = "controlled wait"
        input_schema = {"type": "object"}

        async def invoke(self, params):
            await asyncio.Event().wait()
            return ToolResult("unused")

    registry = ToolRegistry()
    registry.register(SlowTool())
    result = await invoke_tool(registry, ToolCallBlock("slow", "slow", {}), EventBus(), "parent")
    assert result.is_error and result.error_type == "timeout" and result.outcome_unknown


async def test_explicit_deadline_still_cancels_foreground_child(tmp_path):
    tool = spawn_tool(tmp_path, Model(blocked=True))
    registry = ToolRegistry()
    registry.register(tool)
    try:
        result = await invoke_tool(registry, ToolCallBlock("spawn", "spawn_agent", {
            "description": "inspect", "prompt": "inspect",
        }), EventBus(), "parent", timeout=0.03)
        assert result.is_error and result.error_type == "timeout" and result.outcome_unknown
        assert not tool._task_registry._tasks
    finally:
        await tool._task_registry.cancel()


async def test_caller_cancellation_reaches_foreground_child(tmp_path):
    model = Model(blocked=True)
    tool = spawn_tool(tmp_path, model)
    registry = ToolRegistry()
    registry.register(tool)
    task = asyncio.create_task(invoke_tool(registry, ToolCallBlock("spawn", "spawn_agent", {
        "description": "inspect", "prompt": "inspect",
    }), EventBus(), "parent"))
    try:
        await asyncio.wait_for(model.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not tool._task_registry._tasks
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await tool._task_registry.cancel()
