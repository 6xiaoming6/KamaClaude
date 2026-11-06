from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.compact.estimator import TokenCounter, TokenEstimator
from kama_claude.core.compact.history import (
    SUMMARY_ACK,
    SUMMARY_MARKER,
    split_history,
    validate_pairs,
)
from kama_claude.core.config import CompactionConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.run_budget import BudgetExceeded

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider

logger = logging.getLogger(__name__)
_COMPACT_PROMPT = """Generate a historical handoff summary, not a new business answer.
Do not call any tools. Summarize ONLY the old records specified below; the recent
records will be retained verbatim and are supplied for understanding corrections.
Merge any earlier summary into ONE updated summary. Preserve user corrections,
constraints, evidence, failures and uncertainty; never present unknown outcomes as success.
Use these six Markdown sections:
## 1. Goal and constraints
## 2. Completed work and evidence
## 3. Current file state
## 4. Important decisions
## 5. Unfinished work and uncertain outcomes
## 6. Key paths and continuation entry points
Do not include credentials or raw chain of thought.
"""


@dataclass
class CompactionResult:
    messages: list[dict[str, Any]]
    original_token_estimate: int
    remaining_token_estimate: int
    summary_text: str = ""
    summary_tokens: int = 0
    compacted: bool = False
    reason: str = ""
    applied_stages: list[str] = field(default_factory=list)
    llm_used: bool = False
    preserved_messages: int = 0


# 可读副本不是恢复来源；权威检查点已提交后，导出失败只记录日志。
def write_summary(directory: Path, summary: str) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        (directory / f"summary_{stamp}.md").write_text(summary, encoding="utf-8")
    except OSError:
        logger.exception("cannot export summary after checkpoint commit")


class Compactor:
    # 只生成候选，不改 Context、不保存检查点、不重新整理旧工具结果。
    def __init__(
        self, config: CompactionConfig | None = None, estimator: TokenEstimator | None = None,
    ) -> None:
        self._config = config or CompactionConfig()
        self._estimator = estimator or TokenEstimator()

    async def prepare_context(
        self, messages: list[dict[str, Any]], provider: LLMProvider | None = None, *,
        system: str = "", tool_schemas: list[dict[str, Any]] | None = None,
        context_messages: list[dict[str, Any]] | None = None,
        focus: str = "", manual: bool = False,
        provider_factory: Callable[[], LLMProvider] | None = None,
    ) -> CompactionResult:
        schemas = tool_schemas or []
        prefix = context_messages or []
        request = [*prefix, *messages]
        counter = self._estimator.select(system, request, schemas)
        before = counter.raw_request(system, request, schemas)
        result = CompactionResult(messages, before, before)
        threshold = self._config.auto_threshold or (0.8 if manual else 0.0)
        if not threshold:
            result.reason = "auto_disabled"
            return result
        if counter.request(system, request, schemas) < int(self._config.input_limit * threshold):
            result.reason = "below_threshold"
            return result
        # 延迟创建 SDK，未达阈值时 /compact 无需凭证或模型请求。
        if provider is None and provider_factory is not None:
            try:
                provider = provider_factory()
            except (CheckpointError, BudgetExceeded):
                raise
            except (Exception, SystemExit):
                logger.exception("cannot create summary provider")
                result.reason = "provider_unavailable"
                return result
        if provider is None:
            result.reason = "provider_unavailable"
            return result
        return await self.compact_messages(
            messages, provider, focus, system=system, tool_schemas=schemas,
            context_messages=prefix, counter=counter,
        )

    # 摘要请求复用原请求前缀，仅追加指令；原消息不会被 JSON 重新包装。
    async def compact_messages(
        self, messages: list[dict[str, Any]], provider: LLMProvider, focus: str = "", *,
        system: str = "", tool_schemas: list[dict[str, Any]] | None = None,
        context_messages: list[dict[str, Any]] | None = None,
        counter: TokenCounter | None = None,
    ) -> CompactionResult:
        schemas = tool_schemas or []
        prefix = context_messages or []
        request = [*prefix, *messages]
        counter = counter or self._estimator.select(system, request, schemas)
        before = counter.raw_request(system, request, schemas)
        unchanged = CompactionResult(messages, before, before)
        if not validate_pairs(messages):
            unchanged.reason = "incomplete_tool_batch"
            return unchanged
        native_names = {str(s["name"]) for s in schemas if s.get("defer_loading")}
        try:
            old, recent = split_history(
                messages, self._config.keep_recent_turns, self._config.recent_token_budget,
                counter, mcp_names=native_names,
            )
        except ValueError as exc:
            unchanged.reason = str(exc)
            return unchanged
        if not old:
            unchanged.reason = "no_old_history"
            return unchanged
        if counter.raw_request(system, [*prefix, *recent], schemas) >= self._config.input_limit:
            unchanged.reason = "required_context_too_large"
            return unchanged
        # old 可能排除了保留的当前请求：用原始消息序号准确指明范围。
        old_ids = {id(message) for message in old}
        ranges = [str(i + 1) for i, message in enumerate(messages) if id(message) in old_ids]
        instruction = (
            _COMPACT_PROMPT + f"\nKeep under {self._config.summary_max_tokens} tokens."
            + "\nOLD record numbers (1-based, excluding automatic context): " + ", ".join(ranges)
        )
        if focus.strip():
            instruction += "\nUser focus: " + focus.strip()
        summary_request = [*request, {"role": "user", "content": instruction}]
        summary_limit = (
            self._config.context_window_tokens - self._config.summary_max_tokens - 1_024
        )
        summary_counter = self._estimator.select(system, summary_request, schemas, kind="summary")
        if summary_counter.request(system, summary_request, schemas) > summary_limit:
            unchanged.reason = "summary_input_too_large"
            return unchanged
        unchanged.llm_used = True
        try:
            response = await provider.chat(
                messages=summary_request, tool_schemas=schemas, bus=EventBus(),
                run_id="compact", step=0, system=system,
                max_output_tokens=self._config.summary_max_tokens,
            )
        except (CheckpointError, BudgetExceeded):
            raise
        except Exception:
            logger.exception("summary request failed; preserving original history")
            unchanged.reason = "summary_request_failed"
            return unchanged
        summary = response.text.strip()
        tokens = max(counter.count(summary), response.usage.output_tokens if response.usage else 0)
        if response.stop_reason != "end_turn" or response.tool_calls:
            unchanged.reason = "summary_incomplete"
        elif not summary:
            unchanged.reason = "empty_summary"
        elif tokens > self._config.summary_max_tokens:
            unchanged.reason = "summary_too_large"
        else:
            candidate = [
                {"role": "user", "content": SUMMARY_MARKER + "\n" + summary},
                {"role": "assistant", "content": SUMMARY_ACK},
                *deepcopy(recent),
            ]
            # Anthropic thinking 绑定原前缀；只移除本次重写的工作投影，完整历史不动。
            if self._estimator.provider == "anthropic":
                for message in candidate:
                    if isinstance(message.get("content"), list):
                        message["content"] = [
                            block for block in message["content"]
                            if block.get("type") not in ("thinking", "redacted_thinking")
                        ]
                candidate = [m for m in candidate if m.get("content")]
            after = counter.raw_request(system, [*prefix, *candidate], schemas)
            if not validate_pairs(candidate):
                unchanged.reason = "incomplete_tool_batch"
            elif after > before * 0.9:
                unchanged.reason = "insufficient_reduction"
            elif after > self._config.input_limit:
                unchanged.reason = "context_budget_exceeded"
            else:
                return CompactionResult(
                    candidate, before, after, summary, tokens, True,
                    applied_stages=["llm_summary"], llm_used=True,
                    preserved_messages=len(recent),
                )
        return unchanged
