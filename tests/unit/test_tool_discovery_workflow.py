from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.config import KamaConfig, McpServerConfig, ToolSearchConfig
from kama_claude.core.discovery.exposure import load_tools, visible_schemas
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.discovery.tool import SearchTool, SearchToolParams
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from tests.unit.discovery_support import ScriptedProvider, mcp_client


# 构造模型的搜索请求，工具返回值仍走项目实际批次保存代码。
def search_response():
    return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
        "search", "search_tool", {
            "description": "Get GitHub issue details", "keywords": ["get_issue", "github"],
        },
    )])


# 构造远程调用，便于检查是否执行以及执行次数。
def remote_response():
    return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
        "remote", "remote__get_issue", {"number": 123},
    )])


# 为完整运行装配真实目录数据库和假 MCP 网络边界。
async def setup_manager(tmp_path, monkeypatch):
    config = KamaConfig(tool_search=ToolSearchConfig(
        db_path=str(tmp_path / "catalog.db"), model_path=str(tmp_path / "missing-model"),
    ))
    manager = McpServerManager(config.tool_search)
    client = mcp_client()
    monkeypatch.setattr(manager, "_connect", AsyncMock(return_value=client))
    await manager.start_all([McpServerConfig("remote", command="python")])
    return config, manager, client


# 功能：初始隐藏 MCP 定义，搜索后下一步才加载，真实调用仍经过审批。
# 设计：固定三次模型请求，分别断言实际 Schema、权限拒绝和 MCP 调用次数。
async def test_search_loads_next_turn_and_does_not_grant_permission(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    provider = ScriptedProvider([search_response(), remote_response(), LlmResponse("end_turn", text="done")])
    permission = PermissionManager(policies={
        "remote__get_issue": ToolPolicy(PermissionDecision.DENY),
    })
    outcome = await AgentRunner(config, provider=provider, mcp_manager=manager,
                               permission_manager=permission, runs_dir=tmp_path / "runs").run_and_capture("查看 Issue")
    assert outcome.status == "success"
    first = {schema["name"] for schema in provider.requests[0]["schemas"]}
    second = {schema["name"] for schema in provider.requests[1]["schemas"]}
    assert "search_tool" in first and "remote__get_issue" not in first
    assert "remote__get_issue" in second and "read_file" in second
    client.call_tool.assert_not_awaited()
    result = provider.requests[1]["messages"][-1]["content"][0]["content"]
    assert "input_schema" not in result and "keyword_only" in result
    await manager.stop_all()


# 功能：同一模型回复里先搜后调用未展示工具，调用不会提前执行。
# 设计：搜索与伪造远程调用共处一个批次，避免只检查执行时最新加载列表。
async def test_same_response_cannot_use_newly_loaded_tool(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    combined = search_response()
    combined.tool_calls.extend(remote_response().tool_calls)
    provider = ScriptedProvider([combined, LlmResponse("end_turn", text="done")])
    await AgentRunner(config, provider=provider, mcp_manager=manager,
                      runs_dir=tmp_path / "runs").run_and_capture("查看 Issue")
    client.call_tool.assert_not_awaited()
    results = provider.requests[1]["messages"][-1]["content"]
    assert len(results) == 2 and results[1]["is_error"]
    assert "next turn" in results[1]["content"]
    await manager.stop_all()


# 功能：搜索成功后重启恢复使用已提交可见列表，不重新搜索或重跑远程工具。
# 设计：第二次模型请求中断，换 Runner 读取磁盘快照续跑并检查 Step 和调用次数。
async def test_loaded_tools_survive_checkpoint_restore(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session(id="sess-discovery", mode="chat", status="active", title="",
                      created_at="now", updated_at="now")
    first = ScriptedProvider([search_response(), RuntimeError("model disconnected")])
    runner = AgentRunner(config, provider=first, mcp_manager=manager)
    outcome = await runner.run_and_capture("查看 Issue", session=session, store=store)
    assert outcome.status == "interrupted"
    cp = store.checkpoints.load(session.id)
    assert cp.run.step == 2 and cp.run.loaded_tool_names == ["remote__get_issue"]
    assert cp.run.user_input == "查看 Issue"
    second = ScriptedProvider([remote_response(), LlmResponse("end_turn", text="fixed")])
    outcome = await AgentRunner(config, provider=second, mcp_manager=manager).run_and_capture(
        "查看 Issue", session=session, store=store, checkpoint=cp,
    )
    assert outcome.status == "success" and second.requests[0]["step"] == 2
    assert "remote__get_issue" in {schema["name"] for schema in second.requests[0]["schemas"]}
    client.call_tool.assert_awaited_once_with("get_issue", {"number": 123})
    assert store.checkpoints.load(session.id).run.step == 3
    await manager.stop_all()


# 功能：搜索列表提交失败后不能继续下一次模型请求或执行 MCP。
# 设计：只在 loaded 列表首次非空时制造真实保存入口错误，覆盖关键提交边界。
async def test_checkpoint_failure_stops_after_search(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session(id="sess-failure", mode="chat", status="active", title="",
                      created_at="now", updated_at="now")
    original = store.checkpoints.save

    # 让初始状态与执行意图正常提交，仅拒绝加载结果的原子快照。
    def fail_loaded(cp):
        if cp.run is not None and cp.run.loaded_tool_names:
            raise CheckpointError("disk full")
        return original(cp)

    monkeypatch.setattr(store.checkpoints, "save", fail_loaded)
    provider = ScriptedProvider([search_response(), remote_response()])
    with pytest.raises(CheckpointError, match="disk full"):
        await AgentRunner(config, provider=provider, mcp_manager=manager).run_and_capture(
            "查看 Issue", session=session, store=store,
        )
    assert len(provider.requests) == 1
    assert store.checkpoints.load(session.id).run.loaded_tool_names == []
    client.call_tool.assert_not_awaited()
    await manager.stop_all()


# 功能：Schema 预算限制完整加载，不截断定义；工具列表变化使 Usage 锚点失效。
# 设计：对同一个 Run 前后两份可见列表做锚点匹配，检查准确失效边界。
async def test_budget_and_usage_anchor_invalidation(tmp_path, monkeypatch):
    config, manager, _ = await setup_manager(tmp_path, monkeypatch)
    runner = AgentRunner(config, mcp_manager=manager, provider=ScriptedProvider([]))
    run = runner._new_run_state("goal", "run-test", None)
    from kama_claude.core.task.manager import TaskManager
    registry = runner._build_registry(TaskManager(tmp_path / "tasks"), run=run)
    runner._save_tool_schemas(run, registry, None, resuming=False)
    old = deepcopy(visible_schemas(run))
    estimator = TokenEstimator()
    messages = [{"role": "user", "content": "goal"}]
    estimator.observe("", messages, old, 1000, output_tokens=10)
    run.max_loaded_schema_tokens = 1
    assert load_tools(run, ["remote__get_issue"]) == ([], ["remote__get_issue"])
    assert estimator.select("", messages, visible_schemas(run)).method == "usage_anchor"
    run.max_loaded_schema_tokens = 8192
    assert load_tools(run, ["remote__get_issue"])[0] == ["remote__get_issue"]
    assert estimator.select("", messages, visible_schemas(run)).method == "bytes"
    assert load_tools(run, ["remote__get_issue"])[0] == ["remote__get_issue"]
    assert run.loaded_tool_names == ["remote__get_issue"]
    await manager.stop_all()


# 功能：显式白名单工具直接展示，搜索入口不会扩大白名单。
# 设计：只允许一个远程工具时不注册搜索；仅允许搜索时也查不到被排除的远程工具。
@pytest.mark.parametrize("whitelist", [["remote__get_issue"], ["search_tool"]])
async def test_explicit_whitelist_is_preserved(tmp_path, monkeypatch, whitelist):
    config, manager, _ = await setup_manager(tmp_path, monkeypatch)
    provider = ScriptedProvider([LlmResponse("end_turn", text="done")])
    await AgentRunner(config, provider=provider, mcp_manager=manager,
                      runs_dir=tmp_path / "runs").run_and_capture("goal", tool_whitelist=whitelist)
    names = {schema["name"] for schema in provider.requests[0]["schemas"]}
    assert names - {"finish"} <= set(whitelist)
    assert "finish" in names
    if "remote__get_issue" in whitelist:
        assert names == {"remote__get_issue", "finish"}
    await manager.stop_all()


# 功能：Skill 展开后的目标不能覆盖用于检索的原始输入。
# 设计：使用 Manager 相同的 prepare_checkpoint 入口保存原文与展开提示。
def test_original_user_input_saved_before_skill_expansion(tmp_path):
    from kama_claude.core.checkpoint.checkpoint import Checkpoint
    config = KamaConfig()
    store = SessionStore(tmp_path / "sessions")
    session = Session(id="sess-skill", mode="chat", status="active", title="",
                      created_at="now", updated_at="now")
    cp = Checkpoint(session=session.to_dict(), messages=[{"role": "user", "content": "/fix 123"}])
    AgentRunner(config, provider=ScriptedProvider([])).prepare_checkpoint(
        cp, session, "expanded skill instructions", "run-skill", "system", None, store,
    )
    assert store.checkpoints.load(session.id).run.user_input == "/fix 123"


# 功能：旧检查点仍使用全量展示，不自动插入搜索工具。
# 设计：加载缺少新增字段的原始数据，断言默认模式与原 Schema 保持一致。
def test_old_checkpoint_keeps_full_schema_mode():
    run = RunCheckpoint(run_id="old", goal="goal", model="m", workspace=".", max_steps=5,
                        tool_schemas=[{"name": "remote__old", "input_schema": {}}])
    assert run.tool_loading_mode == "all"
    assert visible_schemas(run) == run.tool_schemas


# 功能：空白搜索描述不会修改已加载列表。
# 设计：直接调用工具也必须执行参数验证，不依赖外部 invoke_tool 校验。
async def test_whitespace_search_is_known_error(tmp_path, monkeypatch):
    config, manager, _ = await setup_manager(tmp_path, monkeypatch)
    run = AgentRunner(config)._new_run_state("goal", "run-empty", None)
    tool = SearchTool(run, [], manager.retriever)
    result = await tool.invoke({"description": "  ", "keywords": ["issue"]})
    assert result.is_error and result.error_type == "schema_error"
    assert run.loaded_tool_names == []
    await manager.stop_all()


@pytest.mark.parametrize("params", [
    {"description": "issue"},
    {"keywords": ["get_issue"]},
    {"description": "issue", "keywords": []},
    {"description": "issue", "keywords": [" "]},
    {"description": "issue", "keywords": "get_issue"},
    {"description": "issue", "keywords": [123]},
    {"description": "issue", "keywords": ["x" * 101]},
    {"description": "issue", "keywords": ["github", "get_issue", "comments", "number"]},
])
async def test_invalid_search_parameters_do_not_load_tools(tmp_path, params):
    from kama_claude.core.llm.types import ToolCallBlock
    from kama_claude.core.tools.invocation import invoke_tool
    from kama_claude.core.tools.registry import ToolRegistry

    run = RunCheckpoint(run_id="r", goal="g", model="fake", workspace=str(tmp_path), max_steps=0)
    retriever = AsyncMock(spec=ToolRetriever)
    registry = ToolRegistry()
    registry.register(SearchTool(run, [], retriever))
    result = await invoke_tool(registry, ToolCallBlock("search", "search_tool", params), EventBus(), "r")
    assert result.is_error and result.error_type == "schema_error"
    assert run.loaded_tool_names == []
    retriever.search.assert_not_awaited()


def test_search_keywords_strip_surrounding_whitespace():
    args = SearchToolParams(description="read issue", keywords=[" github ", "get_issue\n", "comments"])
    assert args.keywords == ["github", "get_issue", "comments"]


# 功能：恢复时即使模型 Schema 不变，完整 MCP 定义变化也必须明确拒绝。
# 设计：只修改不进入 tools 数组的输出 Schema，验证不只比较参数列表。
async def test_restore_rejects_full_definition_change(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-changed", "chat", "active", "", "now", "now")
    first = ScriptedProvider([search_response(), RuntimeError("interrupted")])
    await AgentRunner(config, provider=first, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store,
    )
    cp = store.checkpoints.load(session.id)
    definition = client.list_tools.return_value[0]
    definition.raw_definition = {**definition.definition(), "outputSchema": {"type": "string"}}
    await manager.start_all([McpServerConfig("remote", command="python")])
    resumed = ScriptedProvider([LlmResponse("end_turn", text="should not run")])
    outcome = await AgentRunner(config, provider=resumed, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store, checkpoint=cp,
    )
    assert outcome.status == "interrupted" and "definitions" in outcome.reason
    assert resumed.requests == []
    await manager.stop_all()


# 功能：手动压缩与自动请求使用相同可见 Schema，不把完整目录重新计入预算。
# 设计：拦截 Compactor 的公开入口，直接检查实际传入的工具列表。
async def test_manual_compaction_uses_visible_schemas(tmp_path, monkeypatch):
    from kama_claude.core.checkpoint.checkpoint import Checkpoint
    from kama_claude.core.compact.compactor import CompactionResult, Compactor
    from kama_claude.core.compact.manager import compact_session

    config = KamaConfig()
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-compact", "chat", "waiting_for_input", "", "now", "now")
    run = RunCheckpoint(run_id="run-compact", goal="goal", model="m", workspace=str(tmp_path),
                        max_steps=10, tool_loading_mode="on_demand", schemas_frozen=True,
                        tool_schemas=[{"name": "read_file"}, {"name": "remote__hidden"}],
                        mcp_tool_names=["remote__hidden"])
    messages = [{"role": "user", "content": "goal"}]
    store.save_checkpoint(Checkpoint(session=session.to_dict(), run=run, messages=messages))
    compact = AsyncMock(return_value=CompactionResult(messages, 1, 1, reason="below_threshold"))
    monkeypatch.setattr(Compactor, "prepare_context", compact)
    await compact_session(store, session.id, EventBus(), config.compaction,
                          lambda: ScriptedProvider([]), "")
    assert compact.call_args.kwargs["tool_schemas"] == [{"name": "read_file"}]


# 功能：关闭按需加载仍保留原来的全量展示方式，不插入搜索入口。
# 设计：使用真实 Runner 请求捕获，验证配置开关不只改变数据库行为。
async def test_disabled_search_keeps_all_tools(tmp_path, monkeypatch):
    config, manager, _ = await setup_manager(tmp_path, monkeypatch)
    config.tool_search.enabled = False
    model = ScriptedProvider([LlmResponse("end_turn", text="done")])
    await AgentRunner(config, provider=model, mcp_manager=manager,
                      runs_dir=tmp_path / "runs").run_and_capture("goal")
    names = {schema["name"] for schema in model.requests[0]["schemas"]}
    assert "remote__get_issue" in names and "search_tool" not in names
    await manager.stop_all()


# 功能：单工具等待上限可覆盖默认值，调用者显式传入的上限仍优先。
# 设计：用短时间测试替代等待真实下载，确保长模型初始化不受硬编码 120 秒限制。
async def test_tool_timeout_override_and_explicit_priority():
    from kama_claude.core.tools.base import BaseTool, ToolResult
    from kama_claude.core.tools.invocation import invoke_tool
    from kama_claude.core.tools.registry import ToolRegistry

    class SlowTool(BaseTool):
        name = "slow"
        description = "slow test tool"
        input_schema = {}
        timeout_s = 0.01

        # 等待略长于工具默认上限，便于验证调用者覆盖。
        async def invoke(self, params):
            await asyncio.sleep(0.03)
            return ToolResult("done")

    registry = ToolRegistry()
    registry.register(SlowTool())
    failed = await invoke_tool(registry, ToolCallBlock("one", "slow", {}), EventBus(), "r")
    assert failed.is_error
    success = await invoke_tool(registry, ToolCallBlock("two", "slow", {}), EventBus(), "r",
                                timeout=1)
    assert not success.is_error
    assert SearchTool.timeout_s == 600
