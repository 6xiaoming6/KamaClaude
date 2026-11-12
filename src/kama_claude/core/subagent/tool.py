from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from kama_claude.core.agents.loader import AgentProfile, AgentProfileError
from kama_claude.core.assembly import create_agent_loop
from kama_claude.core.bus.events import SubagentStartedEvent
from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.config import (
    AgentConfig,
    CompactionConfig,
    ExecutionConfig,
    MemoryConfig,
    SubagentConfig,
)
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.execution.factory import create_executor
from kama_claude.core.execution.model import CommandBackend, snapshot_execution
from kama_claude.core.execution.owner import SandboxOwner
from kama_claude.core.llm.images import supports_images
from kama_claude.core.loop import AgentLoop
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.runs import new_run_id
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.skills.tool import LoadSkillTool
from kama_claude.core.subagent.query import AgentCancelTool, AgentResultTool
from kama_claude.core.subagent.registry import BackgroundTaskRegistry, SubagentLimitError
from kama_claude.core.subagent.report import HANDOFF_INSTRUCTIONS
from kama_claude.core.subagent.roles import BACKGROUND_TOOLS, RoleCatalog, effective_tools
from kama_claude.core.subagent.tracking import SubagentTracker
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult
from kama_claude.core.tools.builtin.finish import FinishTool
from kama_claude.core.tools.builtin.memory import MemoryReadTool, MemoryWriteTool
from kama_claude.core.tools.factory import builtin_tools
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
        description=(
            "Self-contained task: objective, relevant paths, constraints, required output and "
            "completion criteria. Parent conversation is not inherited."
        ),
    )
    run_in_background: bool = Field(default=False, description="Return a Run ID for agent_result.")
    subagent_type: str = Field(
        default="explorer",
        description="Role from the frozen catalog; default explorer is read-only.",
    )


# 在隔离的冷启动上下文中派生子 agent，支持前台阻塞和后台并行两种模式
class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    # 前台等待可能很长，父子 Loop 已统一检查剩余时间，不再套普通工具的 120 秒。
    timeout_s: ClassVar[float | None] = None
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
        " Parallel work must be read-only. Executor and any write-capable role run in foreground. "
        "Delegate independent tasks with clear scope and evidence requirements. "
        "Collect all background results before finish."
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
        execution: ExecutionConfig | None = None,
        agent_config: AgentConfig | None = None,
        compaction: CompactionConfig | None = None,
        provider_name: str = "custom",
        model: str = "",
        provider_factory: Callable[[str], LLMProvider] | None = None,
        tool_whitelist: list[str] | None = None,
        root_run_id: str | None = None,
        subagent_config: SubagentConfig | None = None,
        memory_config: MemoryConfig | None = None,
        memory_project_root: Path | None = None,
        parent_budget: RunBudget | None = None,
        stop_event: asyncio.Event | None = None,
        sandbox_owner: SandboxOwner | None = None,
    ) -> None:
        self._provider = provider
        self._parent_budget = parent_budget
        self._stop_event = stop_event
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
        self._runs_dir = runs_dir.resolve()
        self._session_id = session_id
        self._depth = depth
        self._workspace = (workspace if workspace is not None else Path.cwd()).resolve()
        self._execution = execution or ExecutionConfig()
        self._sandbox_owner = sandbox_owner or SandboxOwner()
        self._profiles = RoleCatalog(self._workspace)
        self.description = (
            type(self).description + "\nAvailable roles:\n" + self._profiles.description
        )
        self._memory_config = memory_config or MemoryConfig()
        self._memory_project_root = MemoryStore(
            self._workspace, self._memory_config, project=memory_project_root,
            context_budget=self._compaction,
        ).project_root
        self._root_run_id = root_run_id or parent_run_id
        self._subagent_config = replace(subagent_config or SubagentConfig())
        self._task_registry.bind_root(
            self._root_run_id, session_id, self._subagent_config, runs_dir=self._runs_dir,
        )

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
            profile = self._profiles.get(p.subagent_type)
            allowed = effective_tools(profile, self._tool_whitelist)
            if p.run_in_background and (
                profile.name == "executor" or allowed is None or not allowed <= BACKGROUND_TOOLS
            ):
                return ToolResult(
                    "Background subagents must have only read-only tools. "
                    "Use explorer/reviewer, or run this role in foreground.",
                    is_error=True, error_type="invalid_operation",
                )
            model = profile.model or self._model
            provider = self._provider
            if model != self._model:
                if self._provider_factory is None:
                    raise AgentProfileError("this provider does not support a model override")
                provider = self._provider_factory(model)
        except (AgentProfileError, ValueError, RuntimeError) as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="configuration_error")

        child_run_id = new_run_id()
        if self._parent_budget is not None:
            try:
                self._parent_budget.check()
            except BudgetExceeded as exc:
                return ToolResult(str(exc), is_error=True, error_type="budget")
        try:
            self._task_registry.reserve(child_run_id, self._root_run_id, self._session_id)
        except SubagentLimitError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="resource_limit")

        task: asyncio.Task[None] | None = None
        executor: CommandBackend | None = None
        try:
            child_context = ExecutionContext(
                run=RunCheckpoint(
                    execution=snapshot_execution(self._execution),
                    run_id=child_run_id, goal=p.prompt, max_steps=self._agent_config.max_steps,
                    model=model, provider=self._provider_name, workspace=str(self._workspace),
                    completion_mode="explicit",
                    max_run_seconds=self._agent_config.max_run_seconds,
                    max_total_tokens=self._agent_config.max_total_tokens,
                    subagent_max_active=self._subagent_config.max_active,
                    subagent_max_total=self._subagent_config.max_total,
                    repeat_failure_limit=self._agent_config.repeat_failure_limit,
                    no_progress_limit=self._agent_config.no_progress_limit,
                    memory_project_root=str(self._memory_project_root),
                ),
                system_prompt_override=(
                    profile.system_prompt or
                    "You are a helpful AI assistant. Use tools to complete the assigned sub-task."
                ) + HANDOFF_INSTRUCTIONS,
                stop_event=self._stop_event,
            )

            child_bus = EventBus()
            executor = create_executor(
                self._workspace, self._execution, run_id=child_run_id,
                owner=self._sandbox_owner, bus=child_bus,
            )

            # 将子 bus 所有事件桥接到父 bus，TUI 据此渲染嵌套进度
            async def _bridge(event: BaseModel) -> None:
                await self._parent_bus.publish_safe(event)

            child_bus.subscribe(_bridge)
            tracker = SubagentTracker(child_context.run)
            child_bus.subscribe(tracker.handle_event)
            budget = RunBudget(child_context.run, parent=self._parent_budget)
            child_registry = self._build_child_registry(
                child_bus, child_run_id, profile, provider=provider, model=model,
                run=child_context.run,
                budget=budget,
                command_executor=executor,
            )
            child_run_path = self._runs_dir / child_run_id
            child_run_path.mkdir(parents=True, exist_ok=True)
            child_loop = create_agent_loop(
                provider, child_registry, child_bus, context=child_context,
                compaction=self._compaction, permission_manager=self._permission_manager,
                session_id=self._session_id, run_path=child_run_path,
                memory_config=self._memory_config, subagents=tracker, budget=budget,
            )
            # 先登记再发布开始事件；门闩保证父任务先记录派生关系，子任务才执行。
            start = asyncio.Event()
            task = asyncio.create_task(self._run_child(
                child_loop, child_context, child_bus, child_run_path, start, executor,
            ))
            entry = self._task_registry.register(
                child_run_id, task, child_context, session_id=self._session_id,
                parent_run_id=self._parent_run_id, bus=self._parent_bus,
                root_run_id=self._root_run_id, run_path=child_run_path,
            )
            await self._parent_bus.publish_safe(SubagentStartedEvent(
                run_id=child_run_id, parent_run_id=self._parent_run_id,
                description=p.description, ts=_now(),
            ))
            start.set()
        except BaseException:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if executor is not None:
                await executor.aclose()
            raise
        finally:
            self._task_registry.release(child_run_id, self._root_run_id, self._session_id)

        if p.run_in_background:
            return ToolResult(content=(
                f"Subagent started in background. run_id={child_run_id}. "
                f"Use agent_result(run_ids=['{child_run_id}']) to retrieve result."
            ))

        # 前台取消继续传递给子任务；结果与后台查询使用同一份已保存的交接。
        await task
        assert entry.result is not None
        return entry.result.as_tool_result()

    # 前后台共用收尾，异常与取消也报告终态，通知异常不改变执行结果
    async def _run_child(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
        start: asyncio.Event,
        executor: CommandBackend,
    ) -> None:
        try:
            await start.wait()
            async with EventWriter(run_path / "events.jsonl", run_id=context.run.run_id) as writer:
                writer.subscribe(bus)
                await loop.run()
        except asyncio.CancelledError:
            if any(tool.status in ("executing", "unknown") for tool in context.run.tools):
                context.mark_interrupted("cancelled")
            elif not context.is_done():
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
            try:
                if context.run.reason == "user_stopped":
                    await self._task_registry.wait_children(context.run.run_id, self._session_id)
                else:
                    await self._task_registry.cancel_children(context.run.run_id, self._session_id)
            finally:
                await executor.aclose()
                # 报告落盘后才发布完成通知；失败由注册表转成明确的失败结果。
                await self._task_registry.notify_finished(context.run.run_id)

    # 构造子 registry；基于角色配置过滤工具，深度允许时注册嵌套 SpawnAgentTool
    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        profile: AgentProfile | None,
        *, provider: LLMProvider | None = None, model: str | None = None,
        run: RunCheckpoint | None = None,
        budget: RunBudget | None = None,
        command_executor: CommandBackend | None = None,
    ) -> ToolRegistry:
        from kama_claude.core.task.manager import TaskManager

        allowed = (
            self._tool_whitelist if profile is None
            else effective_tools(profile, self._tool_whitelist)
        )

        # 角色只能进一步缩小父任务明确授权的工具范围
        def _allowed(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        child_task_manager = TaskManager(self._runs_dir / child_run_id / ".tasks")
        for tool in builtin_tools(
            self._workspace, self._execution, child_task_manager,
            image_support=supports_images(self._provider_name, model or self._model),
            command_executor=command_executor,
        ):
            if _allowed(tool.name):
                registry.register(tool)
        memory = MemoryStore(
            self._workspace, self._memory_config, project=self._memory_project_root,
            context_budget=self._compaction,
        )
        for tool in (
            MemoryReadTool(memory), MemoryWriteTool(memory, self._session_id, child_run_id),
        ):
            if _allowed(tool.name):
                registry.register(tool)

        if self._depth < 1 and _allowed("spawn_agent"):
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
                root_run_id=self._root_run_id, subagent_config=self._subagent_config,
                memory_config=self._memory_config,
                memory_project_root=self._memory_project_root,
                workspace=self._workspace,
                execution=self._execution,
                agent_config=self._agent_config, compaction=self._compaction,
                provider_name=self._provider_name,
                model=model if model is not None else self._model,
                provider_factory=self._provider_factory,
                tool_whitelist=sorted(allowed) if allowed is not None else None,
                parent_budget=budget,
                stop_event=self._stop_event,
                sandbox_owner=self._sandbox_owner,
            )
            registry.register(nested)
        if _allowed("agent_result"):
            registry.register(AgentResultTool(self._task_registry, self._session_id))
        if _allowed("agent_cancel"):
            registry.register(AgentCancelTool(
                self._task_registry, self._session_id, child_run_id,
            ))

        if run is not None:
            if run.completion_mode == "explicit":
                registry.register(FinishTool())
            run.tool_whitelist = sorted(allowed) if allowed is not None else None
            run.skill_catalog = SkillLoader().list_all_skills() if _allowed("load_skill") else []
            if _allowed("load_skill"):
                registry.register(LoadSkillTool(run))

        return registry
