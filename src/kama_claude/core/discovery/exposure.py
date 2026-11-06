from __future__ import annotations

from typing import Any

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.llm.tool_loading import referenced_tools


# 非原生 Provider 和旧 Run 的可见定义；原生请求由 request_schemas 保持目录稳定。
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


# 原生延迟加载始终发送完整目录与固定标记，搜索后不移动或改写顶层 tools。
def request_schemas(run: RunCheckpoint) -> list[dict[str, Any]]:
    if run.tool_loading_mode != "deferred":
        return visible_schemas(run)
    mcp = set(run.mcp_tool_names)
    return [{**schema, "defer_loading": str(schema["name"]) in mcp}
            for schema in run.tool_schemas]


# 发送给服务端不等于已提供给模型；本轮调用资格只来自请求前已有的引用。
def callable_names(run: RunCheckpoint, messages: list[dict[str, Any]]) -> set[str]:
    if run.tool_loading_mode != "deferred":
        return {str(schema["name"]) for schema in visible_schemas(run)}
    mcp = set(run.mcp_tool_names)
    builtin = {str(schema["name"]) for schema in run.tool_schemas} - mcp
    return builtin | (set(referenced_tools(messages)) & mcp)
