from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

import pytest

from kama_claude.core.config import KamaConfig, get_config


@pytest.mark.parametrize(("profile", "window", "trigger", "recent", "output"), [
    ("context-256k.toml", 262_144, 209_715, 65_536, 16_384),
    ("context-1m.toml", 1_048_576, 838_860, 209_715, 32_768),
])
# 功能：验证两个可交付预设经真实配置入口加载后，窗口、压缩与配套额度正确。
# 设计：从临时工作目录按绝对路径加载，隔离环境覆盖，覆盖跨目录启动而不读取凭据。
def test_context_profile_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    profile: str, window: int, trigger: int, recent: int, output: int,
) -> None:
    for key in tuple(os.environ):
        if key.startswith("KAMA_"):
            monkeypatch.delenv(key)
    path = Path(__file__).resolve().parents[2] / "configs" / profile
    monkeypatch.setenv("KAMA_CONFIG", str(path))
    monkeypatch.chdir(tmp_path)
    config = get_config()
    compact = config.compaction
    assert compact.context_window_tokens == window
    assert compact.trigger_tokens == trigger
    assert compact.recent_token_budget == recent
    assert compact.output_reserve_tokens == compact.summary_max_tokens == output
    assert compact.summary_target_for(window) == 8_192
    assert compact.trigger_tokens < compact.input_limit < window
    assert compact.tool_result_limit == 32_000 and compact.tool_result_keep == 24_000
    assert int(window * config.memory.global_auto_ratio) == 524
    assert int(window * config.memory.project_index_ratio) == 1_048
    assert config.tool_search.schema_budget(window) == 10_485
    if profile == "context-1m.toml":
        defaults = KamaConfig()
        assert asdict(compact) == asdict(defaults.compaction)
        assert asdict(config.memory) == asdict(defaults.memory)
        assert config.tool_search.max_loaded_schema_ratio == defaults.tool_search.max_loaded_schema_ratio
