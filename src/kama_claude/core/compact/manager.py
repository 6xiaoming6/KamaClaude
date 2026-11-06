from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kama_claude.core.bus.commands import SessionCompactResult
from kama_claude.core.bus.events import (
    ContextCompactedEvent,
    ContextCompactionFailedEvent,
    ContextCompactionStartedEvent,
)
from kama_claude.core.compact.compactor import CompactionResult, Compactor, write_summary
from kama_claude.core.compact.estimator import TokenEstimator, estimate_bytes
from kama_claude.core.config import CompactionConfig, MemoryConfig
from kama_claude.core.discovery.exposure import visible_schemas
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.memory.context import refresh_prompt
from kama_claude.core.memory.model import MemoryError
from kama_claude.core.memory.store import MemoryStore

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.persistence import RunPersistence
    from kama_claude.core.context import ExecutionContext
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.run_budget import RunBudget
    from kama_claude.core.session.store import SessionStore

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 自动、手动入口都先提交，再通过此处导出摘要和通知前端。
async def publish_compaction(
    bus: EventBus,
    session_id: str,
    run_id: str,
    result: CompactionResult,
    run_path: Path | None,
    *,
    report_failure: bool,
) -> None:
    if result.summary_text and run_path is not None:
        write_summary(run_path, result.summary_text)
    if result.compacted and result.reason in ("", "below_threshold", "auto_disabled"):
        await bus.publish_safe(
            ContextCompactedEvent(
                session_id=session_id,
                run_id=run_id,
                original_tokens=result.original_token_estimate,
                summary_tokens=result.summary_tokens,
                remaining_tokens=result.remaining_token_estimate,
                applied_stages=result.applied_stages,
                llm_used=result.llm_used,
                ts=_now(),
            )
        )
    elif report_failure:
        await bus.publish_safe(
            ContextCompactionFailedEvent(
                session_id=session_id,
                run_id=run_id,
                reason=result.reason,
                applied_stages=result.applied_stages,
                llm_used=result.llm_used,
                ts=_now(),
            )
        )


class ContextManager:
    # 只管理模型工作历史；完整对话由 RunPersistence 单独登记，不从压缩结果反推。
    def __init__(
        self,
        context: ExecutionContext,
        bus: EventBus,
        config: CompactionConfig,
        estimator: TokenEstimator,
        *,
        compactor: Compactor | None,
        provider: LLMProvider,
        budget: RunBudget,
        persistence: RunPersistence | None = None,
        session_id: str = "",
        run_path: Path | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.context = context
        self.config = config
        self._bus = bus
        self._estimator = estimator
        self._compactor = compactor
        self._provider = provider
        self._budget = budget
        self._persistence = persistence
        self._session_id = session_id
        self._run_path = run_path
        self._memory = memory

    # 每次请求前整理、提交，再检查硬预算；恢复模型调用也走同一入口。
    async def prepare(self, system: str, schemas: list[dict[str, Any]]) -> bool:
        context, run, config = self.context, self.context.run, self.config
        counter = self._estimator.select(system, context.messages, schemas)
        result: CompactionResult | None = None
        report_failure = False
        if self._compactor is not None:
            pressure = config.auto_threshold > 0 and counter.request(
                system, context.messages, schemas
            ) >= int(config.input_limit * config.auto_threshold)
            if pressure:
                await self._bus.publish_safe(
                    ContextCompactionStartedEvent(
                        session_id=self._session_id,
                        run_id=run.run_id,
                        original_tokens=counter.count(context.messages),
                        ts=_now(),
                    )
                )
            result = await self._compactor.prepare_context(
                context.messages,
                self._provider,
                system=system,
                tool_schemas=schemas,
                outputs_dir=self._run_path / "outputs" if self._run_path else None,
            )
            if result.compacted:
                if self._memory is not None:
                    try:
                        snapshot, system = refresh_prompt(run, self._memory)
                    except (MemoryError, ValueError) as exc:
                        context.mark_interrupted(f"memory_refresh_failed: {exc}")
                        self._save()
                        result.compacted = False
                        result.applied_stages = []
                        result.reason = run.reason or "memory_refresh_failed"
                        await publish_compaction(
                            self._bus, self._session_id, run.run_id, result, self._run_path,
                            report_failure=True,
                        )
                        return False
                    run.memory_snapshot = snapshot
                    run.system_prompt = system
                context.messages = result.messages
            report_failure = pressure or result.reason not in ("below_threshold", "auto_disabled")
        # 旧前缀改变时重新选择估算方式，不能继续套用失效的 Usage 锚点。
        counter = self._estimator.select(system, context.messages, schemas)
        if counter.request(system, context.messages, schemas) > config.input_limit:
            context.mark_interrupted("context_budget_exceeded")
            self._save()
            if result is not None:
                result.reason = "context_budget_exceeded"
                await publish_compaction(
                    self._bus, self._session_id, run.run_id, result, self._run_path,
                    report_failure=True,
                )
            return False
        if result is not None:
            if result.compacted:
                self._save()
            await publish_compaction(
                self._bus, self._session_id, run.run_id, result, self._run_path,
                report_failure=report_failure,
            )
        return True

    def _save(self) -> None:
        self._budget.tick()
        if self._persistence is not None:
            self._persistence.save()


# 空闲会话的入口显式使用 SessionStore，不借用已结束 Run 的预算和保存对象。
async def compact_session(
    store: SessionStore,
    session_id: str,
    bus: EventBus,
    config: CompactionConfig,
    provider_factory: Callable[[], LLMProvider],
    focus: str,
    *, memory_config: MemoryConfig | None = None,
) -> SessionCompactResult:
    cp = store.ensure_checkpoint(session_id)
    run_id = cp.run.run_id if cp.run else ""
    run_path = store.runs_dir(session_id) / (run_id or "manual")
    estimator = TokenEstimator(
        cp.run.provider if cp.run else "",
        cp.run.model if cp.run else "",
        cp.token_estimation,
    )
    managed: ManagedProvider | None = None

    def summary_provider() -> LLMProvider:
        nonlocal managed
        managed = ManagedProvider(provider_factory(), estimator, config)
        return managed

    await bus.publish_safe(
        ContextCompactionStartedEvent(
            session_id=session_id,
            run_id=run_id,
            original_tokens=estimate_bytes(cp.messages),
            ts=_now(),
        )
    )
    result = await Compactor(config, estimator).prepare_context(
        cp.messages,
        focus=focus,
        manual=True,
        outputs_dir=run_path / "outputs",
        system=cp.run.system_prompt if cp.run else "",
        tool_schemas=visible_schemas(cp.run) if cp.run else [],
        provider_factory=summary_provider,
    )
    if result.compacted:
        if cp.run is not None:
            memory = MemoryStore(
                Path(cp.run.workspace), memory_config,
                project=Path(cp.run.memory_project_root) if cp.run.memory_project_root else None,
            )
            try:
                snapshot, system = refresh_prompt(cp.run, memory)
            except (MemoryError, ValueError) as exc:
                result.compacted = False
                result.applied_stages = []
                result.reason = f"memory_refresh_failed: {exc}"
            else:
                cp.run.memory_snapshot = snapshot
                cp.run.system_prompt = system
                schemas = visible_schemas(cp.run)
                counter = estimator.select(system, result.messages, schemas)
                required = counter.request(system, result.messages, schemas)
                if required > config.input_limit:
                    result.reason = "context_budget_exceeded"
        if result.compacted:
            cp.messages = result.messages
    # 摘要未被采用也可能取得有效 Usage，和工作历史在这里一起提交。
    if result.compacted or managed is not None:
        store.save_checkpoint(cp)
    log.info("manual compaction actual_tokens=%d", managed.manual_tokens if managed else 0)
    await publish_compaction(bus, session_id, run_id, result, run_path, report_failure=True)
    return SessionCompactResult(
        summary_tokens=result.summary_tokens,
        saved_tokens=max(0, result.original_token_estimate - result.remaining_token_estimate),
        compacted=result.compacted,
        original_tokens=result.original_token_estimate,
        remaining_tokens=result.remaining_token_estimate,
        reason=result.reason,
        applied_stages=result.applied_stages,
        llm_used=result.llm_used,
    )
