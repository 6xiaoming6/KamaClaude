from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.memory.model import MemorySnapshot
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.registry import ToolRegistry


class Crash(BaseException):
    pass


class ProbeTool(BaseTool):
    description = "controlled side effect"
    input_schema = {"type": "object", "properties": {}}

    # 保存调用次数和是否在副作用后模拟进程中断
    def __init__(self, name: str, crash: bool = False) -> None:
        self.name = name
        self.calls = 0
        self.crash = crash

    # 模拟一个无法假定可重复的外部操作
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        if self.crash:
            raise Crash()
        return ToolResult(content=f"{self.name} result")


class Provider:
    # 保存固定响应序列和真实请求内容
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    # 捕获调用后返回固定结果，不访问网络
    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls.append(
            json.loads(
                json.dumps(
                    {
                        "step": kwargs["step"],
                        "messages": kwargs["messages"],
                        "system": kwargs["system"],
                    }
                )
            )
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


# 构造使用真实 Runner 和存储、仅替换外部模型及工具的管理器
def manager(
    store: SessionStore,
    provider: Provider,
    tools: list[ProbeTool],
    bus: EventBus | None = None,
    permissions: PermissionManager | None = None,
) -> SessionManager:
    event_bus = bus or EventBus()

    class Runner(AgentRunner):
        # 只注册实验工具，保证模型响应与 schema 对应
        def _build_registry(self, task_manager: Any, **kwargs: Any) -> ToolRegistry:
            registry = ToolRegistry()
            for tool in tools:
                registry.register(tool)
            return registry

    return SessionManager(
        store,
        lambda: Runner(
            KamaConfig(), provider=provider, bus=event_bus, permission_manager=permissions
        ),
        event_bus,
        permission_manager=permissions,
    )


# 生成功能明确的单步工具调用响应
def tool_response(*names: str, pct: float = 0.0) -> LlmResponse:
    return LlmResponse(
        stop_reason="tool_use",
        tool_calls=[ToolCallBlock(f"id-{name}", name, {}) for name in names],
        usage=UsageStats(10, 10, context_pct=pct),
    )


# 等待由 Core 持有的任务完成，而不是依赖固定 sleep
async def finish(mgr: SessionManager, sid: str) -> None:
    task = mgr._tasks.get(sid)
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout=3)


# 功能：验证两个工具之间中断后跳过已保存工具，保留 Run 身份和 Step
# 设计：在第一个结果已原子提交后模拟硬崩溃，重新创建管理器完成恢复
async def test_resume_skips_committed_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    a, b = ProbeTool("a"), ProbeTool("b")
    provider = Provider([tool_response("a", "b"), LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider, [a, b])
    session = await mgr.create("chat")
    original = store.checkpoints.save

    # 在副作用及结果均已提交的位置中断，不依赖正常取消收尾
    def crash_after_result(cp: Any) -> None:
        original(cp)
        if cp.run and cp.run.phase == "tools" and cp.run.tool_index == 1:
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", crash_after_result)
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    monkeypatch.setattr(store.checkpoints, "save", original)
    before = store.checkpoints.load(session.id)
    assert a.calls == 1 and b.calls == 0
    restored = manager(store, provider, [a, b])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    after = store.checkpoints.load(session.id)
    assert (a.calls, b.calls) == (1, 1)
    assert after.run.run_id == before.run.run_id
    assert after.run.step == 2 and after.run.status == "success"
    assert [call["step"] for call in provider.calls] == [1, 2]
    assert sum(m["content"] == "goal" for m in after.messages) == 1


# 功能：验证工具产生副作用但没有结果时必须人工确认，终止后历史仍可续聊
# 设计：工具内部硬中断，确保执行前检查点已经保存，但不存在伪造的成功结果
async def test_unknown_side_effect_requires_decision(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    tool = ProbeTool("a", crash=True)
    provider = Provider([tool_response("a"), LlmResponse("end_turn", text="new answer")])
    mgr = manager(store, provider, [tool])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    restored = manager(store, provider, [tool])
    opened = await restored.open(session.id)
    assert opened["run"]["tools"][0]["status"] == "unknown"
    with pytest.raises(HandlerError, match="outcome unknown"):
        await restored.resume(session.id, opened["revision"])
    assert tool.calls == 1
    await restored.resume(session.id, opened["revision"], "abort")
    assert tool.calls == 1
    assert store.read_messages(session.id)[-1]["content"][0]["is_error"] is True
    await restored.send_message(session.id, "explain current state")
    assert store.checkpoints.load(session.id).run.status == "success"


# 功能：验证用户明确确认后可重试未知工具，并记录这一决定
# 设计：第一次在副作用后中断，第二次禁用故障，断言额外执行来自明确的 retry_tool
async def test_explicit_retry_of_unknown_tool(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    tool = ProbeTool("a", crash=True)
    provider = Provider([tool_response("a"), LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider, [tool])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    tool.crash = False
    restored = manager(store, provider, [tool])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"], "retry_tool")
    await finish(restored, session.id)
    cp = store.checkpoints.load(session.id)
    assert tool.calls == 2 and cp.run.status == "success"
    assert cp.run.decisions[-1]["action"] == "retry_tool"


# 功能：验证模型调用中断后重用相同 Step，不追加重复用户目标
# 设计：在模型响应返回前硬中断，再使用保存的模型调用阶段续跑
async def test_resume_model_call_preserves_step(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    provider = Provider([Crash(), LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider, [])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    restored = manager(store, provider, [])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    assert [call["step"] for call in provider.calls] == [1, 1]
    assert len(store.read_messages(session.id)) == 2


# 功能：验证原子替换失败时保留旧快照及修订号
# 设计：在 replace 注入磁盘错误，直接重新读取磁盘而不信任内存对象
async def test_failed_replace_preserves_previous_checkpoint(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([]), [])
    session = await mgr.create("chat")
    cp = store.checkpoints.load(session.id)
    original = cp.model_dump()
    cp.messages.append({"role": "user", "content": "uncommitted"})
    with patch("kama_claude.core.checkpoint.checkpoint.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(CheckpointError):
            store.checkpoints.save(cp)
    assert store.checkpoints.load(session.id).model_dump() == original
    assert not list((tmp_path / session.id).glob("*.tmp"))


# 功能：验证损坏和未知版本快照不静默回退到旧 thread
# 设计：保留合法兼容历史并破坏权威文件，确认会话被显示为不可恢复
@pytest.mark.parametrize("content", ["{broken", '{"schema_version":99}'])
async def test_invalid_checkpoint_blocks_recovery(tmp_path: Path, content: str) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([]), [])
    session = await mgr.create("chat")
    store.checkpoints.path(session.id).write_text(content, encoding="utf-8")
    restored = manager(store, Provider([]), [])
    assert restored.list_sessions()[0]["status"] == "unavailable"
    with pytest.raises(HandlerError):
        await restored.open(session.id)


# 功能：验证过期修订和重复恢复请求不会创建重复 Run
# 设计：让恢复后的模型调用等待可控事件，第二次点击时任务仍处于活动状态
async def test_revision_and_duplicate_resume_guard(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    provider = Provider([Crash()])
    mgr = manager(store, provider, [])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    gate = asyncio.Event()

    class WaitingProvider(Provider):
        # 持有模型调用，确保重复恢复发生在原任务运行期间
        async def chat(self, **kwargs: Any) -> LlmResponse:
            await gate.wait()
            return LlmResponse("end_turn", text="done")

    restored = manager(store, WaitingProvider([]), [])
    opened = await restored.open(session.id)
    with pytest.raises(HandlerError, match="changed"):
        await restored.resume(session.id, opened["revision"] - 1)
    await restored.resume(session.id, opened["revision"])
    with pytest.raises(HandlerError, match="running"):
        await restored.resume(session.id, opened["revision"])
    gate.set()
    await finish(restored, session.id)


# 功能：验证结果提交后、通知前中断时不再执行模型或工具
# 设计：在成功状态原子保存之后硬中断，重建管理器后只打开已完成结果
async def test_committed_success_is_not_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    provider = Provider([LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider, [])
    session = await mgr.create("chat")
    original = store.checkpoints.save

    # 在完成状态保存后阻止通知，模拟响应丢失窗口
    def crash_after_success(cp: Any) -> None:
        original(cp)
        if cp.run and cp.run.status == "success":
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", crash_after_success)
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    monkeypatch.setattr(store.checkpoints, "save", original)
    restored = manager(store, provider, [])
    opened = await restored.open(session.id)
    assert opened["run"]["status"] == "success"
    assert opened["session"]["status"] == "waiting_for_input"
    with pytest.raises(HandlerError, match="no interrupted"):
        await restored.resume(session.id, opened["revision"])
    assert len(provider.calls) == 1


# 功能：验证客户端等待被取消后 Core 任务继续运行，重开可找回同一审批
# 设计：在真实 PermissionManager 挂起时取消 send_message 的调用者，随后批准原 Future
async def test_disconnect_keeps_run_and_live_permission(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    bus = EventBus()
    requested = asyncio.Event()

    # 捕获审批到达点，不用固定时长猜测执行阶段
    async def observe(event: Any) -> None:
        if event.type == "permission.requested":
            requested.set()

    bus.subscribe(observe)
    permissions = PermissionManager(
        {"a": ToolPolicy(default=PermissionDecision.ASK)},
        timeout_s=0,
    )
    tool = ProbeTool("a")
    mgr = manager(
        store,
        Provider([tool_response("a"), LlmResponse("end_turn", text="done")]),
        [tool],
        bus,
        permissions,
    )
    session = await mgr.create("chat")
    waiter = asyncio.create_task(mgr.send_message(session.id, "goal"))
    await asyncio.wait_for(requested.wait(), 2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    opened = await mgr.open(session.id)
    assert opened["running"] is True
    assert opened["pending_permissions"][0]["tool_use_id"] == "id-a"
    pending = opened["pending_permissions"][0]
    permissions.respond(
        pending["request_id"], "allow_once", session_id=session.id, run_id=pending["run_id"],
    )
    await finish(mgr, session.id)
    assert tool.calls == 1 and not permissions._pending


# 功能：验证压缩工作历史跨重启和后续 Run 生效，恢复不重置原 Step
# 设计：在候选摘要提交后模拟硬崩溃，使用真实存储恢复并继续工具与后续输入
async def test_compacted_snapshot_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kama_claude.core.compact.compactor import SUMMARY_MARKER
    from tests.unit.test_compactor import history

    class CompactProvider(Provider):
        # 摘要调用不消耗正式模型响应，便于验证两条调用链独立
        async def chat(self, **kwargs: Any) -> LlmResponse:
            if kwargs.get("run_id") == "compact":
                return LlmResponse("end_turn", text="handoff summary")
            return await super().chat(**kwargs)

    store = SessionStore(tmp_path)
    model = CompactProvider([
        tool_response("a"), LlmResponse("end_turn", text="done"),
        LlmResponse("end_turn", text="followed"),
    ])
    a = ProbeTool("a")
    mgr = manager(store, model, [a])
    original_factory = mgr._runner_factory

    # 调小软阈值，让实际长历史触发压缩，而不是伪造上一次 API 的占比
    def factory() -> AgentRunner:
        runner = original_factory()
        runner._config.compaction.auto_threshold = 0.1
        runner._config.compaction.prune_target_ratio = 0.05
        return runner

    mgr._runner_factory = factory
    session = await mgr.create("chat")
    for message in history(30):
        store.append_message(session.id, message["role"], message["content"])
    original_save = store.checkpoints.save

    # 在请求前的摘要检查点提交后停止，正式模型尚未执行
    def crash_after_compaction(cp: Any) -> None:
        original_save(cp)
        if (
            cp.run and cp.run.phase == "llm"
            and str(cp.messages[0]["content"]).startswith(SUMMARY_MARKER)
        ):
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", crash_after_compaction)
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "long original goal")
    assert a.calls == 0
    saved = store.checkpoints.load(session.id)
    assert saved.run.step == 1
    monkeypatch.setattr(store.checkpoints, "save", original_save)
    restored = manager(store, model, [a])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    assert model.calls[0]["messages"][0]["content"].startswith(SUMMARY_MARKER)
    assert model.calls[0]["step"] == 1 and a.calls == 1
    assert store.read_messages(session.id)[-1]["content"][0]["text"] == "done"
    await restored.send_message(session.id, "followup")
    assert model.calls[-1]["messages"][0]["content"].startswith(SUMMARY_MARKER)
    assert store.read_messages(session.id)[-1]["content"][0]["text"] == "followed"


# 功能：验证未完成子任务阻止续跑，但仍允许用户结束原 Run
# 设计：向真实中断快照加入子任务标记，避免通过隐式重建任务树重复副作用
async def test_unfinished_child_blocks_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([Crash()]), [])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    restored = manager(store, Provider([]), [])
    cp = store.checkpoints.load(session.id)
    cp.run.subagents["child"] = "running"
    store.checkpoints.save(cp)
    with pytest.raises(HandlerError, match="subagent"):
        await restored.resume(session.id, cp.revision)
    await restored.resume(session.id, cp.revision, "abort")


# 功能：验证请求返回接受时已存在可恢复的初始 Run
# 设计：不让刚登记的任务获得执行机会即取消，检查原始目标和 Run 身份仍在磁盘
async def test_initial_checkpoint_precedes_task_scheduling(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([]), [])
    session = await mgr.create("chat")
    run_id = await mgr.send_message(session.id, "accepted goal", wait=False)
    cp = store.checkpoints.load(session.id)
    assert cp.run.run_id == run_id and cp.run.step == 0
    assert cp.messages[-1]["content"] == "accepted goal"
    task = mgr._tasks[session.id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    restored = manager(store, Provider([LlmResponse("end_turn", text="done")]), [])
    opened = await restored.open(session.id)
    assert opened["run"]["status"] == "interrupted"
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    assert store.checkpoints.load(session.id).run.run_id == run_id


# 功能：验证工具结果保存失败后不执行下一个工具，也不发布成功终态
# 设计：在第一个工具已产生副作用但结果提交前注入磁盘故障，恢复必须看到未知结果
async def test_result_save_failure_stops_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    a, b = ProbeTool("a"), ProbeTool("b")
    bus = EventBus()
    events: list[Any] = []

    # 收集业务终态，避免把协程异常当成完整验收
    async def collect(event: Any) -> None:
        events.append(event)

    bus.subscribe(collect)
    mgr = manager(store, Provider([tool_response("a", "b")]), [a, b], bus)
    session = await mgr.create("chat")
    original = store.checkpoints.save

    # 阻止副作用之后的第一份结果提交
    def fail_result(cp: Any) -> None:
        if cp.run and cp.run.tool_index == 1:
            raise CheckpointError("disk full")
        original(cp)

    monkeypatch.setattr(store.checkpoints, "save", fail_result)
    with pytest.raises(CheckpointError):
        await mgr.send_message(session.id, "goal")
    assert (a.calls, b.calls) == (1, 0)
    assert not any(e.type == "run.finished" and e.status == "success" for e in events)
    assert not any(e.type == "tool.call_finished" for e in events)
    monkeypatch.setattr(store.checkpoints, "save", original)
    restored = manager(store, Provider([]), [a, b])
    opened = await restored.open(session.id)
    assert opened["run"]["tools"][0]["status"] == "unknown"


# 功能：验证成功事件订阅者故障不会触发工具重执行
# 设计：工具成功后故意抛观察异常，检查真实副作用次数和最终检查点
async def test_observer_failure_does_not_retry_tool(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    bus = EventBus()

    # 模拟日志或界面观察者在成功结果之后故障
    async def observer(event: Any) -> None:
        if event.type == "tool.call_finished":
            raise RuntimeError("observer failed")

    bus.subscribe(observer)
    a = ProbeTool("a")
    mgr = manager(
        store, Provider([tool_response("a"), LlmResponse("end_turn", text="done")]), [a], bus
    )
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "goal")
    assert a.calls == 1 and store.checkpoints.load(session.id).run.status == "success"


# 功能：验证工具 schema 改变时不继续执行或调用模型
# 设计：在模型调用中断后替换 schema，断言恢复保留明确错误并维持原 Step
async def test_changed_schema_blocks_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    a = ProbeTool("a")
    mgr = manager(store, Provider([Crash()]), [a])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    a.input_schema = {"type": "object", "properties": {"new": {"type": "string"}}}
    provider = Provider([])
    restored = manager(store, provider, [a])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    cp = store.checkpoints.load(session.id)
    assert "schemas changed" in cp.run.reason
    assert cp.run.status == "interrupted" and not provider.calls and a.calls == 0


# 功能：验证工作目录不匹配时不能在错误项目续跑
# 设计：修改保存的工作目录后直接通过恢复接口验证，不实际访问其他项目文件
async def test_workspace_mismatch_blocks_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([Crash()]), [])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    restored = manager(store, Provider([]), [])
    cp = store.checkpoints.load(session.id)
    cp.run.workspace = str(tmp_path / "different-project")
    store.checkpoints.save(cp)
    with pytest.raises(HandlerError, match="workspace"):
        await restored.resume(session.id, cp.revision)


# 功能：验证重启前等待审批的工具在恢复后重新申请审批
# 设计：在审批检查点提交后硬中断，使用全新 PermissionManager 验证不存在复用 Future
async def test_restart_recreates_permission_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    a = ProbeTool("a")
    provider = Provider([tool_response("a"), LlmResponse("end_turn", text="done")])
    permissions = PermissionManager({"a": ToolPolicy(default=PermissionDecision.ASK)}, timeout_s=0)
    mgr = manager(store, provider, [a], permissions=permissions)
    session = await mgr.create("chat")
    original = store.checkpoints.save

    # 在尚未执行工具的审批阶段停止
    def crash_at_approval(cp: Any) -> None:
        original(cp)
        if cp.run and cp.run.tools and cp.run.tools[0].status == "awaiting_approval":
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", crash_at_approval)
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    monkeypatch.setattr(store.checkpoints, "save", original)
    fresh_permissions = PermissionManager(
        {"a": ToolPolicy(default=PermissionDecision.ASK)},
        timeout_s=0,
    )
    bus = EventBus()
    requested = asyncio.Event()

    # 精确等待新的审批事件产生
    async def observe(event: Any) -> None:
        if event.type == "permission.requested":
            requested.set()

    bus.subscribe(observe)
    restored = manager(store, provider, [a], bus, fresh_permissions)
    opened = await restored.open(session.id)
    assert not opened["pending_permissions"]
    await restored.resume(session.id, opened["revision"])
    await asyncio.wait_for(requested.wait(), 2)
    assert a.calls == 0 and fresh_permissions is not permissions
    pending = fresh_permissions.pending_for_session(session.id)[0]
    fresh_permissions.respond(
        pending["request_id"], "allow_once", session_id=session.id, run_id=pending["run_id"],
    )
    await finish(restored, session.id)
    assert a.calls == 1


# 功能：验证恢复不会刷新已经耗尽的步数预算
# 设计：在最后一步工具结果提交后中断，恢复只做结束判定而不额外请求模型
async def test_resume_does_not_reset_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    a = ProbeTool("a")
    provider = Provider([tool_response("a")])
    mgr = manager(store, provider, [a])
    factory = mgr._runner_factory

    # 将原 Run 固定为一步预算
    def one_step() -> AgentRunner:
        runner = factory()
        runner._config.agent.max_steps = 1
        return runner

    mgr._runner_factory = one_step
    session = await mgr.create("chat")
    original = store.checkpoints.save

    # 在工具结果已保存但步数终止尚未处理时停止
    def crash_at_result(cp: Any) -> None:
        original(cp)
        if cp.run and cp.run.phase == "tools" and cp.run.tool_index == 1:
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", crash_at_result)
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    monkeypatch.setattr(store.checkpoints, "save", original)
    restored = manager(store, provider, [a])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    cp = store.checkpoints.load(session.id)
    assert cp.run.step == cp.run.max_steps == 1
    assert cp.run.reason == "exceeded_max_steps" and len(provider.calls) == 1


# 功能：验证恢复使用保存的系统提示，而非重启时重新读取的记忆
# 设计：分别在中断前后注入不同项目记忆，检查模型收到的精确提示字符串
async def test_resume_uses_frozen_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStore(tmp_path)
    monkeypatch.setattr("kama_claude.core.runner.build_memory_context", lambda *a, **k: MemorySnapshot(text="original memory"))
    provider = Provider([Crash(), LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider, [])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    monkeypatch.setattr("kama_claude.core.runner.build_memory_context", lambda *a, **k: MemorySnapshot(text="changed memory"))
    restored = manager(store, provider, [])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    assert provider.calls[0]["system"] == provider.calls[1]["system"]
    assert "original memory" in provider.calls[1]["system"]


# 功能：验证旧会话首次打开可导入历史，并以检查点作为后续权威状态
# 设计：手工写入旧格式文件，导入后再改变兼容文件，确认新读取不受它影响
async def test_import_legacy_session_once(tmp_path: Path) -> None:
    sid = "sess-legacy"
    folder = tmp_path / sid
    folder.mkdir()
    (folder / "meta.json").write_text(
        json.dumps(
            {
                "id": sid,
                "mode": "chat",
                "status": "waiting_for_input",
                "title": "old",
                "created_at": "t",
                "updated_at": "t",
                "run_ids": ["old-run"],
            }
        ),
        encoding="utf-8",
    )
    (folder / "thread.jsonl").write_text(
        '{"role":"user","content":"old goal"}\n{"role":"assistant","content":"old answer"}\n',
        encoding="utf-8",
    )
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([]), [])
    opened = await mgr.open(sid)
    assert opened["run"] is None and len(opened["messages"]) == 2
    (folder / "thread.jsonl").write_text("broken", encoding="utf-8")
    assert store.read_messages(sid)[1]["content"] == "old answer"


# 功能：验证副作用工具抛普通异常时也保守停止，不把不确定结果交给模型继续执行
# 设计：操作发生后抛异常但不模拟硬退出，确认程序不自动重试或调用下一个模型步骤
async def test_side_effect_exception_is_unknown(tmp_path: Path) -> None:
    class UncertainTool(ProbeTool):
        # 在已经发生副作用的位置报告丢失结果
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            self.calls += 1
            raise RuntimeError("lost result after side effect")

    store = SessionStore(tmp_path)
    tool = UncertainTool("a")
    provider = Provider([tool_response("a")])
    mgr = manager(store, provider, [tool])
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "goal")
    cp = store.checkpoints.load(session.id)
    assert cp.run.status == "interrupted"
    assert cp.run.tools[0].status == "unknown"
    assert tool.calls == 1 and len(provider.calls) == 1


# 功能：验证合法 JSON 内部消息结构损坏时不会进入模型或 TUI
# 设计：直接破坏已有快照的 content 类型，确认只隔离该会话
async def test_invalid_message_shape_is_unavailable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([]), [])
    session = await mgr.create("chat")
    raw = store.checkpoints.load(session.id).model_dump()
    raw["messages"] = [{"role": "user", "content": 123}]
    store.checkpoints.path(session.id).write_text(json.dumps(raw), encoding="utf-8")
    restored = manager(store, Provider([]), [])
    assert restored.list_sessions()[0]["status"] == "unavailable"
    another = await restored.create("chat")
    assert (await restored.open(another.id))["messages"] == []


# 功能：验证会话事件复用 Runner 的终态提交，元数据输出完整且不重复保存
# 设计：记录 Run 与会话通知时的快照，禁止 Manager 再调用 write_meta，覆盖两种会话模式
@pytest.mark.parametrize("mode", ["chat", "one_shot"])
async def test_session_notification_reuses_runner_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Any,
) -> None:
    store = SessionStore(tmp_path)
    bus = EventBus()
    commits: list[tuple[str, int, dict[str, Any], dict[str, Any]]] = []
    mgr = manager(store, Provider([LlmResponse("end_turn", text="done")]), [], bus)
    session = await mgr.create(mode)

    # 会话完成通知必须观察到与 Run 结束通知相同的已提交状态
    async def observe(event: Any) -> None:
        if event.type in ("run.finished", "session.waiting_for_input", "session.closed"):
            cp = store.checkpoints.load(session.id)
            metadata = store.session_dir(session.id) / "meta.json"
            exported = json.loads(metadata.read_text(encoding="utf-8"))
            commits.append((event.type, cp.revision, cp.session, exported))

    # 正常完成后只允许导出元数据，不能再次走会提交检查点的 write_meta
    def reject_duplicate_save(current: Any) -> None:
        raise AssertionError("Manager must reuse the Runner commit")

    bus.subscribe(observe)
    monkeypatch.setattr(store, "write_meta", reject_duplicate_save)
    await mgr.send_message(session.id, "goal")
    assert [row[0] for row in commits] == [
        "run.finished", "session.closed" if mode == "one_shot" else "session.waiting_for_input",
    ]
    assert commits[0][1:] == commits[1][1:]
    assert all(row[2] == row[3] for row in commits)
    expected = "closed" if mode == "one_shot" else "waiting_for_input"
    assert commits[0][2]["status"] == expected
    assert mgr._sessions[session.id].to_dict() == commits[0][2]


# 功能：验证恢复决定保存失败时不启动业务任务、不修改已提交决定或发布成功
# 设计：中断模型调用后注入存储错误，直接核对磁盘快照和活跃 Task，排除仅前端报错的假保护
async def test_resume_save_failure_does_not_start_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([Crash()]), [])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    provider = Provider([])
    restored = manager(store, provider, [])
    cp = store.checkpoints.load(session.id)
    original = cp.model_dump()

    # 在应用恢复决定后、启动前的唯一提交点阻止写入
    def fail_save(current: Any) -> None:
        raise CheckpointError("disk full")

    monkeypatch.setattr(store.checkpoints, "save", fail_save)
    with pytest.raises(CheckpointError):
        await restored.resume(session.id, cp.revision)
    assert store.checkpoints.load(session.id).model_dump() == original
    assert not restored.is_running(session.id) and not provider.calls
    assert restored._sessions[session.id].status == "interrupted"


# 功能：验证旧 Run 的迟到异常通知不会修改或误报后续 Run 的状态
# 设计：在新 Run 已成功提交后调用旧 Run 的核对入口，检查修订号及事件均不变
async def test_failed_task_reconciliation_ignores_later_run(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    bus = EventBus()
    events: list[Any] = []
    mgr = manager(store, Provider([LlmResponse("end_turn", text="done")]), [], bus)
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "new goal")
    before = store.checkpoints.load(session.id).model_dump()

    # 只观察这次迟到核对可能产生的通知
    async def observe(event: Any) -> None:
        events.append(event)

    bus.subscribe(observe)
    await mgr._notify_failed_task(session.id, "obsolete-run")
    assert store.checkpoints.load(session.id).model_dump() == before
    assert not events


# 功能：验证工具最终通知发生在结果、配对消息与游标提交之后
# 设计：在事件到达时读取真实磁盘快照，分别覆盖成功、明确错误和未知结果，断言放在回调外
@pytest.mark.parametrize("outcome", ["success", "error", "unknown"])
async def test_tool_notification_observes_committed_result(
    tmp_path: Path, outcome: str,
) -> None:
    class ResultTool(ProbeTool):
        # 产生确定结果或无法确认的副作用异常
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            self.calls += 1
            if outcome == "unknown":
                raise RuntimeError("outcome lost")
            return ToolResult(
                content="known result",
                is_error=outcome == "error",
                error_type="runtime_error" if outcome == "error" else None,
            )

    store = SessionStore(tmp_path)
    bus = EventBus()
    snapshots: list[tuple[str, Any]] = []
    tool = ResultTool("a")
    mgr = manager(
        store, Provider([tool_response("a"), LlmResponse("end_turn", text="done")]), [tool], bus,
    )
    session = await mgr.create("chat")

    # 记录通知当时的已提交状态，避免 publish_safe 吞掉测试断言
    async def observe(event: Any) -> None:
        if event.type in ("tool.call_finished", "tool.call_failed"):
            snapshots.append((event.type, store.checkpoints.load(session.id)))

    bus.subscribe(observe)
    await mgr.send_message(session.id, "goal")
    assert len(snapshots) == 1 and tool.calls == 1
    event_type, cp = snapshots[0]
    assert event_type == ("tool.call_finished" if outcome == "success" else "tool.call_failed")
    saved_tool = cp.run.tools[0]
    assert saved_tool.result["content"] == ("outcome lost" if outcome == "unknown" else "known result")
    results = [
        block
        for message in cp.messages
        if isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result" and block.get("tool_use_id") == "id-a"
    ]
    if outcome == "unknown":
        assert saved_tool.status == "unknown" and cp.run.status == "interrupted"
        assert cp.run.tool_index == 0 and not results
    else:
        assert saved_tool.status == "completed" and cp.run.tool_index == 1
        assert len(results) == 1 and results[0].get("is_error", False) == (outcome == "error")


# 功能：验证审批前或执行前保存失败时，工具不会执行或发出完成通知
# 设计：在两个前置保存点注入错误，使用自动允许策略避免人为审批影响执行边界
@pytest.mark.parametrize("phase", ["awaiting_approval", "executing"])
async def test_tool_phase_save_failure_prevents_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    store = SessionStore(tmp_path)
    bus = EventBus()
    events: list[Any] = []
    permissions = PermissionManager(
        {"a": ToolPolicy(default=PermissionDecision.ALLOW)}, timeout_s=0,
    )
    tool = ProbeTool("a")
    mgr = manager(store, Provider([tool_response("a")]), [tool], bus, permissions)
    session = await mgr.create("chat")
    original = store.checkpoints.save

    # 收集通知，确保前置提交失败不会被误报为工具完成
    async def observe(event: Any) -> None:
        events.append(event)

    # 只阻止指定工具阶段的快照提交
    def fail_phase(cp: Any) -> None:
        if cp.run and cp.run.tools and cp.run.tools[0].status == phase:
            raise CheckpointError("cannot save tool phase")
        original(cp)

    bus.subscribe(observe)
    monkeypatch.setattr(store.checkpoints, "save", fail_phase)
    with pytest.raises(CheckpointError):
        await mgr.send_message(session.id, "goal")
    assert tool.calls == 0 and not permissions._pending
    assert not any(event.type in ("tool.call_finished", "tool.call_failed") for event in events)


# 功能：验证恢复后的最终重试通知使用本次尝试次数，而不是跨恢复的累计次数
# 设计：首次执行硬中断后确认重试，再连续返回三次明确错误，对照通知与累计计数
async def test_resumed_tool_reports_current_retry_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryTool(ProbeTool):
        retry_safe = True

        # 首次硬中断，后续明确返回可重试错误
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            self.calls += 1
            if self.calls == 1:
                raise Crash()
            return ToolResult(content="known failure", is_error=True, error_type="runtime_error")

    monkeypatch.setattr("kama_claude.core.tools.invocation._RETRY_BASE_S", 0.0)
    store = SessionStore(tmp_path)
    tool = RetryTool("a")
    provider = Provider([tool_response("a"), LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider, [tool])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    bus = EventBus()
    attempts: list[tuple[int, int]] = []
    restored = manager(store, provider, [tool], bus)
    opened = await restored.open(session.id)

    # 对照本次重试事件和磁盘中的跨恢复累计尝试数
    async def observe(event: Any) -> None:
        if event.type == "tool.call_failed" and not event.retrying:
            cp = store.checkpoints.load(session.id)
            attempts.append((event.attempt, cp.run.tools[0].attempts))

    bus.subscribe(observe)
    await restored.resume(session.id, opened["revision"], "retry_tool")
    await finish(restored, session.id)
    assert attempts == [(3, 4)] and tool.calls == 4


# 功能：验证 Loop 已提交中断但 Session 尚未收尾时，重启会补齐会话中断状态
# 设计：分别在模型错误和未知工具结果提交后硬中断，保留真实磁盘窗口，再检查重复打开不会重复提交
@pytest.mark.parametrize("phase", ["llm", "tools"])
async def test_restart_repairs_interruption_before_session_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    class UncertainTool(ProbeTool):
        # 模拟副作用可能发生但结果无法确认
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            self.calls += 1
            raise RuntimeError("tool outcome lost")

    store = SessionStore(tmp_path)
    tool = UncertainTool("a")
    response = RuntimeError("model connection lost") if phase == "llm" else tool_response("a")
    mgr = manager(store, Provider([response]), [tool])
    session = await mgr.create("chat")
    original = store.checkpoints.save

    # 只在 Run 已中断而 Session 仍 active 的快照提交后停止
    def crash_before_session_finalization(cp: Any) -> None:
        original(cp)
        if cp.run and cp.run.status == "interrupted" and cp.session["status"] == "active":
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", crash_before_session_finalization)
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    monkeypatch.setattr(store.checkpoints, "save", original)
    before = store.checkpoints.load(session.id)
    assert before.run.status == "interrupted" and before.session["status"] == "active"
    restored = manager(store, Provider([]), [tool])
    opened = await restored.open(session.id)
    assert opened["run"]["status"] == opened["session"]["status"] == "interrupted"
    assert opened["run"]["phase"] == before.run.phase
    assert opened["run"]["reason"] == before.run.reason
    assert opened["run"]["tool_index"] == before.run.tool_index
    if phase == "tools":
        assert opened["run"]["tools"][0]["status"] == "unknown" and tool.calls == 1
    revision = opened["revision"]
    assert revision > before.revision
    assert (await restored.open(session.id))["revision"] == revision


# 功能：验证恢复模块可以在新 Python 进程中独立导入
# 设计：使用无预热导入的新解释器，防止测试进程已加载的包掩盖循环依赖
def test_recovery_module_imports_in_fresh_process() -> None:
    result = subprocess.run(
        [
            sys.executable, "-X", "utf8", "-B", "-c",
            "from kama_claude.core.checkpoint.recovery import SessionRecovery",
        ],
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
