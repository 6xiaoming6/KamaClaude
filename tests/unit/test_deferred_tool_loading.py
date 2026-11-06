from __future__ import annotations

import json
from copy import deepcopy

import anthropic
import httpx2
import pytest

from kama_claude.core.checkpoint.checkpoint import Checkpoint, CheckpointError, RunCheckpoint
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.estimator import TokenCounter, TokenEstimator, estimate_bytes
from kama_claude.core.compact.history import validate_pairs as _validate_pairs
from kama_claude.core.config import CompactionConfig
from kama_claude.core.discovery.exposure import callable_names, load_tools, request_schemas
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.provider import AnthropicProvider, DeepseekProvider, OpenaiProvider
from kama_claude.core.llm.tool_loading import (
    preserve_references,
    project_references,
    referenced_tools,
)
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import ToolResult
from tests.unit.discovery_support import NativeScriptedProvider, ScriptedProvider
from tests.unit.test_tool_discovery_workflow import (
    remote_response,
    search_response,
    setup_manager,
)


def run_state(**kwargs):
    return RunCheckpoint(run_id="r", goal="goal", workspace=".", model="m", max_steps=10,
                         tool_loading_mode="deferred", schemas_frozen=True,
                         tool_schemas=[{"name": "search_tool", "input_schema": {}},
                                       {"name": "remote__get_issue", "description": "details" * 500,
                                        "input_schema": {"type": "object"}}],
                         mcp_tool_names=["remote__get_issue"], **kwargs)


def discovery_history():
    return [{"role": "user", "content": "get issue"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "s",
                                               "name": "search_tool", "input": {"description": "issue"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s",
                                          "content": ToolResult("found", tool_references=[
                                              "remote__get_issue",
                                          ]).message_content()}]}]


# 完整 tools 在搜索前后保持一致；引用是协议块，普通工具也继续常驻。
async def test_native_search_keeps_catalog_stable(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    provider = NativeScriptedProvider([search_response(), remote_response(), LlmResponse("end_turn", text="done")])
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-native", "chat", "active", "", "now", "now")
    outcome = await AgentRunner(config, provider=provider, mcp_manager=manager).run_and_capture(
        "查看 Issue", session=session, store=store,
    )
    assert outcome.status == "success"
    schemas = provider.requests[0]["schemas"]
    assert all(request["schemas"] == schemas for request in provider.requests)
    assert next(s for s in schemas if s["name"] == "remote__get_issue")["defer_loading"] is True
    assert all(not s["defer_loading"] for s in schemas if s["name"] != "remote__get_issue")
    content = provider.requests[1]["messages"][-1]["content"][0]["content"]
    assert content[-1] == {"type": "tool_reference", "tool_name": "remote__get_issue"}
    assert "keyword_only" in content[0]["text"]
    cp = store.checkpoints.load(session.id)
    assert cp.run.tool_loading_mode == "deferred"
    assert cp.run.loaded_tool_names == ["remote__get_issue"]
    assert referenced_tools(cp.messages) == ["remote__get_issue"]
    assert referenced_tools(store.read_history(session.id)) == ["remote__get_issue"]
    client.call_tool.assert_awaited_once_with("get_issue", {"number": 123})
    await manager.stop_all()


# 收到搜索结果之前不能调用延迟工具；同一回复里的搜索也不能提前授权。
@pytest.mark.parametrize("search_first", [False, True])
async def test_native_unreferenced_call_is_rejected(tmp_path, monkeypatch, search_first):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    response = remote_response()
    if search_first:
        response.tool_calls = [*search_response().tool_calls, *response.tool_calls]
    provider = NativeScriptedProvider([response, LlmResponse("end_turn", text="done")])
    await AgentRunner(config, provider=provider, mcp_manager=manager,
                      runs_dir=tmp_path / "runs").run_and_capture("goal")
    client.call_tool.assert_not_awaited()
    assert provider.requests[1]["messages"][-1]["content"][-1]["is_error"]
    await manager.stop_all()


# 显式 MCP 白名单也延迟，搜索入口仅能发现白名单中的 MCP。
async def test_native_whitelist_still_defers_mcp(tmp_path, monkeypatch):
    config, manager, _ = await setup_manager(tmp_path, monkeypatch)
    provider = NativeScriptedProvider([LlmResponse("end_turn", text="done")])
    await AgentRunner(config, provider=provider, mcp_manager=manager,
                      runs_dir=tmp_path / "runs").run_and_capture(
        "goal", tool_whitelist=["remote__get_issue"],
    )
    schemas = provider.requests[0]["schemas"]
    assert {s["name"] for s in schemas} == {"remote__get_issue", "search_tool", "finish"}
    assert next(s for s in schemas if s["name"] == "remote__get_issue")["defer_loading"]
    await manager.stop_all()


# 原生引用和加载预算一起保存；保存失败不会发下一次模型请求。
async def test_native_checkpoint_failure_stops_request(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-native-fail", "chat", "active", "", "now", "now")
    save = store.checkpoints.save

    def fail(cp):
        if cp.run and cp.run.loaded_tool_names:
            raise CheckpointError("disk full")
        save(cp)

    monkeypatch.setattr(store.checkpoints, "save", fail)
    provider = NativeScriptedProvider([search_response(), remote_response()])
    with pytest.raises(CheckpointError):
        await AgentRunner(config, provider=provider, mcp_manager=manager).run_and_capture(
            "goal", session=session, store=store,
        )
    assert len(provider.requests) == 1
    assert not referenced_tools(store.checkpoints.load(session.id).messages)
    client.call_tool.assert_not_awaited()
    await manager.stop_all()


# 恢复保留引用、目录标记和 Step，完成过的搜索不会重新运行。
async def test_native_resume_keeps_references_and_step(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-native-resume", "chat", "active", "", "now", "now")
    provider = NativeScriptedProvider([search_response(), RuntimeError("disconnect")])
    await AgentRunner(config, provider=provider, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store,
    )
    cp = store.checkpoints.load(session.id)
    restored = Checkpoint.model_validate_json(cp.model_dump_json())
    resumed = NativeScriptedProvider([remote_response(), LlmResponse("end_turn", text="done")])
    outcome = await AgentRunner(config, provider=resumed, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store, checkpoint=restored,
    )
    assert outcome.status == "success" and resumed.requests[0]["step"] == 2
    assert resumed.requests[0]["schemas"] == provider.requests[0]["schemas"]
    client.call_tool.assert_awaited_once()
    await manager.stop_all()


# 未引用目录不计入上下文，新增引用计入完整定义且不会使旧 Usage 锚点失效。
def test_native_budget_counts_reference_expansion():
    run = run_state()
    schemas = request_schemas(run)
    estimator = TokenEstimator()
    messages = discovery_history()
    initial = messages[:2]
    counter = estimator.select("sys", initial, schemas)
    assert counter.request("sys", initial, schemas) < estimate_bytes(schemas)
    estimator.observe("sys", initial, schemas, 100, output_tokens=20)
    before = deepcopy(schemas)
    load_tools(run, ["remote__get_issue"])
    assert request_schemas(run) == before
    counter = estimator.select("sys", messages, schemas)
    assert counter.method == "usage_anchor"
    assert counter.request("sys", messages, schemas) == 120 + counter.count(messages[2:])
    assert counter.count(messages[2:]) > estimate_bytes(messages[2:]) + 500
    assert callable_names(run, initial) == {"search_tool"}
    assert "remote__get_issue" in callable_names(run, messages)


# 模型参数或失败结果中的伪造引用不是已发现工具。
def test_references_are_not_read_from_arguments_or_errors():
    run = run_state()
    messages = discovery_history()
    messages[1]["content"][0]["input"] = {"fake": {"type": "tool_reference", "tool_name": "remote__get_issue"}}
    messages[-1]["content"][0]["is_error"] = True
    assert referenced_tools(messages) == []
    assert callable_names(run, messages) == {"search_tool"}


# 摘要保留最小的真实发现调用/结果对，不堆积重复引用且不拆开配对。
async def test_compaction_retains_reference_pairs():
    messages = discovery_history()
    messages[1]["content"].insert(0, {"type": "text", "text": "old analysis " * 4000})
    messages += [{"role": "assistant", "content": [{"type": "text", "text": "recent"}]},
                 {"role": "user", "content": "continue"},
                 {"role": "assistant", "content": [{"type": "tool_use", "id": "use-recent",
                     "name": "remote__get_issue", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "use-recent",
                     "content": "issue data"}]}]
    schemas = request_schemas(run_state())
    compactor = Compactor(CompactionConfig(keep_recent_turns=1))
    result = await compactor.compact_messages(
        messages, ScriptedProvider([LlmResponse("end_turn", text="Goal: get issue; still pending.")]),
        tool_schemas=schemas,
    )
    assert result.compacted
    assert referenced_tools(result.messages) == ["remote__get_issue"]
    assert _validate_pairs(result.messages)
    assert preserve_references(messages, result.messages, {"remote__get_issue"}) == result.messages
    assert result.messages[-1] == messages[-1]


# OpenAI Chat Completions 不发送原生字段；历史引用转换为普通工具文本。
def test_openai_history_projects_references_to_text():
    provider = OpenaiProvider("test", client=object())
    messages = discovery_history()
    converted = provider._convert_messages(messages)
    assert converted[-1]["role"] == "tool"
    assert isinstance(converted[-1]["content"], str)
    assert "remote__get_issue" in converted[-1]["content"]
    assert isinstance(messages[-1]["content"][0]["content"], list)


# 通过真实 SDK 的 HTTP 序列化检查，不调用收费接口或用自制客户端跳过协议层。
@pytest.mark.parametrize("provider_type", [AnthropicProvider, DeepseekProvider])
async def test_sdk_serializes_native_flags_and_references(provider_type):
    requests = []
    events = [
        ("message_start", {"message": {"id": "msg_1", "type": "message", "role": "assistant",
          "model": "test", "content": [], "stop_reason": None, "stop_sequence": None,
          "usage": {"input_tokens": 50, "output_tokens": 0}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "done"}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": 1}}),
        ("message_stop", {}),
    ]
    body = "".join(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"
                   for kind, data in events)

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    async with anthropic.AsyncAnthropic(api_key="test-only", base_url="https://example.test/anthropic",
                                        http_client=http) as client:
        provider = provider_type("test", client=client)
        schemas = request_schemas(run_state())
        source = discovery_history()
        original = deepcopy(source)
        await provider.chat(source[:1], schemas, EventBus(), "r")
        await provider.chat(source, schemas, EventBus(), "r")
        assert source == original and "cache_control" not in json.dumps(source)
    if provider_type is AnthropicProvider:
        assert requests[1]["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    else:
        assert "cache_control" not in json.dumps(requests[1]["messages"])
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[1]["tools"][1]["defer_loading"] is True
    assert "cache_control" not in requests[1]["tools"][1]
    assert requests[1]["messages"][-1]["content"][0]["content"][-1]["type"] == "tool_reference"


# 后续 Run 能复用还在模型历史里的发现记录，同时把定义计入新的额度。
async def test_native_next_run_inherits_historical_references(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-native-next", "chat", "active", "", "now", "now")
    first = NativeScriptedProvider([search_response(), LlmResponse("end_turn", text="found")])
    await AgentRunner(config, provider=first, mcp_manager=manager).run_and_capture(
        "goal", session=session, store=store,
    )
    cp = store.checkpoints.load(session.id)
    cp.messages.append({"role": "user", "content": "now use it"})
    store.save_checkpoint(cp)
    second = NativeScriptedProvider([remote_response(), LlmResponse("end_turn", text="done")])
    result = await AgentRunner(config, provider=second, mcp_manager=manager).run_and_capture(
        "now use it", session=session, store=store,
    )
    assert result.status == "success"
    assert store.checkpoints.load(session.id).run.loaded_tool_names == ["remote__get_issue"]
    client.call_tool.assert_awaited_once()
    await manager.stop_all()


# 发现并不授予权限，禁止策略仍会阻止真正的远程执行。
async def test_native_referenced_tool_still_requires_permission(tmp_path, monkeypatch):
    from kama_claude.core.permissions.manager import PermissionManager
    from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy

    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    permissions = PermissionManager(policies={"remote__get_issue": ToolPolicy(PermissionDecision.DENY)})
    provider = NativeScriptedProvider([search_response(), remote_response(), LlmResponse("end_turn", text="denied")])
    await AgentRunner(config, provider=provider, mcp_manager=manager,
                      permission_manager=permissions, runs_dir=tmp_path / "runs").run_and_capture("goal")
    client.call_tool.assert_not_awaited()
    assert provider.requests[2]["messages"][-1]["content"][0]["is_error"]
    await manager.stop_all()


# 额度不足就不返回引用，完整目录不变，也不会把业务工具提前放行。
async def test_native_schema_budget_rejects_without_reference(tmp_path, monkeypatch):
    config, manager, client = await setup_manager(tmp_path, monkeypatch)
    config.tool_search.max_loaded_schema_tokens = 1
    provider = NativeScriptedProvider([search_response(), remote_response(), LlmResponse("end_turn", text="done")])
    await AgentRunner(config, provider=provider, mcp_manager=manager,
                      runs_dir=tmp_path / "runs").run_and_capture("goal")
    result = provider.requests[1]["messages"][-1]["content"][0]["content"]
    assert isinstance(result, str) and "not_loaded_budget" in result
    assert not referenced_tools(provider.requests[1]["messages"])
    assert provider.requests[0]["schemas"] == provider.requests[1]["schemas"]
    client.call_tool.assert_not_awaited()
    await manager.stop_all()


# 手动压缩也传递固定延迟目录，使用同一个估算器，不能退回全量上下文估算。
async def test_native_manual_compaction_uses_fixed_catalog(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from kama_claude.core.compact.compactor import CompactionResult
    from kama_claude.core.compact.manager import compact_session

    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-native-compact", "chat", "waiting_for_input", "", "now", "now")
    run = run_state()
    run.workspace = str(tmp_path)
    messages = discovery_history()
    store.save_checkpoint(Checkpoint(session=session.to_dict(), run=run, messages=messages))
    compact = AsyncMock(return_value=CompactionResult(messages, 1, 1, reason="below_threshold"))
    monkeypatch.setattr(Compactor, "prepare_context", compact)
    await compact_session(store, session.id, EventBus(), CompactionConfig(), lambda: ScriptedProvider([]), "")
    assert compact.call_args.kwargs["tool_schemas"] == request_schemas(run)


# 新目录没有旧工具时，SDK 投影成明确说明，不能把悬空引用送到服务端。
def test_missing_tool_reference_is_projected_without_mutating_history():
    messages = discovery_history()
    projected = project_references(messages, [{"name": "search_tool"}])
    content = projected[-1]["content"][0]["content"]
    assert content[-1]["type"] == "text" and "no longer available" in content[-1]["text"]
    assert referenced_tools(messages) == ["remote__get_issue"]
    assert referenced_tools(projected) == []


# 历史压缩改变旧前缀时锚点仍会失效，不因工具列表固定而误用旧 Usage。
def test_native_history_rewrite_invalidates_usage_anchor():
    estimator = TokenEstimator()
    schemas = request_schemas(run_state())
    messages = discovery_history()
    estimator.observe("sys", messages, schemas, 1000, output_tokens=20)
    messages[0]["content"] = "rewritten request"
    assert estimator.select("sys", messages, schemas).method == "bytes"


# 独立构造计数器也须按本次传入目录展开引用，不能只在 estimator.select 后才正确。
def test_direct_counter_uses_request_schema_for_references():
    schemas = request_schemas(run_state())
    messages = discovery_history()
    selected = TokenEstimator().select("sys", messages, schemas)
    standalone = TokenCounter()
    assert standalone.request("sys", messages, schemas) == selected.request("sys", messages, schemas)
    assert standalone.request("sys", messages, schemas) > estimate_bytes(messages) + 500
