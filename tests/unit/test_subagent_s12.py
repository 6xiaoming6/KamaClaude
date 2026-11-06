from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import ValidationError

from kama_claude.core.agents.loader import AgentProfileLoader
from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.config import KamaConfig, SubagentConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.query import AgentCancelTool, AgentResultParams, AgentResultTool
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.result import PENDING_RESULT, SubagentResult
from kama_claude.core.subagent.store import SubagentResultStore, SubagentStoreError
from kama_claude.core.subagent.tracking import SubagentTracker
from kama_claude.core.tools.base import ToolResult
from tests.unit.test_subagent_collaboration import Model, finish, make_tool, start


def profile(path: Path, name: str, tools: list[str] | None, *, prompt: str = "role prompt"):
    root = path / ".kama" / "agents"
    root.mkdir(parents=True, exist_ok=True)
    text = f'[agent]\ndescription="custom {name}"\nsystem_prompt={json.dumps(prompt)}\n'
    if tools is not None:
        text += "allowed_tools=" + json.dumps(tools) + "\n"
    (root / f"{name}.toml").write_text(text, encoding="utf-8")


async def test_default_explorer_cannot_write_or_execute_commands(tmp_path):
    model, registry = Model(), BackgroundTaskRegistry()
    child_id, _ = await start(make_tool(tmp_path, model, registry))
    await finish(registry, child_id)
    names = {schema["name"] for schema in model.calls[0]["tool_schemas"]}
    assert {"read_file", "find_files", "search_text", "finish"} <= names
    assert not names & {"bash", "write_file", "edit_file", "memory_write", "spawn_agent"}


@pytest.mark.parametrize("tools", [["write_file"], ["edit_file"], ["bash"],
                                   ["memory_write"], ["spawn_agent"], ["unknown"], None])
async def test_custom_background_role_is_checked_before_spawning(tmp_path, tools):
    profile(tmp_path, "custom", tools)
    registry, model, bus = BackgroundTaskRegistry(), Model(), EventBus()
    events = []

    async def record(event):
        events.append(event.type)

    bus.subscribe(record)
    tool = make_tool(tmp_path, model, registry, bus=bus)
    result = await tool.invoke({"description": "work", "prompt": "goal",
                                "subagent_type": "custom", "run_in_background": True})
    assert result.error_type == "invalid_operation"
    assert not registry._tasks and not model.calls and "subagent.started" not in events
    assert not registry._roots[("session", "root")].spawned


async def test_executor_is_foreground_and_role_snapshot_is_stable(tmp_path):
    profile(tmp_path, "custom", ["read_file"], prompt="original role")
    registry, model = BackgroundTaskRegistry(), Model()
    tool = make_tool(tmp_path, model, registry)
    assert "explorer:" in tool.description and "custom:" in tool.description
    profile(tmp_path, "custom", ["bash"], prompt="changed role")
    result = await tool.invoke({"description": "work", "prompt": "goal",
                                "subagent_type": "custom", "run_in_background": True})
    child_id = result.content.split("run_id=")[1].split(".")[0]
    await finish(registry, child_id)
    assert "original role" in model.calls[0]["system"]
    assert "bash" not in {schema["name"] for schema in model.calls[0]["tool_schemas"]}
    rejected = await tool.invoke({"description": "work", "prompt": "goal",
                                  "subagent_type": "executor", "run_in_background": True})
    assert rejected.error_type == "invalid_operation"
    foreground = await tool.invoke({"description": "work", "prompt": "goal",
                                    "subagent_type": "executor"})
    assert not foreground.is_error
    assert "write_file" in {schema["name"] for schema in model.calls[-1]["tool_schemas"]}
    replacement = make_tool(tmp_path, Model(), registry)
    assert replacement.description != tool.description


def test_role_catalog_resolves_overrides_and_reports_bad_roles(tmp_path):
    profile(tmp_path, "explorer", ["list_dir"], prompt="local explorer")
    (tmp_path / ".kama" / "agents" / "broken.toml").write_text("[agent\n", encoding="utf-8")
    profiles, errors = AgentProfileLoader(tmp_path).catalog()
    assert profiles["explorer"].system_prompt == "local explorer"
    assert "broken" in errors and {"planner", "reviewer", "executor"} <= profiles.keys()


@pytest.mark.parametrize("ids", [[], ["x"] * 2, [f"x{i}" for i in range(9)],
                                 ["../x"], ["x/y"], [""], "x"])
def test_batch_ids_reject_invalid_or_ambiguous_inputs(ids):
    with pytest.raises(ValidationError):
        AgentResultParams.model_validate({"run_ids": ids})


async def test_wait_any_returns_completed_and_pending_without_cancelling(tmp_path):
    registry = BackgroundTaskRegistry()
    first, second = Model(blocked=True, text="first"), Model(blocked=True, text="second")
    one, _ = await start(make_tool(tmp_path, first, registry))
    two, _ = await start(make_tool(tmp_path, second, registry))
    await asyncio.gather(first.started.wait(), second.started.wait())
    query = AgentResultTool(registry, "session")
    waiting = asyncio.create_task(query.invoke({"run_ids": [one, two], "wait_s": 30}))
    first.release.set()
    result = await asyncio.wait_for(waiting, 1)
    assert result.collected_subagents == [one] and two in result.content
    assert PENDING_RESULT in result.content and not registry.get(two, "session").task.done()
    pending = await query.invoke({"run_ids": [two], "wait_s": 0.01})
    assert not pending.collected_subagents and PENDING_RESULT in pending.content
    second.release.set()
    await finish(registry, two)


async def test_batch_waiter_cancel_does_not_cancel_children(tmp_path):
    registry, model = BackgroundTaskRegistry(), Model(blocked=True)
    one, _ = await start(make_tool(tmp_path, model, registry))
    two, _ = await start(make_tool(tmp_path, model, registry))
    waiting = asyncio.create_task(AgentResultTool(registry, "session").invoke({
        "run_ids": [one, two], "wait_s": 30,
    }))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert all(not registry.get(identifier, "session").task.cancelling() for identifier in (one, two))
    model.release.set()
    await asyncio.gather(*(finish(registry, identifier) for identifier in (one, two)))


async def test_cancellation_is_owned_and_terminal_result_must_be_collected(tmp_path):
    registry, model = BackgroundTaskRegistry(), Model(blocked=True)
    child, _ = await start(make_tool(tmp_path, model, registry))
    await model.started.wait()
    for sid, parent in (("other", "root"), ("session", "other")):
        denied = await AgentCancelTool(registry, sid, parent).invoke({"run_ids": [child]})
        assert denied.is_error and not registry.get(child, "session").task.cancelling()
    result = await AgentCancelTool(registry, "session", "root").invoke({"run_ids": [child]})
    assert not result.is_error and not result.collected_subagents
    outcome = await AgentResultTool(registry, "session").invoke({"run_ids": [child]})
    assert outcome.is_error and outcome.collected_subagents == [child]
    assert "cancelled" in outcome.content


async def test_root_cleanup_finds_descendant_even_after_intermediate_parent_disappears(tmp_path):
    from kama_claude.core.checkpoint.checkpoint import ToolCheckpoint

    registry, model = BackgroundTaskRegistry(), Model(blocked=True)
    child, _ = await start(make_tool(tmp_path, model, registry))
    await model.started.wait()
    entry = registry.get(child, "session")
    entry.parent_run_id = "finished-intermediate-parent"
    entry.context.run.tools = [ToolCheckpoint(id="write", name="bash", input={}, status="executing")]
    await registry.cancel_children("root", "session")
    outcome = registry.get(child, "session")
    assert outcome.status == "interrupted" and outcome.outcome_unknown
    assert not registry._tasks and not outcome.as_tool_result().collected_subagents


async def test_parent_cleanup_reclaims_blocked_notification_without_downgrading_result(tmp_path):
    registry, model, bus = BackgroundTaskRegistry(), Model(), EventBus()
    entered = asyncio.Event()

    async def block_notification(event):
        if event.type == "subagent.finished":
            entered.set()
            await asyncio.Future()

    bus.subscribe(block_notification)
    child, _ = await start(make_tool(tmp_path, model, registry, bus=bus))
    await asyncio.wait_for(entered.wait(), 1)
    result = registry.get(child, "session")
    assert result.status == "success"
    await registry.cancel_children("root", "session")
    assert not registry._tasks and registry.get(child, "session").status == "success"


async def test_finished_result_reloads_in_fresh_python_process(tmp_path):
    registry, model = BackgroundTaskRegistry(max_results=1), Model()
    child, _ = await start(make_tool(tmp_path, model, registry))
    stored = await finish(registry, child)
    program = (
        "import sys; from pathlib import Path; "
        "from kama_claude.core.subagent.store import SubagentResultStore; "
        "r=SubagentResultStore(Path(sys.argv[1])).load(sys.argv[2],sys.argv[3]); "
        "assert r is not None and r.status=='success'; print(r.run_id)"
    )
    completed = subprocess.run([sys.executable, "-c", program, str(tmp_path / "runs"), child, "session"],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == child
    fresh = BackgroundTaskRegistry()
    fresh.bind_root("next-root", "session", SubagentConfig(), runs_dir=tmp_path / "runs")
    assert fresh.get(child, "session") == stored
    assert fresh.get(child, "other") is None and len(model.calls) == 1


async def test_metadata_failure_cannot_publish_success(tmp_path, monkeypatch):
    def fail(*args):
        raise SubagentStoreError("disk unavailable")

    monkeypatch.setattr(SubagentResultStore, "save", fail)
    registry, model, bus = BackgroundTaskRegistry(), Model(), EventBus()
    statuses = []

    async def record(event):
        if event.type == "subagent.finished":
            statuses.append(event.status)

    bus.subscribe(record)
    child, _ = await start(make_tool(tmp_path, model, registry, bus=bus))
    result = await finish(registry, child)
    assert result.status == "failed" and result.reason == "result_save_failed"
    assert statuses == ["failed"]


async def test_corrupt_result_is_reported_and_not_reexecuted(tmp_path):
    registry, model = BackgroundTaskRegistry(), Model()
    child, _ = await start(make_tool(tmp_path, model, registry))
    await finish(registry, child)
    (tmp_path / "runs" / child / "result.json").write_text('{"broken":', encoding="utf-8")
    registry._results.clear()
    result = await AgentResultTool(registry, "session").invoke({"run_ids": [child]})
    assert result.error_type == "storage_error" and not result.outcome_unknown
    assert len(model.calls) == 1


def test_metadata_atomic_failure_and_identity_checks(tmp_path, monkeypatch):
    run = RunCheckpoint(run_id="child", goal="goal", model="test", workspace="", max_steps=5,
                        status="success", result="original")
    result = SubagentResult.from_run(run, "session", "root")
    store = SubagentResultStore(tmp_path)
    store.save(result)
    original = (tmp_path / "child" / "result.json").read_bytes()

    def fail(*args):
        raise OSError("replace unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("kama_claude.core.subagent.store.os.replace", fail)
        with pytest.raises(SubagentStoreError):
            store.save(result)
    assert (tmp_path / "child" / "result.json").read_bytes() == original
    assert not list((tmp_path / "child").glob(".result-*.tmp"))
    assert store.load("child", "other") is None
    for invalid in ("../child", "child/x", "..", ""):
        with pytest.raises(SubagentStoreError):
            store.load(invalid, "session")
    raw = {"schema_version": 1, "result": asdict(result)}
    raw["result"]["actual_tokens"] = -1
    (tmp_path / "child" / "result.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SubagentStoreError):
        store.load("child", "session")


def test_collection_uses_internal_ids_instead_of_display_text():
    run = RunCheckpoint(run_id="root", goal="goal", model="test", workspace="", max_steps=5,
                        subagents={"one": "background", "two": "background"})
    tracker = SubagentTracker(run)
    tracker.record_result(ToolResult("looks finished"))
    assert tracker.finish_error() is not None
    tracker.record_result(ToolResult(PENDING_RESULT, collected_subagents=["one", "foreign"]))
    assert run.subagents == {"one": "collected", "two": "background"}
    tracker.record_result(ToolResult("known failure", is_error=True, collected_subagents=["two"]))
    assert tracker.finish_error() is None


async def test_finish_waits_for_result_and_history_stays_paired(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry, bus = BackgroundTaskRegistry(), EventBus()
    child_id, root_id = "", ""
    root_steps = 0
    child_release = asyncio.Event()
    events = []

    async def record(event):
        nonlocal child_id
        if event.type == "subagent.started":
            child_id = event.run_id
        if event.type == "run.finished":
            events.append(event)

    bus.subscribe(record)

    class Provider:
        async def chat(self, **kwargs):
            nonlocal root_id, root_steps
            if kwargs["run_id"] == child_id:
                await child_release.wait()
                return LlmResponse("tool_use", tool_calls=[ToolCallBlock("cf", "finish", {
                    "result": "child evidence",
                })], usage=UsageStats(10, 5))
            root_id = kwargs["run_id"]
            root_steps += 1
            if root_steps == 1:
                call = ToolCallBlock("spawn", "spawn_agent", {
                    "description": "explore", "prompt": "locate evidence", "run_in_background": True,
                })
            elif root_steps == 2:
                call = ToolCallBlock("early", "finish", {"result": "premature"})
            elif root_steps == 3:
                assert not events
                assert "Collect outstanding" in kwargs["messages"][-1]["content"][0]["content"]
                child_release.set()
                call = ToolCallBlock("collect", "agent_result", {"run_ids": [child_id]})
            else:
                call = ToolCallBlock("final", "finish", {"result": "verified handoff"})
            return LlmResponse("tool_use", tool_calls=[call], usage=UsageStats(10, 5))

    config = KamaConfig()
    config.compaction.auto_threshold = 0
    runner = AgentRunner(config, provider=Provider(), bus=bus, task_registry=registry)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: runner, bus, task_registry=registry)
    session = await manager.create("chat")
    try:
        await manager.send_message(session.id, "goal")
        cp = store.checkpoints.load(session.id)
        from kama_claude.core.compact.history import validate_pairs

        assert cp.run.status == "success" and cp.run.result == "verified handoff"
        assert cp.run.subagents[child_id] == "collected" and not registry._tasks
        assert validate_pairs(cp.messages)
        assert len(events) == 1 and events[0].run_id == root_id
        assert len(store.read_history(session.id)) == 9
    finally:
        child_release.set()
        await manager.shutdown()


async def test_parent_budget_exit_cleans_up_background_work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = BackgroundTaskRegistry()
    child_id = ""

    async def record(event):
        nonlocal child_id
        if event.type == "subagent.started":
            child_id = event.run_id

    bus = EventBus()
    bus.subscribe(record)

    class Provider:
        async def chat(self, **kwargs):
            if kwargs["run_id"] == child_id:
                await asyncio.Future()
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock("s", "spawn_agent", {
                "description": "background", "prompt": "wait", "run_in_background": True,
            })], usage=UsageStats(10, 5))

    config = KamaConfig()
    config.agent.max_steps = 1
    result = await AgentRunner(config, provider=Provider(), bus=bus, task_registry=registry).run_and_capture("goal")
    assert result.reason == "exceeded_max_steps" and child_id and not registry._tasks
    saved = registry.get(child_id, "")
    assert saved.status == "failed" and saved.reason == "cancelled"


async def test_resume_can_collect_saved_result_but_cannot_recreate_missing_child(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: AgentRunner(KamaConfig()), EventBus())
    session = await manager.create("chat")
    cp = store.checkpoints.load(session.id)
    cp.session["status"] = "interrupted"
    cp.run = RunCheckpoint(run_id="root", goal="goal", model="test", workspace=str(tmp_path),
                           max_steps=5, status="interrupted", subagents={"child": "background"})
    store.save_checkpoint(cp)
    with pytest.raises(HandlerError, match="unfinished subagent"):
        manager._recovery.prepare_resume(cp, cp.revision, "continue")
    child = RunCheckpoint(run_id="child", goal="goal", model="test", workspace=str(tmp_path),
                          max_steps=5, status="success", result="already done")
    SubagentResultStore(store.runs_dir(session.id)).save(SubagentResult.from_run(child, session.id, "root"))
    manager._recovery.prepare_resume(cp, cp.revision, "continue")
    assert cp.run.status == "running" and cp.run.subagents["child"] == "background"
