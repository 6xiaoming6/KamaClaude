from __future__ import annotations

import asyncio

import pytest

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.checkpoint.recovery import RECOVERY_CONFLICT
from kama_claude.core.compact.history import validate_pairs
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import ToolResult
from tests.unit.test_checkpoint_recovery import ProbeTool, Provider, manager, tool_response


class BlockingTool(ProbeTool):
    def __init__(self, name="a"):
        super().__init__(name)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def invoke(self, params):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult("saved side effect")


# 当前工具运行完、结果提交后停止；同批后续工具保持 pending，恢复不重跑第一项。
async def test_stop_waits_for_current_tool_and_resume_keeps_cursor(tmp_path):
    store = SessionStore(tmp_path)
    first, second = BlockingTool(), ProbeTool("b")
    model = Provider([tool_response("a", "b"), LlmResponse("end_turn", text="done")])
    mgr = manager(store, model, [first, second])
    session = await mgr.create("chat")
    sent = asyncio.create_task(mgr.send_message(session.id, "goal"))
    try:
        await asyncio.wait_for(first.started.wait(), 2)
        run_id = store.checkpoints.load(session.id).run.run_id
        state = await mgr.interrupt(session.id, run_id)
        assert state["status"] == "stopping" and mgr.is_running(session.id)
        assert not first.cancelled and second.calls == 0
        assert store.checkpoints.load(session.id).run.tools[0].status == "executing"
        first.release.set()
        assert await asyncio.wait_for(sent, 2) == run_id
        cp = store.checkpoints.load(session.id)
        assert cp.run.reason == "user_stopped" and cp.run.status == "interrupted"
        assert cp.session["status"] == "interrupted" and cp.run.tool_index == 1
        assert [t.status for t in cp.run.tools] == ["completed", "pending"]
        assert cp.run.tools[0].result["outcome_unknown"] is False
        assert not first.cancelled
        await mgr.resume(session.id, cp.revision, "continue")
        await asyncio.wait_for(asyncio.shield(mgr._tasks[session.id]), 2)
        assert first.calls == second.calls == 1
        assert store.checkpoints.load(session.id).run.status == "success"
    finally:
        first.release.set()
        await mgr.shutdown()
        await asyncio.gather(sent, return_exceptions=True)


# 重复请求幂等；等待停止的 RPC 断开，不会取消正在执行的工具。
async def test_cancel_stop_waiter_does_not_cancel_tool(tmp_path):
    store = SessionStore(tmp_path)
    tool = BlockingTool()
    mgr = manager(store, Provider([tool_response("a")]), [tool])
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "goal", wait=False)
    try:
        await asyncio.wait_for(tool.started.wait(), 2)
        waiter = asyncio.create_task(mgr.interrupt(session.id, wait=True))
        await asyncio.sleep(0)
        await mgr.interrupt(session.id)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not tool.cancelled and mgr.is_running(session.id)
        task = mgr._tasks[session.id]
        tool.release.set()
        await asyncio.wait_for(asyncio.shield(task), 2)
        assert store.checkpoints.load(session.id).run.status == "interrupted"
    finally:
        tool.release.set()
        await mgr.shutdown()


# 停在审批位置，既不执行工具，也不把待审批调用误记成一次拒绝或未知副作用。
async def test_stop_approval_preserves_unexecuted_tool(tmp_path):
    store = SessionStore(tmp_path)
    tool = ProbeTool("a")
    permissions = PermissionManager({"a": ToolPolicy(default=PermissionDecision.ASK)}, timeout_s=0)
    mgr = manager(store, Provider([tool_response("a")]), [tool], permissions=permissions)
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "goal", wait=False)
    try:
        async with asyncio.timeout(2):
            while not permissions.pending_for_session(session.id):
                await asyncio.sleep(0)
        result = await mgr.interrupt(session.id, wait=True)
        cp = store.checkpoints.load(session.id)
        assert result["status"] == "interrupted" and tool.calls == 0
        assert cp.run.tools[0].status == "awaiting_approval"
        assert not permissions.pending_for_session(session.id)
        await mgr.close(session.id)
        cp = store.checkpoints.load(session.id)
        assert cp.session["status"] == "closed" and cp.run.reason == "session_closed"
        assert validate_pairs(cp.messages)
        assert store.read_history(session.id)[-1]["content"][0]["is_error"]
    finally:
        await mgr.shutdown()


# 模型请求完成后先保存完整回复，再停止，不执行回复中尚未开始的工具。
async def test_stop_waits_for_model_response_without_executing_its_tools(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(Provider):
        async def chat(self, **kwargs):
            started.set()
            await release.wait()
            return await super().chat(**kwargs)

    store = SessionStore(tmp_path)
    tool = ProbeTool("a")
    mgr = manager(store, WaitingProvider([tool_response("a")]), [tool])
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "goal", wait=False)
    try:
        await asyncio.wait_for(started.wait(), 2)
        await mgr.interrupt(session.id)
        task = mgr._tasks[session.id]
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 2)
        cp = store.checkpoints.load(session.id)
        assert cp.run.phase == "tools" and cp.run.tools[0].status == "pending"
        assert tool.calls == 0 and cp.run.reason == "user_stopped"
    finally:
        release.set()
        await mgr.shutdown()


async def test_stop_before_task_starts_and_stale_identity(tmp_path):
    store = SessionStore(tmp_path)
    model = Provider([])
    mgr = manager(store, model, [])
    session = await mgr.create("chat")
    run_id = await mgr.send_message(session.id, "goal", wait=False)
    with pytest.raises(HandlerError) as error:
        await mgr.interrupt(session.id, "different-run")
    assert error.value.code == RECOVERY_CONFLICT
    result = await mgr.interrupt(session.id, run_id, wait=True)
    assert result["status"] == "interrupted" and not model.calls
    assert store.checkpoints.load(session.id).run.step == 0
    assert not mgr._stop_requests
    await mgr.close(session.id)


async def test_close_keeps_unknown_outcome_in_complete_history(tmp_path):
    from tests.unit.test_checkpoint_recovery import Crash

    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([tool_response("a")]), [ProbeTool("a", crash=True)])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    await mgr.open(session.id)
    await mgr.close(session.id)
    cp = store.checkpoints.load(session.id)
    assert cp.session["status"] == "closed" and validate_pairs(cp.messages)
    assert "UNKNOWN" in cp.messages[-1]["content"][0]["content"]


async def test_stop_after_context_preparation_does_not_start_another_request(tmp_path, monkeypatch):
    from tests.unit.test_compactor import provider
    from tests.unit.test_s9_context import make_loop

    model = provider()
    loop, context, _ = make_loop(tmp_path, [{"role": "user", "content": "goal"}], model)
    context.stop_event = asyncio.Event()

    async def prepare(*args):
        context.stop_event.set()
        return True

    monkeypatch.setattr(loop._context_manager, "prepare", prepare)
    await loop.run()
    assert context.run.reason == "user_stopped" and context.run.phase == "llm"
    model.chat.assert_not_called()


@pytest.mark.parametrize("background", [False, True])
async def test_child_agent_finishes_current_tool_before_shared_stop(tmp_path, monkeypatch, background):
    from kama_claude.core.tools.registry import ToolRegistry
    from tests.unit.test_subagent_runtime import Provider as ChildProvider
    from tests.unit.test_subagent_runtime import spawn_tool

    stop = asyncio.Event()
    probe = BlockingTool()
    registry = ToolRegistry()
    registry.register(probe)
    spawner = spawn_tool(tmp_path, ChildProvider(tool_response("a")), stop_event=stop)
    monkeypatch.setattr(spawner, "_build_child_registry", lambda *args, **kwargs: registry)
    spawned = asyncio.create_task(spawner.invoke({
        "description": "test", "prompt": "goal", "run_in_background": background,
    }))
    try:
        await asyncio.wait_for(probe.started.wait(), 2)
        entry = next(iter(spawner._task_registry._tasks.values()))
        assert entry.context.stop_event is stop
        stop.set()
        await asyncio.sleep(0)
        assert not probe.cancelled and not entry.task.done()
        probe.release.set()
        await asyncio.wait_for(spawner._task_registry.wait_children("parent", "session"), 2)
        await asyncio.wait_for(spawned, 2)
        assert entry.context.run.reason == "user_stopped"
        assert entry.context.run.tools[0].status == "completed"
        assert not probe.cancelled
    finally:
        probe.release.set()
        await spawner._task_registry.cancel()
        await asyncio.gather(spawned, return_exceptions=True)
