from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from kama_claude.core.bus.events import StepFinishedEvent, StepStartedEvent
from kama_claude.core.checkpoint.checkpoint import ToolCheckpoint
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
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
        compact_threshold: float = 0.80,
        session_id: str = "",
        persistence: RunPersistence | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._context = context
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._persistence = persistence

    # 只调度步骤准备、模型调用和工具批次
    async def run(self) -> None:
        context = self._context
        # 这里只是同一对象的简写，修改 run 就是在修改 context.run
        run = context.run
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

    # 检查步数预算并提交新步骤，恢复已有模型调用时不经过这里
    def _start_step(self) -> bool:
        context = self._context
        run = context.run
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
        try:
            response = await self._provider.chat(
                messages=context.messages,
                tool_schemas=self._registry.tool_schemas(),
                bus=self._bus,
                run_id=run.run_id,
                step=run.step,
                system=run.system_prompt
                or context.system_prompt(
                    "You are a helpful AI assistant. "
                    "Use the available tools to complete the user's goal. "
                    "When the goal is fully achieved, respond with a final answer "
                    "and do not call any more tools."
                ),
            )
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

            tool.result = asdict(result)
            if result.outcome_unknown and self._persistence is not None:
                # 结果未知时保留当前位置，等待用户核实后决定
                tool.status = "unknown"
                context.mark_interrupted("unknown_tool_result")
            else:
                tool.status = "completed"
                context.add_tool_result(tool.id, result.content, is_error=result.is_error)
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
            if context.is_done():
                return False
        await self._finish_tool_batch()
        return True

    # 只保存审批前和执行前的状态，不处理结果、消息或游标
    def _save_tool_phase(self, phase: Literal["awaiting_approval", "executing"]) -> None:
        run = self._context.run
        tool = run.tools[run.tool_index]
        tool.status = phase
        if phase == "executing":
            tool.attempts += 1
        self._save_checkpoint()

    # 工具批次结束后检查预算、按需压缩，并提交下一阶段
    async def _finish_tool_batch(self) -> None:
        context = self._context
        run = context.run
        if run.step >= run.max_steps:
            context.mark_failed("exceeded_max_steps")
        elif (
            self._compactor
            and self._compact_threshold > 0
            and run.context_pct >= self._compact_threshold
        ):
            # 工具结果已配对后再压缩，下一次请求才能使用完整历史
            await self._compactor.compact(context, self._provider)
        # 如果这一个循环没有完成任务，就继续设置为ready阶段，准备下一次循环
        run.phase = "ready" if not context.is_done() else "finished"
        self._save_checkpoint()

    # 直接提交本轮检查点，保存错误不经过 Runner 回调且不被吞掉
    def _save_checkpoint(self) -> None:
        if self._persistence is not None:
            self._persistence.save()
