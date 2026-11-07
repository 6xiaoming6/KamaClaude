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
from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.compact.compactor import CompactionResult, Compactor, write_summary
from kama_claude.core.compact.estimator import TokenEstimator, estimate_bytes
from kama_claude.core.compact.request import assemble_messages
from kama_claude.core.config import CompactionConfig, MemoryConfig
from kama_claude.core.discovery.exposure import request_schemas
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.factory import ProviderFactory
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.llm.tool_loading import referenced_tools
from kama_claude.core.memory.context import refresh_prompt
from kama_claude.core.memory.model import MemoryError, MemorySnapshot
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.skills.context import retain_skill

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
    if result.compacted and result.summary_text and run_path is not None:
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
                preserved_messages=result.preserved_messages,
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


# 自动、手动入口共用采用前校验；此处不修改 Run，也不提交半套候选。
def validate_candidate(
    run: RunCheckpoint, result: CompactionResult, memory: MemoryStore | None,
    config: CompactionConfig, estimator: TokenEstimator, *, system: str | None = None,
) -> tuple[MemorySnapshot | None, list[str]]:
    retained = retain_skill(result.messages, run.active_skill)
    result.preserved_messages += len(retained) - len(result.messages)
    result.messages = retained
    snapshot = refresh_prompt(run, memory)[0] if memory is not None else run.memory_snapshot
    loaded = list(run.loaded_tool_names)
    if run.tool_loading_mode in ("deferred", "on_demand"):
        required = set(referenced_tools(result.messages))
        required.update(
            str(block.get("name")) for message in result.messages
            if message.get("role") == "assistant" and isinstance(message.get("content"), list)
            for block in message["content"] if block.get("type") == "tool_use"
            and block.get("name") in run.mcp_tool_names
        )
        if not required <= set(run.mcp_tool_names):
            raise ValueError("tool_reference_outside_frozen_catalog")
        loaded = [str(schema["name"]) for schema in run.tool_schemas
                  if schema["name"] in required]
        definitions = [s for s in run.tool_schemas if s["name"] in loaded]
        if estimate_bytes(definitions) > run.max_loaded_schema_tokens:
            raise ValueError("retained_tool_schema_budget_exceeded")
    # 临时副本仅用于投影工具列表，不产生第二份运行状态或保存来源。
    candidate_run = run.model_copy(update={"loaded_tool_names": loaded})
    schemas = request_schemas(candidate_run)
    request = assemble_messages(run, result.messages, snapshot=snapshot)
    system = run.system_prompt if system is None else system
    counter = estimator.select(system, request, schemas)
    required_tokens = counter.request(system, request, schemas)
    if required_tokens > config.input_limit:
        raise ValueError("context_budget_exceeded")
    after = counter.raw_request(system, request, schemas)
    # 刷新索引也可能抵消摘要收益，最终候选仍须满足同一缩减条件。
    if after > result.original_token_estimate * 0.9:
        result.compacted = False
        result.applied_stages = []
        result.reason = "insufficient_reduction"
        result.remaining_token_estimate = result.original_token_estimate
        return run.memory_snapshot, list(run.loaded_tool_names)
    result.remaining_token_estimate = after
    return snapshot, loaded


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

    # 请求前只判断压力；输出预览已在工具入口处理，不再改写旧结果。
    async def prepare(self, system: str, schemas: list[dict[str, Any]]) -> bool:
        context, run, config = self.context, self.context.run, self.config
        retained = retain_skill(context.messages, run.active_skill)
        if retained is not context.messages:
            context.messages = retained
            self._save()
        request = assemble_messages(run, context.messages)
        counter = self._estimator.select(system, request, schemas)
        pressure = config.auto_threshold > 0 and counter.request(
            system, request, schemas,
        ) >= config.trigger_tokens
        result: CompactionResult | None = None
        if pressure and self._compactor is not None:
            await self._bus.publish_safe(ContextCompactionStartedEvent(
                session_id=self._session_id, run_id=run.run_id,
                original_tokens=counter.request(system, request, schemas), ts=_now(),
            ))
            result = await self._compactor.prepare_context(
                context.messages, self._provider, system=system, tool_schemas=schemas,
                context_messages=assemble_messages(run, []),
            )
            if result.compacted:
                try:
                    snapshot, loaded = validate_candidate(
                        run, result, self._memory, config, self._estimator, system=system,
                    )
                except (MemoryError, ValueError) as exc:
                    result.compacted = False
                    result.applied_stages = []
                    result.reason = (
                        f"memory_refresh_failed: {exc}"
                        if isinstance(exc, MemoryError) else str(exc)
                    )
                    result.remaining_token_estimate = result.original_token_estimate
                    context.mark_interrupted(result.reason)
                    self._save()
                    await publish_compaction(
                        self._bus, self._session_id, run.run_id, result, self._run_path,
                        report_failure=True,
                    )
                    return False
                if result.compacted:
                    previous = context.messages, run.memory_snapshot, run.loaded_tool_names
                    context.messages = result.messages
                    run.memory_snapshot, run.loaded_tool_names = snapshot, loaded
                    try:
                        self._save()
                    except BaseException:
                        context.messages, run.memory_snapshot, run.loaded_tool_names = previous
                        if self._persistence is not None:
                            self._persistence.checkpoint.messages = context.messages
                        raise
                    # 非原生兼容模式的可见列表也必须与预算和实际请求保持一致。
                    schemas[:] = request_schemas(run) if run.schemas_frozen else schemas
        request = assemble_messages(run, context.messages)
        counter = self._estimator.select(system, request, schemas)
        if counter.request(system, request, schemas) > config.input_limit:
            context.mark_interrupted("context_budget_exceeded")
            self._save()
            if result is not None:
                result.reason = "context_budget_exceeded"
        if result is not None:
            await publish_compaction(
                self._bus, self._session_id, run.run_id, result, self._run_path,
                report_failure=not result.compacted or context.is_done(),
            )
        return not context.is_done()

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
    if cp.run is not None:
        retained = retain_skill(cp.messages, cp.run.active_skill)
        if retained is not cp.messages:
            cp.messages = retained
            store.save_checkpoint(cp)
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
        provider = provider_factory()
        if cp.run is not None:
            factory = ProviderFactory(provider, cp.run.model)
            if factory.identity()[0] != cp.run.provider:
                raise ValueError("provider changed; restore the original backend for compaction")
            provider = factory.for_model(cp.run.model)
        managed = ManagedProvider(provider, estimator, config)
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
        context_messages=assemble_messages(cp.run, []) if cp.run else [],
        system=cp.run.system_prompt if cp.run else "",
        tool_schemas=request_schemas(cp.run) if cp.run else [],
        provider_factory=summary_provider,
    )
    if result.compacted:
        if cp.run is not None:
            memory = MemoryStore(
                Path(cp.run.workspace), memory_config,
                project=Path(cp.run.memory_project_root) if cp.run.memory_project_root else None,
                context_budget=config,
            )
            try:
                snapshot, loaded = validate_candidate(cp.run, result, memory, config, estimator)
            except (MemoryError, ValueError) as exc:
                result.compacted = False
                result.applied_stages = []
                result.reason = str(exc)
                result.remaining_token_estimate = result.original_token_estimate
            else:
                cp.run.memory_snapshot, cp.run.loaded_tool_names = snapshot, loaded
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
        preserved_messages=result.preserved_messages,
    )
