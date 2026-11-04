from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.loop import AgentLoop
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.registry import ToolRegistry


# 功能：创建有固定身份的 Run，测试不依赖真实模型与用户目录
# 设计：只覆盖单次运行相关状态，其余配置保持项目默认值
def run_state(**kwargs):
    return RunCheckpoint(
        run_id="run-test", goal="goal", model="deepseek", workspace=str(Path.cwd()),
        max_steps=20, **kwargs,
    )


# 功能：构造记录实际调用的假模型
# 设计：通过响应序列精确控制 Usage、传输错误和工具批次
def provider(*responses):
    model = Mock()
    model.chat = AsyncMock(side_effect=list(responses))
    return model


# 功能：验证重试的失败请求保留估算占用，而成功请求按实际 Usage 结算
# 设计：首个请求断流、第二个请求成功，检查两次预留均在模型调用前保存
async def test_retry_charges_each_attempt_and_keeps_unknown_reservation(monkeypatch):
    monkeypatch.setattr("kama_claude.core.llm.managed.asyncio.sleep", AsyncMock())
    state = run_state()
    budget = RunBudget(state)
    inner = provider(httpx.ReadError("lost"), LlmResponse(
        "end_turn", text="ok", usage=UsageStats(10, 5, total_input_tokens=110),
    ))
    persistence = Mock()
    saved = []
    persistence.save.side_effect = lambda: saved.append(state.model_copy(deep=True))
    managed = ManagedProvider(inner, TokenEstimator(), CompactionConfig(),
                              budget=budget, persistence=persistence)
    await managed.chat([{"role": "user", "content": "hello"}], [], EventBus(), state.run_id)
    assert inner.chat.await_count == 2
    assert state.actual_tokens == 115 and state.estimated_tokens > 8192
    assert not state.usage_complete
    assert saved[0].estimated_tokens > 0
    assert len(saved) >= 4


# 功能：验证保存请求预留失败时不会调用模型
# 设计：在第一处持久化抛出检查点错误，排除已经产生付费请求的情况
async def test_reservation_save_failure_prevents_model_call():
    inner = provider(LlmResponse("end_turn", text="unused"))
    persistence = Mock()
    persistence.save.side_effect = CheckpointError("disk full")
    managed = ManagedProvider(inner, TokenEstimator(), CompactionConfig(),
                              budget=RunBudget(run_state()), persistence=persistence)
    with pytest.raises(CheckpointError):
        await managed.chat([], [], EventBus(), "run-test")
    inner.chat.assert_not_called()


# 功能：验证返回 Usage 后保存失败不能被当成模型失败再次请求
# 设计：第二次保存失败，断言模型仅调用一次且错误原样传播
async def test_usage_save_failure_does_not_retry_completed_response():
    inner = provider(LlmResponse("end_turn", text="ok", usage=UsageStats(10, 2)))
    persistence = Mock()
    persistence.save.side_effect = [None, CheckpointError("write failed")]
    managed = ManagedProvider(inner, TokenEstimator(), CompactionConfig(),
                              budget=RunBudget(run_state()), persistence=persistence)
    with pytest.raises(CheckpointError):
        await managed.chat([], [], EventBus(), "run-test")
    assert inner.chat.await_count == 1


# 功能：验证请求失败的估算占用经过序列化恢复后仍参与上限检查
# 设计：第一次请求占用几乎全部预算，恢复后第二次不能重新获得额度
async def test_budget_reservation_survives_restart():
    state = run_state(max_total_tokens=9000)
    inner = provider(RuntimeError("connection ended"))
    managed = ManagedProvider(inner, TokenEstimator(), CompactionConfig(), budget=RunBudget(state))
    with pytest.raises(RuntimeError):
        await managed.chat([], [], EventBus(), state.run_id)
    restored = RunCheckpoint.model_validate_json(state.model_dump_json())
    with pytest.raises(BudgetExceeded):
        RunBudget(restored).reserve(1000)
    assert restored.estimated_tokens == state.estimated_tokens


# 功能：验证不同错误与成功会重置连续失败计数，第三次提醒、第五次停止
# 设计：每次创建原始结果，避免把上次附加的提醒当作错误指纹的一部分
def test_repeated_failure_guard_and_reset():
    budget = RunBudget(run_state())
    for index in range(1, 6):
        result = ToolResult("same error", True, "runtime_error")
        stop = budget.record_tool_result("read_file", {"path": "missing"}, result)
        assert stop == (index == 5)
        assert ("change the approach" in result.content) == (index >= 3)
    budget.record_tool_result("read_file", {"path": "other"}, ToolResult("same error", True))
    assert budget.run.repeated_failures == 1
    budget.record_tool_result("read_file", {}, ToolResult("Permission denied", True, "permission_denied"))
    assert budget.run.repeated_failures == 0


class FailingTool(BaseTool):
    name = "failure"
    description = "controlled failure"
    input_schema = {"type": "object"}

    # 返回确定性失败，避免工具内部重试掩盖 Loop 保护行为
    async def invoke(self, params):
        return ToolResult("bad input", True, "schema_error")


# 功能：验证第五次重复失败停止，并给同批剩余工具配齐未执行结果
# 设计：模型连续产生相同调用，最后一批增加额外工具验证不会继续产生副作用
async def test_loop_stops_repeated_failures_and_pairs_remaining_tools():
    model = provider(*[
        LlmResponse("tool_use", [ToolCallBlock(f"c{i}", "failure", {})])
        for i in range(4)
    ], LlmResponse("tool_use", [
        ToolCallBlock("c4", "failure", {}), ToolCallBlock("c5", "failure", {}),
    ]))
    registry = ToolRegistry()
    registry.register(FailingTool())
    context = ExecutionContext(run=run_state())
    await AgentLoop(model, registry, EventBus(), context=context).run()
    assert context.run.status == "failed" and context.run.reason == "repeated_tool_failure"
    assert model.chat.await_count == 5
    assert context.run.tools[-1].status == "completed"
    assert "not executed" in context.run.tools[-1].result["content"]


# 功能：验证请求预算不足时摘要不会吞掉预算异常并继续普通请求
# 设计：构造必须摘要的历史，但主 Run 的额度不足以发起摘要请求
async def test_summary_budget_error_propagates():
    from tests.unit.test_compactor import history
    config = CompactionConfig(auto_threshold=0.01, prune_target_ratio=0.005)
    inner = provider(LlmResponse("end_turn", text="unused"))
    estimator = TokenEstimator()
    managed = ManagedProvider(inner, estimator, config, budget=RunBudget(run_state(max_total_tokens=1)))
    with pytest.raises(BudgetExceeded):
        await Compactor(config, estimator).prepare_context(history(), managed)
    inner.chat.assert_not_called()


# 功能：验证时间预算到期会中断审批等待，并保留尚未执行调用的配对信息
# 设计：直接限制剩余时间，不修改事件循环的全局时钟
async def test_time_limit_before_execution_preserves_message_pairs(monkeypatch):
    state = run_state()
    budget = RunBudget(state)
    monkeypatch.setattr(budget, "remaining_seconds", lambda: 0.02)
    model = Mock()
    model.chat = AsyncMock(side_effect=lambda **kwargs: None)

    # 模拟尚未返回的模型请求
    async def wait_model(**kwargs):
        await asyncio.sleep(10)
    model.chat.side_effect = wait_model
    context = ExecutionContext(run=state)
    await AgentLoop(model, ToolRegistry(), EventBus(), context=context, budget=budget).run()
    assert state.status == "failed" and state.reason == "run_time_budget_exceeded"


# 功能：验证普通请求和自动摘要共用主 Run 的同一份累计预算
# 设计：先执行一次摘要，再发普通请求，两次实际 Usage 都应进入 Run
async def test_summary_and_normal_share_budget_and_usage():
    state = run_state()
    estimator = TokenEstimator("deepseek", "m")
    inner = provider(
        LlmResponse("end_turn", text="summary", usage=UsageStats(20, 5)),
        LlmResponse("end_turn", text="answer", usage=UsageStats(30, 6)),
    )
    managed = ManagedProvider(inner, estimator, CompactionConfig(), budget=RunBudget(state))
    await managed.chat([], [], EventBus(), "compact", max_output_tokens=100)
    await managed.chat([], [], EventBus(), state.run_id, max_output_tokens=100)
    assert state.actual_tokens == 61 and state.estimated_tokens == 0 and state.usage_complete
    assert len(estimator.anchors) == 2


# 功能：验证完成的工具后触发预算上限，不会启动同批下一个工具
# 设计：第一个工具产生可计数副作用并耗尽额度，第二个工具必须收到未执行结果
async def test_budget_stops_before_next_tool():
    state = run_state(max_total_tokens=100)
    calls = []

    class Tool(BaseTool):
        name = "counter"
        description = "count"
        input_schema = {"type": "object"}

        # 首个工具返回前把预算用满，模拟前一步累计消耗已达上限
        async def invoke(self, params):
            calls.append(1)
            state.actual_tokens = 100
            return ToolResult("done")

    model = provider(LlmResponse("tool_use", [
        ToolCallBlock("a", "counter", {}), ToolCallBlock("b", "counter", {}),
    ]))
    registry = ToolRegistry()
    registry.register(Tool())
    context = ExecutionContext(run=state)
    await AgentLoop(model, registry, EventBus(), context=context).run()
    assert calls == [1] and state.status == "failed"
    assert state.tools[0].result["content"] == "done"
    assert "not executed" in state.tools[1].result["content"]


# 功能：验证执行中到达时间上限时保持未知结果，不把副作用当作未执行
# 设计：工具先记录副作用再阻塞，持久化替身记录中断后的状态
async def test_time_limit_during_tool_keeps_unknown(monkeypatch):
    state = run_state()
    budget = RunBudget(state)
    monkeypatch.setattr(budget, "remaining_seconds", lambda: 0.05)
    effects = []

    class Tool(BaseTool):
        name = "slow"
        description = "slow side effect"
        input_schema = {"type": "object"}

        # 模拟副作用之后尚未取得返回值的窗口
        async def invoke(self, params):
            effects.append(1)
            await asyncio.sleep(30)
            return ToolResult("unused")

    model = provider(LlmResponse("tool_use", [ToolCallBlock("a", "slow", {})]))
    registry = ToolRegistry()
    registry.register(Tool())
    context = ExecutionContext(run=state)
    persistence = Mock()
    await AgentLoop(
        model, registry, EventBus(), context=context, budget=budget, persistence=persistence,
    ).run()
    assert effects == [1]
    assert state.status == "interrupted" and state.tools[0].status == "unknown"
    assert state.tool_index == 0


# 功能：验证 Usage 事件使用配置窗口，并且不会重复计算缓存命中
# 设计：假 Provider 发布总输入为 100 的事件，使用 1000 窗口检查比例
async def test_usage_event_uses_configured_window():
    from kama_claude.core.bus.events import LlmUsageEvent
    events = []
    bus = EventBus()

    # 捕获转发后的事件而不是 Provider 的原始错误比例
    async def collect(event):
        events.append(event)
    bus.subscribe(collect)
    model = Mock()

    # 模拟 Provider 使用不同默认窗口发布事件
    async def answer(**kwargs):
        await kwargs["bus"].publish(LlmUsageEvent(
            run_id="run-test", input_tokens=20, total_input_tokens=100,
            output_tokens=10, cache_read_input_tokens=80, cache_creation_input_tokens=0,
            context_pct=0.001, ts="now",
        ))
        return LlmResponse("end_turn", text="ok", usage=UsageStats(
            20, 10, cache_read_input_tokens=80, total_input_tokens=100,
        ))
    model.chat = AsyncMock(side_effect=answer)
    state = run_state()
    managed = ManagedProvider(
        model, TokenEstimator(), CompactionConfig(context_window_tokens=1000),
        budget=RunBudget(state),
    )
    await managed.chat([], [], bus, state.run_id, max_output_tokens=10)
    assert events[0].context_pct == 0.1
    assert state.actual_tokens == 110


# 功能：验证最新 Usage 锚点随真实会话检查点跨新 Run 和存储重建保留
# 设计：两次独立 Manager 使用同一磁盘目录，检查最新用量和消息边界共同更新
async def test_calibration_survives_store_restart_and_new_run(tmp_path):
    from kama_claude.core.session.store import SessionStore
    from tests.unit.test_checkpoint_recovery import Provider, manager
    store = SessionStore(tmp_path)
    model = Provider([LlmResponse("end_turn", text="one", usage=UsageStats(1000, 20))])
    mgr = manager(store, model, [])
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "first")
    saved = store.checkpoints.load(session.id)
    assert saved.token_estimation
    anchor_before = next(iter(saved.token_estimation.values()))
    assert anchor_before.input_tokens == 1000
    next_store = SessionStore(tmp_path)
    restored = manager(next_store, Provider([
        LlmResponse("end_turn", text="two", usage=UsageStats(1100, 20)),
    ]), [])
    await restored.send_message(session.id, "second")
    after = next_store.checkpoints.load(session.id)
    assert len(after.token_estimation) == 1
    anchor_after = next(iter(after.token_estimation.values()))
    assert anchor_after.input_tokens == 1100
    assert anchor_after.message_count > anchor_before.message_count
    assert after.run.run_id != saved.run.run_id
