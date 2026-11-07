from __future__ import annotations

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.models import _DEFAULT_CONFIG_PATH, KamaConfig
from kama_claude.core.config.toml import apply_toml


# 先加载 .env 定位配置，再按全局、项目、环境变量的优先级叠加。
def get_config() -> KamaConfig:
    config = KamaConfig()

    # .env 必须在读取 KAMA_CONFIG 之前加载，以便 .env 中的 KAMA_CONFIG 能影响 TOML 路径
    load_dotenv(".env", override=False)

    # 若显式指定 KAMA_CONFIG，只读该文件；否则按优先级叠加：全局 → 项目本地
    explicit = os.environ.get("KAMA_CONFIG")
    if explicit:
        config_paths = [Path(explicit).expanduser()]
    else:
        config_paths = [
            Path(_DEFAULT_CONFIG_PATH).expanduser(),
            Path(".kama/config.toml"),
        ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"Config parse error ({config_path}): {e}") from e
            apply_toml(config, data)

    apply_env(config)
    comp = config.compaction
    if comp.tool_result_keep > comp.tool_result_limit:
        raise SystemExit("Config error: tool_result_keep must not exceed tool_result_limit")
    try:
        comp.validate()
    except ValueError as exc:
        raise SystemExit(f"Config error: {exc}") from exc
    allocations = (
        comp.output_reserve_ratio + comp.safety_margin_ratio + comp.recent_history_ratio
        + comp.summary_max_ratio + config.memory.global_auto_ratio
        + config.memory.project_index_ratio + config.tool_search.max_loaded_schema_ratio
    )
    if allocations >= 1:
        raise SystemExit("Config error: context allocations must leave room for fixed user rules")
    return config
