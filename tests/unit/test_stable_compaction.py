from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint, ToolCheckpoint
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.estimator import TokenCounter, TokenEstimator
from kama_claude.core.compact.history import SUMMARY_MARKER, split_history, validate_pairs
from kama_claude.core.compact.manager import validate_candidate
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.compact.request import assemble_messages
from kama_claude.core.config import CompactionConfig, KamaConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.discovery.exposure import request_schemas
from kama_claude.core.llm.provider import _conversation_cache_point
from kama_claude.core.llm.tool_loading import referenced_tools
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.memory.context import initialize_prompt, refresh_prompt
from kama_claude.core.memory.model import MemorySnapshot
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.tools.base import ToolResult
from tests.unit.test_compactor import history, provider
from tests.unit.test_deferred_tool_loading import discovery_history, run_state
from tests.unit.test_s9_context import make_loop


def fresh_run(path: Path) -> RunCheckpoint:
    return RunCheckpoint(run_id="stable", goal="g", model="fake", max_steps=10, workspace=str(path))


# 普通调用、摘要请求及压缩后的调用共享同一 system 和原生工具目录。
async def test_stable_prefix_and_atomic_memory_refresh(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("FROZEN_RULE", encoding="utf-8")
    memory = MemoryStore(tmp_path)
    memory.home.mkdir(parents=True)
    memory.global_path().write_text("OLD_AUTO", encoding="utf-8")
    run = fresh_run(tmp_path)
    initialize_prompt(run, memory, "BASE")
    run.tool_loading_mode = "deferred"
    run.tool_schemas = [{"name": "search_tool", "input_schema": {}},
                        {"name": "remote", "input_schema": {}}]
    run.schemas_frozen = True
    run.mcp_tool_names = ["remote"]
    system, schemas = run.system_prompt, request_schemas(run)
    model = provider()
    model.chat.side_effect = [LlmResponse("end_turn", text="old work completed"),
                              LlmResponse("end_turn", text="done")]
    loop, context, events = make_loop(tmp_path, history(6), model)
    context.run = run
    # 测试替换同一 Context 的 Run 后，预算绑定也同步更新。
    loop._budget.run = run
    loop._context_manager._memory = memory
    persistence = Mock()
    loop._context_manager._persistence = persistence
    rules.write_text("NEXT_RUN_RULE", encoding="utf-8")
    memory.global_path().write_text("NEW_AUTO", encoding="utf-8")
    await loop.run()
    assert run.status == "success" and model.chat.call_count == 2
    first, second = [call.kwargs for call in model.chat.call_args_list]
    assert first["system"] == second["system"] == system
    assert first["tool_schemas"] == second["tool_schemas"] == schemas
    assert "OLD_AUTO" in first["messages"][0]["content"]
    assert "NEW_AUTO" in second["messages"][0]["content"]
    assert json.dumps(second["messages"]).count("NEW_AUTO") == 1
    assert "NEXT_RUN_RULE" not in run.system_prompt
    assert context.messages[0]["content"].startswith(SUMMARY_MARKER)
    assert "Automatic memory context" not in json.dumps(context.messages)
    assert persistence.save.called and any(e.type == "context.compacted" for e in events)
    next_run = fresh_run(tmp_path)
    initialize_prompt(next_run, memory, "BASE")
    assert "NEXT_RUN_RULE" in next_run.system_prompt


def test_legacy_prompt_layout_is_not_reinterpreted(tmp_path: Path) -> None:
    run = fresh_run(tmp_path)
    run.system_prompt = "LEGACY_COMBINED_SYSTEM"
    run.memory_snapshot = MemorySnapshot(text="legacy memory")
    messages = history()
    memory = MemoryStore(tmp_path)
    assert assemble_messages(run, messages) == messages
    assert refresh_prompt(run, memory) == (run.memory_snapshot, run.system_prompt)
    assert ToolCheckpoint(id="old", name="read_file", input={}).output_prepared


# 最近三轮是真实用户轮次，而非三个工具步骤。
def test_recent_turns_and_budget_fallback() -> None:
    messages = history(6)
    old, recent = split_history(messages, 3, 16_384, TokenCounter(), mcp_names=set())
    assert old == messages[:6] and recent == messages[6:]
    budget = TokenCounter().count(messages[-2:]) + 1
    old, recent = split_history(messages, 3, budget, TokenCounter(), mcp_names=set())
    assert old == messages[:-2] and recent == messages[-2:]


def test_long_single_turn_preserves_latest_request_and_whole_groups() -> None:
    request = {"role": "user", "content": "Only change tests. NEVER commit."}
    messages = [request]
    for i in range(4):
        messages += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": str(i),
                                               "name": "read_file", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i),
                                          "content": "x" * 2000}]},
        ]
    budget = TokenCounter().count([request, *messages[-2:]]) + 1
    old, recent = split_history(messages, 3, budget, TokenCounter(), mcp_names=set())
    assert recent == [request, *messages[-2:]]
    assert request not in old and validate_pairs(recent)
    with pytest.raises(ValueError, match="recent_context_too_large"):
        split_history(messages, 3, 30, TokenCounter(), mcp_names=set())


# 未达阈值不调用摘要、不回溯裁剪历史；手动关闭自动时仍使用 80% 阈值。
async def test_under_threshold_preserves_old_outputs_and_skips_provider(tmp_path: Path) -> None:
    messages = discovery_history()
    messages[-1]["content"][0]["content"] = "x" * 12_000
    before = copy.deepcopy(messages)
    factory = Mock(side_effect=AssertionError("must not construct SDK"))
    result = await Compactor(CompactionConfig(auto_threshold=0)).prepare_context(
        messages, manual=True, provider_factory=factory,
    )
    assert result.reason == "below_threshold" and not result.compacted
    assert result.messages == messages == before and not list(tmp_path.iterdir())
    factory.assert_not_called()


# 摘要携带原请求，失效或有工具调用的摘要都不会执行或替换历史。
async def test_summary_keeps_prefix_and_rejects_tool_calls() -> None:
    messages = history(6)
    model = provider()
    model.chat.return_value = LlmResponse("tool_use", tool_calls=[ToolCallBlock("x", "danger", {})])
    schemas = [{"name": "danger", "input_schema": {}}]
    automatic = [{"role": "user", "content": "automatic background"}]
    result = await Compactor().compact_messages(
        messages, model, system="fixed", tool_schemas=schemas, context_messages=automatic,
    )
    assert result.reason == "summary_incomplete" and result.messages == messages
    args = model.chat.call_args.kwargs
    assert args["messages"][:-1] == automatic + messages
    assert args["system"] == "fixed" and args["tool_schemas"] == schemas
    assert "OLD record numbers" in args["messages"][-1]["content"]


# 旧引用仅在近期调用需要时带回；不再恢复过时工具的全部可见额度。
async def test_old_references_drop_and_required_reference_returns(tmp_path: Path) -> None:
    messages = discovery_history()
    messages[1]["content"].insert(0, {"type": "text", "text": "old explanation " * 4000})
    latest = {"role": "user", "content": "new request"}
    run = run_state(loaded_tool_names=["remote__get_issue"])
    run.workspace = str(tmp_path)
    schemas = request_schemas(run)
    config = CompactionConfig(keep_recent_turns=1)
    result = await Compactor(config).compact_messages([*messages, latest], provider(), tool_schemas=schemas)
    assert result.compacted and referenced_tools(result.messages) == []
    _, loaded = validate_candidate(run, result, None, config, TokenEstimator())
    assert loaded == [] and run.loaded_tool_names == ["remote__get_issue"]
    call = {"role": "assistant", "content": [{"type": "tool_use", "id": "actual",
                                               "name": "remote__get_issue", "input": {}}]}
    answer = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "actual",
                                           "content": "issue"}]}
    result = await Compactor(config).compact_messages(
        [*messages, latest, call, answer], provider(), tool_schemas=schemas,
    )
    assert result.compacted and referenced_tools(result.messages) == ["remote__get_issue"]
    assert result.messages[-3:] == [latest, call, answer] and validate_pairs(result.messages)
    _, loaded = validate_candidate(run, result, None, config, TokenEstimator())
    assert loaded == ["remote__get_issue"]
    # 引用展开后的定义计入近期额度，不能用短引用掩盖真实占用。
    too_small = CompactionConfig(keep_recent_turns=1, recent_history_ratio=(100) / (1_048_576))
    model = provider()
    rejected = await Compactor(too_small).compact_messages(
        [*messages, latest, call, answer], model, tool_schemas=schemas,
    )
    assert rejected.reason == "recent_context_too_large"
    model.chat.assert_not_called()


@pytest.mark.parametrize("provider_name", ["anthropic", "deepseek"])
async def test_thinking_only_removed_from_anthropic_rewritten_projection(provider_name: str) -> None:
    messages = history(6)
    messages[-1] = {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "opaque", "signature": "real-signature"},
        {"type": "text", "text": "recent answer"},
    ]}
    original = copy.deepcopy(messages)
    result = await Compactor(estimator=TokenEstimator(provider_name, "m")).compact_messages(messages, provider())
    assert result.compacted
    content = result.messages[-1]["content"]
    assert any(b.get("type") == "thinking" for b in content) == (provider_name == "deepseek")
    assert messages == original


# 全文先归档，结果 ID/错误与结构化引用仍由原 ToolResult 提供。
def test_output_preparer_keeps_full_result_and_references(tmp_path: Path) -> None:
    result = ToolResult("HEAD" + "x" * 40_000 + "TAIL", tool_references=["remote"])
    before = copy.deepcopy(result)
    preparer = ToolOutputPreparer(CompactionConfig(), tmp_path)
    preview = preparer.prepare("call", result)
    assert isinstance(preview, list) and preview[-1] == {"type": "tool_reference", "tool_name": "remote"}
    assert "chars omitted" in preview[0]["text"]
    archive = next((tmp_path / "outputs").glob("*.txt"))
    assert archive.read_text(encoding="utf-8") == result.content
    assert preparer.prepare("call", result) == preview and result == before
    assert len(list((tmp_path / "outputs").glob("*.txt"))) == 1


def test_cache_point_is_only_on_request_copy() -> None:
    original = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c",
                                              "content": "ok"}]}]
    projected = _conversation_cache_point(copy.deepcopy(original))
    assert projected[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(original)


@pytest.mark.parametrize("old", ["keep_recent_groups", "prune_target_ratio"])
def test_removed_toml_settings_require_migration(old: str) -> None:
    with pytest.raises(SystemExit, match="migration"):
        apply_toml(KamaConfig(), {"compaction": {old: 1}})


@pytest.mark.parametrize("old", ["KAMA_COMPACT_KEEP_GROUPS", "KAMA_COMPACT_PRUNE_TARGET"])
def test_removed_environment_requires_migration(old: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(old, "1")
    with pytest.raises(SystemExit, match="migration"):
        apply_env(KamaConfig())


def test_recent_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = KamaConfig()
    apply_toml(config, {"compaction": {"keep_recent_turns": 4, "recent_history_ratio": 0.3}})
    monkeypatch.setenv("KAMA_COMPACT_KEEP_TURNS", "2")
    monkeypatch.setenv("KAMA_COMPACT_RECENT_RATIO", "0.2")
    apply_env(config)
    assert config.compaction.keep_recent_turns == 2
    assert config.compaction.recent_token_budget == int(config.compaction.context_window_tokens * 0.2)


# 候选和自动记忆必须一次提交，不能在保存失败后留下半套内存状态。
async def test_adoption_save_failure_restores_working_state(tmp_path: Path) -> None:
    from kama_claude.core.checkpoint.checkpoint import CheckpointError
    from kama_claude.core.compact.compactor import CompactionResult

    run = fresh_run(tmp_path)
    run.prompt_layout = "stable_v1"
    run.system_prompt = "fixed"
    run.memory_snapshot = MemorySnapshot(text="old memory")
    model = provider()
    loop, context, events = make_loop(tmp_path, history(), model)
    context.run = run
    loop._budget.run = run
    original_messages = copy.deepcopy(context.messages)
    loop._context_manager._compactor = Mock(prepare_context=AsyncMock(return_value=CompactionResult(
        [{"role": "user", "content": "short"}], 1000, 10, compacted=True,
        applied_stages=["llm_summary"],
    )))
    loop._context_manager.config.auto_threshold = 0.0001
    loop._context_manager._persistence = Mock(save=Mock(side_effect=CheckpointError("disk")))
    with pytest.raises(CheckpointError):
        await loop.run()
    assert context.messages == original_messages and run.memory_snapshot.text == "old memory"
    model.chat.assert_not_called()
    assert not any(event.type == "context.compacted" for event in events)


# 同一个调用 ID 可以在不同模型交互中再次出现，但同批次重复必须拒绝。
def test_tool_pairs_are_scoped_to_complete_interactions() -> None:
    pair = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "same", "name": "read_file", "input": {}},
    ]}, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "same", "content": "done"},
    ]}]
    assert validate_pairs([*pair, *copy.deepcopy(pair)])
    invalid = copy.deepcopy(pair)
    invalid[0]["content"].append(copy.deepcopy(invalid[0]["content"][0]))
    assert not validate_pairs(invalid)


# 刷新自动记忆后再次验证整体缩减率，索引增长不能制造假成功。
def test_refreshed_memory_cannot_erase_required_savings(tmp_path: Path) -> None:
    from kama_claude.core.compact.compactor import CompactionResult
    from kama_claude.core.config import MemoryConfig

    run = fresh_run(tmp_path)
    memory = MemoryStore(tmp_path, MemoryConfig(global_auto_ratio=2000 / 1_048_576))
    memory.home.mkdir(parents=True)
    initialize_prompt(run, memory, "BASE")
    before_memory = run.memory_snapshot.model_copy(deep=True)
    memory.global_path().write_text("x" * 3000, encoding="utf-8")
    result = CompactionResult(history(1), 1000, 100, compacted=True)
    snapshot, loaded = validate_candidate(run, result, memory, CompactionConfig(), TokenEstimator())
    assert not result.compacted and result.reason == "insufficient_reduction"
    assert snapshot == run.memory_snapshot == before_memory and loaded == []


# 旧快照的 pending 工具也可能刚刚执行；只能跳过旧 completed 结果的整理。
async def test_legacy_pending_tool_prepares_new_output(tmp_path: Path) -> None:
    from tests.unit.test_s9_context import LargeTool

    model = provider()
    call = {"role": "assistant", "content": [{"type": "tool_use", "id": "legacy",
                                               "name": "large", "input": {}}]}
    loop, context, _ = make_loop(
        tmp_path, [{"role": "user", "content": "goal"}, call], model,
        CompactionConfig(auto_threshold=0),
    )
    context.run.phase = "tools"
    context.run.tools = [ToolCheckpoint(id="legacy", name="large", input={})]
    tool = LargeTool()
    loop._tools.registry.register(tool)
    await loop.run()
    assert context.run.status == "success" and tool.calls == 1
    assert "chars omitted" in model.chat.call_args.kwargs["messages"][-1]["content"][0]["content"]
    assert next((tmp_path / "outputs").glob("*.txt")).read_text(encoding="utf-8").endswith("TAIL")
