from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kama_claude.core.bus.events import (
    StepFinishedEvent,
    StepStartedEvent,
)
from kama_claude.core.checkpoint.checkpoint import CheckpointError, ToolCheckpoint
from kama_claude.core.compact.manager import ContextManager
from kama_claude.core.compact.request import assemble_messages
from kama_claude.core.completion import FINISH_INSTRUCTIONS, FINISH_REMINDER
from kama_claude.core.context import ExecutionContext
from kama_claude.core.discovery.exposure import callable_names
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.run_budget import BudgetExceeded, RunBudget, publish_budget
from kama_claude.core.tools.batch import ToolBatchExecutor

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.persistence import RunPersistence

log = logging.getLogger(__name__)


# 返回事件时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 注入的组件共用同一个 Context；这里只决定执行顺序。
    def __init__(
        self,
        provider: LLMProvider,
        bus: EventBus,
        *,
        context: ExecutionContext,
        tools: ToolBatchExecutor,
        context_manager: ContextManager,
        budget: RunBudget,
        persistence: RunPersistence | None = None,
    ) -> None:
        self._provider = provider
        self._bus = bus
        self._context = context
        self._tools = tools
        self._context_manager = context_manager
        self._budget = budget
        self._persistence = persistence

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
                        if not await self._tools.run_pending():
                            return
                    await self._bus.publish_safe(
                        StepFinishedEvent(run_id=run.run_id, step=run.step, ts=_now())
                    )
        except (BudgetExceeded, TimeoutError) as exc:
            reason = str(exc) if isinstance(exc, BudgetExceeded) else "run_time_budget_exceeded"
            self._tools.stop_for_budget(reason)
            await publish_budget(self._bus, self._budget, reason=reason)
        finally:
            # 正常取消也结算最后一段等待时间，由 Runner 原有收尾保存
            self._budget.tick()

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
        await self._bus.publish_safe(StepStartedEvent(run_id=run.run_id, step=run.step, ts=_now()))
        system = run.system_prompt or context.system_prompt(
            "You are a helpful AI assistant. "
            "Use the available tools to complete the user's goal. " + (
                FINISH_INSTRUCTIONS if run.completion_mode == "explicit"
                else "When the goal is fully achieved, respond with a final answer "
                "and do not call any more tools."
            )
        )
        schemas = self._tools.model_schemas()
        # 请求前准备
        if not await self._context_manager.prepare(system, schemas):
            return False
        # 规则 system 冻结；自动记忆由统一请求投影注入。
        system = run.system_prompt or system
        exposed = callable_names(run, context.messages) if run.schemas_frozen else {
            str(schema["name"]) for schema in schemas
        }
        try:
            response = await self._provider.chat(
                messages=assemble_messages(run, context.messages),
                tool_schemas=schemas,
                bus=self._bus,
                run_id=run.run_id,
                step=run.step,
                system=system,
                max_output_tokens=self._context_manager.config.output_reserve_tokens,
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
        self._record_response(response, exposed)
        if run.max_total_tokens and run.actual_tokens + run.estimated_tokens > run.max_total_tokens:
            raise BudgetExceeded("run_token_budget_exceeded")
        return True

    # 保存完整模型消息与工具批次，并根据停止原因设置下一阶段
    def _record_response(self, response: LlmResponse, exposed: set[str]) -> None:
        context = self._context
        run = context.run
        blocks = response.content_blocks()
        context.add_assistant_message(blocks)
        if self._persistence is not None:
            self._persistence.record_message("assistant", blocks)
        run.context_pct = response.usage.context_pct if response.usage else 0.0
        # 先登记完整工具批次并保存，之后才真正执行工具
        run.tools = [
            ToolCheckpoint(
                id=t.id, name=t.name, input=t.input, exposed=t.name in exposed,
                output_prepared=False,
            )
            for t in response.tool_calls
        ]
        run.tool_index = 0
        if response.stop_reason == "end_turn" and not response.tool_calls:
            if run.completion_mode == "legacy":
                run.result = response.text or ""
                context.mark_success()
            elif run.finish_reminders:
                context.mark_failed("missing_finish")
            else:
                run.finish_reminders = 1
                context.messages.append({"role": "user", "content": FINISH_REMINDER})
                run.phase = "ready"
        elif response.stop_reason == "tool_use":
            run.phase = "tools"
        else:
            for tc in response.tool_calls:
                block = context.add_tool_result(
                    tc.id,
                    "Output limit or unsupported stop reason; tool not executed.",
                    is_error=True,
                )
                if self._persistence is not None:
                    self._persistence.record_message("user", [block])
            run.tools = []
            run.phase = "ready"
            if run.step >= run.max_steps:
                context.mark_failed("exceeded_max_steps")
        self._save_checkpoint()

    # 直接提交本轮检查点，保存错误不经过 Runner 回调且不被吞掉
    def _save_checkpoint(self) -> None:
        self._budget.tick()
        if self._persistence is not None:
            self._persistence.save()
