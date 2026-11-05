from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.compact.manager import ContextManager
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.loop import AgentLoop
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
) -> AgentLoop:
    estimator = TokenEstimator(
        context.run.provider, context.run.model,
        persistence.checkpoint.token_estimation if persistence else None,
    )
    budget = RunBudget(context.run)
    managed = ManagedProvider(
        provider, estimator, compaction, budget=budget, persistence=persistence, event_bus=bus,
    )
    context_manager = ContextManager(
        context, bus, compaction, estimator, compactor=Compactor(compaction, estimator),
        provider=managed, budget=budget, persistence=persistence,
        session_id=session_id, run_path=run_path,
    )
    tools = ToolBatchExecutor(
        context, registry, bus, budget, permission_manager=permission_manager,
        persistence=persistence, session_id=session_id, subagents=subagents,
    )
    return AgentLoop(
        managed, bus, context=context, tools=tools, context_manager=context_manager,
        budget=budget, persistence=persistence,
    )
