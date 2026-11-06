from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

from kama_claude.core.agents.loader import AgentProfile, AgentProfileError, AgentProfileLoader
from kama_claude.core.bus.events import PermissionRequestedEvent, SubagentFinishedEvent
from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.config import AgentConfig, CompactionConfig, KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.provider import AnthropicProvider, DeepseekProvider, OpenaiProvider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.query import AgentResultTool
from kama_claude.core.subagent.registry import ActiveSubagent, BackgroundTaskRegistry
from kama_claude.core.subagent.result import SubagentResult
from kama_claude.core.subagent.tool import SpawnAgentTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from tests.unit.completion_support import submit_response


# 构造记录输入的假模型，默认返回真实形状的 Usage，不访问网络
class Provider:
    _model = "test-model"

    # 响应可为完整回复、异常或异步等待函数
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    # 保留输入快照以验证压缩后的模型请求
    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls.append(kwargs)
        response = self.responses.pop(0) if self.responses else LlmResponse(
            "end_turn", text="done", usage=UsageStats(100, 20),
        )
        if isinstance(response, BaseException):
            raise response
        response = await response() if callable(response) else response
        return submit_response(response, kwargs["tool_schemas"], kwargs["run_id"], kwargs["step"])


# 构造共享同一个注册表、固定工作区的独立委派工具
def spawn_tool(tmp_path: Path, provider: Any = None, **kwargs: Any) -> SpawnAgentTool:
    return SpawnAgentTool(
        provider=provider or Provider(), parent_bus=kwargs.pop("bus", EventBus()),
        parent_run_id="parent", permission_manager=kwargs.pop("permissions", None),
        max_steps=kwargs.pop("max_steps", 5),
        task_registry=kwargs.pop("registry", BackgroundTaskRegistry()),
        runs_dir=tmp_path / "runs", session_id=kwargs.pop("session_id", "session"),
        workspace=tmp_path, model="test-model", **kwargs,
    )


# 提交后台任务并等待实际完成，避免依赖固定 sleep
async def background_result(tool: SpawnAgentTool) -> SubagentResult:
    result = await tool.invoke({"description": "test", "prompt": "goal", "run_in_background": True})
    assert not result.is_error
    run_id = result.content.split("run_id=")[1].split(".")[0]
    entry = tool._task_registry.get(run_id, tool._session_id)
    if isinstance(entry, ActiveSubagent):
        await entry.task
    finished = tool._task_registry.get(run_id, tool._session_id)
    assert isinstance(finished, SubagentResult)
    return finished


# 功能：验证前后台业务失败一致，耗尽 Step 不会伪装为空成功
# 设计：模型只请求缺失工具，Loop 正常返回 failed，覆盖 Task 无异常的失败路径
@pytest.mark.parametrize("background", [False, True])
async def test_failure_status_is_not_success(tmp_path: Path, background: bool) -> None:
    model = Provider(LlmResponse("tool_use", tool_calls=[ToolCallBlock("t", "missing", {})]))
    tool = spawn_tool(tmp_path, model, max_steps=1)
    if background:
        outcome = await background_result(tool)
        result = await AgentResultTool(tool._task_registry, "session").invoke({
            "run_ids": [outcome.run_id],
        })
    else:
        result = await tool.invoke({"description": "test", "prompt": "goal"})
    assert result.is_error
    assert result.error_type == "subagent_failed"
    assert "exceeded_max_steps" in result.content


# 功能：验证已完成任务释放 Context，缓存有界且隔离会话
# 设计：依次完成超过容量的任务，只能保留最近结果，并禁止其他会话查询
async def test_completed_cache_is_bounded_and_scoped(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(max_results=1)
    tool = spawn_tool(tmp_path, registry=registry)
    first = await background_result(tool)
    second = await background_result(tool)
    assert not registry._tasks
    assert first.run_id not in registry._results
    assert registry.get(first.run_id, "session") == first
    assert registry.get(second.run_id, "other") is None
    assert registry.get(second.run_id, "session") == second
    assert not hasattr(second, "messages") and not hasattr(second, "context")
    denied = await AgentResultTool(registry, "other").invoke({"run_ids": [second.run_id]})
    assert denied.is_error


# 功能：验证取消会话只停止其子任务，取消路径也发送一次完成通知
# 设计：两个会话各有阻塞模型，用 Event 确认模型已经开始后再取消
async def test_cancel_session_is_scoped_and_notifies(tmp_path: Path) -> None:
    registry, bus = BackgroundTaskRegistry(), EventBus()
    started = [asyncio.Event(), asyncio.Event()]
    finished: list[BaseModel] = []

    # 收集不会因界面观察者异常而丢失的终态
    async def collect(event: BaseModel) -> None:
        if isinstance(event, SubagentFinishedEvent):
            finished.append(event)

    bus.subscribe(collect)
    for index, sid in enumerate(("one", "two")):
        # 每个模型停在请求中，确认取消不是碰巧赶在启动之前
        async def wait(index: int = index) -> LlmResponse:
            started[index].set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        await spawn_tool(
            tmp_path, Provider(wait), registry=registry, bus=bus, session_id=sid,
        ).invoke({"description": sid, "prompt": "wait", "run_in_background": True})
    await asyncio.gather(*(event.wait() for event in started))
    await registry.cancel(session_id="one")
    assert len(registry._tasks) == 1
    assert next(iter(registry._tasks.values())).session_id == "two"
    await registry.cancel()
    assert len(finished) == 2
    assert all(getattr(event, "reason") == "cancelled" for event in finished)
    assert not registry._tasks


# 功能：验证协程尚未运行就取消时，也记录取消结果并补发通知
# 设计：启动后不让出事件循环，直接 Core 清理，覆盖无法进入协程 finally 的窗口
async def test_cancel_before_child_starts(tmp_path: Path) -> None:
    registry, bus = BackgroundTaskRegistry(), EventBus()
    finished: list[BaseModel] = []

    # 仅记录结束事件
    async def collect(event: BaseModel) -> None:
        if isinstance(event, SubagentFinishedEvent):
            finished.append(event)

    bus.subscribe(collect)
    tool = spawn_tool(tmp_path, registry=registry, bus=bus)
    result = await tool.invoke({"description": "test", "prompt": "g", "run_in_background": True})
    run_id = result.content.split("run_id=")[1].split(".")[0]
    await registry.cancel()
    outcome = registry.get(run_id, "session")
    assert isinstance(outcome, SubagentResult)
    assert outcome.reason == "cancelled" and outcome.as_tool_result().is_error
    assert len(finished) == 1


# 功能：验证异常被主动接收，完成通知观察者失败不改变子任务结果
# 设计：注入 Loop 初始化后的异常和报错观察者，检查缓存与事件数量
async def test_exception_and_observer_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    finishes: list[BaseModel] = []

    # 记录通知后故意抛错，模拟界面或广播器不可用
    async def broken_observer(event: BaseModel) -> None:
        if isinstance(event, SubagentFinishedEvent):
            finishes.append(event)
            raise RuntimeError("observer failed")

    bus.subscribe(broken_observer)
    monkeypatch.setattr("kama_claude.core.subagent.tool.AgentLoop.run", AsyncMock(
        side_effect=RuntimeError("loop failed"),
    ))
    outcome = await background_result(spawn_tool(tmp_path, bus=bus))
    assert outcome.status == "failed" and outcome.reason == "subagent_error"
    assert "loop failed" in outcome.as_tool_result().content
    assert len(finishes) == 1


# 功能：验证角色缺失、损坏和非法名称全部明确拒绝，不调用模型
# 设计：损坏的本地 reviewer 覆盖有效内建配置，不能静默降级为完整工具
async def test_invalid_profile_never_falls_back(tmp_path: Path) -> None:
    agents = tmp_path / ".kama" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.toml").write_text('[agent]\nallowed_tools = "read_file"', encoding="utf-8")
    model = Provider()
    tool = spawn_tool(tmp_path, model)
    for name in ("missing-role", "reviewer", "../reviewer"):
        result = await tool.invoke({"description": "test", "prompt": "g", "subagent_type": name})
        assert result.is_error and result.error_type == "configuration_error"
    assert model.calls == []


# 功能：验证缺省白名单继承父范围，空白名单没有工具，角色不能放大权限
# 设计：直接检查实际工具注册表，同时确认内建审查与规划角色不含 shell 和写工具
def test_profile_tool_ranges(tmp_path: Path) -> None:
    tool = spawn_tool(tmp_path, tool_whitelist=["read_file", "spawn_agent"])
    for requested, expected in ((None, {"read_file", "spawn_agent"}), ([], set()),
                                (["read_file", "write_file", "bash"], {"read_file"})):
        profile = AgentProfile("test", "", "", allowed_tools=requested)
        registry = tool._build_child_registry(EventBus(), "child", profile)
        assert {schema["name"] for schema in registry.tool_schemas()} == expected
    loader = AgentProfileLoader(tmp_path)
    for role in ("reviewer", "planner"):
        assert not ({"bash", "write_file", "spawn_agent"} & set(loader.load(role).allowed_tools or []))
    with pytest.raises(AgentProfileError):
        loader.load("../../outside")


# 功能：验证子任务请求前检查 Token 上限，成功任务正确记录缓存输入及输出
# 设计：极小上限应零请求；正常 Usage 包含缓存时按总输入计入子任务
async def test_child_budget_and_usage(tmp_path: Path) -> None:
    model = Provider()
    exhausted = await background_result(spawn_tool(
        tmp_path, model, agent_config=AgentConfig(max_total_tokens=1),
    ))
    assert exhausted.reason == "run_token_budget_exceeded" and not model.calls
    success = await background_result(spawn_tool(tmp_path, Provider(LlmResponse(
        "end_turn", text="done", usage=UsageStats(
            10, 5, cache_read_input_tokens=100, total_input_tokens=110,
        ),
    ))))
    assert success.actual_tokens == 115 and success.estimated_tokens == 0
    assert success.usage_complete and success.elapsed_seconds >= 0


# 功能：验证模型重试与 Usage 缺失不绕过子任务记账
# 设计：先断流后成功但不返回 Usage，检查两次估算预留都保留
async def test_child_retry_retains_unknown_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kama_claude.core.llm.managed.asyncio.sleep", AsyncMock())
    model = Provider(httpx.ReadError("lost"), LlmResponse("end_turn", text="ok"))
    outcome = await background_result(spawn_tool(tmp_path, model))
    assert len(model.calls) == 2
    assert outcome.estimated_tokens > 16_384 and not outcome.usage_complete


# 功能：验证子任务时间上限会结束等待中的请求
# 设计：阻塞模型并将剩余时间缩短，避免测试实际等待整秒或调用模型
async def test_child_time_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 模拟永不返回的远端请求
    async def wait() -> LlmResponse:
        await asyncio.Future()
        raise AssertionError("unreachable")

    from kama_claude.core.run_budget import RunBudget

    original = RunBudget.remaining_seconds

    # 保留真实计时逻辑，只把测试窗口缩小
    def short_timeout(self: RunBudget) -> float | None:
        value = original(self)
        return min(value, 0.02) if value is not None else None

    monkeypatch.setattr(RunBudget, "remaining_seconds", short_timeout)
    outcome = await background_result(spawn_tool(
        tmp_path, Provider(wait), agent_config=AgentConfig(max_run_seconds=1),
    ))
    assert outcome.reason == "run_time_budget_exceeded"
    assert outcome.estimated_tokens > 0


# 功能：验证角色模型覆盖保留当前供应商及客户端，不按模型别名跳转供应商
# 设计：三家 Provider 均使用假客户端，只检查重建参数，不建立网络连接
@pytest.mark.parametrize("provider_type", [AnthropicProvider, DeepseekProvider, OpenaiProvider])
def test_role_model_uses_same_backend(provider_type: Any) -> None:
    client = object()
    runner = AgentRunner(KamaConfig(), provider=provider_type("original", client=client))
    selected = runner._providers.for_model("role-model")
    assert type(selected) is provider_type
    assert selected._model == "role-model" and selected._client is client


# 功能：验证新建 Runner 后仍可收集上一轮后台任务
# 设计：通过真实 SessionManager 连续发送两轮消息，仅替换模型，不绕过 Runner 组装
async def test_completed_background_result_survives_new_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    registry, bus = BackgroundTaskRegistry(), EventBus()
    children: list[str] = []
    roots: dict[str, int] = {}

    # 从实际开始事件取得子 ID，下一轮查询使用同一 ID
    async def collect(event: BaseModel) -> None:
        if getattr(event, "type", "") == "subagent.started":
            children.append(getattr(event, "run_id"))

    bus.subscribe(collect)

    # 第一轮委派，第二轮收集；子任务有自己的调用上下文
    async def chat(**kwargs: Any) -> LlmResponse:
        run_id = kwargs["run_id"]
        if run_id in children:
            return submit_response(
                LlmResponse("end_turn", text="child answer", usage=UsageStats(10, 5)),
                kwargs["tool_schemas"], run_id, kwargs["step"],
            )
        roots[run_id] = roots.get(run_id, 0) + 1
        first = len(roots) == 1
        if (first and roots[run_id] <= 2) or (not first and roots[run_id] == 1):
            delegate = first and roots[run_id] == 1
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                "delegate" if delegate else "collect", "spawn_agent" if delegate else "agent_result",
                {"description": "child", "prompt": "child", "run_in_background": True}
                if delegate else {"run_ids": [children[0]]},
            )], usage=UsageStats(10, 5))
        return submit_response(
            LlmResponse("end_turn", text="parent done", usage=UsageStats(10, 5)),
            kwargs["tool_schemas"], run_id, kwargs["step"],
        )

    provider = AsyncMock()
    provider.chat.side_effect = chat
    permissions = PermissionManager()

    # 本次委派与查询都走正常审批
    async def approve(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            permissions.respond(event.request_id, "allow_once", session_id=event.session_id, run_id=event.run_id)

    bus.subscribe(approve)
    runners: list[AgentRunner] = []

    # 模拟 Core 的共享注册表注入，Runner 本身仍每轮创建
    def new_runner() -> AgentRunner:
        runner = AgentRunner(
            KamaConfig(), bus=bus, provider=provider,
            task_registry=registry, permission_manager=permissions,
        )
        runners.append(runner)
        return runner

    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, new_runner, bus, permission_manager=permissions, task_registry=registry,
    )
    session = await manager.create("chat")
    await manager.send_message(session.id, "delegate")
    entry = registry.get(children[0], session.id)
    if isinstance(entry, ActiveSubagent):
        await entry.task
    await manager.send_message(session.id, "collect the result")
    checkpoint = store.checkpoints.load(session.id)
    assert len(runners) == 2 and runners[0] is not runners[1]
    assert all(runner._task_registry is registry for runner in runners)
    assert checkpoint.run is not None and checkpoint.run.status == "success"
    assert any(
        "child answer" in block.get("content", "")
        for message in checkpoint.messages if isinstance(message["content"], list)
        for block in message["content"] if block.get("type") == "tool_result"
    )
    await manager.shutdown()


# 功能：验证关闭会话清理该会话子任务，Core shutdown 清理其余子任务
# 设计：使用真实 Manager 生命周期方法和两个阻塞子模型，检查完成通知与会话隔离
async def test_manager_close_and_shutdown(tmp_path: Path) -> None:
    registry, bus = BackgroundTaskRegistry(), EventBus()
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"), lambda: AgentRunner(KamaConfig()), bus,
        task_registry=registry,
    )
    first, second = await manager.create("chat"), await manager.create("chat")
    started = [asyncio.Event(), asyncio.Event()]
    for index, session in enumerate((first, second)):
        # 确保模型启动后仍保持阻塞
        async def wait(index: int = index) -> LlmResponse:
            started[index].set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        await spawn_tool(tmp_path, Provider(wait), registry=registry, session_id=session.id).invoke({
            "description": "waiting", "prompt": "g", "run_in_background": True,
        })
    await asyncio.gather(*(event.wait() for event in started))
    await manager.close(first.id)
    assert len(registry._tasks) == 1
    assert next(iter(registry._tasks.values())).session_id == second.id
    await manager.shutdown()
    assert not registry._tasks


# 功能：验证恢复后派生子任务沿用父 Run 已冻结的预算，而非当前新配置
# 设计：构造不同限制的配置与 Run，通过真实组装得到的 Spawn 工具检查子状态
def test_restored_parent_passes_frozen_limits(tmp_path: Path) -> None:
    config = KamaConfig()
    config.agent.max_total_tokens = 100_000
    runner = AgentRunner(config, provider=Provider())
    run = RunCheckpoint(
        run_id="parent", goal="g", model="test-model", provider="custom", workspace=str(tmp_path),
        max_steps=3, max_total_tokens=20_000, max_run_seconds=60, repeat_failure_limit=2,
    )
    context = ExecutionContext(run=run)
    loop = runner._build_loop(context, EventBus(), tmp_path / "runs", None, resuming=True)
    spawn = loop._tools.registry.get("spawn_agent")
    assert isinstance(spawn, SpawnAgentTool)
    assert spawn._agent_config == AgentConfig(
        max_steps=3, max_total_tokens=20_000, max_run_seconds=60, repeat_failure_limit=2,
        no_progress_limit=0,
    )


# 功能：验证子任务长工具结果归档并裁剪，模型不再重复接收全文
# 设计：读取真实长文件，只跑两次普通请求，检查归档和第二次请求正文
async def test_child_long_output_uses_compaction(tmp_path: Path) -> None:
    text = "head-\n" + ("x" * 100 + "\n") * 100 + "-tail\n"
    (tmp_path / "large.txt").write_text(text, encoding="utf-8")
    model = Provider(LlmResponse("tool_use", tool_calls=[ToolCallBlock(
        "read", "read_file", {"path": "large.txt"},
    )], usage=UsageStats(100, 10)))
    outcome = await background_result(spawn_tool(
        tmp_path, model, compaction=CompactionConfig(auto_threshold=0),
    ))
    assert outcome.status == "success" and len(model.calls) == 2
    files = list((tmp_path / "runs" / outcome.run_id / "outputs").iterdir())
    original_result = await ReadFileTool(tmp_path).invoke({"path": "large.txt"})
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == original_result.content
    content = model.calls[1]["messages"][2]["content"][0]["content"]
    assert len(content) < len(text) and "head-" in content and "-tail" in content


# 功能：验证子任务硬预算不足时不请求模型，未知副作用时不继续下一次模型
# 设计：分别构造过大的用户请求和写入未知结果，确保安全终止不会被主任务成功掩盖
async def test_child_hard_context_limit_and_unknown_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = Provider()
    tool = spawn_tool(tmp_path, model, compaction=CompactionConfig(
        context_window_tokens=2048, output_reserve_tokens=512, summary_max_tokens=128,
        auto_threshold=0,
    ))
    result = await tool.invoke({"description": "big", "prompt": "中" * 5000})
    assert result.is_error and "context_budget_exceeded" in result.content and not model.calls
    from kama_claude.core.tools.base import ToolResult

    monkeypatch.setattr("kama_claude.core.tools.builtin.write_file.WriteFileTool._invoke_path",
                        AsyncMock(return_value=ToolResult(
                            "uncertain", is_error=True, outcome_unknown=True,
                        )))
    model = Provider(LlmResponse("tool_use", tool_calls=[ToolCallBlock(
        "write", "write_file", {"path": "a.txt", "content": "x"},
    )]))
    result = await spawn_tool(tmp_path, model).invoke({
        "description": "write", "prompt": "g", "subagent_type": "executor",
    })
    assert result.is_error and result.outcome_unknown
    assert "unknown_tool_result" in result.content and len(model.calls) == 1


# 功能：验证子任务实际触发 LLM 摘要，摘要 Usage 计入同一子任务
# 设计：三轮较长 assistant 历史且工具输出很短，程序裁剪无法解决，必须请求一次摘要
async def test_child_summary_is_managed_and_charged(tmp_path: Path) -> None:
    (tmp_path / "small.txt").write_text("small", encoding="utf-8")

    class SummaryProvider(Provider):
        # 普通请求使用固定响应，摘要请求单独返回可核验的 Usage
        async def chat(self, **kwargs: Any) -> LlmResponse:
            if kwargs["run_id"] == "compact":
                self.calls.append(kwargs)
                return LlmResponse("end_turn", text="Old work completed; continue.",
                                   usage=UsageStats(25, 5))
            return await super().chat(**kwargs)

    model = SummaryProvider(*[
        LlmResponse("tool_use", text="explanation " * 600, tool_calls=[ToolCallBlock(
            f"read-{index}", "read_file", {"path": "small.txt"},
        )]) for index in range(3)
    ])
    outcome = await background_result(spawn_tool(tmp_path, model, compaction=CompactionConfig(
        context_window_tokens=12_000, output_reserve_tokens=512, summary_max_tokens=128,
        keep_recent_turns=1, recent_token_budget=2_000, auto_threshold=0.5,
    )))
    summaries = [call for call in model.calls if call["run_id"] == "compact"]
    assert outcome.status == "success" and summaries
    assert all(call["max_output_tokens"] == 128 for call in summaries)
    # 工具 Schema 增大可能更早触发摘要；每次摘要都必须按 Usage=30 结算
    assert outcome.actual_tokens == 120 + 30 * len(summaries) and outcome.estimated_tokens > 0
    assert not outcome.usage_complete


# 功能：验证连续失败限制传给子 Loop，达到限制后不再请求模型
# 设计：两次完全相同的明确失败，Step 仍有余量，隔离重复失败保护的停止条件
async def test_child_repeated_failures_stop(tmp_path: Path) -> None:
    model = Provider(*[
        LlmResponse("tool_use", tool_calls=[ToolCallBlock(f"t-{i}", "missing", {})])
        for i in range(2)
    ])
    outcome = await background_result(spawn_tool(
        tmp_path, model, agent_config=AgentConfig(repeat_failure_limit=2),
    ))
    assert outcome.reason == "repeated_tool_failure" and len(model.calls) == 2


# 功能：验证角色中的模型实际生效且继续派生时继承已选择的模型
# 设计：用两个不同的假 Provider 和可记录工厂，确认只调用角色模型
async def test_profile_model_is_applied(tmp_path: Path) -> None:
    agents = tmp_path / ".kama" / "agents"
    agents.mkdir(parents=True)
    (agents / "custom.toml").write_text(
        '[agent]\nmodel="role-model"\nallowed_tools=[]\n', encoding="utf-8",
    )
    original, selected = Provider(), Provider()
    requested: list[str] = []

    # 只有通过明确的工厂才允许切换模型
    def factory(model: str) -> Any:
        requested.append(model)
        return selected

    tool = spawn_tool(tmp_path, original, provider_factory=factory)
    result = await tool.invoke({"description": "test", "prompt": "g", "subagent_type": "custom"})
    assert not result.is_error and requested == ["role-model"]
    assert not original.calls and len(selected.calls) == 1
    assert [s["name"] for s in selected.calls[0]["tool_schemas"]] == ["finish"]
    registry = tool._build_child_registry(EventBus(), "child", None,
                                          provider=selected, model="role-model")
    nested = registry.get("spawn_agent")
    assert isinstance(nested, SpawnAgentTool)
    assert nested._model == "role-model" and nested._provider is selected
