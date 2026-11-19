from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.estimator import TokenEstimator
from kama_claude.core.config import KamaConfig, McpServerConfig, ToolSearchConfig
from kama_claude.core.discovery.exposure import load_tools, visible_schemas
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.discovery.tool import (
    GetToolSchema,
    SearchToolCandidates,
    SearchToolCandidatesParams,
)
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
        "search", "search_tool_candidates", {
            "description": "Get GitHub issue details", "keywords": ["get_issue", "github"],
        },
    )])


# 选择候选中的单个工具，下一次模型请求才获得定义。
def schema_response():
    return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
        "schema", "get_tool_schema", {"names": ["remote__get_issue"]},
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


# 功能：搜索只返回候选，确认加载后的下一步才展示定义，真实调用仍经过审批。
# 设计：固定四次模型请求，分别断言候选、Schema 可见性、权限拒绝和 MCP 调用次数。
async def test_search_loads_next_turn_and_does_not_grant_permission(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    provider = ScriptedProvider([search_response(), schema_response(), remote_response(), LlmResponse("end_turn", text="done")])
    permission = PermissionManager(policies={
        "remote__get_issue": ToolPolicy(PermissionDecision.DENY),
    })
    outcome = await AgentRunner(config, provider=provider, mcp_manager=manager,
                               permission_manager=permission, runs_dir=tmp_path / "runs").run_and_capture("查看 Issue")
    assert outcome.status == "success"
    first = {schema["name"] for schema in provider.requests[0]["schemas"]}
    second = {schema["name"] for schema in provider.requests[2]["schemas"]}
    assert "search_tool_candidates" in first and "remote__get_issue" not in first
    assert "get_tool_schema" in first and "search_tool" not in first
    assert "remote__get_issue" not in {s["name"] for s in provider.requests[1]["schemas"]}
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


# 功能：确认加载后重启恢复使用已提交可见列表，不重新搜索或重跑远程工具。
# 设计：第三次模型请求中断，换 Runner 读取磁盘快照续跑并检查 Step 和调用次数。
async def test_loaded_tools_survive_checkpoint_restore(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session(id="sess-discovery", mode="chat", status="active", title="",
                      created_at="now", updated_at="now")
    first = ScriptedProvider([search_response(), schema_response(), RuntimeError("model disconnected")])
    runner = AgentRunner(config, provider=first, mcp_manager=manager)
    outcome = await runner.run_and_capture("查看 Issue", session=session, store=store)
    assert outcome.status == "interrupted"
    cp = store.checkpoints.load(session.id)
    assert cp.run.step == 3 and cp.run.loaded_tool_names == ["remote__get_issue"]
    assert cp.run.user_input == "查看 Issue"
    second = ScriptedProvider([remote_response(), LlmResponse("end_turn", text="fixed")])
    outcome = await AgentRunner(config, provider=second, mcp_manager=manager).run_and_capture(
        "查看 Issue", session=session, store=store, checkpoint=cp,
    )
    assert outcome.status == "success" and second.requests[0]["step"] == 3
    assert "remote__get_issue" in {schema["name"] for schema in second.requests[0]["schemas"]}
    client.call_tool.assert_awaited_once_with("get_issue", {"number": 123})
    assert store.checkpoints.load(session.id).run.step == 4
    await manager.stop_all()


# 功能：已加载列表提交失败后不能继续下一次模型请求或执行 MCP。
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
    provider = ScriptedProvider([search_response(), schema_response(), remote_response()])
    with pytest.raises(CheckpointError, match="disk full"):
        await AgentRunner(config, provider=provider, mcp_manager=manager).run_and_capture(
            "查看 Issue", session=session, store=store,
        )
    assert len(provider.requests) == 2
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
@pytest.mark.parametrize("whitelist", [["remote__get_issue"], ["search_tool_candidates"]])
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
    tool = SearchToolCandidates(run, [], manager.retriever)
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
    *({"description": "issue", "keywords": ["issue"], "topk": value}
      for value in (0, -1, 11, True, 1.5, "5", None)),
])
# 功能：非法搜索参数不执行检索或改变加载状态。
# 设计：通过公共调用入口校验，覆盖缺失、类型、空值和数量上限。
async def test_invalid_search_parameters_do_not_load_tools(tmp_path, params):
    from kama_claude.core.llm.types import ToolCallBlock
    from kama_claude.core.tools.invocation import invoke_tool
    from kama_claude.core.tools.registry import ToolRegistry

    run = RunCheckpoint(run_id="r", goal="g", model="fake", workspace=str(tmp_path), max_steps=0)
    retriever = AsyncMock(spec=ToolRetriever)
    retriever.config = ToolSearchConfig()
    registry = ToolRegistry()
    registry.register(SearchToolCandidates(run, [], retriever))
    result = await invoke_tool(registry, ToolCallBlock("search", "search_tool_candidates", params), EventBus(), "r")
    assert result.is_error and result.error_type == "schema_error"
    assert run.loaded_tool_names == []
    retriever.search.assert_not_awaited()


# 功能：关键词去掉两端空白后用于检索。
# 设计：直接检查参数模型，避免检索排序掩盖参数规范化行为。
def test_search_keywords_strip_surrounding_whitespace():
    args = SearchToolCandidatesParams(description="read issue", keywords=[" github ", "get_issue\n", "comments"])
    assert args.keywords == ["github", "get_issue", "comments"]


# 功能：恢复时即使模型 Schema 不变，完整 MCP 定义变化也必须明确拒绝。
# 设计：只修改不进入 tools 数组的输出 Schema，验证不只比较参数列表。
async def test_restore_rejects_full_definition_change(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-changed", "chat", "active", "", "now", "now")
    first = ScriptedProvider([search_response(), schema_response(), RuntimeError("interrupted")])
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
    assert "remote__get_issue" in names and "search_tool_candidates" not in names
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
    assert SearchToolCandidates.timeout_s == 600


# 功能：搜索不加载定义，支持批量选择并仅返回未激活工具的引用。
# 设计：提供三个大 Schema 与精确排序替身，直接核对元数据、额度和协议块。
@pytest.mark.parametrize("mode", ["deferred", "on_demand"])
async def test_candidates_only_load_selected_schema(tmp_path, mode):
    from kama_claude.core.discovery.retrieval import SearchResult
    from tests.unit.discovery_support import record

    records = [record(name, "Useful capability " * 500) for name in ("first", "second", "third")]
    names = [item.public_name for item in records]
    run = RunCheckpoint(
        run_id="r", goal="g", model="m", workspace=str(tmp_path), max_steps=0, step=1,
        tool_loading_mode=mode, mcp_tool_names=names, tool_schemas=[r.schema for r in records],
        schemas_frozen=True, max_loaded_schema_tokens=20_000,
    )
    retriever = AsyncMock(spec=ToolRetriever)
    retriever.config = ToolSearchConfig()
    retriever.search.return_value = SearchResult([(name, 1.0) for name in names], "hybrid")
    result = await SearchToolCandidates(run, records, retriever).invoke({
        "description": "capability", "keywords": ["first"],
    })
    payload = json.loads(result.content)
    assert [item["name"] for item in payload["candidates"]] == names
    assert all(set(item) == {"name", "server", "description", "loaded"}
               and len(item["description"]) <= 240 for item in payload["candidates"])
    assert not result.tool_references and isinstance(result.message_content(), str)
    assert "input_schema" not in result.content and run.loaded_tool_names == []
    assert run.tool_candidates == dict.fromkeys(names, 1)
    # 即使搜索和确认混在同一模型回复中，也不能提前加载猜中的候选。
    premature = await GetToolSchema(run).invoke({"names": [names[1]]})
    assert premature.is_error and not run.loaded_tool_names
    run.step = 2
    selected = await GetToolSchema(run).invoke({"names": [names[1]]})
    assert not selected.is_error and run.loaded_tool_names == [names[1]]
    assert selected.tool_references == ([names[1]] if mode == "deferred" else [])
    assert json.loads(selected.content)["loaded"] == [names[1]]
    repeated = await GetToolSchema(run).invoke({"names": [names[1]]})
    assert not repeated.tool_references and json.loads(repeated.content)["already_loaded"] == [names[1]]
    assert run.loaded_tool_names == [names[1]]  # 重复加载幂等，不重复占用预算。
    batch = await GetToolSchema(run).invoke({"names": [names[1], names[0], names[1], names[2]]})
    assert json.loads(batch.content)["loaded"] == [names[0], names[2]]
    assert json.loads(batch.content)["already_loaded"] == [names[1]]
    assert batch.tool_references == ([names[0], names[2]] if mode == "deferred" else [])
    assert run.loaded_tool_names == [names[1], names[0], names[2]]


# 功能：未搜索、白名单外和额度不足的工具不能产生引用。
# 设计：分别覆盖三个拒绝边界，确认已加载列表保持不变。
async def test_schema_requires_candidate_and_respects_budget(tmp_path):
    run = RunCheckpoint(
        run_id="r", goal="g", model="m", workspace=str(tmp_path), max_steps=0, step=2,
        tool_loading_mode="deferred", mcp_tool_names=["remote__allowed"],
        tool_schemas=[{"name": "remote__allowed", "input_schema": {}}],
        max_loaded_schema_tokens=1,
    )
    tool = GetToolSchema(run)
    missing = await tool.invoke({"names": ["remote__allowed"]})
    assert missing.is_error and missing.error_type == "invalid_operation"
    outside = await tool.invoke({"names": ["other__tool"]})
    assert outside.is_error and outside.error_type == "not_found"
    run.tool_candidates["remote__allowed"] = 1
    over_budget = await tool.invoke({"names": ["remote__allowed"]})
    assert json.loads(over_budget.content)["not_loaded_budget"] == ["remote__allowed"]
    assert not over_budget.tool_references and run.loaded_tool_names == []


# 功能：仅搜索后中断，恢复仍可确认候选，不必重新检索。
# 设计：从磁盘重新构建 Runner，检查候选 Step、加载列表及真实调用次数。
@pytest.mark.parametrize("native", [False, True])
async def test_candidate_selection_survives_restart(tmp_path, monkeypatch, native):
    from tests.unit.discovery_support import NativeScriptedProvider

    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("candidate-resume", "chat", "active", "", "now", "now")
    provider_type = NativeScriptedProvider if native else ScriptedProvider
    first = provider_type([search_response(), RuntimeError("disconnect after search")])
    await AgentRunner(config, provider=first, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store,
    )
    cp = store.checkpoints.load(session.id)
    assert cp.run.tool_candidates == {"remote__get_issue": 1}
    assert cp.run.loaded_tool_names == [] and cp.run.step == 2
    second = provider_type([schema_response(), remote_response(), LlmResponse("end_turn", text="done")])
    outcome = await AgentRunner(config, provider=second, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store, checkpoint=cp,
    )
    assert outcome.status == "success" and second.requests[0]["step"] == 2
    client.call_tool.assert_awaited_once()
    await manager.stop_all()


# 功能：候选保存失败后不能确认加载或继续模型请求。
# 设计：在候选首次写入时失败，与已有的加载列表保存失败测试覆盖不同边界。
async def test_candidate_checkpoint_failure_stops_request(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("candidate-failure", "chat", "active", "", "now", "now")
    save = store.checkpoints.save

    # 初始状态与执行意图可提交，候选与工具结果一起提交时模拟磁盘错误。
    def fail(cp):
        if cp.run and cp.run.tool_candidates:
            raise CheckpointError("candidate disk full")
        return save(cp)

    monkeypatch.setattr(store.checkpoints, "save", fail)
    provider = ScriptedProvider([search_response(), schema_response()])
    with pytest.raises(CheckpointError, match="candidate disk full"):
        await AgentRunner(config, provider=provider, mcp_manager=manager).run_and_capture(
            "goal", session=session, store=store,
        )
    assert len(provider.requests) == 1
    assert not store.checkpoints.load(session.id).run.tool_candidates
    client.call_tool.assert_not_awaited()
    await manager.stop_all()


# 功能：两个发现工具都默认允许，但 MCP 业务操作继续独立审批。
# 设计：注入真实权限管理器并拒绝远程工具，检查没有多余发现审批或绕过。
async def test_discovery_permissions_do_not_authorize_remote(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    bus = EventBus()
    requested = []

    # 如果发现工具误入审批，立即拒绝而非等待超时。
    async def reject(event):
        if event.type == "permission.requested":
            requested.append(event.tool_name)
            permissions.respond(event.request_id, "deny_once", session_id=event.session_id,
                                run_id=event.run_id)

    bus.subscribe(reject)
    permissions = PermissionManager()
    provider = ScriptedProvider([
        search_response(), schema_response(), remote_response(), LlmResponse("end_turn", text="done"),
    ])
    await AgentRunner(config, provider=provider, bus=bus, permission_manager=permissions,
                      mcp_manager=manager, runs_dir=tmp_path / "runs").run_and_capture("goal")
    assert requested == ["remote__get_issue"]
    client.call_tool.assert_not_awaited()
    await manager.stop_all()


# 功能：搜索无结果不加载、不产生引用，额度不足也不妨碍候选检索。
# 设计：用空检索结果和最小 Schema 预算，明确搜索与加载预算各自的职责。
async def test_empty_candidates_are_success_without_loading(tmp_path):
    from kama_claude.core.discovery.retrieval import SearchResult

    run = RunCheckpoint(run_id="r", goal="g", model="m", workspace=str(tmp_path), max_steps=0,
                        max_loaded_schema_tokens=1)
    retriever = AsyncMock(spec=ToolRetriever)
    retriever.config = ToolSearchConfig()
    retriever.search.return_value = SearchResult([], "keyword_only", "model unavailable")
    result = await SearchToolCandidates(run, [], retriever).invoke({
        "description": "missing capability", "keywords": ["missing"],
    })
    assert not result.is_error and json.loads(result.content)["candidates"] == []
    assert "model unavailable" in result.content
    assert not run.tool_candidates and not run.loaded_tool_names and not result.tool_references


# 功能：获取 Schema 的参数只接受有效名称数组，不能传入内部授权或额外字段。
# 设计：经统一调用入口验证空值、类型、长度和伪造参数，不触发加载。
@pytest.mark.parametrize("params", [
    {}, {"names": [" "]}, {"names": []}, {"names": [123]}, {"names": ["x" * 513]},
    {"names": ["remote__tool"], "approved": True}, {"names": "remote__tool"},
    {"name": "remote__tool"}, {"names": ["remote__tool"] * 11},
])
async def test_invalid_get_schema_parameters_do_not_load(tmp_path, params):
    from kama_claude.core.tools.invocation import invoke_tool
    from kama_claude.core.tools.registry import ToolRegistry

    run = RunCheckpoint(run_id="r", goal="g", model="m", workspace=str(tmp_path), max_steps=0)
    registry = ToolRegistry()
    registry.register(GetToolSchema(run))
    result = await invoke_tool(
        registry, ToolCallBlock("schema", "get_tool_schema", params), EventBus(), "r",
    )
    assert result.is_error and result.error_type == "schema_error"
    assert not run.loaded_tool_names


# 功能：损坏检查点中的白名单外候选和负数步骤被拒绝。
# 设计：直接验证磁盘模型，不能依靠运行时搜索覆盖不可信旧数据。
@pytest.mark.parametrize("candidates", [{"remote__outside": 1}, {"remote__allowed": -1}])
def test_checkpoint_rejects_invalid_candidates(tmp_path, candidates):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RunCheckpoint(
            run_id="r", goal="g", model="m", workspace=str(tmp_path), max_steps=0,
            schemas_frozen=True, tool_loading_mode="deferred",
            mcp_tool_names=["remote__allowed"], tool_schemas=[{"name": "remote__allowed"}],
            tool_candidates=candidates,
        )


# 功能：同一模型回复不能先搜索再猜测加载并直接执行业务工具。
# 设计：构造混合批次，检查加载已拒绝且后续模型请求无引用或可见定义。
@pytest.mark.parametrize("native", [False, True])
async def test_same_batch_cannot_skip_candidate_selection(tmp_path, monkeypatch, native):
    from tests.unit.discovery_support import NativeScriptedProvider

    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    combined = search_response()
    combined.tool_calls += [*schema_response().tool_calls, *remote_response().tool_calls]
    provider_type = NativeScriptedProvider if native else ScriptedProvider
    provider = provider_type([combined, LlmResponse("end_turn", text="done")])
    store = SessionStore(tmp_path / "sessions")
    session = Session("same-batch", "chat", "active", "", "now", "now")
    await AgentRunner(config, provider=provider, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store,
    )
    results = provider.requests[1]["messages"][-1]["content"]
    assert len(results) == 3 and results[1]["is_error"] and results[2]["is_error"]
    assert not store.checkpoints.load(session.id).run.loaded_tool_names
    client.call_tool.assert_not_awaited()
    await manager.stop_all()


# 功能：冻结旧搜索工具定义的中断 Run 明确拒绝续跑，不静默改变工具协议。
# 设计：在旧快照中还原已删除名称，验证请求模型之前的兼容校验仍生效。
async def test_old_search_tool_schema_requires_new_run(tmp_path, monkeypatch):
    config, manager, _ = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("old-search", "chat", "active", "", "now", "now")
    first = ScriptedProvider([RuntimeError("disconnect")])
    await AgentRunner(config, provider=first, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store,
    )
    cp = store.checkpoints.load(session.id)
    cp.run.tool_schemas = [
        {**schema, "name": "search_tool"} if schema["name"] == "search_tool_candidates" else schema
        for schema in cp.run.tool_schemas if schema["name"] != "get_tool_schema"
    ]
    resumed = ScriptedProvider([LlmResponse("end_turn", text="must not run")])
    result = await AgentRunner(config, provider=resumed, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store, checkpoint=cp,
    )
    assert result.status == "interrupted" and "schemas changed" in result.reason
    assert not resumed.requests
    await manager.stop_all()


# 功能：批量中有非法候选时不进行部分加载；预算不足则明确区分已加载与未加载。
# 设计：先混入白名单外名称，再精确配置一份定义的额度，核对返回引用与激活状态。
async def test_batch_schema_validation_and_partial_budget(tmp_path):
    from kama_claude.core.compact.estimator import estimate_bytes

    schemas = [{"name": name, "description": "detail " * 100, "input_schema": {}}
               for name in ("remote__a", "remote__b")]
    run = RunCheckpoint(
        run_id="r", goal="g", model="m", workspace=str(tmp_path), max_steps=0, step=2,
        tool_loading_mode="deferred", mcp_tool_names=[s["name"] for s in schemas],
        tool_schemas=schemas, tool_candidates={"remote__a": 1, "remote__b": 1},
        max_loaded_schema_tokens=estimate_bytes(schemas[:1]),
    )
    invalid = await GetToolSchema(run).invoke({"names": ["remote__a", "outside__tool"]})
    assert invalid.is_error and not run.loaded_tool_names
    run.tool_candidates.pop("remote__b")
    unsearched = await GetToolSchema(run).invoke({"names": ["remote__a", "remote__b"]})
    assert unsearched.is_error and not run.loaded_tool_names
    run.tool_candidates["remote__b"] = 1
    result = await GetToolSchema(run).invoke({"names": ["remote__a", "remote__b"]})
    assert result.tool_references == ["remote__a"]
    assert json.loads(result.content)["not_loaded_budget"] == ["remote__b"]
    assert run.loaded_tool_names == ["remote__a"]


# 功能：默认候选少量返回，模型可增大 topk 扩展到十个，仍不加载定义或越过白名单。
# 设计：使用真实 BM25、RRF 与 SQLite，只替换向量边界，同时覆盖语义不可用的降级路径。
@pytest.mark.parametrize("default_topk", [2, 3])
@pytest.mark.parametrize("degraded", [False, True])
async def test_search_topk_expands_candidate_scope(tmp_path, monkeypatch, default_topk, degraded):
    from kama_claude.core.discovery.store import CatalogStore
    from tests.unit.discovery_support import FakeEmbedder, record

    records = [record(f"issue_{i:02}") for i in range(12)]
    hidden = record("hidden_issue")
    config = ToolSearchConfig(top_k=default_topk)
    embedder = FakeEmbedder()
    if degraded:
        monkeypatch.setattr(embedder, "query", lambda *args: (_ for _ in ()).throw(
            ValueError("model unavailable"),
        ))
    retriever = ToolRetriever(CatalogStore(tmp_path / "catalog.db"), config, embedder)
    run = RunCheckpoint(
        run_id="r", goal="issue", model="m", workspace=str(tmp_path), max_steps=0, step=1,
        mcp_tool_names=[r.public_name for r in records],
    )
    tool = SearchToolCandidates(run, [*records, hidden], retriever)
    params = {"description": "GitHub issue details", "keywords": ["issue"]}
    first = await tool.invoke(params)
    first_names = [c["name"] for c in json.loads(first.content)["candidates"]]
    assert len(first_names) == default_topk
    assert tool.input_schema["properties"]["topk"]["default"] == default_topk
    assert "topk" not in tool.input_schema["required"]
    run.step = 2
    expanded = await tool.invoke({**params, "topk": 10})
    payload = json.loads(expanded.content)
    expanded_names = [c["name"] for c in payload["candidates"]]
    assert len(expanded_names) == 10 and expanded_names[:default_topk] == first_names
    assert payload["method"] == ("keyword_only" if degraded else "hybrid")
    assert hidden.public_name not in run.tool_candidates
    assert all(run.tool_candidates[name] == 1 for name in first_names)
    assert all(run.tool_candidates[name] == 2 for name in expanded_names[default_topk:])
    assert not expanded.tool_references and not run.loaded_tool_names
    narrowed = await tool.invoke({**params, "topk": 1})
    assert len(json.loads(narrowed.content)["candidates"]) == 1
    # 单次扩大范围不改变默认配置，下一次省略 topk 仍返回原数量。
    again = await tool.invoke(params)
    assert len(json.loads(again.content)["candidates"]) == default_topk
