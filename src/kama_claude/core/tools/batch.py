from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import ValidationError

from kama_claude.core.checkpoint.checkpoint import ToolCheckpoint
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionContext, RunStopRequested
from kama_claude.core.discovery.exposure import callable_names, request_schemas
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.progress import ProgressGuard
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.tools.base import FileTool, ToolResult
from kama_claude.core.tools.invocation import invoke_tool, publish_tool_result
from kama_claude.core.tools.output import append_tool_warning
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.workspace import WorkspacePathError

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
        parallel_limit: int = 3,
    ) -> None:
        if type(parallel_limit) is not int or parallel_limit < 1:
            raise ValueError("parallel_limit must be a positive integer")
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
        self._parallel_limit = parallel_limit

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

    # 按声明顺序分组，只读工具有界并发，串行工具是组间屏障。
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
        while run.tool_index < len(run.tools):
            context.check_stop()
            self._budget.check()
            tool = run.tools[run.tool_index]
            if tool.status == "completed":
                if not self._finish_completed():
                    return False
                continue
            if tool.status in ("unknown", "executing"):
                context.mark_interrupted("unknown_tool_result")
                self._save_checkpoint()
                return False
            group = self._parallel_group() if not mixed_finish else []
            if len(group) > 1:
                await self._run_parallel(group)
            else:
                outcome = await self._execute_tool(tool, mixed_finish=mixed_finish)
                await self._accept_result(tool, *outcome)
            if not self._finish_completed():
                return False
            if context.is_done():
                return run.status == "success"
        self._finish_tool_batch()
        return True

    # 仅收集连续的待执行只读调用，数量有界，不预先创建整批 Task。
    def _parallel_group(self) -> list[ToolCheckpoint]:
        run = self._context.run
        group = []
        for tool in run.tools[run.tool_index:run.tool_index + self._parallel_limit]:
            registered = self.registry.get(tool.name)
            if (
                tool.status != "pending" or not tool.exposed
                or not isinstance(registered, FileTool) or not registered.parallel_safe
                or tool.name in run.mcp_tool_names
            ):
                break
            try:
                if registered.params_model is not None:
                    registered.params_model.model_validate(tool.input)
                access = registered.path_access(tool.input)
            except (ValidationError, WorkspacePathError):
                break
            if access.outside_workspace or (
                self._permission_manager is not None
                and not self._permission_manager.can_auto_allow(
                    tool.name, tool.input, self._session_id, path_access=access,
                )
            ):
                break
            group.append(tool)
        return group

    # 子任务只负责调用；协调者串行接收结果、保存状态并回收所有 Task。
    async def _run_parallel(self, group: list[ToolCheckpoint]) -> None:
        tasks = {asyncio.create_task(self._execute_tool(tool, allow_prompt=False)): tool
                 for tool in group}
        pending = set(tasks)
        stopped = False
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                # 同一调度轮完成的调用按原顺序接收，减少无意义的显示抖动。
                for task in sorted(done, key=lambda item: group.index(tasks[item])):
                    try:
                        outcome = task.result()
                    except RunStopRequested:
                        stopped = True
                        continue  # 不取消已启动的读取，等这一组自然结束。
                    await self._accept_result(tasks[task], *outcome)
            if stopped and not self._context.is_done():
                raise RunStopRequested()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # 同一入口处理串并行调用，钩子明确绑定工具，不通过共享游标猜测身份。
    async def _execute_tool(
        self, tool: ToolCheckpoint, *, allow_prompt: bool = True, mixed_finish: bool = False,
    ) -> tuple[ToolResult, int, int]:
        run = self._context.run
        call = ToolCallBlock(tool.id, tool.name, tool.input)
        started_at = time.monotonic()
        attempts_before = tool.attempts
        registered = self.registry.get(tool.name)
        if mixed_finish:
            result = ToolResult(
                "No tool in this batch was executed: finish must be the only tool call. "
                "Execute business tools first, observe their results, then finish separately.",
                is_error=True, error_type="invalid_operation",
            )
        elif not tool.exposed or (
            tool.name in run.mcp_tool_names
            and tool.name not in callable_names(run, self._context.messages)
        ):
            result = ToolResult(
                "Tool was not exposed on this model turn. For MCP tools, first call "
                "search_tool_candidates, inspect candidates, then get_tool_schema(names). "
                "Use the loaded tool on the next turn.", is_error=True, error_type="schema_error",
            )
        elif (
            self._subagents is not None and registered is not None and registered.ends_run
            and (blocked := self._subagents.finish_error()) is not None
        ):
            result = blocked
        else:
            # 每个调用拥有自己的状态钩子，实际状态仍只保存在 context.run。
            def save_phase(phase: Literal["awaiting_approval", "executing"]) -> None:
                self._save_tool_phase(tool, phase)

            result = await invoke_tool(
                self.registry, call, self._bus, run.run_id,
                permission_manager=self._permission_manager, session_id=self._session_id,
                workspace=Path(run.workspace), checkpoint_hook=save_phase,
                notify_result=False, allow_prompt=allow_prompt,
            )
        return result, int((time.monotonic() - started_at) * 1000), max(
            1, tool.attempts - attempts_before,
        )

    # 乱序完成也立即保存原始结果与配对消息，再整理预览、发布通知。
    async def _accept_result(
        self, tool: ToolCheckpoint, result: ToolResult, elapsed_ms: int, attempt: int,
    ) -> None:
        context, run = self._context, self._context.run
        tool.result = asdict(result)
        tool.output_prepared = False
        if result.outcome_unknown:
            tool.status = "unknown"
            context.mark_interrupted("unknown_tool_result")
        else:
            tool.status = "completed"
            block = context.add_tool_result(
                tool.id, result.message_content(), is_error=result.is_error,
            )
            if self._persistence is not None:
                self._persistence.record_message("user", [block])
            if result.ends_run and not result.is_error:
                run.result = result.content
                context.mark_success()
                tool.output_prepared = True
        if self._subagents is not None:
            self._subagents.record_result(result)
        self._save_checkpoint()
        if tool.status == "completed":
            if not self._finish_completed() or not self._prepare_output(tool):
                return
            result = ToolResult(**tool.result)
        await publish_tool_result(
            self._bus, run.run_id, ToolCallBlock(tool.id, tool.name, tool.input),
            result, elapsed_ms, attempt=attempt,
        )

    # 按声明顺序处理保护计数；结果已保存并不代表此前的调用已完成收尾。
    def _finish_completed(self) -> bool:
        run = self._context.run
        while run.tool_index < len(run.tools):
            tool = run.tools[run.tool_index]
            if tool.status != "completed":
                break
            if tool.result is None:
                raise ValueError("completed tool has no result")
            result = ToolResult(**tool.result)
            original = result.content
            registered = self.registry.get(tool.name)
            warning, stalled = self._progress.observe(registered, tool.input, result)
            repeated = self._budget.record_tool_result(tool.name, tool.input, result)
            if warning:
                append_tool_warning(result, warning)
            tool.result = asdict(result)
            if result.content != original:
                # 保护提醒是工作上下文指令，不改写已经追加的原始工具输出。
                self._result_block(tool)["content"] = result.message_content()
                tool.output_prepared = False
            run.tool_index += 1
            # 游标与计数一起提交，归档失败后恢复也不重复计数。
            self._save_checkpoint()
            if not self._prepare_output(tool):
                return False
            if repeated:
                raise BudgetExceeded("repeated_tool_failure")
            if stalled:
                raise BudgetExceeded("no_progress")
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
        self._result_block(tool)["content"] = preview
        tool.output_prepared = True
        self._save_checkpoint()
        return True

    # 通过稳定调用 ID 定位结果，不依赖完成顺序或共享执行游标。
    def _result_block(self, tool: ToolCheckpoint) -> dict[str, Any]:
        for message in reversed(self._context.messages):
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result" and block.get("tool_use_id") == tool.id:
                    return cast(dict[str, Any], block)
        raise ValueError(f"completed tool result missing from working history: {tool.id}")

    # 只保存审批前和执行前的状态，不处理结果、消息或游标
    def _save_tool_phase(
        self, tool: ToolCheckpoint, phase: Literal["awaiting_approval", "executing"],
    ) -> None:
        # 运行中的调用（含受控重试）自然完成；尚未执行的调用不再启动。
        if tool.status != "executing":
            self._context.check_stop()
            if self._context.is_done():
                raise RunStopRequested()
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
                if tool.status == "completed":
                    continue  # 后面的只读调用可能早已完成，不能覆盖真实结果。
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
            try:
                self._persistence.save()
            except Exception as exc:
                self._budget.block(exc)  # 阻止同组其他调用绕过失败的提交边界。
                raise
