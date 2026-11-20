from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.checkpoint.checkpoint import (
    Checkpoint,
    CheckpointError,
    RunCheckpoint,
    ToolCheckpoint,
)
from kama_claude.core.checkpoint.persistence import RunPersistence
from kama_claude.core.checkpoint.recovery import SessionRecovery
from kama_claude.core.compact.history import validate_pairs
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionContext, RunStopRequested
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.mode import PermissionMode
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.batch import ToolBatchExecutor
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


class ControlledRead(ReadFileTool):
    # 只替换文件 I/O，路径、参数、审批、重试和批次保存仍走真实代码。
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls: list[str] = []
        self.started: dict[str, asyncio.Event] = {}
        self.entered: dict[int, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.per_call: list[asyncio.Event] = []
        self.outputs: dict[str, ToolResult] = {}
        self.active = self.peak = 0

    # 通过门闩控制每个完成时机，不用耗时断言证明是否并发。
    async def _invoke_path(self, params: dict[str, object], path: Path) -> ToolResult:
        index = len(self.calls)
        name = path.name
        self.calls.append(name)
        self.entered.setdefault(len(self.calls), asyncio.Event()).set()
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.setdefault(name, asyncio.Event()).set()
        gate = self.per_call[index] if self.per_call else self.gates.setdefault(name, asyncio.Event())
        try:
            await gate.wait()
            return deepcopy(self.outputs.get(name, ToolResult(f"read:{name}")))
        finally:
            self.active -= 1


class SerialTool(BaseTool):
    name = "serial"
    description = "controlled serial barrier"
    input_schema = {"type": "object", "properties": {}}
    retry_safe = True

    # 记录串行操作发生时还有多少读取运行，确认屏障生效。
    def __init__(self, read: ControlledRead) -> None:
        self.read = read
        self.observed: list[int] = []

    # 没有文件副作用，仅记录执行瞬间的并发数。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.observed.append(self.read.active)
        return ToolResult("serial result")


@dataclass
class Batch:
    context: ExecutionContext
    executor: ToolBatchExecutor
    read: ControlledRead
    serial: SerialTool
    store: SessionStore
    persistence: RunPersistence
    session: Session
    events: list[Any]
    finished: dict[str, asyncio.Event]


# 构造真实检查点与历史存储，测试只替换外部文件读取的完成时机。
def make_batch(
    root: Path, paths: list[str], *, limit: int = 3, permissions: PermissionManager | None = None,
) -> Batch:
    read = ControlledRead(root)
    serial = SerialTool(read)
    registry = ToolRegistry()
    registry.register(read)
    registry.register(serial)
    tools = [ToolCheckpoint(id=f"t{i}", name="serial" if path == "!" else "read_file",
                            input={} if path == "!" else {"path": path}, output_prepared=False)
             for i, path in enumerate(paths)]
    run = RunCheckpoint(run_id="parallel-run", goal="inspect", model="fake", workspace=str(root),
                        max_steps=0, phase="tools", step=1, tools=tools)
    context = ExecutionContext(run)
    context.add_assistant_message([
        {"type": "tool_use", "id": tool.id, "name": tool.name, "input": tool.input}
        for tool in tools
    ])
    session = Session("sess-parallel", "chat", "active", "", "now", "now")
    store = SessionStore(root / "sessions")
    cp = Checkpoint(session=session.to_dict(), messages=context.messages, run=run)
    persistence = RunPersistence(context, cp, session, store)
    persistence.save()
    bus = EventBus()
    events: list[Any] = []
    finished: dict[str, asyncio.Event] = {}

    # 通知发生前检查点已经提交，事件门闩可用来定位可靠的故障边界。
    async def collect(event: Any) -> None:
        events.append(event)
        if event.type in {"tool.call_finished", "tool.call_failed"}:
            finished.setdefault(event.tool_use_id, asyncio.Event()).set()

    bus.subscribe(collect)
    executor = ToolBatchExecutor(
        context, registry, bus, RunBudget(run), persistence=persistence,
        permission_manager=permissions, session_id=session.id, parallel_limit=limit,
    )
    return Batch(context, executor, read, serial, store, persistence, session, events, finished)


# 等待已启动的读取，不以睡眠长度猜测是否成功调度。
async def started(batch: Batch, *names: str) -> None:
    await asyncio.wait_for(asyncio.gather(*[
        batch.read.started.setdefault(name, asyncio.Event()).wait() for name in names
    ]), 5)


# 等待已经提交并发布的结果，可安全在此模拟退出或崩溃。
async def finished(batch: Batch, tool_id: str) -> None:
    await asyncio.wait_for(batch.finished.setdefault(tool_id, asyncio.Event()).wait(), 5)


# 释放指定读取，调用前后均可设置门闩。
def release(batch: Batch, *names: str) -> None:
    for name in names:
        batch.read.gates.setdefault(name, asyncio.Event()).set()


# 无论断言是否失败都回收测试创建的任务，不污染其他异步用例。
async def cleanup(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# 提取工作历史或完整历史里的调用结果 ID。
def result_ids(messages: list[dict[str, Any]]) -> list[str]:
    return [block["tool_use_id"] for message in messages
            if message["role"] == "user" and isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "tool_result"]


# 功能：默认三路并发，第四个等前组结束，串行操作等待所有先行读取。
# 设计：逐个释放门闩并读取磁盘快照，同时检查分组、屏障和两种历史的顺序。
async def test_bounded_groups_barrier_and_order(tmp_path):
    batch = make_batch(tmp_path, ["a", "b", "c", "d", "!", "e", "f"])
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a", "b", "c")
        assert batch.read.peak == 3 and "d" not in batch.read.calls
        release(batch, "b")
        await finished(batch, "t1")
        cp = batch.store.checkpoints.load(batch.session.id)
        assert cp.run.tools[1].status == "completed" and cp.run.tool_index == 0
        assert result_ids(cp.messages) == ["t1"]
        assert "d" not in batch.read.calls and not batch.serial.observed
        release(batch, "a", "c")
        await started(batch, "d")
        assert not batch.serial.observed
        release(batch, "d")
        await started(batch, "e", "f")
        assert batch.serial.observed == [0]
        release(batch, "f", "e")
        assert await asyncio.wait_for(task, 5)
        cp = batch.store.checkpoints.load(batch.session.id)
        assert result_ids(cp.messages) == [f"t{i}" for i in range(7)]
        assert result_ids(batch.store.read_history(batch.session.id))[0] == "t1"
        assert validate_pairs(cp.messages) and cp.run.tool_index == 7
    finally:
        await cleanup(task)
    assert batch.read.active == 0


# 功能：B 先完成、A 被取消后，重建执行器能恢复 A 并复用 B。
# 设计：读取真实原子快照，走 orphan 与 retry_tool，验证状态、Step 和调用次数。
async def test_restore_skips_out_of_order_completed_tool(tmp_path):
    batch = make_batch(tmp_path, ["a", "b"])
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a", "b")
        release(batch, "b")
        await finished(batch, "t1")
    finally:
        await cleanup(task)
    assert batch.read.active == 0
    recovery = SessionRecovery(batch.store)
    cp = recovery.recover_orphaned_run(batch.session.id, running=False)
    assert [t.status for t in cp.run.tools] == ["unknown", "completed"]
    # 恢复校验要求 Core 仍运行在原工作目录，这里只替换该只读比较的来源。
    from unittest.mock import patch
    with patch("kama_claude.core.checkpoint.recovery.Path.cwd", return_value=tmp_path):
        recovery.prepare_resume(cp, cp.revision, "retry_tool")
    batch.store.save_checkpoint(cp)
    restored = ExecutionContext(cp.run, cp.messages)
    read = ControlledRead(tmp_path)
    read.gates["a"] = asyncio.Event()
    read.gates["a"].set()
    registry = ToolRegistry()
    registry.register(read)
    persistence = RunPersistence(restored, cp, batch.session, batch.store)
    batch.session.status = "active"
    executor = ToolBatchExecutor(restored, registry, EventBus(), RunBudget(restored.run),
                                 persistence=persistence)
    assert await executor.run_pending()
    assert read.calls == ["a"] and restored.run.step == 1
    saved = batch.store.checkpoints.load(batch.session.id)
    assert result_ids(saved.messages) == ["t0", "t1"] and validate_pairs(saved.messages)
    assert result_ids(batch.store.read_history(batch.session.id)) == ["t1", "t0"]


# 功能：终止中断 Run 时保留已完成结果，为较早未完成工具补齐错误并排序。
# 设计：B 已落盘后取消 A，再使用真实恢复入口终止，检查没有覆盖或重复结果。
async def test_abort_preserves_later_completed_result(tmp_path):
    batch = make_batch(tmp_path, ["a", "b"])
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a", "b")
        release(batch, "b")
        await finished(batch, "t1")
    finally:
        await cleanup(task)
    recovery = SessionRecovery(batch.store)
    cp = recovery.recover_orphaned_run(batch.session.id, running=False)
    recovery.prepare_resume(cp, cp.revision, "abort")
    batch.store.save_checkpoint(cp)
    saved = batch.store.checkpoints.load(batch.session.id)
    assert saved.run.status == "failed" and saved.run.tools[1].result["content"] == "read:b"
    assert result_ids(saved.messages) == ["t0", "t1"] and validate_pairs(saved.messages)
    assert result_ids(batch.store.read_history(batch.session.id)) == ["t1", "t0"]


# 功能：预算终止不覆盖游标之后已完成的工具，并给未执行项补齐结果。
# 设计：使用乱序提交后的真实快照，将尚未开始的项留为 pending 后触发预算收尾。
async def test_budget_stop_keeps_committed_later_results(tmp_path):
    batch = make_batch(tmp_path, ["a", "b"])
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a", "b")
        release(batch, "b")
        await finished(batch, "t1")
    finally:
        await cleanup(task)
    batch.context.run.tools[0].status = "pending"
    batch.executor.stop_for_budget("test_budget")
    cp = batch.store.checkpoints.load(batch.session.id)
    assert cp.run.tools[1].result["content"] == "read:b"
    assert result_ids(cp.messages) == ["t0", "t1"] and validate_pairs(cp.messages)
    assert cp.run.status == "failed"


# 功能：优雅停止等待已经启动的读取，但不启动下一组。
# 设计：停止事件在两项运行中置位，B 先完成后仍等待 A，第三项始终未调用。
async def test_stop_waits_for_started_group_only(tmp_path):
    batch = make_batch(tmp_path, ["a", "b", "c"], limit=2)
    batch.context.stop_event = asyncio.Event()
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a", "b")
        batch.context.stop_event.set()
        release(batch, "b")
        await finished(batch, "t1")
        assert not task.done() and "c" not in batch.read.calls
        release(batch, "a")
        with pytest.raises(RunStopRequested):
            await asyncio.wait_for(task, 5)
        cp = batch.store.checkpoints.load(batch.session.id)
        assert [t.status for t in cp.run.tools] == ["completed", "completed", "pending"]
        assert cp.run.tool_index == 2
    finally:
        await cleanup(task)


# 功能：执行意图或结果保存失败时不启动下一组，不发布未提交的成功结果。
# 设计：在真实存储边界注入失败，并验证同组 Task 已被回收。
@pytest.mark.parametrize("boundary", ["intent", "result"])
async def test_checkpoint_failure_blocks_execution_and_reaps_tasks(tmp_path, monkeypatch, boundary):
    batch = make_batch(tmp_path, ["a", "b", "c"], limit=2)
    save = batch.store.checkpoints.save

    # 意图提交失败必须零执行，结果提交失败允许已有读取启动但不能发成功通知。
    def fail(cp):
        if boundary == "intent" and any(t.status == "executing" for t in cp.run.tools):
            raise CheckpointError("intent disk full")
        if boundary == "result" and cp.run.tools[1].status == "completed":
            raise CheckpointError("result disk full")
        return save(cp)

    monkeypatch.setattr(batch.store.checkpoints, "save", fail)
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        if boundary == "result":
            await started(batch, "a", "b")
            release(batch, "b")
        with pytest.raises(CheckpointError, match="disk full"):
            await asyncio.wait_for(task, 5)
        assert "c" not in batch.read.calls and batch.read.active == 0
        assert not any(e.type == "tool.call_finished" for e in batch.events)
        if boundary == "intent":
            assert not batch.read.calls
    finally:
        await cleanup(task)


# 功能：显式询问与禁止策略保持串行，full 模式可并行允许的内部读取。
# 设计：用真实权限管理器即时回答，检查审批数量与实际读取峰值。
@pytest.mark.parametrize("decision,mode", [
    (PermissionDecision.ASK, PermissionMode.ASK),
    (PermissionDecision.DENY, PermissionMode.FULL),
    (PermissionDecision.ASK, PermissionMode.FULL),
])
async def test_permission_modes_and_serial_approval(tmp_path, decision, mode):
    permission = PermissionManager(policies={"read_file": ToolPolicy(decision)})
    batch = make_batch(tmp_path, ["a", "b"], permissions=permission)
    permission.set_mode(batch.session.id, mode)
    requested = []

    # 自动回答只用于确认审批仍走原路径，不等待真实用户。
    async def answer(event):
        if event.type == "permission.requested":
            requested.append(event.tool_use_id)
            permission.respond(event.request_id, "allow_once", session_id=event.session_id,
                               run_id=event.run_id)

    batch.executor._bus.subscribe(answer)
    if decision != PermissionDecision.DENY:
        release(batch, "a", "b")
    assert await asyncio.wait_for(batch.executor.run_pending(), 5)
    if decision == PermissionDecision.DENY:
        assert not batch.read.calls and not requested
    elif mode == PermissionMode.ASK:
        assert requested == ["t0", "t1"] and batch.read.peak == 1
    else:
        assert not requested


# 功能：预检后权限收紧或路径越界，并行调用返回已知拒绝，不弹后台审批。
# 设计：在工具开始事件中改变后端事实，覆盖分组与真正执行之间的窗口。
@pytest.mark.parametrize("changed", ["policy", "path"])
async def test_parallel_revalidation_does_not_prompt(tmp_path, changed):
    permission = PermissionManager()
    batch = make_batch(tmp_path, ["a", "b"], permissions=permission)
    original = batch.read.path_access

    # 先完成分组，收到开始事件后再切换事实，不能仅依赖预检结果。
    async def change(event):
        if event.type == "tool.call_started":
            if changed == "policy":
                permission._policies["read_file"] = ToolPolicy(PermissionDecision.ASK)
            else:
                batch.read.path_access = lambda params: batch.read.paths.inspect(
                    str(tmp_path.parent / "outside.txt"),
                )

    batch.executor._bus.subscribe(change)
    assert await asyncio.wait_for(batch.executor.run_pending(), 5)
    assert not batch.read.calls
    assert not any(e.type == "permission.requested" for e in batch.events)
    assert all(t.result["error_type"] == "permission_denied" for t in batch.context.run.tools)
    batch.read.path_access = original


# 功能：外部路径和未暴露工具不能进入并行组，工具默认不允许并发。
# 设计：检查真实后端解析结果与属性默认值，模型提供同名参数也不能获得内部权限。
async def test_parallel_eligibility_is_backend_only(tmp_path):
    batch = make_batch(tmp_path, ["a", str(tmp_path.parent / "external"), "c"])
    assert [t.id for t in batch.executor._parallel_group()] == ["t0"]
    batch.context.run.tools[0].exposed = False
    assert batch.executor._parallel_group() == []
    assert not batch.serial.parallel_safe and batch.read.parallel_safe
    forged = await invoke_tool(
        batch.executor.registry, ToolCallBlock("forged", "read_file", {
            "path": "a", "allow_prompt": False, "parallel_safe": True,
        }), EventBus(), "r",
    )
    assert forged.error_type == "schema_error" and not batch.read.calls


# 功能：保护计数按声明顺序结算，不受相同调用的完成顺序影响。
# 设计：两个相同失败之间夹一个成功，逆序完成也必须最终只有一次连续失败。
async def test_failure_guard_uses_declared_order(tmp_path):
    batch = make_batch(tmp_path, ["a", "b", "a"])
    batch.read.per_call = [asyncio.Event() for _ in range(3)]
    batch.read.outputs["a"] = ToolResult("missing", is_error=True, error_type="not_found")
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a", "b")
        batch.read.per_call[2].set()
        await finished(batch, "t2")
        batch.read.per_call[1].set()
        await finished(batch, "t1")
        batch.read.per_call[0].set()
        assert await asyncio.wait_for(task, 5)
        assert batch.context.run.repeated_failures == 1
        cp = batch.store.checkpoints.load(batch.session.id)
        assert cp.run.repeated_failures == 1 and cp.run.tool_index == 3
    finally:
        await cleanup(task)


# 功能：重复失败达到上限后保留已落盘的较晚结果，不再启动下一组。
# 设计：后组第五项先完成，第四项触发停止，检查预算收尾没有覆盖第五项。
async def test_failure_limit_keeps_already_completed_peer(tmp_path):
    batch = make_batch(tmp_path, ["a"] * 7)
    batch.context.run.repeat_failure_limit = 4
    batch.read.per_call = [asyncio.Event() for _ in range(7)]
    batch.read.outputs["a"] = ToolResult("missing", is_error=True, error_type="not_found")
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a")
        for gate in batch.read.per_call[:3]:
            gate.set()
        # t2 通知后第一组已完整收尾，等待下一组开始事件完成意图提交。
        await finished(batch, "t2")
        await asyncio.wait_for(batch.read.entered.setdefault(6, asyncio.Event()).wait(), 5)
        batch.read.per_call[4].set()
        await finished(batch, "t4")
        batch.read.per_call[3].set()
        with pytest.raises(BudgetExceeded, match="repeated_tool_failure"):
            await asyncio.wait_for(task, 5)
        batch.executor.stop_for_budget("repeated_tool_failure")
        cp = batch.store.checkpoints.load(batch.session.id)
        assert cp.run.tools[4].status == "completed"
        assert cp.run.tools[4].result["content"] == "missing"
        assert len(batch.read.calls) == 6 and cp.run.tools[6].status == "pending"
    finally:
        await cleanup(task)


# 功能：合法乱序完成可加载，但没有配对消息或结果的伪造完成状态仍被拒绝。
# 设计：只修改已有快照的一项，避免放宽原检查点完整性保护。
@pytest.mark.parametrize("missing", ["result", "message", "historical"])
def test_out_of_order_checkpoint_requires_result_and_message(tmp_path, missing):
    batch = make_batch(tmp_path, ["a", "b"])
    tool = batch.context.run.tools[1]
    tool.status = "completed"
    tool.result = {"content": "read:b"} if missing != "result" else None
    if missing == "historical":
        batch.context.messages[1:1] = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": tool.id,
                "name": tool.name, "input": tool.input}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool.id,
                "content": "old result with reused ID"}]},
        ]
    elif missing != "message":
        batch.context.add_tool_result(tool.id, "read:b")
    batch.persistence.save()
    with pytest.raises(CheckpointError, match="missing its result or paired message"):
        batch.store.checkpoints.load(batch.session.id)


# 功能：输出归档失败后恢复只整理已保存结果，不再次读取文件。
# 设计：B 先完成并触发归档失败，A 仍自然结束；重新执行相同批次时调用次数不增加。
async def test_output_failure_does_not_reexecute_completed_reads(tmp_path, monkeypatch):
    batch = make_batch(tmp_path, ["a", "b"])
    batch.read.outputs["b"] = ToolResult("x" * 100)
    outputs = batch.executor._outputs
    outputs._config = CompactionConfig(tool_result_limit=10, tool_result_keep=4)
    prepare = outputs.prepare

    # 首轮 B 的全文归档失败，不影响工具已经完成的事实。
    def fail(tool_id, result):
        if tool_id == "t1":
            raise OSError("archive full")
        return prepare(tool_id, result)

    monkeypatch.setattr(outputs, "prepare", fail)
    task = asyncio.create_task(batch.executor.run_pending())
    try:
        await started(batch, "a", "b")
        release(batch, "b", "a")
        assert not await asyncio.wait_for(task, 5)
    finally:
        await cleanup(task)
    assert batch.context.run.status == "interrupted"
    assert all(t.status == "completed" for t in batch.context.run.tools)
    monkeypatch.setattr(outputs, "prepare", prepare)
    outputs._outputs = tmp_path / "outputs"
    batch.context.run.status = "running"
    assert await batch.executor.run_pending()
    assert batch.read.calls == ["a", "b"]
    assert all(t.output_prepared for t in batch.context.run.tools)
