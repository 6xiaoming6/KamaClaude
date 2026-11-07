from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kama_claude.core.checkpoint.checkpoint import ToolCheckpoint
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.discovery.exposure import callable_names, request_schemas
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.progress import ProgressGuard
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.invocation import invoke_tool, publish_tool_result
from kama_claude.core.tools.output import append_tool_warning
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
        output_preparer: ToolOutputPreparer | None = None,
    ) -> None:
        self._context = context
        self.registry = registry
        self._bus = bus
        self._budget = budget
        self._permission_manager = permission_manager
        self._persistence = persistence
        self._session_id = session_id
        self._subagents = subagents
        self._outputs = output_preparer or ToolOutputPreparer(CompactionConfig(), None)
        self._progress = ProgressGuard(context.run)

    # 预算检查、压缩与真实请求共用同一份可见定义。
    def model_schemas(self) -> list[dict[str, object]]:
        if self._context.run.schemas_frozen:
            return request_schemas(self._context.run)
        return self.registry.tool_schemas()

    # 完成通知与工作历史一同提交，再交给请求预算和压缩处理。
    def deliver_subagent_notifications(self) -> None:
        if self._subagents is not None and self._subagents.deliver_notifications(
            self._context.messages,
        ):
            self._save_checkpoint()

    # 从游标继续执行，结果、历史和游标都在这里按顺序处理
    async def run_pending(self) -> bool:
        context = self._context
        run = context.run
        # 已保存结果但归档未完成：只重试整理，不再次执行有副作用的工具。
        for completed in run.tools[:run.tool_index]:
            if not self._prepare_output(completed):
                return False
        if run.repeat_failure_limit and run.repeated_failures >= run.repeat_failure_limit:
            raise BudgetExceeded("repeated_tool_failure")
        # 结束调用必须独占一批；整批拒绝，不能先执行写入再发现结束协议错误。
        mixed_finish = len(run.tools) != 1 and any(
            (registered := self.registry.get(tool.name)) is not None and registered.ends_run
            for tool in run.tools
        )
        # 开始按上次记录的顺序执行工具
        while run.tool_index < len(run.tools):
            context.check_stop()
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
            visible = callable_names(run, context.messages)
            registered = self.registry.get(tool.name)
            if mixed_finish:
                result = ToolResult(
                    "No tool in this batch was executed: finish must be the only tool call. "
                    "Execute business tools first, observe their results, then finish separately.",
                    is_error=True, error_type="invalid_operation",
                )
            elif not tool.exposed or (
                tool.name in run.mcp_tool_names and tool.name not in visible
            ):
                result = ToolResult(
                    "Tool was not exposed on this model turn. Search and use it on the next turn.",
                    is_error=True, error_type="schema_error",
                )
            elif (
                self._subagents is not None and registered is not None and registered.ends_run
                and (blocked := self._subagents.finish_error()) is not None
            ):
                result = blocked
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

            warning, stalled = self._progress.observe(registered, tool.input, result)
            repeated = self._budget.record_tool_result(tool.name, tool.input, result)
            if warning:
                append_tool_warning(result, warning)
            tool.result = asdict(result)
            tool.output_prepared = False  # 本次新结果必须整理，不能继承旧快照的默认标记。
            if result.outcome_unknown:
                # 结果未知时保留当前位置，等待用户核实后决定
                tool.status = "unknown"
                context.mark_interrupted("unknown_tool_result")
            else:
                tool.status = "completed"
                block = context.add_tool_result(
                    tool.id, result.message_content(), is_error=result.is_error,
                )
                if self._persistence is not None:
                    # 逐工具记录完整结果，之后的裁剪或摘要只改变工作历史
                    self._persistence.record_message("user", [block])
                run.tool_index += 1
                if result.ends_run and not result.is_error:
                    run.result = result.content
                    context.mark_success()
                    tool.output_prepared = True

            # 先一起保存结果、消息和游标，再通知前端；保存失败就停在这里
            if self._subagents is not None:
                self._subagents.record_result(result)
            self._save_checkpoint()
            if tool.status == "completed" and not self._prepare_output(tool):
                return False
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
            if stalled:
                raise BudgetExceeded("no_progress")
            if context.is_done():
                return run.status == "success"
        self._finish_tool_batch()
        return True

    def _prepare_output(self, tool: ToolCheckpoint) -> bool:
        if tool.output_prepared:
            return True
        if tool.result is None:
            raise ValueError("completed tool has no result")
        try:
            preview = self._outputs.prepare(tool.id, ToolResult(**tool.result))
        except OSError as exc:
            self._context.mark_interrupted(f"tool_output_preparation_failed: {exc}")
            self._save_checkpoint()
            return False
        # 全文已随结果与完整历史提交；这里只替换模型工作历史中的那一块。
        for message in reversed(self._context.messages):
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result" and block.get("tool_use_id") == tool.id:
                    block["content"] = preview
                    tool.output_prepared = True
                    self._save_checkpoint()
                    return True
        raise ValueError(f"completed tool result missing from working history: {tool.id}")

    # 只保存审批前和执行前的状态，不处理结果、消息或游标
    def _save_tool_phase(self, phase: Literal["awaiting_approval", "executing"]) -> None:
        run = self._context.run
        tool = run.tools[run.tool_index]
        # 运行中的调用（含受控重试）自然完成；尚未执行的调用不再启动。
        if tool.status != "executing":
            self._context.check_stop()
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
        if run.max_steps > 0 and run.step >= run.max_steps:
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
                tool.output_prepared = True
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
