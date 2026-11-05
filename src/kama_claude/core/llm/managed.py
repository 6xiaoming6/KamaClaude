from __future__ import annotations

import asyncio
from copy import deepcopy

import anthropic
import httpx
from pydantic import BaseModel

from kama_claude.core.checkpoint.persistence import RunPersistence
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.config import CompactionConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.run_budget import RunBudget, publish_budget


# 仅对明确的传输故障、限流和服务端错误做有限重试
def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TransportError, anthropic.APIConnectionError)):
        return True
    # OpenAI 是可选依赖，按 SDK 异常来源识别，不要求无条件导入它
    if not type(exc).__module__.startswith(("anthropic", "openai")):
        return False
    status = getattr(exc, "status_code", 0)
    return type(exc).__name__ in ("APIConnectionError", "APITimeoutError") or (
        isinstance(status, int) and (status == 429 or status >= 500)
    )


class ManagedProvider:
    # 普通请求和自动摘要共享预算，手动摘要只统计与校准、不修改已结束 Run
    def __init__(
        self, inner: LLMProvider, estimator: TokenEstimator, config: CompactionConfig,
        *, budget: RunBudget | None = None, persistence: RunPersistence | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._inner = inner
        self.estimator = estimator
        self._config = config
        self._budget = budget
        self._persistence = persistence
        self._bus = event_bus
        self.manual_tokens = 0

    # 预算占用与实际用量均先提交，不能依赖异步 Trace 队列保存
    def _save(self) -> None:
        if self._budget is not None:
            self._budget.tick()
        if self._persistence is not None:
            self._persistence.save()

    # 发布主任务预算，摘要也归属原 Run，避免污染其他会话
    async def notify(
        self, method: str = "", estimated_input: int = 0, reason: str = "",
        *, trigger_tokens: int = 0, underestimate_factor: float = 1.0,
    ) -> None:
        if self._bus is not None and self._budget is not None:
            await publish_budget(
                self._bus, self._budget, method, estimated_input, reason,
                trigger_tokens=trigger_tokens, underestimate_factor=underestimate_factor,
            )

    # 每次实际重试分别预留并记账，完整响应和当前请求估算严格配对
    async def chat(
        self, messages: list[dict[str, object]], tool_schemas: list[dict[str, object]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
        max_output_tokens: int = 8_192,
    ) -> LlmResponse:
        kind = "summary" if run_id == "compact" else "normal"
        limit = self._config.context_window_tokens - max_output_tokens - 1_024
        counter = self.estimator.select(system or "", messages, tool_schemas, kind=kind)
        # 固定本次实际提交的输入，不能拿回复追加后的消息去匹配 Usage
        request_messages = deepcopy(messages)
        request_schemas = deepcopy(tool_schemas)
        trigger = counter.trigger(limit, self._config.auto_threshold)
        estimate = counter.request(system or "", messages, tool_schemas)
        # 事件只转发观察信息，Usage 比例使用配置的窗口而不是模型名猜测
        async def forward(event: BaseModel) -> None:
            if getattr(event, "type", "") == "llm.usage":
                raw = event.model_dump()
                total = raw.get("total_input_tokens")
                if total is None:
                    total = raw.get("input_tokens", 0)
                event = event.model_copy(update={
                    "context_pct": total / self._config.context_window_tokens,
                })
            await bus.publish_safe(event)

        relay = EventBus()
        relay.subscribe(forward)
        for attempt in range(3):
            reservation = estimate + max_output_tokens
            if self._budget is not None:
                self._budget.reserve(reservation)
            self._save()
            await self.notify(
                counter.method, estimate, "model_retry" if attempt else "",
                trigger_tokens=trigger, underestimate_factor=counter.underestimate_factor,
            )
            try:
                remaining = self._budget.remaining_seconds() if self._budget else None
                async with asyncio.timeout(remaining):
                    response = await self._inner.chat(
                        messages=messages, tool_schemas=tool_schemas,
                        bus=relay,
                        run_id=run_id, step=step, system=system,
                        max_output_tokens=max_output_tokens,
                    )
            except asyncio.CancelledError:
                self._save()
                raise
            except Exception as exc:
                self._save()
                if isinstance(exc, TimeoutError) and self._budget is not None:
                    self._budget.check()
                if attempt == 2 or not _retryable(exc):
                    raise
                if self._budget is not None:
                    self._budget.check()
                await asyncio.sleep(2**attempt)
                continue
            usage = response.usage
            if usage is not None and all(
                type(value) is int and value >= 0
                for value in (usage.total_input, usage.output_tokens)
            ):
                actual = usage.total_input + usage.output_tokens
                if self._budget is not None:
                    self._budget.settle(reservation, actual)
                else:
                    self.manual_tokens += actual
                self.estimator.observe(
                    system or "", request_messages, request_schemas, usage.total_input, kind=kind,
                )
                usage.context_pct = usage.total_input / self._config.context_window_tokens
            self._save()
            await self.notify(
                counter.method, estimate, trigger_tokens=trigger,
                underestimate_factor=counter.underestimate_factor,
            )
            return response
        raise AssertionError("unreachable")
