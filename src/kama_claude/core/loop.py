from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from kama_claude.core.bus.events import (
    ContextCompactedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
)
from kama_claude.core.checkpoint.checkpoint import CheckpointError, ToolCheckpoint
from kama_claude.core.compact.compactor import write_summary
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.invocation import invoke_tool, publish_tool_result
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.persistence import RunPersistence
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.permissions.manager import PermissionManager

log = logging.getLogger(__name__)


# 返回事件时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 绑定本轮上下文和可选存储，所有执行状态统一来自 context.run
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        context: ExecutionContext,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compaction: CompactionConfig | None = None,
        run_path: Path | None = None,
        session_id: str = "",
        persistence: RunPersistence | None = None,
        budget: RunBudget | None = None,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._context = context
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compaction = compaction or CompactionConfig()
        self._run_path = run_path
        self._session_id = session_id
        self._persistence = persistence
        self._budget = budget or RunBudget(context.run)
        self._estimator = estimator or TokenEstimator(context.run.provider, context.run.model)

    # 只调度步骤准备、模型调用和工具批次
    async def run(self) -> None:
        context = self._context
        # 这里只是同一对象的简写，修改 run 就是在修改 context.run
        run = context.run
        try:
            async with asyncio.timeout(self._budget.remaining_seconds()):
                while not context.is_done():
                    # 使用独立 if，让同一步依次进入模型和工具阶段
                    if run.phase == "ready" and not self._start_step():
                        return
                    if run.phase == "llm" and not await self._call_model():
                        return
                    if run.phase == "tools" and not context.is_done():
                        if not await self._run_tools():
                            return
                    await self._bus.publish_safe(
                        StepFinishedEvent(run_id=run.run_id, step=run.step, ts=_now())
                    )
        except (BudgetExceeded, TimeoutError) as exc:
            reason = str(exc) if isinstance(exc, BudgetExceeded) else "run_time_budget_exceeded"
            self._stop_for_budget(reason)
            if isinstance(self._provider, ManagedProvider):
                await self._provider.notify(reason=reason)
        finally:
            # 正常取消也结算最后一段等待时间，由 Runner 原有收尾保存
            self._budget.tick()

    # 预算终止补齐未执行调用；执行结果未知时保留现场，仍由用户确认
    def _stop_for_budget(self, reason: str) -> None:
        context = self._context
        run = context.run
        if any(tool.status in ("executing", "unknown") for tool in run.tools):
            for tool in run.tools:
                if tool.status == "executing":
                    tool.status = "unknown"
            context.mark_interrupted(reason)
        else:
            for tool in run.tools[run.tool_index:]:
                result = ToolResult(
                    content=f"Tool not executed: {reason}", is_error=True, error_type="budget",
                )
                tool.result = asdict(result)
                tool.status = "completed"
                context.add_tool_result(tool.id, result.content, is_error=True)
                if self._persistence is not None:
                    self._persistence.record_message("user", [context.messages[-1]["content"][-1]])
            run.tool_index = len(run.tools)
            context.mark_failed(reason)
        self._save_checkpoint()

    # 检查步数预算并提交新步骤，恢复已有模型调用时不经过这里
    def _start_step(self) -> bool:
        context = self._context
        run = context.run
        self._budget.check()
        if run.step >= run.max_steps:
            context.mark_failed("exceeded_max_steps")
            self._save_checkpoint()
            return False
        # 只在开始新步骤时计数，恢复 llm 阶段会跳过这里
        run.step += 1
        run.phase = "llm"
        self._save_checkpoint()
        return True

    # 请求当前步骤的模型回复，模型异常时记录中断或失败
    async def _call_model(self) -> bool:
        context = self._context
        run = context.run
        await self._bus.publish_safe(
            StepStartedEvent(run_id=run.run_id, step=run.step, ts=_now())
        )
        system = run.system_prompt or context.system_prompt(
            "You are a helpful AI assistant. "
            "Use the available tools to complete the user's goal. "
            "When the goal is fully achieved, respond with a final answer "
            "and do not call any more tools."
        )
        schemas = self._registry.tool_schemas()
        # 请求前准备
        if not await self._prepare_model_context(system, schemas):
            return False
        try:
            response = await self._provider.chat(
                messages=context.messages,
                tool_schemas=schemas,
                bus=self._bus,
                run_id=run.run_id,
                step=run.step,
                system=system,
                max_output_tokens=self._compaction.output_reserve_tokens,
            )
        except (BudgetExceeded, CheckpointError):
            raise
        except asyncio.CancelledError:
            if self._persistence is None:
                context.mark_failed("cancelled")
            raise
        except Exception:
            log.exception("LLM call failed run_id=%s step=%d", run.run_id, run.step)
            if self._persistence is not None:
                context.mark_interrupted("llm_error")
            else:
                context.mark_failed("llm_error")
            self._save_checkpoint()
            return False
        # 保存放在模型异常捕获之外，磁盘错误必须直接上抛
        self._record_response(response)
        if run.max_total_tokens and run.actual_tokens + run.estimated_tokens > run.max_total_tokens:
            raise BudgetExceeded("run_token_budget_exceeded")
        return True

    # 请求前执行同一条分层管线，程序层不足才调用摘要，提交后再通知
    async def _prepare_model_context(
        self, system: str, schemas: list[dict[str, Any]],
    ) -> bool:
        context = self._context
        run = context.run
        config = self._compaction
        counter = self._estimator.select(system, context.messages, schemas)
        if self._compactor is not None:
            pressure = (
                config.auto_threshold > 0
                and counter.request(system, context.messages, schemas)
                >= counter.trigger(config.input_limit, config.auto_threshold)
            )
            if pressure:
                await self._bus.publish_safe(ContextCompactionStartedEvent(
                    session_id=self._session_id, run_id=run.run_id,
                    original_tokens=counter.count(context.messages), ts=_now(),
                ))
            result = await self._compactor.prepare_context(
                context.messages, self._provider, system=system, tool_schemas=schemas,
                outputs_dir=self._run_path / "outputs" if self._run_path else None,
            )
            if result.compacted:
                context.messages = result.messages
                # 一次提交候选工作历史，不修改 Run 身份、Step 或工具游标
                self._save_checkpoint()
            if result.summary_text and self._run_path is not None:
                write_summary(self._run_path, result.summary_text)
            if result.compacted and result.reason in ("", "below_threshold", "auto_disabled"):
                await self._bus.publish_safe(ContextCompactedEvent(
                    session_id=self._session_id, run_id=run.run_id,
                    original_tokens=result.original_token_estimate,
                    summary_tokens=result.summary_tokens,
                    remaining_tokens=result.remaining_token_estimate,
                    applied_stages=result.applied_stages, llm_used=result.llm_used, ts=_now(),
                ))
            elif pressure or result.reason not in ("below_threshold", "auto_disabled"):
                await self._bus.publish_safe(ContextCompactionFailedEvent(
                    session_id=self._session_id, run_id=run.run_id,
                    reason=result.reason, applied_stages=result.applied_stages,
                    llm_used=result.llm_used, ts=_now(),
                ))
        # 整理若改变旧前缀，重新选择会自然回退全文估算，不能继续套旧 Usage
        counter = self._estimator.select(system, context.messages, schemas)
        if counter.request(system, context.messages, schemas) > (
            counter.effective_limit(config.input_limit)
        ):
            context.mark_interrupted("context_budget_exceeded")
            self._save_checkpoint()
            return False
        return True

    # 保存完整模型消息与工具批次，并根据停止原因设置下一阶段
    def _record_response(self, response: LlmResponse) -> None:
        context = self._context
        run = context.run
        blocks: list[dict[str, object]] = list(response.thinking_blocks)
        if response.text:
            blocks.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
        context.add_assistant_message(blocks)
        if self._persistence is not None:
            self._persistence.record_message("assistant", blocks)
        run.context_pct = response.usage.context_pct if response.usage else 0.0
        # 先登记完整工具批次并保存，之后才真正执行工具
        run.tools = [
            ToolCheckpoint(id=t.id, name=t.name, input=t.input) for t in response.tool_calls
        ]
        run.tool_index = 0
        if response.stop_reason == "end_turn":
            run.result = response.text or ""
            context.mark_success()
        elif response.stop_reason == "tool_use":
            run.phase = "tools"
        else:
            for tc in response.tool_calls:
                context.add_tool_result(
                    tc.id,
                    "Output limit or unsupported stop reason; tool not executed.",
                    is_error=True,
                )
                if self._persistence is not None:
                    self._persistence.record_message("user", [context.messages[-1]["content"][-1]])
            run.tools = []
            run.phase = "ready"
            if run.step >= run.max_steps:
                context.mark_failed("exceeded_max_steps")
        self._save_checkpoint()

    # 从游标继续执行，结果、历史和游标都在这里按顺序处理
    async def _run_tools(self) -> bool:
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
            result = await invoke_tool(
                self._registry, tool_call, self._bus, run.run_id,
                permission_manager=self._permission_manager,
                session_id=self._session_id,
                # 审批前、真正执行前，调用 _save_tool_phase 保存状态
                checkpoint_hook=self._save_tool_phase,
                notify_result=False,
            )

            repeated = self._budget.record_tool_result(tool.name, tool.input, result)
            tool.result = asdict(result)
            if result.outcome_unknown and self._persistence is not None:
                # 结果未知时保留当前位置，等待用户核实后决定
                tool.status = "unknown"
                context.mark_interrupted("unknown_tool_result")
            else:
                tool.status = "completed"
                context.add_tool_result(tool.id, result.content, is_error=result.is_error)
                if self._persistence is not None:
                    # 逐工具记录完整结果，之后的裁剪或摘要只改变工作历史
                    self._persistence.record_message("user", [context.messages[-1]["content"][-1]])
                run.tool_index += 1
                # 主 Agent 已取回子任务结果，不再视为未完成子任务
                if (
                    tool.name == "agent_result"
                    and not result.is_error
                    and result.content != "still running"
                ):
                    child_id = str(tool.input.get("run_id", ""))
                    if child_id in run.subagents:
                        run.subagents[child_id] = "collected"

            # 先一起保存结果、消息和游标，再通知前端；保存失败就停在这里
            self._save_checkpoint()
            await publish_tool_result(
                self._bus, run.run_id, tool_call, result,
                int((time.monotonic() - started_at) * 1000),
                attempt=max(1, tool.attempts - attempts_before),
            )
            if repeated:
                raise BudgetExceeded("repeated_tool_failure")
            if context.is_done():
                return False
        await self._finish_tool_batch()
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
    async def _finish_tool_batch(self) -> None:
        context = self._context
        run = context.run
        if run.step >= run.max_steps:
            context.mark_failed("exceeded_max_steps")
        # 如果这一个循环没有完成任务，就继续设置为ready阶段，准备下一次循环
        run.phase = "ready" if not context.is_done() else "finished"
        self._save_checkpoint()

    # 直接提交本轮检查点，保存错误不经过 Runner 回调且不被吞掉
    def _save_checkpoint(self) -> None:
        self._budget.tick()
        if self._persistence is not None:
            self._persistence.save()
