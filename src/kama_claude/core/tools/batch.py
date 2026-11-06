from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kama_claude.core.context import ExecutionContext
from kama_claude.core.discovery.exposure import visible_schemas
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.invocation import invoke_tool, publish_tool_result
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.persistence import RunPersistence
    from kama_claude.core.permissions.manager import PermissionManager
    from kama_claude.core.subagent.tracking import SubagentTracker


class ToolBatchExecutor:
    def __init__(
        self,
        context: ExecutionContext,
        registry: ToolRegistry,
        bus: EventBus,
        budget: RunBudget,
        *,
        permission_manager: PermissionManager | None = None,
        persistence: RunPersistence | None = None,
        session_id: str = "",
        subagents: SubagentTracker | None = None,
    ) -> None:
        self._context = context
        self.registry = registry
        self._bus = bus
        self._budget = budget
        self._permission_manager = permission_manager
        self._persistence = persistence
        self._session_id = session_id
        self._subagents = subagents

    # 预算检查、压缩与真实请求共用同一份可见定义。
    def model_schemas(self) -> list[dict[str, object]]:
        if self._context.run.schemas_frozen:
            return visible_schemas(self._context.run)
        return self.registry.tool_schemas()

    # 从游标继续执行，结果、历史和游标都在这里按顺序处理
    async def run_pending(self) -> bool:
        context = self._context
        run = context.run
        # 开始按上次记录的顺序执行工具
        while run.tool_index < len(run.tools):
            self._budget.check()
            tool = run.tools[run.tool_index]
            if tool.status == "completed":
                run.tool_index += 1
                self._save_checkpoint()
                continue
            if tool.status in ("unknown", "executing"):
                context.mark_interrupted("unknown_tool_result")
                self._save_checkpoint()
                return False
            # pending 或 awaiting_approval：进入正常工具调用流程
            tool_call = ToolCallBlock(tool.id, tool.name, tool.input)
            started_at = time.monotonic()
            attempts_before = tool.attempts
            visible = {str(schema["name"]) for schema in self.model_schemas()}
            if not tool.exposed or (
                tool.name in run.mcp_tool_names and tool.name not in visible
            ):
                result = ToolResult(
                    "Tool was not exposed on this model turn. Search and use it on the next turn.",
                    is_error=True, error_type="schema_error",
                )
            else:
                result = await invoke_tool(
                    self.registry,
                    tool_call,
                    self._bus,
                    run.run_id,
                    permission_manager=self._permission_manager,
                    session_id=self._session_id,
                    workspace=Path(run.workspace),
                    # 审批前、真正执行前，调用 _save_tool_phase 保存状态
                    checkpoint_hook=self._save_tool_phase,
                    notify_result=False,
                )

            repeated = self._budget.record_tool_result(tool.name, tool.input, result)
            tool.result = asdict(result)
            if result.outcome_unknown:
                # 结果未知时保留当前位置，等待用户核实后决定
                tool.status = "unknown"
                context.mark_interrupted("unknown_tool_result")
            else:
                tool.status = "completed"
                block = context.add_tool_result(tool.id, result.content, is_error=result.is_error)
                if self._persistence is not None:
                    # 逐工具记录完整结果，之后的裁剪或摘要只改变工作历史
                    self._persistence.record_message("user", [block])
                run.tool_index += 1

            # 先一起保存结果、消息和游标，再通知前端；保存失败就停在这里
            if self._subagents is not None:
                self._subagents.record_result(tool, result)
            self._save_checkpoint()
            await publish_tool_result(
                self._bus,
                run.run_id,
                tool_call,
                result,
                int((time.monotonic() - started_at) * 1000),
                attempt=max(1, tool.attempts - attempts_before),
            )
            if repeated:
                raise BudgetExceeded("repeated_tool_failure")
            if context.is_done():
                return False
        self._finish_tool_batch()
        return True

    # 只保存审批前和执行前的状态，不处理结果、消息或游标
    def _save_tool_phase(self, phase: Literal["awaiting_approval", "executing"]) -> None:
        run = self._context.run
        tool = run.tools[run.tool_index]
        if phase == "executing":
            self._budget.check()
        tool.status = phase
        if phase == "executing":
            tool.attempts += 1
        self._save_checkpoint()

    # 工具批次结束后检查步数，压缩统一留到下次模型请求前
    def _finish_tool_batch(self) -> None:
        context = self._context
        run = context.run
        if run.step >= run.max_steps:
            context.mark_failed("exceeded_max_steps")
        # 如果这一个循环没有完成任务，就继续设置为ready阶段，准备下一次循环
        run.phase = "ready" if not context.is_done() else "finished"
        self._save_checkpoint()

    # 预算终止补齐未执行调用；执行结果未知时保留现场，仍由用户确认
    def stop_for_budget(self, reason: str) -> None:
        context = self._context
        run = context.run
        if any(tool.status in ("executing", "unknown") for tool in run.tools):
            for tool in run.tools:
                if tool.status == "executing":
                    tool.status = "unknown"
            context.mark_interrupted(reason)
        else:
            for tool in run.tools[run.tool_index :]:
                result = ToolResult(
                    content=f"Tool not executed: {reason}",
                    is_error=True,
                    error_type="budget",
                )
                tool.result = asdict(result)
                tool.status = "completed"
                block = context.add_tool_result(tool.id, result.content, is_error=True)
                if self._persistence is not None:
                    self._persistence.record_message("user", [block])
            run.tool_index = len(run.tools)
            context.mark_failed(reason)
        self._save_checkpoint()

    # 直接提交本轮检查点，保存错误不经过 Runner 回调且不被吞掉
    def _save_checkpoint(self) -> None:
        self._budget.tick()
        if self._persistence is not None:
            self._persistence.save()
