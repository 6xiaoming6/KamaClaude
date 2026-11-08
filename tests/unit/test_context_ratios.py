from __future__ import annotations

import math

import pytest

from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.config import CompactionConfig, KamaConfig, MemoryConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.memory.model import MemoryError
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.runner import AgentRunner
from kama_claude.core.tools.base import ToolResult
from tests.unit.test_compactor import history, provider


@pytest.mark.parametrize("window", [8_192, 65_536, 262_144, 1_048_576])
# 功能：验证日常编码的软触发线、输出及近期历史额度随模型窗口按比例缩放。
# 设计：覆盖小窗口到 1M，分别检查软阈值和硬预算，验证所有额度都使用有效窗口。
def test_all_context_allowances_scale_with_window(window):
    config = CompactionConfig(context_window_tokens=window)
    assert config.trigger_tokens == int(window * 0.8)
    assert config.output_reserve_tokens == int(window * 0.0625)
    assert config.recent_token_budget == int(window * 0.25)
    assert config.summary_max_tokens == int(window * 0.0625)
    assert config.safety_margin_tokens == math.ceil(window * 0.02)
    assert config.input_limit == window - config.output_reserve_tokens - config.safety_margin_tokens
    assert config.summary_input_limit == window - config.summary_max_tokens - config.safety_margin_tokens
    assert config.summary_target_for(window) == int(window * 0.03125)


# 功能：验证窗口变更后重新计算上下文份额，小历史摘要仍不会被扩写。
# 设计：在同一配置对象上放大窗口，避免只验证构造时一次计算的结果。
def test_allowances_are_recomputed_not_cached_as_fixed_tokens():
    config = CompactionConfig(context_window_tokens=65_536)
    before = config.output_reserve_tokens
    config.context_window_tokens *= 4
    assert config.output_reserve_tokens == before * 4  # 此比例在所测窗口下可整除。
    assert config.recent_token_budget == int(config.context_window_tokens * 0.25)
    assert config.summary_target_for(400) == 100  # 小历史不因窗口增大而被扩写。


@pytest.mark.parametrize("field", ["output_reserve_ratio", "safety_margin_ratio", "recent_history_ratio",
                                   "summary_max_ratio", "summary_target_ratio"])
@pytest.mark.parametrize("value", [0, -0.1, 1, float("nan"), float("inf"), True, "0.1"])
def test_invalid_ratio_is_rejected(field, value):
    with pytest.raises(ValueError):
        CompactionConfig(**{field: value})
    with pytest.raises(SystemExit):
        apply_toml(KamaConfig(), {"compaction": {field: value}})


@pytest.mark.parametrize("settings", [
    {"summary_target_ratio": 0.2},
    {"recent_history_ratio": 0.95},
    {"auto_threshold": 0.95},
])
def test_ratio_combinations_must_leave_room(settings):
    with pytest.raises(ValueError):
        CompactionConfig(**settings)


@pytest.mark.parametrize(("section", "field"), [
    ("compaction", "output_reserve_tokens"), ("compaction", "recent_token_budget"),
    ("compaction", "summary_max_tokens"), ("memory", "global_auto_tokens"),
    ("memory", "project_index_tokens"), ("tool_search", "max_loaded_schema_tokens"),
])
def test_fixed_token_configuration_requires_migration(section, field):
    with pytest.raises(SystemExit, match="migration"):
        apply_toml(KamaConfig(), {section: {field: 100}})


@pytest.mark.parametrize("name", [
    "KAMA_COMPACT_OUTPUT_TOKENS", "KAMA_COMPACT_RECENT_TOKENS", "KAMA_COMPACT_SUMMARY_TOKENS",
    "KAMA_MEMORY_GLOBAL_AUTO_TOKENS", "KAMA_MEMORY_PROJECT_INDEX_TOKENS",
    "KAMA_TOOL_SEARCH_MAX_LOADED_SCHEMA_TOKENS",
])
def test_fixed_token_environment_requires_migration(name, monkeypatch):
    monkeypatch.setenv(name, "100")
    with pytest.raises(SystemExit, match="migration"):
        apply_env(KamaConfig())


def test_environment_ratios_follow_window_override(monkeypatch):
    config = KamaConfig()
    apply_toml(config, {"compaction": {"context_window_tokens": 65_536,
                                      "output_reserve_ratio": 0.04}})
    monkeypatch.setenv("KAMA_COMPACT_WINDOW_TOKENS", "262144")
    monkeypatch.setenv("KAMA_COMPACT_RECENT_RATIO", "0.2")
    monkeypatch.setenv("KAMA_COMPACT_SUMMARY_RATIO", "0.12")
    monkeypatch.setenv("KAMA_MEMORY_GLOBAL_AUTO_RATIO", "0.001")
    monkeypatch.setenv("KAMA_TOOL_SEARCH_MAX_LOADED_SCHEMA_RATIO", "0.02")
    apply_env(config)
    config.compaction.validate()
    assert config.compaction.output_reserve_tokens == int(262144 * 0.04)
    assert config.compaction.recent_token_budget == int(262144 * 0.2)
    assert config.compaction.summary_max_tokens == int(262144 * 0.12)
    assert config.memory.global_auto_ratio == 0.001
    assert config.tool_search.schema_budget(262144) == int(262144 * 0.02)


def test_memory_uses_the_same_live_window_config(tmp_path):
    config = CompactionConfig(context_window_tokens=65_536)
    store = MemoryStore(tmp_path, MemoryConfig(global_auto_ratio=0.01), context_budget=config)
    store.home.mkdir(parents=True)
    store.global_path().write_text("x" * 2000, encoding="utf-8")
    assert store.global_text() == "x" * 2000
    config.context_window_tokens = 16_384
    with pytest.raises(MemoryError, match="exceeds"):
        store.global_text()
    assert store.global_path().read_text(encoding="utf-8") == "x" * 2000


def test_mcp_budget_is_derived_when_new_run_is_frozen():
    config = KamaConfig()
    config.tool_search.max_loaded_schema_ratio = 0.01
    config.compaction.context_window_tokens = 65_536
    runner = AgentRunner(config)
    first = runner._new_run_state("goal", "first", ["finish"])
    config.compaction.context_window_tokens = 1_048_576
    second = runner._new_run_state("goal", "second", ["finish"])
    assert first.max_loaded_schema_tokens == 655
    assert second.max_loaded_schema_tokens == 10485


def test_tool_output_preview_stays_fixed_when_window_changes(tmp_path):
    config = CompactionConfig(context_window_tokens=65_536)
    preparer = ToolOutputPreparer(config, tmp_path)
    result = ToolResult("HEAD" + "x" * 40000 + "TAIL")
    before = preparer.prepare("tool", result)
    config.context_window_tokens = 1_048_576
    after = preparer.prepare("tool", result)
    assert before == after and "HEAD" in after and "TAIL" in after
    assert config.tool_result_limit == 32000 and config.tool_result_keep == 24000


async def test_actual_fixed_prefix_cannot_be_hidden_by_history_ratios():
    model = provider()
    messages = history()
    result = await Compactor(CompactionConfig(context_window_tokens=20_000)).compact_messages(
        messages, model, system="x" * 80_000,
    )
    assert not result.compacted and result.reason == "required_context_too_large"
    assert result.messages == messages
    model.chat.assert_not_called()
