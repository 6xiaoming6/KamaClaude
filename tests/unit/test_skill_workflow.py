from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.history import is_user_request
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.config import CompactionConfig, KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills.context import render_skill, retain_skill
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.skills.tool import LoadSkillParams, LoadSkillTool
from tests.unit.test_checkpoint_recovery import Crash, Provider, finish
from tests.unit.test_compactor import history, provider
from tests.unit.test_skill_loader import write_skill
from tests.unit.test_subagent_runtime import spawn_tool


def run_state(tmp_path: Path) -> RunCheckpoint:
    return RunCheckpoint(run_id="r", goal="goal", model="fake", workspace=str(tmp_path), max_steps=10,
                         skill_catalog=SkillLoader().list_all_skills())


def setup(tmp_path: Path, model, **kwargs):
    store, bus = SessionStore(tmp_path / "sessions"), EventBus()
    config = kwargs.pop("config", KamaConfig())
    manager = SessionManager(store, lambda: AgentRunner(config, provider=model, bus=bus, **kwargs), bus,
                             compaction=config.compaction)
    return store, manager


async def test_load_only_accepts_name_and_one_skill_per_run(tmp_path: Path) -> None:
    run = run_state(tmp_path)
    tool = LoadSkillTool(run)
    assert set(tool.input_schema["properties"]) == {"name"}
    assert tool.input_schema["additionalProperties"] is False
    with pytest.raises(ValidationError):
        await tool.invoke({"name": "review", "path": "elsewhere"})
    with pytest.raises(ValidationError):
        LoadSkillParams(name="../review")
    result = await tool.invoke({"name": "review"})
    assert not result.is_error and result.preserve_in_context
    assert run.active_skill.name == "review" and run.active_skill.body in result.content
    assert run.active_skill.directory in result.content
    snapshot = run.active_skill.model_copy(deep=True)
    assert "already loaded" in (await tool.invoke({"name": "review"})).content
    assert (await tool.invoke({"name": "summarize"})).error_type == "invalid_operation"
    assert run.active_skill == snapshot


async def test_catalog_is_frozen_and_missing_files_report_errors(tmp_path: Path) -> None:
    run = run_state(tmp_path)
    loader = SkillLoader()
    tool = LoadSkillTool(run)
    before = tool.description
    write_skill(loader.root, "later")
    assert (await tool.invoke({"name": "later"})).error_type == "not_found"
    (loader.root / "review" / "SKILL.md").write_text("invalid", encoding="utf-8")
    assert (await tool.invoke({"name": "review"})).error_type == "configuration_error"
    assert run.active_skill is None and tool.description == before


async def test_skill_is_not_truncated_by_normal_output_limit(tmp_path: Path) -> None:
    loader = SkillLoader()
    write_skill(loader.root, "long", "x" * 12_000)
    run = run_state(tmp_path)
    result = await LoadSkillTool(run).invoke({"name": "long"})
    assert result.preserve_in_context
    projected = ToolOutputPreparer(CompactionConfig(tool_result_limit=100, tool_result_keep=50), tmp_path)
    assert projected.prepare("call", result) == result.content
    assert not (tmp_path / "outputs").exists()


async def test_explicit_skill_keeps_system_original_user_and_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    model = Provider([LlmResponse("end_turn", text="review done"), LlmResponse("end_turn", text="next")])
    store, manager = setup(tmp_path, model)
    session = await manager.create("chat")
    await manager.send_message(session.id, "/review src")
    cp = store.checkpoints.load(session.id)
    assert cp.run.goal == cp.run.user_input == "/review src"
    assert cp.run.tool_whitelist is None and cp.run.active_skill.name == "review"
    assert {s["name"] for s in cp.run.tool_schemas} >= {"write_file", "load_skill", "memory_read"}
    assert cp.run.active_skill.body not in model.calls[0]["system"]
    assert "Coding Agent" in model.calls[0]["system"]
    assert any(m.get("content") == render_skill(cp.run.active_skill) for m in model.calls[0]["messages"])
    assert store.read_history(session.id)[0] == {"role": "user", "content": "/review src"}
    assert len(store.read_history(session.id)) == 3
    assert sum(is_user_request(message) for message in cp.messages) == 1
    await manager.send_message(session.id, "new task")
    assert store.checkpoints.load(session.id).run.active_skill is None


async def test_unknown_or_invalid_skill_leaves_session_unmodified(tmp_path: Path) -> None:
    model = Provider([LlmResponse("end_turn", text="unused")])
    store, manager = setup(tmp_path, model)
    session = await manager.create("chat")
    original = store.checkpoints.load(session.id).model_dump()
    with pytest.raises(HandlerError, match="cannot read"):
        await manager.send_message(session.id, "/missing target")
    assert store.checkpoints.load(session.id).model_dump() == original and not model.calls
    assert not manager.is_running(session.id)


async def test_automatic_load_saves_body_snapshot_and_full_history(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    model = Provider([
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("load", "load_skill", {"name": "review"})]),
        LlmResponse("end_turn", text="done"),
    ])
    store, manager = setup(tmp_path, model)
    session = await manager.create("chat")
    await manager.send_message(session.id, "review the code")
    cp = store.checkpoints.load(session.id)
    assert cp.run.active_skill.name == "review"
    assert cp.run.tool_whitelist is None
    assert model.calls[0]["system"] == model.calls[1]["system"]
    assert render_skill(cp.run.active_skill) == model.calls[1]["messages"][-1]["content"][0]["content"]
    assert sum(is_user_request(m) for m in cp.messages) == 1
    assert store.read_history(session.id)[2]["content"][0]["content"] == render_skill(cp.run.active_skill)


async def test_skill_does_not_grant_business_permissions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    permissions = PermissionManager({"write_file": ToolPolicy(default=PermissionDecision.DENY)})
    model = Provider([
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("l", "load_skill", {"name": "review"})]),
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("w", "write_file", {"path": "a", "content": "x"})]),
        LlmResponse("end_turn", text="denied"),
    ])
    store, manager = setup(tmp_path, model, permission_manager=permissions)
    session = await manager.create("chat")
    await manager.send_message(session.id, "review")
    assert store.checkpoints.load(session.id).run.status == "success"
    assert not (tmp_path / "a").exists()
    assert model.calls[2]["messages"][-1]["content"][0]["is_error"]


async def test_resume_uses_snapshots_without_reading_global_skills(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    model = Provider([Crash(), LlmResponse("end_turn", text="done")])
    store, manager = setup(tmp_path, model)
    session = await manager.create("chat")
    with pytest.raises(Crash):
        await manager.send_message(session.id, "/review src")
    original = store.checkpoints.load(session.id)
    (SkillLoader().root / "review" / "SKILL.md").unlink()
    _, restored = setup(tmp_path, model)
    opened = await restored.open(session.id)
    monkeypatch.setattr(SkillLoader, "list_all_skills", Mock(side_effect=AssertionError("reread")))
    monkeypatch.setattr(SkillLoader, "load", Mock(side_effect=AssertionError("reread")))
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    cp = store.checkpoints.load(session.id)
    assert cp.run.active_skill == original.run.active_skill
    assert cp.run.skill_catalog == original.run.skill_catalog and cp.run.step == 1
    assert model.calls[0] == model.calls[1]


async def test_load_checkpoint_failure_stops_next_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    model = Provider([
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("l", "load_skill", {"name": "review"})]),
        LlmResponse("end_turn", text="unused"),
    ])
    store, manager = setup(tmp_path, model)
    session = await manager.create("chat")
    save = store.checkpoints.save

    def fail(cp):
        if cp.run and cp.run.active_skill is not None:
            raise CheckpointError("disk full")
        save(cp)

    monkeypatch.setattr(store.checkpoints, "save", fail)
    with pytest.raises(CheckpointError):
        await manager.send_message(session.id, "review")
    assert len(model.calls) == 1 and store.checkpoints.load(session.id).run.active_skill is None


async def test_compaction_carries_skill_once_without_loading_again(tmp_path: Path, monkeypatch) -> None:
    from kama_claude.core.compact.manager import compact_session

    monkeypatch.chdir(tmp_path)
    model = Provider([LlmResponse("end_turn", text="done")])
    store, manager = setup(tmp_path, model)
    session = await manager.create("chat")
    await manager.send_message(session.id, "/review src")
    cp = store.checkpoints.load(session.id)
    skill = cp.run.active_skill.model_copy(deep=True)
    cp.messages = [*history(10), {"role": "user", "content": "Only inspect. Never commit."}]
    store.save_checkpoint(cp)
    original_thread = store.read_history(session.id)
    (SkillLoader().root / "review" / "SKILL.md").unlink()
    summary_model = provider("Old work inspected; continue with the latest request.")
    summary_model._model = cp.run.model
    config = CompactionConfig(auto_threshold=0.01)
    result = await compact_session(store, session.id, manager._bus, config, lambda: summary_model, "")
    assert result.compacted
    assert any(m.get("content") == render_skill(skill) for m in summary_model.chat.call_args.kwargs["messages"])
    saved = store.checkpoints.load(session.id)
    assert saved.run.active_skill == skill
    text = render_skill(skill)
    assert sum(m.get("content") == text for m in saved.messages) == 1
    assert retain_skill(saved.messages, skill) is saved.messages
    assert store.read_history(session.id) == original_thread
    assert sum(is_user_request(m) for m in saved.messages) == 3
    assert saved.run.system_prompt == cp.run.system_prompt


async def test_retention_reuses_existing_tool_result(tmp_path: Path) -> None:
    run = run_state(tmp_path)
    result = await LoadSkillTool(run).invoke({"name": "review"})
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": result.content}]}]
    assert retain_skill(messages, run.active_skill) is messages


async def test_child_skills_respect_parent_and_role_whitelists(tmp_path: Path) -> None:
    from tests.unit.test_subagent_runtime import Provider as ChildProvider

    model = ChildProvider(LlmResponse("tool_use", tool_calls=[ToolCallBlock("l", "load_skill", {"name": "review"})]))
    delegated = spawn_tool(tmp_path, model, tool_whitelist=["load_skill"])
    result = await delegated.invoke({"description": "review", "prompt": "review code"})
    assert not result.is_error
    assert model.calls[1]["messages"][2]["content"][0]["content"] == render_skill(SkillLoader().load("review"))
    restricted = spawn_tool(tmp_path, ChildProvider(), tool_whitelist=["read_file"])
    run = run_state(tmp_path)
    registry = restricted._build_child_registry(EventBus(), "child", None, run=run)
    assert registry.get("load_skill") is None and run.skill_catalog == []


def test_tui_lists_global_skills(tmp_path: Path, monkeypatch) -> None:
    from kama_claude.tui.app import KamaTuiApp

    loader = SkillLoader()
    write_skill(loader.root, "unique", description="GLOBAL_SKILL")
    write_skill(tmp_path / ".kama" / "skills", "local", description="LOCAL_SKILL")
    monkeypatch.chdir(tmp_path)
    items = dict(KamaTuiApp("127.0.0.1", 7437)._build_slash_items())
    assert items["unique"] == "GLOBAL_SKILL" and "local" not in items
