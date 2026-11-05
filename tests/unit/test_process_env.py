from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.config import ExecutionConfig, KamaConfig, _apply_toml
from kama_claude.core.mcp.client import McpClient
from kama_claude.core.process_env import build_process_env
from kama_claude.core.tools.command import CommandExecutor
from tests.unit.test_command_executor import python_command


def test_minimum_environment_and_explicit_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
                 "S10_BUILD_FLAG", "PYTHONPATH", "NODE_OPTIONS"):
        monkeypatch.setenv(name, "fake-value-for-test")
    child = build_process_env()
    assert not any(name in child for name in (
        "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
        "S10_BUILD_FLAG", "PYTHONPATH", "NODE_OPTIONS",
    ))
    child = build_process_env(forward_env=["S10_BUILD_FLAG"], env={"S10_BUILD_FLAG": "explicit"})
    assert child["S10_BUILD_FLAG"] == "explicit"
    assert "OPENAI_API_KEY" not in child


async def test_real_command_environment_is_filtered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "s10-fake-key")
    monkeypatch.setenv("S10_BUILD_FLAG", "configured")
    code = (
        "import os,json; print(json.dumps({'key_present':'OPENAI_API_KEY' in os.environ,"
        "'flag':os.getenv('S10_BUILD_FLAG')}))"
    )
    command = python_command(code)
    normal = await CommandExecutor(tmp_path).execute(command, 10)
    assert normal.returncode == 0
    assert json.loads(normal.output) == {"key_present": False, "flag": None}
    explicit = await CommandExecutor(
        tmp_path, ExecutionConfig(forward_env=["S10_BUILD_FLAG"]),
    ).execute(command, 10)
    assert json.loads(explicit.output) == {"key_present": False, "flag": "configured"}


async def test_mcp_uses_same_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "s10-fake-key")
    monkeypatch.setenv("S10_MCP_FLAG", "configured")
    client = McpClient()
    connect = AsyncMock()
    monkeypatch.setattr(client, "_connect", connect)
    await client.connect_stdio("python", [], {"MODE": "test"}, forward_env=["S10_MCP_FLAG"])
    parameters = connect.await_args.args[0]
    assert "ANTHROPIC_API_KEY" not in parameters.env
    assert parameters.env["MODE"] == "test"
    assert parameters.env["S10_MCP_FLAG"] == "configured"


def test_execution_and_per_server_configuration() -> None:
    config = KamaConfig()
    _apply_toml(config, {
        "execution": {"forward_env": ["BUILD_FLAG"], "env": {"MODE": "test"}},
        "mcp": {"servers": [{"name": "local", "command": "python", "forward_env": ["MCP_KEY"]}]},
    })
    assert config.execution.forward_env == ["BUILD_FLAG"]
    assert config.execution.env == {"MODE": "test"}
    assert config.mcp.servers[0].forward_env == ["MCP_KEY"]


@pytest.mark.parametrize("settings", [
    {"forward_env": "ALL"}, {"forward_env": [42]}, {"forward_env": ["A=B"]},
    {"env": {"A": 1}}, {"env": {"": "x"}}, {"env": {"A": "x\0y"}}, {"inherit_all": True},
])
def test_invalid_environment_configuration(settings: dict[str, Any]) -> None:
    with pytest.raises(SystemExit):
        _apply_toml(KamaConfig(), {"execution": settings})


async def test_main_and_nested_agents_share_environment_config(tmp_path: Path) -> None:
    from kama_claude.core.runner import AgentRunner
    from kama_claude.core.task.manager import TaskManager
    from tests.unit.test_subagent_runtime import spawn_tool

    config = KamaConfig(execution=ExecutionConfig(forward_env=["BUILD_FLAG"]))
    runner = AgentRunner(config)
    registry = runner._build_registry(TaskManager(tmp_path / "tasks"), workspace=tmp_path)
    assert registry.get("bash")._execution is config.execution
    parent = spawn_tool(tmp_path, execution=config.execution)
    child_tools = parent._build_child_registry(parent._parent_bus, "child", None)
    assert child_tools.get("bash")._execution is config.execution
    nested = child_tools.get("spawn_agent")
    nested_tools = nested._build_child_registry(parent._parent_bus, "nested", None)
    assert nested_tools.get("bash")._execution is config.execution
