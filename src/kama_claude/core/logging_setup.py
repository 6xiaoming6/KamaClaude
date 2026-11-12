from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

from kama_claude.core.config import KamaConfig

_TEXT_FMT = 'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"'


class JsonFormatter(logging.Formatter):
    # 异常堆栈和换行也作为 JSON 字符串转义，每条日志始终占一行。
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "level": record.levelname,
            "ts": self.formatTime(record, self.datefmt),
            "source": record.name,
            "msg": super().format(record),
        }, ensure_ascii=False)


def config_summary(config: KamaConfig) -> dict[str, object]:
    # 只挑选明确安全的运行参数；不遍历配置，不输出环境值、请求头、URL 或启动参数。
    return {
        "model": config.llm.default_model,
        "agent": {
            "max_steps": config.agent.max_steps,
            "max_run_seconds": config.agent.max_run_seconds,
            "max_total_tokens": config.agent.max_total_tokens,
            "repeat_failure_limit": config.agent.repeat_failure_limit,
            "no_progress_limit": config.agent.no_progress_limit,
        },
        "subagent": {
            "max_active": config.subagent.max_active,
            "max_total": config.subagent.max_total,
        },
        "compaction": {
            "auto_threshold": config.compaction.auto_threshold,
            "context_window_tokens": config.compaction.context_window_tokens,
            "output_reserve_tokens": config.compaction.output_reserve_tokens,
            "summary_max_tokens": config.compaction.summary_max_tokens,
            "recent_token_budget": config.compaction.recent_token_budget,
            "safety_margin_tokens": config.compaction.safety_margin_tokens,
            "trigger_tokens": config.compaction.trigger_tokens,
        },
        "trace_enabled": config.trace.enabled,
        "permission_timeout_s": config.permission.timeout_s,
        "tool_search_enabled": config.tool_search.enabled,
        "mcp_server_count": len(config.mcp.servers),
        "execution_env_count": len(config.execution.env),
        "execution_backend": config.execution.backend,
        "execution_forward_env_count": len(config.execution.forward_env),
    }


# 根据配置初始化 root logger：设置级别、格式，并挂载 stderr 和可选的滚动文件 handler
def setup_logging(config: KamaConfig) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    formatter = (
        JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S") if config.logging.format == "json"
        else logging.Formatter(_TEXT_FMT, datefmt="%Y-%m-%dT%H:%M:%S")
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    if config.logging.file:
        log_path = Path(config.logging.file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
