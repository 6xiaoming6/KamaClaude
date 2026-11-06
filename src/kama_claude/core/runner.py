from __future__ import annotations

import asyncio
import json
import logging
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.assembly import create_agent_loop
from kama_claude.core.bus.events import (
    RunFinishedEvent,
    RunInterruptedEvent,
    RunResumedEvent,
    RunStartedEvent,
)
from kama_claude.core.checkpoint.checkpoint import Checkpoint, CheckpointError, RunCheckpoint
from kama_claude.core.checkpoint.persistence import RunPersistence
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.discovery.exposure import load_tools
from kama_claude.core.discovery.model import digest
from kama_claude.core.discovery.tool import SearchTool
from kama_claude.core.events.bus import EventBus, EventHandler
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.factory import ProviderFactory
from kama_claude.core.llm.tool_loading import referenced_tools
from kama_claude.core.loop import AgentLoop
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.memory.context import DEFAULT_SYSTEM, initialize_prompt
from kama_claude.core.memory.paths import project_root
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runs import RUNS_DIR, new_run_id
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills.context import retain_skill
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.skills.model import SkillSnapshot
from kama_claude.core.skills.tool import LoadSkillTool
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tool import AgentResultTool, SpawnAgentTool
from kama_claude.core.subagent.tracking import SubagentTracker
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.builtin import (
    MemoryReadTool,
    MemoryWriteTool,
)
from kama_claude.core.tools.builtin.finish import FinishTool
from kama_claude.core.tools.factory import builtin_tools
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.writer import TraceWriter


# 返回当前事件时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


# 每次完整的[用户输入-结束]run都会创建一个新的runner
class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: KamaConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        task_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        self._config = config
        self._bus = bus  # 现在事件总线放到了core里面，所有事件
        self._providers = ProviderFactory(
            provider, config.llm.default_model, trace,
            include_payload=config.trace.include_llm_payload,
        )
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        # Core 注入跨 Run 注册表；独立 Runner 使用自己的注册表
        self._task_registry = (
            task_registry if task_registry is not None else BackgroundTaskRegistry()
        )

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    # 根据白名单注册工具，按需注入其他额外的工具
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        workspace: Path | None = None,
        run: RunCheckpoint | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist is not None else None

        # 没有设置白名单默认注册，设置了白名单就注册白名单里面的工具
        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        workspace = workspace if workspace is not None else Path.cwd()
        for tool in builtin_tools(workspace, self._config.execution, task_manager):
            if _ok(tool.name):
                registry.register(tool)
        memory = MemoryStore(
            workspace, self._config.memory,
            project=Path(run.memory_project_root) if run and run.memory_project_root else None,
        )
        for memory_tool in (
            MemoryReadTool(memory), MemoryWriteTool(memory, session_id, run_id or ""),
        ):
            if _ok(memory_tool.name):
                registry.register(memory_tool)

        if run is not None and run.completion_mode == "explicit":
            registry.register(FinishTool())

        if run is not None and run.skill_catalog is not None and _ok("load_skill"):
            registry.register(LoadSkillTool(run))

        # 创建子agent的工具是否注册
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            self._task_registry.bind_root(
                run_id, session_id, self._config.subagent,
                known_ids=run.subagents if run is not None else (),
            )
            # 恢复后派生的子任务也沿用父 Run 冻结的限制，而非重启时的新配置
            limits = replace(
                self._config.agent, max_steps=run.max_steps,
                max_run_seconds=run.max_run_seconds, max_total_tokens=run.max_total_tokens,
                repeat_failure_limit=run.repeat_failure_limit,
            ) if run is not None else self._config.agent
            provider_name, model = (run.provider, run.model) if run else self._providers.identity()
            child_tools = (
                [str(schema["name"]) for schema in run.tool_schemas]
                if run is not None and run.schemas_frozen else tool_whitelist
            )
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=limits.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                        workspace=workspace,
                        execution=self._config.execution,
                        agent_config=limits, compaction=self._config.compaction,
                        memory_config=self._config.memory,
                        memory_project_root=memory.project_root,
                        provider_name=provider_name, model=model,
                        provider_factory=self._providers.for_model, tool_whitelist=child_tools,
                        root_run_id=run_id, subagent_config=self._config.subagent,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry, session_id))
        # mcp内提供的工具是否注册
        if self._mcp_manager is not None:
            records = []
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
                    records.append(mcp_tool.record)
            if run is not None and run.tool_loading_mode in ("on_demand", "deferred") and records:
                # MCP 全部延迟时搜索入口必需，检索范围仍严格受 MCP 白名单约束。
                if run.tool_loading_mode == "deferred" or _ok("search_tool"):
                    registry.register(SearchTool(run, records, self._mcp_manager.retriever))
        return registry


    # 在接收目标时先提交初始 Run，避免任务尚未调度就退出而丢失目标
    def prepare_checkpoint(
        self,
        cp: Checkpoint,
        session: Session,
        goal: str,
        run_id: str,
        system_prompt: str | None,
        tools: list[str] | None,
        store: SessionStore,
        *, skill: SkillSnapshot | None = None,
    ) -> None:
        context = self._create_context(
            self._new_run_state(goal, run_id, tools),
            cp.messages,
            memory_store=MemoryStore(Path.cwd(), self._config.memory),
            system_prompt_override=system_prompt,
        )
        persistence = RunPersistence(context, cp, session, store)
        # 先登记 Manager 加入的真实输入，再附加 Skill 说明。
        message = cp.messages[-1] if cp.messages else context.messages[0]
        context.run.user_input = str(message["content"])
        persistence.record_message(message["role"], message["content"])
        context.run.active_skill = skill
        context.messages = retain_skill(context.messages, skill)
        persistence.save()

    # 执行一次完整 Run，忽略返回的文字结果
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 按准备、观察、执行和收尾的顺序协调 Run，执行状态仅存在 context.run
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> RunOutcome:
        # 显式传入待恢复检查点才是续跑，普通新消息仍开始新的 Run
        resuming = checkpoint is not None
        context, persistence = self._prepare_context(
            goal, run_id=run_id, session=session, store=store,
            system_prompt_override=system_prompt_override,
            tool_whitelist=tool_whitelist, checkpoint=checkpoint,
        )
        run_path = (
            store.runs_dir(session.id) / context.run.run_id
            if session is not None and store is not None
            else self._runs_dir / context.run.run_id
        )
        run_path.mkdir(parents=True, exist_ok=True)
        bus = self._bus if self._bus is not None else EventBus()
        # 观察者和事件文件只在本轮有效，任何退出路径都会清理
        with ExitStack() as observers:
            for handler in self._extra_handlers:
                bus.subscribe(handler)
                observers.callback(bus.unsubscribe, handler)
            subagents = SubagentTracker(context.run)
            track_children = subagents.handle_event
            bus.subscribe(track_children)
            observers.callback(bus.unsubscribe, track_children)
            if persistence is not None:
                persistence.save()
            async with EventWriter(run_path / "events.jsonl", run_id=context.run.run_id) as writer:
                writer.subscribe(bus)
                if resuming:
                    await bus.publish_safe(
                        RunResumedEvent(
                            run_id=context.run.run_id,
                            session_id=session.id if session else "",
                            step=context.run.step, ts=_now(),
                        )
                    )
                else:
                    await bus.publish_safe(
                        RunStartedEvent(run_id=context.run.run_id, goal=context.run.goal, ts=_now())
                    )

                # 和 S7 一样，在主流程中组装并运行 Loop，直接看见异常处理边界
                cancelled = False
                try:
                    loop = self._build_loop(
                        context, bus, run_path, persistence, resuming=resuming, subagents=subagents,
                    )
                    await loop.run()
                except CheckpointError:
                    # 保存失败立即传播，不再进入正常收尾或发布成功
                    await bus.publish_safe(
                        RunInterruptedEvent(
                            run_id=context.run.run_id,
                            session_id=session.id if session else "",
                            reason="checkpoint_save_failed",
                            revision=persistence.checkpoint.revision if persistence else 0,
                            ts=_now(),
                        )
                    )
                    raise
                # 正常取消，比如ctrl + c取消后，会出现这个异常
                except asyncio.CancelledError:
                    cancelled = True
                    # finish 已提交终态后取消通知，不能把磁盘上的成功降级为中断。
                    if not context.is_done():
                        if persistence is not None:
                            context.mark_interrupted("core_stopped")
                        else:
                            context.mark_failed("cancelled")
                except Exception:
                    logging.getLogger(__name__).exception(
                        "agent run failed run_id=%s", context.run.run_id,
                    )
                    reason = context.run.reason or "run_error"
                    if persistence is not None:
                        context.mark_interrupted(reason)
                    else:
                        context.mark_failed(reason)
                # 没有异常的话就是正常执行完一次agent loop，调用该方法收尾
                return await self._finish_run(context, bus, persistence, cancelled=cancelled)

    # 读取原检查点或新建 Run，返回上下文及可选的持久化绑定
    def _prepare_context(
        self,
        goal: str,
        *,
        run_id: str | None,
        session: Session | None,
        store: SessionStore | None,
        system_prompt_override: str | None,
        tool_whitelist: list[str] | None,
        checkpoint: Checkpoint | None,
    ) -> tuple[ExecutionContext, RunPersistence | None]:
        cp = checkpoint
        if session is not None and store is not None and cp is None:
            if not store.checkpoints.exists(session.id):
                store.write_meta(session)
            cp = store.ensure_checkpoint(session.id)
        # 恢复沿用原 Run，不能重新生成身份或重置步数预算
        if checkpoint is not None and cp is not None and cp.run is not None:
            run = cp.run
        else:
            run_id = run_id or new_run_id()
            run = (
                cp.run
                if cp and cp.run and cp.run.run_id == run_id
                else self._new_run_state(goal, run_id, tool_whitelist)
            )
        context = self._create_context(
            run,
            cp.messages if cp else [],
            memory_store=MemoryStore(
                Path(run.workspace), self._config.memory,
                project=Path(run.memory_project_root) if run.memory_project_root else None,
            ),
            system_prompt_override=system_prompt_override,
        )
        run.status = "running"
        run.reason = None
        persistence = (
            RunPersistence(context, cp, session, store)
            if cp is not None and session is not None and store is not None
            else None
        )
        if persistence is not None and cp is not None and not cp.messages:
            persistence.record_message("user", goal)
        return context, persistence

    # 统一构建新 Run，模型、工作目录与预算只记录在这一份状态中
    def _new_run_state(
        self, goal: str, run_id: str, tool_whitelist: list[str] | None,
    ) -> RunCheckpoint:
        provider, model = self._providers.identity()
        return RunCheckpoint(
            run_id=run_id, goal=goal, provider=provider, model=model, completion_mode="explicit",
            skill_catalog=(SkillLoader().list_all_skills()
                           if tool_whitelist is None or "load_skill" in tool_whitelist else []),
            workspace=str(Path.cwd().resolve()),
            memory_project_root=str(project_root(Path.cwd())),
            max_steps=self._config.agent.max_steps,
            max_run_seconds=self._config.agent.max_run_seconds,
            max_total_tokens=self._config.agent.max_total_tokens,
            repeat_failure_limit=self._config.agent.repeat_failure_limit,
            tool_whitelist=tool_whitelist,
            user_input=goal,
            tool_loading_mode=(
                ("deferred" if provider in ("anthropic", "deepseek") else "on_demand")
                if self._config.tool_search.enabled else "all"
            ),
            max_loaded_schema_tokens=self._config.tool_search.max_loaded_schema_tokens,
        )

    # 新任务与恢复共用 Context 构建，已有系统提示优先使用冻结值
    def _create_context(
        self,
        run: RunCheckpoint,
        messages: list[dict[str, Any]],
        *,
        memory_store: MemoryStore,
        system_prompt_override: str | None,
    ) -> ExecutionContext:
        context = ExecutionContext(
            run=run,
            messages=messages,
            system_prompt_override=system_prompt_override,
        )
        # 没有冻结提示才重新构建，已有值不随记忆文件变化而覆盖
        if not run.system_prompt:
            run.memory_project_root = str(memory_store.project_root)
            initialize_prompt(run, memory_store, system_prompt_override or DEFAULT_SYSTEM)
        return context



    # 组装模型、工具和压缩器，Loop 直接持有 Context 与持久化对象
    def _build_loop(
        self,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
        persistence: RunPersistence | None,
        *,
        resuming: bool,
        subagents: SubagentTracker | None = None,
    ) -> AgentLoop:
        task_manager = TaskManager(run_path / ".tasks")
        provider = self._providers.create(context.run, resuming=resuming)
        session = persistence.session if persistence else None
        store = persistence.store if persistence else None
        registry = self._build_registry(
            task_manager,
            session=session,
            store=store,
            run_id=context.run.run_id,
            provider=provider,
            bus=bus,
            child_runs_dir=store.runs_dir(session.id) if session and store else self._runs_dir,
            session_id=session.id if session else "",
            tool_whitelist=context.run.tool_whitelist,
            workspace=Path(context.run.workspace),
            run=context.run,
        )
        if resuming and context.run.schemas_frozen:
            # 恢复只暴露原清单，新工具不能悄悄改变旧 Run 的工具范围
            frozen_registry = ToolRegistry()
            for schema in context.run.tool_schemas:
                tool = registry.get(str(schema["name"]))
                if tool is not None:
                    frozen_registry.register(tool)
            registry = frozen_registry
        self._save_tool_schemas(context.run, registry, persistence, resuming=resuming)
        if context.run.tool_loading_mode == "deferred":
            # 新 Run 可继承同会话中尚在工作历史里的引用，并计入本轮 Schema 额度。
            _, rejected = load_tools(context.run, referenced_tools(context.messages))
            if rejected:
                raise RuntimeError("historical tool references exceed schema budget; compact first")
        return create_agent_loop(
            provider, registry, bus, context=context, compaction=self._config.compaction,
            permission_manager=self._permission_manager, persistence=persistence,
            session_id=session.id if session else "", run_path=run_path, subagents=subagents,
            memory_config=self._config.memory,
        )



    # 恢复时校验冻结的工具定义，并在模型执行前提交当前 schema
    def _save_tool_schemas(
        self,
        run: RunCheckpoint,
        registry: ToolRegistry,
        persistence: RunPersistence | None,
        *,
        resuming: bool,
    ) -> None:
        schemas = registry.tool_schemas()
        mcp_tools = [registry.get(str(schema["name"])) for schema in schemas]
        definitions = {
            tool.name: digest(tool.record.server_id + tool.record.definition_hash)
            for tool in mcp_tools if isinstance(tool, McpTool)
        }
        if (
            resuming and run.schemas_frozen
            and json.dumps(schemas, sort_keys=True) != json.dumps(run.tool_schemas, sort_keys=True)
        ):
            missing = {str(schema["name"]) for schema in run.tool_schemas} - {
                str(schema["name"]) for schema in schemas
            }
            run.reason = "tool schemas changed; restore compatible tools before resuming"
            if missing:
                run.reason += "; missing: " + ", ".join(sorted(missing))
            raise RuntimeError(run.reason)
        if resuming and run.mcp_definition_hashes and definitions != run.mcp_definition_hashes:
            run.reason = "MCP tool definitions or server identity changed; cannot resume"
            raise RuntimeError(run.reason)
        run.tool_schemas = schemas
        if not run.schemas_frozen:
            run.mcp_tool_names = list(definitions)
            run.mcp_definition_hashes = definitions
            if run.tool_loading_mode == "on_demand" and run.tool_whitelist is not None:
                selected = [name for name in run.mcp_tool_names if name in run.tool_whitelist]
                _, rejected = load_tools(run, selected)
                if rejected:
                    raise RuntimeError("explicit MCP whitelist exceeds schema budget")
        run.schemas_frozen = True
        if persistence is not None:
            persistence.save()

    # 收敛会话与未知工具状态，先保存和导出历史，再发终态并传播取消
    async def _finish_run(
        self,
        context: ExecutionContext,
        bus: EventBus,
        persistence: RunPersistence | None,
        *,
        cancelled: bool,
    ) -> RunOutcome:
        # 一次runner结束后整理最终状态
        run = context.run
        if run.status in ("success", "failed"):
            run.phase = "finished"
        else:
            for tool in run.tools:
                if tool.status == "executing":
                    tool.status = "unknown"
        if persistence is not None:
            session = persistence.session
            session.status = (
                "interrupted" if run.status == "interrupted"
                else "closed" if session.mode == "one_shot" else "waiting_for_input"
            )
            session.updated_at = _now()
            # 先提交终态再导出和通知，通知丢失也能从磁盘确认结果
            persistence.save()
            persistence.export_metadata()

        if run.status == "interrupted":
            await bus.publish_safe(
                RunInterruptedEvent(
                    run_id=run.run_id,
                    session_id=persistence.session.id if persistence else "",
                    reason=run.reason or "interrupted",
                    revision=persistence.checkpoint.revision if persistence else 0,
                    ts=_now(),
                )
            )
        else:
            await bus.publish_safe(
                RunFinishedEvent(
                    run_id=run.run_id, status=run.status, reason=run.reason,
                    result=(
                        run.result if run.completion_mode == "explicit" and run.status == "success"
                        else ""
                    ),
                    steps=run.step, ts=_now(),
                )
            )
        if cancelled:
            raise asyncio.CancelledError()
        return RunOutcome(status=run.status, result=run.result, reason=run.reason)
