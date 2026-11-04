from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from kama_claude.core.agents.loader import AgentProfile, AgentProfileError, AgentProfileLoader
from kama_claude.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.config import AgentConfig, CompactionConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.loop import AgentLoop
from kama_claude.core.run_budget import RunBudget
from kama_claude.core.runs import new_run_id
from kama_claude.core.subagent.registry import ActiveSubagent, BackgroundTaskRegistry
from kama_claude.core.subagent.result import SubagentResult
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.edit_file import EditFileTool
from kama_claude.core.tools.builtin.find_files import FindFilesTool
from kama_claude.core.tools.builtin.list_dir import ListDirTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.search_text import SearchTextTool
from kama_claude.core.tools.builtin.task_create import TaskCreateTool
from kama_claude.core.tools.builtin.task_get import TaskGetTool
from kama_claude.core.tools.builtin.task_list import TaskListTool
from kama_claude.core.tools.builtin.task_update import TaskUpdateTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.permissions.manager import PermissionManager

log = logging.getLogger(__name__)


# 返回子任务事件的 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(ToolParams):
    description: str = Field(min_length=1, max_length=200, description="Short task title for TUI.")
    prompt: str = Field(
        min_length=1, max_length=262_144,
        description="Complete task and necessary context; parent conversation is not inherited.",
    )
    run_in_background: bool = Field(default=False, description="Return a Run ID for agent_result.")
    subagent_type: str = Field(
        default="", description="planner/executor/reviewer or a custom role; empty uses defaults.",
    )


# 在隔离的冷启动上下文中派生子 agent，支持前台阻塞和后台并行两种模式
class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
    )
    input_schema = SpawnAgentParams.model_json_schema()
    params_model = SpawnAgentParams

    # 构造 SpawnAgentTool；depth=0 表示根 agent，最大允许嵌套深度为 2
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: BackgroundTaskRegistry,
        runs_dir: Path,
        session_id: str,
        depth: int = 0,
        workspace: Path | None = None,
        agent_config: AgentConfig | None = None,
        compaction: CompactionConfig | None = None,
        provider_name: str = "custom",
        model: str = "",
        provider_factory: Callable[[str], LLMProvider] | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> None:
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._agent_config = replace(agent_config or AgentConfig(), max_steps=max_steps)
        self._compaction = compaction or CompactionConfig()
        self._provider_name = provider_name
        self._model = model
        self._provider_factory = provider_factory
        self._tool_whitelist = set(tool_whitelist) if tool_whitelist is not None else None
        self._task_registry = task_registry
        self._runs_dir = runs_dir
        self._session_id = session_id
        self._depth = depth
        self._workspace = (workspace if workspace is not None else Path.cwd()).resolve()
        self._profiles = AgentProfileLoader(self._workspace)

    # 派生子 agent，前台时阻塞直到完成并返回结果，后台时立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)

        if self._depth >= 2:
            return ToolResult(
                content="Subagent nesting limit (2) reached; cannot spawn further subagents.",
                is_error=True,
                error_type="runtime_error",
            )

        try:
            profile = self._profiles.load(p.subagent_type) if p.subagent_type else None
            model = profile.model if profile and profile.model else self._model
            provider = self._provider
            if model != self._model:
                if self._provider_factory is None:
                    raise AgentProfileError("this provider does not support a model override")
                provider = self._provider_factory(model)
        except (AgentProfileError, ValueError, RuntimeError) as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="configuration_error")

        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run=RunCheckpoint(
                run_id=child_run_id, goal=p.prompt, max_steps=self._agent_config.max_steps,
                model=model, provider=self._provider_name, workspace=str(self._workspace),
                max_run_seconds=self._agent_config.max_run_seconds,
                max_total_tokens=self._agent_config.max_total_tokens,
                repeat_failure_limit=self._agent_config.repeat_failure_limit,
            ),
            system_prompt_override=profile.system_prompt if profile else None,
        )

        child_bus = EventBus()

        # 将子 bus 所有事件桥接到父 bus，TUI 据此渲染嵌套进度
        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish_safe(event)

        child_bus.subscribe(_bridge)

        # 为子agent构建专门的tool_registry
        child_registry = self._build_child_registry(
            child_bus, child_run_id, profile, provider=provider, model=model,
        )
        child_run_path = self._runs_dir / child_run_id
        child_run_path.mkdir(parents=True, exist_ok=True)
        # 每个子任务独立记账，普通请求、摘要与重试共用同一预算
        estimator = TokenEstimator(self._provider_name, model)
        budget = RunBudget(child_context.run)
        managed = ManagedProvider(
            provider, estimator, self._compaction, budget=budget, event_bus=child_bus,
        )
        # 复用 S9 的请求前四层整理，归档保存在子 Run 的 outputs 中
        child_loop = AgentLoop(
            managed,
            child_registry,
            child_bus,
            context=child_context,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
            compactor=Compactor(self._compaction, estimator),
            compaction=self._compaction, run_path=child_run_path,
            budget=budget, estimator=estimator,
        )

        await self._parent_bus.publish_safe(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        # 前后台共用运行与收尾；Core 统一持有任务，查询权限绑定所属会话
        task = asyncio.create_task(self._run_child(
            child_loop, child_context, child_bus, child_run_path,
        ))
        self._task_registry.register(
            child_run_id, task, child_context, session_id=self._session_id,
            parent_run_id=self._parent_run_id, bus=self._parent_bus,
        )
        if p.run_in_background:
            return ToolResult(
                content=(
                    f"Subagent started in background. run_id={child_run_id}. "
                    f"Use agent_result(run_id='{child_run_id}') to retrieve result."
                )
            )

        # 前台调用等待同一 Task；取消会向子 Loop 和工具清理传递
        await task
        return SubagentResult.from_run(
            child_context.run, self._session_id, self._parent_run_id,
        ).as_tool_result()

    # 前后台共用收尾，异常与取消也报告终态，通知异常不改变执行结果
    async def _run_child(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
    ) -> None:
        try:
            async with EventWriter(run_path / "events.jsonl") as writer:
                writer.subscribe(bus)
                await loop.run()
        except asyncio.CancelledError:
            if any(tool.status in ("executing", "unknown") for tool in context.run.tools):
                context.mark_interrupted("cancelled")
            else:
                context.mark_failed("cancelled")
            raise
        except Exception as exc:
            log.exception("subagent failed run_id=%s", context.run.run_id)
            context.run.result = str(exc)
            if any(tool.status in ("executing", "unknown") for tool in context.run.tools):
                context.mark_interrupted("subagent_error")
            else:
                context.mark_failed("subagent_error")
        finally:
            run = context.run
            await self._parent_bus.publish_safe(SubagentFinishedEvent(
                run_id=run.run_id, parent_run_id=self._parent_run_id, status=run.status,
                reason=run.reason, actual_tokens=run.actual_tokens,
                estimated_tokens=run.estimated_tokens, elapsed_seconds=run.elapsed_seconds,
                usage_complete=run.usage_complete, ts=_now(),
            ))

    # 构造子 registry；基于角色配置过滤工具，深度允许时注册嵌套 SpawnAgentTool
    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        profile: AgentProfile | None,
        *, provider: LLMProvider | None = None, model: str | None = None,
    ) -> ToolRegistry:
        from kama_claude.core.task.manager import TaskManager

        allowed = self._tool_whitelist
        if profile is not None and profile.allowed_tools is not None:
            requested = set(profile.allowed_tools)
            allowed = requested if allowed is None else allowed & requested

        # 角色只能进一步缩小父任务明确授权的工具范围
        def _allowed(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        _all_tools = [
            ReadFileTool(self._workspace),
            BashTool(self._workspace),
            WriteFileTool(self._workspace),
            ListDirTool(self._workspace),
            FindFilesTool(self._workspace),
            SearchTextTool(self._workspace),
            EditFileTool(self._workspace),
        ]
        for t in _all_tools:
            if _allowed(t.name):
                registry.register(t)

        child_task_manager = TaskManager(self._runs_dir / child_run_id / ".tasks")
        for t in [
            TaskCreateTool(child_task_manager),
            TaskUpdateTool(child_task_manager),
            TaskListTool(child_task_manager),
            TaskGetTool(child_task_manager),
        ]:
            if _allowed(t.name):
                registry.register(t)

        if self._depth < 1:
            nested = SpawnAgentTool(
                provider=provider if provider is not None else self._provider,
                parent_bus=child_bus,
                parent_run_id=child_run_id,
                permission_manager=self._permission_manager,
                max_steps=self._agent_config.max_steps,
                task_registry=self._task_registry,
                runs_dir=self._runs_dir,
                session_id=self._session_id,
                depth=self._depth + 1,
                workspace=self._workspace,
                agent_config=self._agent_config, compaction=self._compaction,
                provider_name=self._provider_name,
                model=model if model is not None else self._model,
                provider_factory=self._provider_factory,
                tool_whitelist=sorted(allowed) if allowed is not None else None,
            )
            if _allowed("spawn_agent"):
                registry.register(nested)
            if _allowed("agent_result"):
                registry.register(AgentResultTool(self._task_registry, self._session_id))

        return registry


class AgentResultParams(ToolParams):
    run_id: str = Field(min_length=1, description="Child Run ID from this session's spawn_agent.")


# 查询后台 subagent 的执行状态和最终结果
class AgentResultTool(BaseTool):
    name = "agent_result"
    description = (
        "Retrieve the result of a background sub-agent previously started with spawn_agent. "
        "Returns 'still running' if the sub-agent has not yet completed."
    )
    input_schema = AgentResultParams.model_json_schema()
    params_model = AgentResultParams

    # 初始化，持有共享的后台任务注册表
    def __init__(self, task_registry: BackgroundTaskRegistry, session_id: str) -> None:
        self._task_registry = task_registry
        self._session_id = session_id

    # 查询指定 run_id 的后台任务状态，返回结果或错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        entry = self._task_registry.get(p.run_id, self._session_id)
        if entry is None:
            return ToolResult(
                content=f"Unknown run_id for this session: {p.run_id} (missing or expired).",
                is_error=True,
                error_type="runtime_error",
            )
        if isinstance(entry, ActiveSubagent):
            return ToolResult(content="still running")
        return entry.as_tool_result()
