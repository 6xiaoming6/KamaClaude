from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from kama_claude.core.config.models import KamaConfig, McpServerConfig, ToolSearchConfig


# 校验顶层配置名，再委托各小节解析。
def apply_toml(config: KamaConfig, data: dict[str, Any]) -> None:
    unknown = set(data.keys()) - {
        "core", "logging", "agent", "llm", "trace", "permission", "execution",
        "compaction", "memory", "mcp", "subagent", "tool_search",
    }
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        _apply_core(config, data["core"])
    if "logging" in data:
        _apply_logging(config, data["logging"])
    if "agent" in data:
        _apply_agent(config, data["agent"])
    if "subagent" in data:
        _apply_subagent(config, data["subagent"])
    if "llm" in data:
        _apply_llm(config, data["llm"])
    if "trace" in data:
        _apply_trace(config, data["trace"])
    if "permission" in data:
        _apply_permission(config, data["permission"])
    if "execution" in data:
        _apply_execution(config, data["execution"])
    if "compaction" in data:
        _apply_compaction(config, data["compaction"])
    if "memory" in data:
        _apply_memory(config, data["memory"])
    if "mcp" in data:
        _apply_mcp(config, data["mcp"])
    if "tool_search" in data:
        _apply_tool_search(config, data["tool_search"])


# 小节整体校验成功后再替换，避免配置只应用一半。
def _apply_tool_search(config: KamaConfig, value: Any) -> None:
    if not isinstance(value, dict):
        raise SystemExit("Config error: [tool_search] must be a table")
    unknown = set(value) - set(ToolSearchConfig.__dataclass_fields__)
    if unknown:
        raise SystemExit(f"Unknown [tool_search] keys: {', '.join(sorted(unknown))}")
    try:
        config.tool_search = ToolSearchConfig(**value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Config error: {exc}") from exc


# 子任务只配置同一根 Run 的并发与总派生数量，不引入排队状态。
def _apply_subagent(config: KamaConfig, value: Any) -> None:
    if not isinstance(value, dict):
        raise SystemExit("Config error: [subagent] must be a table")
    unknown = set(value) - {"max_active", "max_total"}
    if unknown:
        raise SystemExit(f"Unknown [subagent] keys: {', '.join(sorted(unknown))}")
    for key, amount in value.items():
        if type(amount) is not int or amount <= 0:
            raise SystemExit(f"Config error: subagent.{key} must be a positive integer")
        setattr(config.subagent, key, amount)


def _apply_core(config: KamaConfig, value: Any) -> None:
    core = value
    if not isinstance(core, dict):
        raise SystemExit("Config error: [core] must be a table")
    unknown_core: set[str] = set(core.keys()) - {"host", "port"}
    if unknown_core:
        raise SystemExit(f"Unknown [core] keys: {', '.join(sorted(unknown_core))}")
    if "host" in core:
        val = core["host"]
        if not isinstance(val, str):
            raise SystemExit("Config error: core.host must be a string")
        config.host = val
    if "port" in core:
        val = core["port"]
        if not isinstance(val, int):
            raise SystemExit("Config error: core.port must be an integer")
        config.port = val


def _apply_logging(config: KamaConfig, value: Any) -> None:
    log = value
    if not isinstance(log, dict):
        raise SystemExit("Config error: [logging] must be a table")
    unknown_log: set[str] = set(log.keys()) - {"level", "file", "format"}
    if unknown_log:
        raise SystemExit(f"Unknown [logging] keys: {', '.join(sorted(unknown_log))}")
    for key in ("level", "file", "format"):
        if key in log:
            val = log[key]
            if not isinstance(val, str):
                raise SystemExit(f"Config error: logging.{key} must be a string")
            setattr(config.logging, key, val)


def _apply_agent(config: KamaConfig, value: Any) -> None:
    agent = value
    if not isinstance(agent, dict):
        raise SystemExit("Config error: [agent] must be a table")
    unknown_agent: set[str] = set(agent.keys()) - {
        "max_steps", "max_run_seconds", "max_total_tokens", "repeat_failure_limit",
    }
    if unknown_agent:
        raise SystemExit(f"Unknown [agent] keys: {', '.join(sorted(unknown_agent))}")
    if "max_steps" in agent:
        val = agent["max_steps"]
        if not isinstance(val, int) or val <= 0:
            raise SystemExit("Config error: agent.max_steps must be a positive integer")
        config.agent.max_steps = val

    for key in ("max_run_seconds", "max_total_tokens", "repeat_failure_limit"):
        if key in agent:
            value = agent[key]
            if type(value) is not int or value < 0:
                raise SystemExit(f"Config error: agent.{key} must be a nonnegative integer")
            setattr(config.agent, key, value)


def _apply_llm(config: KamaConfig, value: Any) -> None:
    llm = value
    if not isinstance(llm, dict):
        raise SystemExit("Config error: [llm] must be a table")
    unknown_llm: set[str] = set(llm.keys()) - {"default_model", "router"}
    if unknown_llm:
        raise SystemExit(f"Unknown [llm] keys: {', '.join(sorted(unknown_llm))}")
    if "default_model" in llm:
        val = llm["default_model"]
        if not isinstance(val, str):
            raise SystemExit("Config error: llm.default_model must be a string")
        config.llm.default_model = val
    if "router" in llm:
        val = llm["router"]
        if not isinstance(val, str):
            raise SystemExit("Config error: llm.router must be a string")
        config.llm.router = val


def _apply_trace(config: KamaConfig, value: Any) -> None:
    trace = value
    if not isinstance(trace, dict):
        raise SystemExit("Config error: [trace] must be a table")
    unknown_trace: set[str] = set(trace.keys()) - {"enabled", "file", "include_llm_payload"}
    if unknown_trace:
        raise SystemExit(f"Unknown [trace] keys: {', '.join(sorted(unknown_trace))}")
    if "enabled" in trace:
        val = trace["enabled"]
        if not isinstance(val, bool):
            raise SystemExit("Config error: trace.enabled must be a boolean")
        config.trace.enabled = val
    if "file" in trace:
        val = trace["file"]
        if not isinstance(val, str):
            raise SystemExit("Config error: trace.file must be a string")
        config.trace.file = val
    if "include_llm_payload" in trace:
        val = trace["include_llm_payload"]
        if not isinstance(val, bool):
            raise SystemExit("Config error: trace.include_llm_payload must be a boolean")
        config.trace.include_llm_payload = val


def _apply_permission(config: KamaConfig, value: Any) -> None:
    perm = value
    if not isinstance(perm, dict):
        raise SystemExit("Config error: [permission] must be a table")
    unknown_perm: set[str] = set(perm.keys()) - {"timeout_s"}
    if unknown_perm:
        raise SystemExit(f"Unknown [permission] keys: {', '.join(sorted(unknown_perm))}")
    if "timeout_s" in perm:
        val = perm["timeout_s"]
        if not isinstance(val, (int, float)) or val < 0:
            raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
        config.permission.timeout_s = float(val)


def _apply_execution(config: KamaConfig, value: Any) -> None:
    execution = value
    if not isinstance(execution, dict) or set(execution) - {"env", "forward_env"}:
        raise SystemExit("Config error: [execution] accepts only env and forward_env")
    config.execution.env = _env_values(execution.get("env", config.execution.env))
    config.execution.forward_env = _env_names(
        execution.get("forward_env", config.execution.forward_env),
    )


def _apply_compaction(config: KamaConfig, value: Any) -> None:
    comp = value
    if not isinstance(comp, dict):
        raise SystemExit("Config error: [compaction] must be a table")
    positive_keys = {
        "tool_result_limit", "tool_result_keep", "context_window_tokens",
        "output_reserve_tokens", "keep_recent_turns", "recent_token_budget", "summary_max_tokens",
    }
    if {"prune_target_ratio", "keep_recent_groups"} & set(comp):
        raise SystemExit(
            "Config migration: remove prune_target_ratio; replace keep_recent_groups with "
            "keep_recent_turns and recent_token_budget"
        )
    unknown_comp = set(comp) - {"auto_threshold", *positive_keys}
    if unknown_comp:
        raise SystemExit(f"Unknown [compaction] keys: {', '.join(sorted(unknown_comp))}")
    if "auto_threshold" in comp:
        val = comp["auto_threshold"]
        if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
            raise SystemExit("Config error: compaction.auto_threshold must be between 0 and 1")
        config.compaction.auto_threshold = float(val)
    for key in positive_keys:
        if key in comp:
            val = comp[key]
            if type(val) is not int or val <= 0:
                raise SystemExit(f"Config error: compaction.{key} must be a positive integer")
            setattr(config.compaction, key, val)


def _apply_memory(config: KamaConfig, value: Any) -> None:
    memory = value
    keys = {"global_auto_tokens", "project_index_tokens"}
    if not isinstance(memory, dict):
        raise SystemExit("Config error: [memory] must be a table")
    if set(memory) - keys:
        raise SystemExit("Config error: unknown [memory] keys")
    for key, val in memory.items():
        if type(val) is not int or val <= 0:
            raise SystemExit(f"Config error: memory.{key} must be a positive integer")
        setattr(config.memory, key, val)


def _apply_mcp(config: KamaConfig, value: Any) -> None:
    mcp = value
    if not isinstance(mcp, dict):
        raise SystemExit("Config error: [mcp] must be a table")
    unknown_mcp: set[str] = set(mcp.keys()) - {"servers"}
    if unknown_mcp:
        raise SystemExit(f"Unknown [mcp] keys: {', '.join(sorted(unknown_mcp))}")
    servers_raw = mcp.get("servers", [])
    if not isinstance(servers_raw, list):
        raise SystemExit("Config error: mcp.servers must be an array of tables")
    names = {server.name for server in config.mcp.servers}
    for i, srv in enumerate(servers_raw):
        if not isinstance(srv, dict):
            raise SystemExit(f"Config error: mcp.servers[{i}] must be a table")
        name = srv.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise SystemExit(f"Config error: mcp.servers[{i}] requires a unique non-empty name")
        transport = srv.get("transport", "stdio")
        if transport not in ("stdio", "streamable-http"):
            raise SystemExit(
                "Config error: MCP transport must be 'stdio' or 'streamable-http'; "
                "replace old TCP host/port with a Streamable HTTP url"
            )
        allowed = {"name", "transport"}
        allowed |= (
            {"command", "args", "env", "forward_env"}
            if transport == "stdio" else {"url", "headers"}
        )
        if set(srv) - allowed:
            raise SystemExit(f"Config error: unsupported fields for MCP {transport}")
        s = McpServerConfig(name=name, transport=transport)
        if "command" in srv:
            val = srv["command"]
            if not isinstance(val, str):
                raise SystemExit(f"Config error: mcp.servers[{i}].command must be a string")
            s.command = val
        if "args" in srv:
            val = srv["args"]
            if not isinstance(val, list) or any(not isinstance(arg, str) for arg in val):
                raise SystemExit(f"Config error: mcp.servers[{i}].args must be an array")
            s.args = [str(a) for a in val]
        if "env" in srv:
            val = srv["env"]
            if not isinstance(val, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in val.items()
            ):
                raise SystemExit(f"Config error: mcp.servers[{i}].env must be a table")
            s.env = _env_values(val)
        if "forward_env" in srv:
            s.forward_env = _env_names(srv["forward_env"])
        if transport == "stdio" and not s.command.strip():
            raise SystemExit(f"Config error: MCP '{name}' requires command")
        if transport == "streamable-http":
            url = srv.get("url")
            if not isinstance(url, str):
                raise SystemExit(f"Config error: MCP '{name}' requires a Streamable HTTP url")
            try:
                parsed = urlsplit(url)
                valid = (
                    parsed.scheme in ("http", "https") and bool(parsed.hostname)
                    and parsed.username is None and not parsed.fragment
                )
                parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise SystemExit(f"Config error: MCP '{name}' requires a valid http(s) url")
            headers = srv.get("headers", {})
            if not isinstance(headers, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
            ):
                raise SystemExit(
                    f"Config error: MCP '{name}' headers must contain string values"
                )
            s.url, s.headers = url, headers
        names.add(name)
        config.mcp.servers.append(s)


def _env_names(value: Any) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(name, str) or not name or "=" in name or "\0" in name for name in value
    ):
        raise SystemExit("Config error: forward_env must be an array of environment variable names")
    return value



def _env_values(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not name or "=" in name or "\0" in name
        or not isinstance(item, str) or "\0" in item for name, item in value.items()
    ):
        raise SystemExit("Config error: env must contain valid string names and values")
    return value
