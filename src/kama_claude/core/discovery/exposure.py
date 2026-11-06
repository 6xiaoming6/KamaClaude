from __future__ import annotations

from typing import Any

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.compact.estimator import estimate_bytes


# 完整目录仍用于恢复校验，只有这一份派生列表进入模型和压缩预算。
def visible_schemas(run: RunCheckpoint) -> list[dict[str, Any]]:
    if run.tool_loading_mode == "all":
        return run.tool_schemas
    mcp = set(run.mcp_tool_names)
    schemas = {str(schema["name"]): schema for schema in run.tool_schemas}
    return [schema for name, schema in schemas.items() if name not in mcp] + [
        schemas[name] for name in run.loaded_tool_names if name in schemas and name in mcp
    ]


# 逐个选择完整 Schema；字节预算只计算 MCP 定义，不挤占内置工具清单。
def load_tools(run: RunCheckpoint, names: list[str]) -> tuple[list[str], list[str]]:
    schemas = {str(schema["name"]): schema for schema in run.tool_schemas}
    loaded = list(run.loaded_tool_names)
    selected, rejected = [], []
    for name in names:
        if name not in run.mcp_tool_names or name not in schemas:
            continue
        if name not in loaded:
            candidate = [schemas[item] for item in [*loaded, name]]
            if estimate_bytes(candidate) > run.max_loaded_schema_tokens:
                rejected.append(name)
                continue
            loaded.append(name)
        selected.append(name)
    run.loaded_tool_names = loaded
    return selected, rejected
