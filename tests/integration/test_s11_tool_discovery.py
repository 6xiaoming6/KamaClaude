from __future__ import annotations

import pytest

from kama_claude.core.config import KamaConfig, McpServerConfig, ToolSearchConfig
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.runner import AgentRunner
from tests.integration.test_mcp_streamable_http import HEADERS
from tests.integration.test_mcp_streamable_http import mcp_endpoint as mcp_endpoint
from tests.unit.discovery_support import NativeScriptedProvider, ScriptedProvider


# 功能：实际 Streamable HTTP 服务经过搜索、加载后才被 Agent 调用。
# 设计：JSON/SSE 两种真实传输复用官方服务；仅替换收费模型。
@pytest.mark.parametrize("native", [False, True])
async def test_search_then_call_real_mcp(mcp_endpoint, tmp_path, native):
    config = KamaConfig(tool_search=ToolSearchConfig(
        db_path=str(tmp_path / "catalog.db"), model_path=str(tmp_path / "missing-model"),
    ))
    manager = McpServerManager(config.tool_search)
    await manager.start_all([McpServerConfig(
        "remote", "streamable-http", url=mcp_endpoint, headers=HEADERS,
    )])
    try:
        provider_type = NativeScriptedProvider if native else ScriptedProvider
        provider = provider_type([
            LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                "search", "search_tool", {"description": "add two numbers", "keywords": ["add"]},
            )]),
            LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                "add", "remote__add", {"a": 10, "b": 20},
            )]),
            LlmResponse("end_turn", text="30"),
        ])
        outcome = await AgentRunner(config, provider=provider, mcp_manager=manager,
                                   runs_dir=tmp_path / "runs").run_and_capture("Add 10 and 20")
        assert outcome.status == "success" and outcome.result == "30"
        if native:
            assert provider.requests[0]["schemas"] == provider.requests[1]["schemas"]
            assert all(schema["defer_loading"] == schema["name"].startswith("remote__")
                       for schema in provider.requests[0]["schemas"])
            content = provider.requests[1]["messages"][-1]["content"][0]["content"]
            assert any(block.get("type") == "tool_reference" for block in content)
        else:
            assert all(not schema["name"].startswith("remote__")
                       for schema in provider.requests[0]["schemas"])
        assert "remote__add" in {schema["name"] for schema in provider.requests[1]["schemas"]}
        assert provider.requests[2]["messages"][-1]["content"][0]["content"] == "30"
        first = manager.get_tools()[0].record
        assert manager.catalog.current_hash(first.server_id, first.name) == first.definition_hash
    finally:
        await manager.stop_all()
