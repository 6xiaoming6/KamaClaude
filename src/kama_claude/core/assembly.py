from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.compact.manager import ContextManager
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.config import CompactionConfig, MemoryConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.loop import AgentLoop
from kama_claude.core.memory.context import DEFAULT_SYSTEM, initialize_prompt
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.run_budget import RunBudget
from kama_claude.core.tools.batch import ToolBatchExecutor
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.persistence import RunPersistence
    from kama_claude.core.permissions.manager import PermissionManager
    from kama_claude.core.subagent.tracking import SubagentTracker


# 主、子 Agent 共用装配入口；只返回 Loop，不包装或复制运行状态。
def create_agent_loop(
    provider: LLMProvider, registry: ToolRegistry, bus: EventBus, *,
    context: ExecutionContext, compaction: CompactionConfig,
    permission_manager: PermissionManager | None = None,
    persistence: RunPersistence | None = None, session_id: str = "",
    run_path: Path | None = None, subagents: SubagentTracker | None = None,
    memory_config: MemoryConfig | None = None,
    budget: RunBudget | None = None,
) -> AgentLoop:
    memory = MemoryStore(
        Path(context.run.workspace), memory_config,
        project=Path(context.run.memory_project_root) if context.run.memory_project_root else None,
        context_budget=compaction,
    )
    if not context.run.system_prompt:
        initialize_prompt(context.run, memory, context.system_prompt_override or DEFAULT_SYSTEM)
    estimator = TokenEstimator(
        context.run.provider, context.run.model,
        persistence.checkpoint.token_estimation if persistence else None,
    )
    budget = budget or RunBudget(context.run, persistence=persistence)
    managed = ManagedProvider(
        provider, estimator, compaction, budget=budget, persistence=persistence, event_bus=bus,
    )
    context_manager = ContextManager(
        context, bus, compaction, estimator, compactor=Compactor(compaction, estimator),
        provider=managed, budget=budget, persistence=persistence,
        session_id=session_id, run_path=run_path,
        memory=memory,
    )
    tools = ToolBatchExecutor(
        context, registry, bus, budget, permission_manager=permission_manager,
        persistence=persistence, session_id=session_id, subagents=subagents,
        output_preparer=ToolOutputPreparer(compaction, run_path),
    )
    return AgentLoop(
        managed, bus, context=context, tools=tools, context_manager=context_manager,
        budget=budget, persistence=persistence,
    )
