from __future__ import annotations

import os
from dataclasses import asdict

from kama_claude.core.config.models import KamaConfig, ToolSearchConfig


# 按配置小节应用环境覆盖，各小节自行完成类型校验。
def apply_env(config: KamaConfig) -> None:
    _apply_core(config)
    _apply_logging(config)
    _apply_agent(config)
    _apply_subagent(config)
    _apply_llm(config)
    _apply_trace(config)
    _apply_permission(config)
    _apply_compaction(config)
    _apply_memory(config)
    _apply_tool_search(config)


# 环境变量沿用 TOML 的字段和验证规则，不读取远程模型地址或密钥。
def _apply_tool_search(config: KamaConfig) -> None:
    values = asdict(config.tool_search)
    for key in values:
        raw = os.environ.get(f"KAMA_TOOL_SEARCH_{key.upper()}")
        if raw is None:
            continue
        if key == "enabled":
            if raw.lower() not in {"true", "false", "1", "0"}:
                raise SystemExit("Config error: KAMA_TOOL_SEARCH_ENABLED must be a boolean")
            values[key] = raw.lower() in {"true", "1"}
        elif key in {"top_k", "max_loaded_schema_tokens"}:
            try:
                values[key] = int(raw)
            except ValueError as exc:
                raise SystemExit(f"Config error: tool_search.{key} must be an integer") from exc
        else:
            values[key] = raw
    try:
        config.tool_search = ToolSearchConfig(**values)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Config error: {exc}") from exc


# 环境变量覆盖 TOML，零、负数和非整数都不能成为有效子任务限额。
def _apply_subagent(config: KamaConfig) -> None:
    for key in ("max_active", "max_total"):
        name = f"KAMA_SUBAGENT_{key.upper()}"
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            amount = int(raw)
        except ValueError as exc:
            raise SystemExit(f"Config error: {name} must be a positive integer") from exc
        if amount <= 0:
            raise SystemExit(f"Config error: {name} must be a positive integer")
        setattr(config.subagent, key, amount)


def _apply_core(config: KamaConfig) -> None:
    host = os.environ.get("KAMA_HOST")
    if host is not None:
        config.host = host

    port_str = os.environ.get("KAMA_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: KAMA_PORT must be an integer, got: {port_str!r}")


def _apply_logging(config: KamaConfig) -> None:
    log_level = os.environ.get("KAMA_LOG_LEVEL")
    if log_level is not None:
        config.logging.level = log_level

    log_file = os.environ.get("KAMA_LOG_FILE")
    if log_file is not None:
        config.logging.file = log_file

    log_format = os.environ.get("KAMA_LOG_FORMAT")
    if log_format is not None:
        config.logging.format = log_format


def _apply_agent(config: KamaConfig) -> None:
    max_steps_str = os.environ.get("KAMA_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )
    for key in ("max_run_seconds", "max_total_tokens", "repeat_failure_limit", "no_progress_limit"):
        name = "KAMA_AGENT_" + key.upper()
        raw = os.environ.get(name)
        if raw is not None:
            try:
                amount = int(raw)
            except ValueError as exc:
                raise SystemExit(f"Config error: {name} must be a nonnegative integer") from exc
            if amount < 0:
                raise SystemExit(f"Config error: {name} must be a nonnegative integer")
            if key == "no_progress_limit" and amount != 0 and not 4 <= amount <= 20:
                raise SystemExit(f"Config error: {name} must be 0 or from 4 to 20")
            setattr(config.agent, key, amount)


def _apply_llm(config: KamaConfig) -> None:
    default_model = os.environ.get("KAMA_LLM_DEFAULT_MODEL")
    if default_model is not None:
        config.llm.default_model = default_model


def _apply_trace(config: KamaConfig) -> None:
    trace_enabled = os.environ.get("KAMA_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = os.environ.get("KAMA_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = os.environ.get("KAMA_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")


def _apply_permission(config: KamaConfig) -> None:
    perm_timeout = os.environ.get("KAMA_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    f"Config error: KAMA_PERMISSION_TIMEOUT_S must be >= 0, got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_PERMISSION_TIMEOUT_S must be a number, got: {perm_timeout!r}"
            )


def _apply_compaction(config: KamaConfig) -> None:
    if any(key in os.environ for key in ("KAMA_COMPACT_KEEP_GROUPS", "KAMA_COMPACT_PRUNE_TARGET")):
        raise SystemExit(
            "Config migration: remove KAMA_COMPACT_PRUNE_TARGET; use KAMA_COMPACT_KEEP_TURNS "
            "and KAMA_COMPACT_RECENT_TOKENS instead of KAMA_COMPACT_KEEP_GROUPS"
        )
    compact_threshold = os.environ.get("KAMA_COMPACT_THRESHOLD")
    if compact_threshold is not None:
        try:
            compact_threshold_val = float(compact_threshold)
            if not (0.0 <= compact_threshold_val <= 1.0):
                raise SystemExit(
                    "Config error: KAMA_COMPACT_THRESHOLD must be between 0 and 1, "
                    f"got: {compact_threshold!r}"
                )
            config.compaction.auto_threshold = compact_threshold_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_COMPACT_THRESHOLD must be a number, got: {compact_threshold!r}"
            )

    compact_tool_limit = os.environ.get("KAMA_COMPACT_TOOL_LIMIT")
    if compact_tool_limit is not None:
        try:
            compact_tool_limit_val = int(compact_tool_limit)
            if compact_tool_limit_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_LIMIT must be a positive integer, "
                    f"got: {compact_tool_limit!r}"
                )
            config.compaction.tool_result_limit = compact_tool_limit_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_LIMIT must be an integer, "
                f"got: {compact_tool_limit!r}"
            )

    compact_tool_keep = os.environ.get("KAMA_COMPACT_TOOL_KEEP")
    if compact_tool_keep is not None:
        try:
            compact_tool_keep_val = int(compact_tool_keep)
            if compact_tool_keep_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_KEEP must be a positive integer, "
                    f"got: {compact_tool_keep!r}"
                )
            config.compaction.tool_result_keep = compact_tool_keep_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_KEEP must be an integer, "
                f"got: {compact_tool_keep!r}"
            )
    for suffix, key in {
        "WINDOW_TOKENS": "context_window_tokens",
        "OUTPUT_TOKENS": "output_reserve_tokens",
        "KEEP_TURNS": "keep_recent_turns",
        "RECENT_TOKENS": "recent_token_budget",
        "SUMMARY_TOKENS": "summary_max_tokens",
    }.items():
        name = "KAMA_COMPACT_" + suffix
        value = os.environ.get(name)
        if value is None:
            continue
        try:
            parsed = int(value)
        except ValueError as exc:
            raise SystemExit(f"Config error: {name} must be a positive integer") from exc
        if parsed <= 0:
            raise SystemExit(f"Config error: {name} must be a positive integer")
        setattr(config.compaction, key, parsed)


def _apply_memory(config: KamaConfig) -> None:
    for key in ("global_auto_tokens", "project_index_tokens"):
        name = f"KAMA_MEMORY_{key.upper()}"
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            memory_amount = int(raw)
        except ValueError as exc:
            raise SystemExit(f"Config error: {name} must be a positive integer") from exc
        if memory_amount <= 0:
            raise SystemExit(f"Config error: {name} must be a positive integer")
        setattr(config.memory, key, memory_amount)
