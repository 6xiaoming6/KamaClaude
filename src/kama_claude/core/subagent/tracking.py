from __future__ import annotations

from typing import Any

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint, ToolCheckpoint
from kama_claude.core.tools.base import ToolResult


class SubagentTracker:
    # 跟踪数据仍写在原 Run；本对象不维护第二份任务状态。
    def __init__(self, run: RunCheckpoint) -> None:
        self.run = run

    async def handle_event(self, event: Any) -> None:
        run = self.run
        if getattr(event, "type", "") == "subagent.started":
            if event.parent_run_id == run.run_id or event.parent_run_id in run.subagents:
                current = run.tools[run.tool_index] if run.tool_index < len(run.tools) else None
                background = (
                    current is not None and current.name == "spawn_agent"
                    and bool(current.input.get("run_in_background"))
                ) or run.subagents.get(event.parent_run_id) == "background"
                run.subagents[event.run_id] = "background" if background else "running"
        elif getattr(event, "type", "") == "subagent.finished":
            if event.run_id in run.subagents and run.subagents[event.run_id] != "background":
                run.subagents[event.run_id] = event.status

    # 结果正式提交前登记已收集；通用 Loop 不需要认识 agent_result 的业务约定。
    def record_result(self, tool: ToolCheckpoint, result: ToolResult) -> None:
        if (
            tool.name == "agent_result"
            and (not result.is_error or result.error_type == "subagent_failed")
            and not result.outcome_unknown and result.content != "still running"
        ):
            child_id = str(tool.input.get("run_id", ""))
            if child_id in self.run.subagents:
                self.run.subagents[child_id] = "collected"
