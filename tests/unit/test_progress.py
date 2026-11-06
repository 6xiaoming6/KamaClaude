from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.history import validate_pairs
from kama_claude.core.config import AgentConfig, KamaConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.progress import ProgressGuard
from kama_claude.core.run_budget import BudgetExceeded, RunBudget
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tool import SpawnAgentTool
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from tests.unit.test_checkpoint_recovery import Crash, ProbeTool


def state(path: Path, limit: int = 5) -> RunCheckpoint:
    return RunCheckpoint(run_id="root", goal="goal", model="test", workspace=str(path),
                         max_steps=20, no_progress_limit=limit)


@pytest.mark.parametrize("period", [1, 2, 3])
def test_repeated_read_cycles_warn_then_stop_across_rotations(tmp_path, period):
    run = state(tmp_path)
    guard, tool = ProgressGuard(run), ReadFileTool(tmp_path)
    warnings, stops = [], []
    for step in range(1, period * 5 + 1):
        run.step = step
        index = (step - 1) % period
        warning, stopped = guard.observe(tool, {"path": f"{index}.txt"}, ToolResult(f"page {index}"))
        if warning:
            warnings.append(step)
        if stopped:
            stops.append(step)
    assert warnings == [period * 3, period * 5]
    assert stops == [period * 5] and run.progress.stopped


def test_model_gets_a_turn_after_warning_even_with_many_calls_in_one_batch(tmp_path):
    run = state(tmp_path)
    guard, tool = ProgressGuard(run), ReadFileTool(tmp_path)
    run.step = 1
    warnings = 0
    for _ in range(100):
        warning, stopped = guard.observe(tool, {"path": "a"}, ToolResult("same"))
        warnings += bool(warning)
        assert not stopped
    assert warnings == 1 and len(run.progress.recent) == 15
    run.step = 2
    assert guard.observe(tool, {"path": "a"}, ToolResult("same"))[1]


def test_default_arguments_and_backend_paths_share_fingerprint(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("same", encoding="utf-8")
    run = state(tmp_path)
    guard, tool = ProgressGuard(run), ReadFileTool(tmp_path)
    warnings = []
    for step, params in enumerate((
        {"path": "a.txt"}, {"path": "./a.txt", "start_line": 1},
        {"max_lines": 200, "path": str(path), "start_line": 1},
    ), 1):
        run.step = step
        warnings.append(guard.observe(tool, params, ToolResult("same"))[0])
    assert not warnings[0] and not warnings[1] and "No progress warning" in warnings[2]


def test_changing_results_and_sequential_pages_do_not_trigger(tmp_path):
    for changing in (True, False):
        run = state(tmp_path)
        guard, tool = ProgressGuard(run), ReadFileTool(tmp_path)
        for step in range(1, 31):
            run.step = step
            params = {"path": "log.txt", "start_line": 1 if changing else step * 200}
            assert guard.observe(tool, params, ToolResult(f"page {step}")) == ("", False)
        assert not run.progress.stopped and len(run.progress.recent) <= 15


@pytest.mark.parametrize("name", ["agent_result", "agent_cancel", "spawn_agent", "finish",
                                  "bash", "write_file", "edit_file", "memory_read", "task_get"])
def test_polling_or_other_operations_break_the_read_window(tmp_path, name):
    run = state(tmp_path)
    guard, read = ProgressGuard(run), ReadFileTool(tmp_path)
    for step in range(1, 4):
        run.step = step
        guard.observe(read, {"path": "a"}, ToolResult("same"))
    assert run.progress.warned_pattern
    guard.observe(ProbeTool(name), {}, ToolResult("still running"))
    assert not run.progress.recent and run.progress.warned_step is None
    for step in range(4, 6):
        run.step = step
        assert guard.observe(read, {"path": "a"}, ToolResult("same")) == ("", False)


@pytest.mark.parametrize("result", [
    ToolResult("uncertain", is_error=True, error_type="runtime_error", outcome_unknown=True),
    ToolResult("denied", is_error=True, error_type="permission_denied"),
])
def test_unknown_and_denied_results_are_not_progress_evidence(tmp_path, result):
    run = state(tmp_path)
    guard, read = ProgressGuard(run), ReadFileTool(tmp_path)
    for step in range(1, 4):
        run.step = step
        guard.observe(read, {"path": "a"}, ToolResult("same"))
    assert guard.observe(read, {"path": "a"}, result) == ("", False)
    assert not run.progress.recent


def test_known_failure_cycles_can_be_detected_without_rewriting_errors(tmp_path):
    run = state(tmp_path)
    guard, read = ProgressGuard(run), ReadFileTool(tmp_path)
    stopped = False
    for step in range(1, 11):
        run.step = step
        result = ToolResult("file missing", is_error=True, error_type="not_found")
        _, stopped = guard.observe(read, {"path": str(step % 2)}, result)
        assert result.content == "file missing"  # Guard 返回提醒，由提交入口统一追加。
    assert stopped


def test_checkpoint_round_trip_keeps_warning_and_does_not_reset_repetition(tmp_path):
    run = state(tmp_path)
    guard, read = ProgressGuard(run), ReadFileTool(tmp_path)
    for step in range(1, 4):
        run.step = step
        guard.observe(read, {"path": "a"}, ToolResult("same"))
    restored = RunCheckpoint.model_validate_json(run.model_dump_json())
    guard = ProgressGuard(restored)
    restored.step = 4
    assert guard.observe(read, {"path": "a"}, ToolResult("same")) == ("", False)
    restored.step = 5
    assert guard.observe(read, {"path": "a"}, ToolResult("same"))[1]


def test_legacy_run_and_disabled_guard_do_not_gain_a_new_stop_rule(tmp_path):
    run = state(tmp_path)
    raw = run.model_dump()
    raw.pop("no_progress_limit")
    legacy = RunCheckpoint.model_validate(raw)
    assert legacy.no_progress_limit == 0
    for step in range(100):
        legacy.step = step
        assert ProgressGuard(legacy).observe(ReadFileTool(tmp_path), {"path": "a"},
                                              ToolResult("same")) == ("", False)
    assert not legacy.progress.recent


@pytest.mark.parametrize("value", [0, 4, 5, 20])
def test_progress_config_and_environment(value, monkeypatch):
    config = KamaConfig()
    assert config.agent.no_progress_limit == 5
    apply_toml(config, {"agent": {"no_progress_limit": value}})
    assert config.agent.no_progress_limit == value
    monkeypatch.setenv("KAMA_AGENT_NO_PROGRESS_LIMIT", "4")
    apply_env(config)
    assert config.agent.no_progress_limit == 4


@pytest.mark.parametrize("value", [-1, 1, 2, 3, 21, True, "5"])
def test_invalid_progress_limit_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        AgentConfig(no_progress_limit=value)
    with pytest.raises(SystemExit):
        apply_toml(KamaConfig(), {"agent": {"no_progress_limit": value}})


@pytest.mark.parametrize("value", ["-1", "1", "3", "21", "true", "4.5"])
def test_invalid_environment_limit_rejected(value, monkeypatch):
    monkeypatch.setenv("KAMA_AGENT_NO_PROGRESS_LIMIT", value)
    with pytest.raises(SystemExit):
        apply_env(KamaConfig())


def test_parent_stop_blocks_new_child_requests(tmp_path):
    parent, child = state(tmp_path), state(tmp_path)
    parent.progress.stopped = True
    with pytest.raises(BudgetExceeded, match="no_progress"):
        RunBudget(child, parent=RunBudget(parent)).reserve(10)
    assert child.estimated_tokens == parent.estimated_tokens == 0


class RepeatingModel:
    def __init__(self):
        self.calls = []
        self.finish_now = False

    async def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        call = (
            ToolCallBlock("finish", "finish", {"result": "done"}) if self.finish_now
            else ToolCallBlock(f"read-{kwargs['step']}", "read_file", {"path": "a.txt"})
        )
        return LlmResponse("tool_use", tool_calls=[call], usage=UsageStats(10, 5))


def manager(tmp_path, model):
    config = KamaConfig()
    config.compaction.auto_threshold = 0
    bus = EventBus()
    store = SessionStore(tmp_path / "sessions")
    return store, SessionManager(store, lambda: AgentRunner(config, bus=bus, provider=model), bus)


async def test_real_loop_stops_after_warning_and_new_run_starts_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("unchanged", encoding="utf-8")
    model = RepeatingModel()
    store, sessions = manager(tmp_path, model)
    session = await sessions.create("chat")
    await sessions.send_message(session.id, "inspect file")
    cp = store.checkpoints.load(session.id)
    assert cp.run.status == "failed" and cp.run.reason == "no_progress"
    assert cp.run.step == len(model.calls) == 5 and cp.run.progress.stopped
    assert "No progress warning" in model.calls[3]["messages"][-1]["content"][0]["content"]
    assert cp.run.tools[0].status == "completed" and not cp.run.tools[0].result["is_error"]
    assert validate_pairs(cp.messages) and len(store.read_history(session.id)) == 11
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "unchanged"
    model.finish_now = True
    await sessions.send_message(session.id, "return control")
    new = store.checkpoints.load(session.id)
    assert new.run.status == "success" and not new.run.progress.recent
    assert new.run.run_id != cp.run.run_id


async def test_stop_does_not_execute_remaining_calls_and_keeps_message_pairs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("unchanged", encoding="utf-8")

    class BatchModel(RepeatingModel):
        async def chat(self, **kwargs):
            response = await super().chat(**kwargs)
            if kwargs["step"] == 5:
                response.tool_calls.append(ToolCallBlock("extra", "read_file", {"path": "b.txt"}))
            return response

    calls = []
    read_page = ReadFileTool._read_page

    def tracked_read(tool, path, params):
        calls.append(path.name)
        return read_page(tool, path, params)

    monkeypatch.setattr(ReadFileTool, "_read_page", tracked_read)
    model = BatchModel()
    store, sessions = manager(tmp_path, model)
    session = await sessions.create("chat")
    await sessions.send_message(session.id, "inspect")
    cp = store.checkpoints.load(session.id)
    assert cp.run.reason == "no_progress" and len(model.calls) == 5
    assert calls == ["a.txt"] * 5
    assert validate_pairs(cp.messages)
    assert cp.run.tools[-1].status == "completed"
    assert "not executed: no_progress" in cp.run.tools[-1].result["content"]


async def test_stop_save_failure_propagates_without_success_or_more_execution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("unchanged", encoding="utf-8")
    model = RepeatingModel()
    store, sessions = manager(tmp_path, model)
    session = await sessions.create("chat")
    save = store.checkpoints.save
    events = []

    async def collect(event):
        events.append(event)

    sessions._bus.subscribe(collect)

    def fail_stop_commit(cp):
        if cp.run and cp.run.progress.stopped:
            raise CheckpointError("disk full")
        return save(cp)

    monkeypatch.setattr(store.checkpoints, "save", fail_stop_commit)
    with pytest.raises(CheckpointError, match="disk full"):
        await sessions.send_message(session.id, "inspect")
    assert len(model.calls) == 5
    assert not any(event.type == "run.finished" and event.status == "success" for event in events)


async def test_child_agent_uses_same_guard(tmp_path):
    (tmp_path / "a.txt").write_text("unchanged", encoding="utf-8")
    model = RepeatingModel()
    tool = SpawnAgentTool(model, EventBus(), "root", None, 20, BackgroundTaskRegistry(),
                          tmp_path / "runs", "session", workspace=tmp_path)
    result = await tool.invoke({"description": "inspect", "prompt": "inspect file"})
    assert result.is_error and "no_progress" in result.content and len(model.calls) == 5


async def test_committed_stop_survives_crash_without_another_model_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("unchanged", encoding="utf-8")
    model = RepeatingModel()
    store, sessions = manager(tmp_path, model)
    session = await sessions.create("chat")
    save = store.checkpoints.save

    def crash_after_commit(cp):
        save(cp)
        if cp.run and cp.run.progress.stopped and cp.run.status == "running":
            raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(store.checkpoints, "save", crash_after_commit)
        with pytest.raises(Crash):
            await sessions.send_message(session.id, "inspect file")
    _, restarted = manager(tmp_path, model)
    opened = await restarted.open(session.id)
    await restarted.resume(session.id, opened["revision"], "continue")
    task = restarted._tasks.get(session.id)
    if task is not None:
        await task
    saved = store.checkpoints.load(session.id)
    assert saved.run.status == "failed" and saved.run.reason == "no_progress"
    assert saved.run.step == len(model.calls) == 5
    assert validate_pairs(saved.messages)
