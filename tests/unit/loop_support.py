from __future__ import annotations

from pathlib import Path
from typing import Any

from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.compact.manager import ContextManager
from kama_claude.core.config import CompactionConfig
from kama_claude.core.loop import AgentLoop
from kama_claude.core.run_budget import RunBudget
from kama_claude.core.tools.batch import ToolBatchExecutor


# 低层循环测试直接注入假模型，不叠加请求重试和记账包装。
def make_loop(
    provider: Any, registry: Any, bus: Any, *, context: Any,
    permission_manager: Any = None, compactor: Any = None,
    compaction: CompactionConfig | None = None, run_path: Path | None = None,
    session_id: str = "", persistence: Any = None, budget: Any = None, estimator: Any = None,
) -> AgentLoop:
    config = compaction or CompactionConfig()
    budget = budget or RunBudget(context.run)
    estimator = estimator or TokenEstimator(context.run.provider, context.run.model)
    tools = ToolBatchExecutor(
        context, registry, bus, budget, permission_manager=permission_manager,
        session_id=session_id, persistence=persistence,
    )
    context_manager = ContextManager(
        context, bus, config, estimator, compactor=compactor, provider=provider,
        budget=budget, persistence=persistence, session_id=session_id, run_path=run_path,
    )
    return AgentLoop(
        provider, bus, context=context, tools=tools, context_manager=context_manager,
        budget=budget, persistence=persistence,
    )
