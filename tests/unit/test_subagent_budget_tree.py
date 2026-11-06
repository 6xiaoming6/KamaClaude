from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.config import CompactionConfig, KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.llm.types import LlmResponse, UsageStats
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.task.manager import TaskManager
from tests.unit.test_subagent_collaboration import Model, finish, make_tool, start


def run(name: str, limit: int = 100) -> RunCheckpoint:
    return RunCheckpoint(run_id=name, goal="goal", model="test", workspace="", max_steps=5,
                         max_total_tokens=limit)


# 父、子、孙各自保留含后代的用量，祖先只接收一次增量。
def test_descendant_usage_is_inclusive_without_double_counting():
    root, child, grandchild = run("root"), run("child"), run("grandchild")
    parent = RunBudget(root)
    middle = RunBudget(child, parent=parent)
    leaf = RunBudget(grandchild, parent=middle)
    parent.reserve(10)
    parent.settle(10, 5)
    middle.reserve(20)
    middle.settle(20, 12)
    leaf.reserve(30)
    assert root.actual_tokens == 17 and root.estimated_tokens == 30
    assert root.child_actual_tokens == 12 and root.child_estimated_tokens == 30
    leaf.settle(30, 7)
    assert (root.actual_tokens, child.actual_tokens, grandchild.actual_tokens) == (24, 19, 7)
    assert root.child_actual_tokens == 19 and child.child_actual_tokens == 7
    assert root.actual_tokens - root.child_actual_tokens == 5
    assert all(item.usage_complete and item.estimated_tokens == 0
               for item in (root, child, grandchild))
    with pytest.raises(BudgetExceeded):
        leaf.reserve(77)
    assert root.estimated_tokens == child.estimated_tokens == grandchild.estimated_tokens == 0


def test_child_cannot_extend_parent_wall_clock_budget(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("kama_claude.core.run_budget.time.monotonic", lambda: now[0])
    parent_run = run("root")
    parent_run.max_run_seconds = 5
    parent = RunBudget(parent_run)
    now[0] = 14.0
    child = RunBudget(run("child"), parent=parent)
    assert child.remaining_seconds() == 1.0
    now[0] = 15.0
    with pytest.raises(BudgetExceeded, match="time"):
        child.check()
    assert parent_run.elapsed_seconds == 5.0


def test_stopped_parent_blocks_descendant_requests_even_with_remaining_tokens():
    parent = RunBudget(run("root"))
    child = RunBudget(run("child"), parent=parent)
    grandchild = RunBudget(run("grandchild"), parent=child)
    parent.run.status = "failed"
    with pytest.raises(BudgetExceeded, match="parent_run_stopped"):
        grandchild.reserve(1)
    assert parent.run.estimated_tokens == child.run.estimated_tokens == 0


@pytest.mark.parametrize("kind", ["normal", "summary"])
async def test_concurrent_requests_cannot_oversubscribe_shared_budget(kind: str):
    root = RunBudget(run("root", 1_000))
    entered, release = asyncio.Event(), asyncio.Event()
    called = []

    class Provider:
        async def chat(self, **kwargs):
            called.append(kwargs["run_id"])
            entered.set()
            await release.wait()
            return LlmResponse("end_turn", text="ok", usage=UsageStats(60, 10))

    provider = Provider()
    config = CompactionConfig()
    budgets = [RunBudget(run(f"c{index}", 1_000), parent=root) for index in range(2)]
    providers = [ManagedProvider(provider, TokenEstimator(), config, budget=budget)
                 for budget in budgets]

    async def request(item):
        return await item.chat(
            messages=[{"role": "user", "content": "x" * 800}], tool_schemas=[],
            bus=EventBus(), run_id="compact" if kind == "summary" else "child",
            max_output_tokens=512,
        )

    first = asyncio.create_task(request(providers[0]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(BudgetExceeded):
            await request(providers[1])
        assert len(called) == 1 and root.run.estimated_tokens > 512
        assert budgets[1].run.estimated_tokens == 0
    finally:
        release.set()
        await first
    assert root.run.actual_tokens == root.run.child_actual_tokens == 70
    assert root.run.estimated_tokens == 0


async def test_missing_usage_and_retry_keep_shared_estimated_occupancy(monkeypatch):
    import httpx

    calls = 0

    class Provider:
        async def chat(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("lost response")
            return LlmResponse("end_turn", text="ok")

    async def no_delay(_):
        return None

    monkeypatch.setattr("kama_claude.core.llm.managed.asyncio.sleep", no_delay)
    root = RunBudget(run("root", 10_000))
    child = RunBudget(run("child", 10_000), parent=root)
    provider = ManagedProvider(Provider(), TokenEstimator(), CompactionConfig(), budget=child)
    await provider.chat(messages=[], tool_schemas=[], bus=EventBus(), run_id="child",
                        max_output_tokens=128)
    assert calls == 2 and child.run.estimated_tokens > 256
    assert root.run.estimated_tokens == root.run.child_estimated_tokens == child.run.estimated_tokens
    assert not root.run.usage_complete and not child.run.usage_complete


async def test_child_reservation_is_committed_before_api_and_settlement(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: AgentRunner(KamaConfig()), EventBus())
    session = await manager.create("chat")
    cp = store.checkpoints.load(session.id)
    root_run = run("root", 20_000)
    root_run.workspace = str(tmp_path)
    cp.run = root_run
    from kama_claude.core.checkpoint.persistence import RunPersistence
    from kama_claude.core.context import ExecutionContext

    context = ExecutionContext(run=root_run, messages=[])
    persistence = RunPersistence(context, cp, session, store)
    parent = RunBudget(root_run, persistence=persistence)
    child = RunBudget(run("child", 20_000), parent=parent)

    class Provider:
        async def chat(self, **kwargs):
            saved = store.checkpoints.load(session.id).run
            assert saved.estimated_tokens == root_run.estimated_tokens > 0
            assert saved.child_estimated_tokens == saved.estimated_tokens
            return LlmResponse("end_turn", text="ok", usage=UsageStats(20, 4))

    managed = ManagedProvider(Provider(), TokenEstimator(), CompactionConfig(), budget=child)
    await managed.chat(messages=[], tool_schemas=[], bus=EventBus(), run_id="child",
                       max_output_tokens=128)
    saved = store.checkpoints.load(session.id).run
    assert saved.actual_tokens == saved.child_actual_tokens == 24 and saved.estimated_tokens == 0
    restarted = RunBudget(RunCheckpoint.model_validate(saved.model_dump()))
    assert restarted.run.actual_tokens == 24
    with pytest.raises(BudgetExceeded):
        restarted.reserve(20_000)


async def test_shared_save_failure_blocks_all_siblings_before_request():
    class Persistence:
        def save(self):
            raise CheckpointError("disk unavailable")

    calls = []

    class Provider:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            return LlmResponse("end_turn", text="unexpected")

    root = RunBudget(run("root", 20_000), persistence=Persistence())
    siblings = [RunBudget(run(f"c{index}", 20_000), parent=root) for index in range(2)]
    for budget in siblings:
        managed = ManagedProvider(Provider(), TokenEstimator(), CompactionConfig(), budget=budget)
        with pytest.raises(CheckpointError):
            await managed.chat(messages=[], tool_schemas=[], bus=EventBus(), run_id="child",
                               max_output_tokens=128)
    assert not calls and siblings[1].run.estimated_tokens == 0


async def test_spawned_child_uses_parent_budget_in_real_loop(tmp_path: Path):
    registry, model = BackgroundTaskRegistry(), Model()
    tool = make_tool(tmp_path, model, registry)
    root = run("root", 1)
    tool._parent_budget = RunBudget(root)
    child_id, _ = await start(tool)
    result = await finish(registry, child_id)
    assert result.reason == "run_token_budget_exceeded" and not model.calls
    assert root.actual_tokens == root.estimated_tokens == 0


# 模型控制真实两层派生，验证 Runner 的父预算注入没有遗漏。
async def test_runner_nested_usage_and_frozen_task_limits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    roles = tmp_path / ".kama" / "agents"
    roles.mkdir(parents=True)
    (roles / "coordinator.toml").write_text(
        '[agent]\ndescription="Delegate"\nallowed_tools=["spawn_agent", "read_file"]\n',
        encoding="utf-8",
    )
    calls: dict[str, int] = {}
    children = []
    config = KamaConfig()
    config.agent.max_total_tokens = 100_000
    config.compaction.auto_threshold = 0
    config.subagent.max_active = 2
    config.subagent.max_total = 2
    bus = EventBus()

    async def observe(event):
        if event.type == "subagent.started":
            children.append(event.run_id)

    bus.subscribe(observe)

    class Provider:
        async def chat(self, **kwargs):
            from kama_claude.core.llm.types import ToolCallBlock

            identifier = kwargs["run_id"]
            calls[identifier] = calls.get(identifier, 0) + 1
            if calls[identifier] == 1 and len(children) < 2:
                return LlmResponse("tool_use", tool_calls=[ToolCallBlock("spawn", "spawn_agent", {
                    "description": "bounded work", "prompt": "return evidence",
                    "subagent_type": "coordinator" if not children else "explorer",
                })], usage=UsageStats(10, 5))
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock("f", "finish", {"result": "ok"})],
                               usage=UsageStats(10, 5))

    registry = BackgroundTaskRegistry()
    runner = AgentRunner(config, provider=Provider(), bus=bus, task_registry=registry)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: runner, bus, task_registry=registry)
    session = await manager.create("chat")
    await manager.send_message(session.id, "goal")
    saved = store.checkpoints.load(session.id).run
    assert saved.status == "success" and len(children) == 2
    assert saved.actual_tokens == sum(calls.values()) * 15 == 75
    assert saved.child_actual_tokens == 45
    assert saved.actual_tokens - saved.child_actual_tokens == 30
    assert saved.subagent_max_active == saved.subagent_max_total == 2
    assert set(saved.subagents.values()) == {"collected"}
    reports = [json.loads(path.read_text(encoding="utf-8"))["result"]
               for path in store.runs_dir(session.id).glob("*/result.json")]
    assert sorted(item["actual_tokens"] for item in reports) == [15, 45]
    config.subagent.max_total = 100
    tools = runner._build_registry(
        TaskManager(tmp_path / "tasks"), run=saved, workspace=tmp_path,
        provider=Provider(), bus=bus, run_id=saved.run_id, session_id=session.id,
        child_runs_dir=store.runs_dir(session.id),
    )
    replacement = tools.get("spawn_agent")
    assert replacement._subagent_config.max_total == 2
    denied = await replacement.invoke({"description": "extra", "prompt": "goal"})
    assert denied.error_type == "resource_limit"
