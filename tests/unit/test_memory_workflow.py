from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.compact.compactor import CompactionResult
from kama_claude.core.config import CompactionConfig, KamaConfig, MemoryConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.memory.context import initialize_prompt
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy, evaluate
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin.edit_file import EditFileTool
from kama_claude.core.tools.builtin.memory import MemoryReadTool, MemoryWriteTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.workspace import WorkspacePathError
from tests.unit.test_checkpoint_recovery import Crash, Provider, finish
from tests.unit.test_s9_context import make_loop


# 构建真实协调层，只替换模型，保留检查点和完整历史提交路径。
def setup_manager(path: Path, provider: Provider) -> tuple[SessionStore, SessionManager]:
    store, bus = SessionStore(path / "sessions"), EventBus()
    return store, SessionManager(
        store, lambda: AgentRunner(KamaConfig(), provider=provider, bus=bus), bus,
    )


# 功能：写入只影响下次新 Run，工具详情进入历史而自动索引不混入历史。
# 设计：三次固定模型回复跨两个真实 Run，直接比较模型实际收到的 system。
async def test_new_run_reads_new_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model = Provider([
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("m", "memory_write", {
            "key": "decision", "summary": "INDEX_ONLY_DECISION", "content": "DETAIL_ONLY_TEXT",
        })]),
        LlmResponse("end_turn", text="saved"), LlmResponse("end_turn", text="next"),
    ])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    await manager.send_message(session.id, "remember this decision")
    assert "INDEX_ONLY_DECISION" not in model.calls[0]["system"]
    assert model.calls[0]["system"] == model.calls[1]["system"]
    await manager.send_message(session.id, "continue")
    assert "INDEX_ONLY_DECISION" in json.dumps(model.calls[2]["messages"])
    assert "INDEX_ONLY_DECISION" not in model.calls[2]["system"]
    assert "DETAIL_ONLY_TEXT" not in model.calls[2]["system"]
    assert "Project memory index" not in json.dumps(store.read_history(session.id))
    cp = store.checkpoints.load(session.id)
    assert cp.run.base_system_prompt and cp.run.memory_snapshot.text not in cp.run.system_prompt


# 功能：恢复沿用已提交提示，不重新读取规则或记忆文件。
# 设计：中断后把磁盘规则改成不同值并禁止重新构建快照，续跑仍应成功。
async def test_resume_keeps_saved_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("ORIGINAL_RULE", encoding="utf-8")
    model = Provider([Crash(), LlmResponse("end_turn", text="done")])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    with pytest.raises(Crash):
        await manager.send_message(session.id, "goal")
    (tmp_path / "AGENTS.md").write_text("CHANGED_RULE", encoding="utf-8")
    _, restored = setup_manager(tmp_path, model)
    opened = await restored.open(session.id)
    with patch("kama_claude.core.memory.context.build_memory_context", side_effect=AssertionError("reload")):
        await restored.resume(session.id, opened["revision"])
        await finish(restored, session.id)
    assert model.calls[0]["system"] == model.calls[1]["system"]
    assert "ORIGINAL_RULE" in store.checkpoints.load(session.id).run.system_prompt


# 功能：初始规则读取失败不登记任务、不保留假运行状态或丢失下次输入能力。
# 设计：损坏用户规则后提交，再修复并提交，验证真实 Manager 可继续使用。
async def test_initial_load_failure_rolls_back_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    rules = tmp_path / "AGENTS.md"
    rules.write_bytes(b"\xff")
    model = Provider([LlmResponse("end_turn", text="done")])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    before = store.checkpoints.load(session.id).model_dump()
    with pytest.raises(HandlerError):
        await manager.send_message(session.id, "bad initialization")
    assert not manager.is_running(session.id) and not model.calls
    assert store.checkpoints.load(session.id).model_dump() == before
    rules.write_text("fixed", encoding="utf-8")
    await manager.send_message(session.id, "retry as new input")
    assert store.checkpoints.load(session.id).run.status == "success"


# 功能：全局更新默认允许，但静态禁止仍生效，工具不能操作用户区。
# 设计：真实读写工具执行 hash 更新，参数模型拒绝旧作用域和伪造文件选择。
async def test_global_tools_and_permissions(tmp_path: Path) -> None:
    from pydantic import ValidationError

    store = MemoryStore(tmp_path)
    reader, writer = MemoryReadTool(store), MemoryWriteTool(store, "s", "r")
    current = json.loads((await reader.invoke({"scope": "global"})).content)
    result = await writer.invoke({
        "scope": "global", "content": "Prefer concise answers", "expected_hash": current["expected_hash"],
    })
    assert not result.is_error
    assert evaluate("memory_write", {"scope": "global"}) == PermissionDecision.ALLOW
    assert evaluate("memory_write", {}, ToolPolicy(PermissionDecision.DENY)) == PermissionDecision.DENY
    with pytest.raises(ValidationError):
        await writer.invoke({"scope": "session", "key": "x", "summary": "x", "content": "x"})
    with pytest.raises(ValidationError):
        await writer.invoke({"scope": "global", "key": "AGENTS.md", "content": "replace"})


# 功能：写文件与编辑工具直接调用也不能改用户规则或全局自动文件。
# 设计：分别验证 workspace 内部和已批准外部目标，批准不能覆盖保护规则。
@pytest.mark.parametrize("tool_type", [WriteFileTool, EditFileTool])
@pytest.mark.parametrize("global_file", [False, True])
async def test_protected_rule_files(
    tmp_path: Path, tool_type, global_file: bool,
) -> None:
    target = Path.home() / ".kama" / "AGENTS.auto.md" if global_file else tmp_path / "AGENTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("USER_OWNED", encoding="utf-8")
    tool = tool_type(tmp_path)
    params = {"path": str(target), "content": "changed"} if tool_type is WriteFileTool else {
        "path": str(target), "old_text": "USER_OWNED", "new_text": "changed",
    }
    with pytest.raises(WorkspacePathError, match="protected"):
        await tool.invoke(params, approved_path=target)
    assert target.read_text(encoding="utf-8") == "USER_OWNED"


# 功能：真实路径别名及历史允许策略不能绕过规则保护，也不会进入执行意图。
# 设计：通过目录链接访问全局用户规则，使用实际 invoke_tool 检查权限错误边界。
async def test_protected_alias_rejected_before_execution(tmp_path: Path) -> None:
    from kama_claude.core.permissions.manager import PermissionManager
    from tests.unit.test_workspace import directory_link, unlink_directory

    home = Path.home() / ".kama"
    home.mkdir()
    rules = home / "AGENTS.md"
    rules.write_text("USER_RULE", encoding="utf-8")
    alias = tmp_path / "alias"
    directory_link(alias, home)
    try:
        registry, hook = ToolRegistry(), Mock()
        registry.register(WriteFileTool(tmp_path))
        result = await invoke_tool(
            registry, ToolCallBlock("write", "write_file", {
                "path": str(alias / "AGENTS.md"), "content": "overwrite",
            }), EventBus(), "run", checkpoint_hook=hook,
            permission_manager=PermissionManager({"write_file": ToolPolicy(PermissionDecision.ALLOW)}),
        )
        assert result.error_type == "permission_denied" and not result.outcome_unknown
        hook.assert_not_called()
        assert rules.read_text(encoding="utf-8") == "USER_RULE"
    finally:
        unlink_directory(alias)


# 构造确定的程序压缩候选，隔离摘要质量，只验证刷新与提交链路。
def refreshing_loop(path: Path, persistence=None):
    model = Mock(chat=AsyncMock(return_value=LlmResponse("end_turn", text="done")))
    original = [{"role": "user", "content": "original history " * 200}]
    loop, context, events = make_loop(path, original, model, persistence=persistence)
    memory = MemoryStore(path)
    memory.home.mkdir(parents=True, exist_ok=True)
    memory.global_path().write_text("OLD_AUTO", encoding="utf-8")
    initialize_prompt(context.run, memory, "BASE")
    loop._context_manager.config.auto_threshold = 0.001
    loop._context_manager._memory = memory
    candidate = CompactionResult(
        [{"role": "user", "content": "short history"}], 10_000, 10,
        compacted=True, applied_stages=["llm_summary"],
    )
    loop._context_manager._compactor = Mock(prepare_context=AsyncMock(return_value=candidate))
    return loop, context, model, events


# 功能：程序压缩后重新读取规则，实际模型请求使用新快照且只出现一份注入块。
# 设计：Loop 装配后修改磁盘，再验证模型实参，覆盖局部旧 system 字符串问题。
async def test_compaction_refreshes_actual_model_prompt(tmp_path: Path) -> None:
    rule = tmp_path / "AGENTS.md"
    rule.write_text("OLD_RULE", encoding="utf-8")
    persistence = Mock()
    loop, context, model, _ = refreshing_loop(tmp_path, persistence)
    rule.write_text("NEW_RULE", encoding="utf-8")
    loop._context_manager._memory.global_path().write_text("NEW_AUTO", encoding="utf-8")
    await loop.run()
    system = model.chat.call_args.kwargs["system"]
    assert "OLD_RULE" in system and "NEW_RULE" not in system
    assert system.count("Project user rules") == 1
    assert context.run.system_prompt == system and persistence.save.called
    assert json.dumps(model.chat.call_args.kwargs["messages"]).count("NEW_AUTO") == 1


# 功能：刷新失败保留旧历史和快照并中断，保存失败不调用模型或通知成功。
# 设计：分别注入磁盘读取错误和检查点保存错误，验证各自异常边界。
@pytest.mark.parametrize("save_failure", [False, True])
async def test_refresh_failures_stop_execution(tmp_path: Path, save_failure: bool) -> None:
    rule = tmp_path / "AGENTS.md"
    rule.write_text("OLD_RULE", encoding="utf-8")
    persistence = Mock()
    loop, context, model, events = refreshing_loop(tmp_path, persistence)
    old_messages = context.messages.copy()
    old_system = context.run.system_prompt
    if save_failure:
        persistence.save.side_effect = CheckpointError("disk full")
        with pytest.raises(CheckpointError):
            await loop.run()
    else:
        loop._context_manager._memory.global_path().write_bytes(b"\xff")
        await loop.run()
        assert context.run.status == "interrupted" and "memory_refresh_failed" in context.run.reason
        assert context.messages == old_messages and context.run.system_prompt == old_system
    model.chat.assert_not_called()
    assert not any(event.type == "context.compacted" for event in events)


# 功能：刷新后的用户规则撑满预算时中断，不调用模型或误发压缩成功。
# 设计：先观察旧提示的 Usage 锚点，再只增加规则，验证整份新请求重新计量。
async def test_refreshed_prompt_rechecks_budget_and_anchor(tmp_path: Path) -> None:
    rule = tmp_path / "AGENTS.md"
    rule.write_text("OLD_RULE", encoding="utf-8")
    loop, context, model, events = refreshing_loop(tmp_path, Mock())
    estimator = loop._context_manager._estimator
    schemas = loop._tools.registry.tool_schemas()
    estimator.observe(context.run.system_prompt, context.messages, schemas, 1)
    assert estimator.select(context.run.system_prompt, context.messages, schemas).method == "usage_anchor"
    memory = loop._context_manager._memory
    memory.config.global_auto_ratio = 100_000 / 1_048_576
    memory.global_path().write_text("NEW_LARGE_AUTO " * 10_000, encoding="utf-8")
    await loop.run()
    assert context.run.reason == "context_budget_exceeded" and context.run.step == 3
    assert "NEW_LARGE_AUTO" not in context.run.memory_snapshot.text
    assert "OLD_RULE" in context.run.system_prompt
    assert estimator.select(context.run.system_prompt, context.messages, schemas).method == "usage_anchor"
    model.chat.assert_not_called()
    assert not any(event.type == "context.compacted" for event in events)


# 功能：没有实际改变历史的压缩尝试不重新读取规则。
# 设计：磁盘文件变坏但压缩返回未改变，模型应沿用已提交的旧快照。
async def test_noop_compaction_does_not_refresh(tmp_path: Path) -> None:
    rule = tmp_path / "AGENTS.md"
    rule.write_text("OLD_RULE", encoding="utf-8")
    loop, context, model, _ = refreshing_loop(tmp_path)
    loop._context_manager._compactor.prepare_context.return_value.compacted = False
    rule.write_bytes(b"\xff")
    await loop.run()
    assert context.run.status == "success" and "OLD_RULE" in model.chat.call_args.kwargs["system"]


# 功能：手动压缩与自动入口同样刷新，失败不提交半套历史，也不改变完整对话。
# 设计：用真实会话存储加确定候选，分别覆盖读取、保存和硬预算边界。
@pytest.mark.parametrize("mode", ["success", "read_failure", "save_failure", "hard_budget"])
async def test_manual_compaction_refresh_and_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    rule = tmp_path / "AGENTS.md"
    rule.write_text("OLD_RULE", encoding="utf-8")
    model = Provider([LlmResponse("end_turn", text="done")])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    await manager.send_message(session.id, "goal")
    original = store.checkpoints.load(session.id)
    original_history = store.read_history(session.id)
    rule.write_text("NEW_RULE", encoding="utf-8")
    auto = MemoryStore(tmp_path).global_path()
    auto.parent.mkdir(parents=True, exist_ok=True)
    auto.write_text("NEW_AUTO", encoding="utf-8")
    if mode == "read_failure":
        auto.write_bytes(b"\xff")
    if mode == "hard_budget":
        manager._compaction = CompactionConfig(
            context_window_tokens=2_048, output_reserve_ratio=(512) / (2_048), summary_max_ratio=(256) / (2_048), summary_target_ratio=(256) / (2 * (2_048)),
            auto_threshold=0.5,
        )
        manager._memory.global_auto_ratio = 0.01
        rule.write_text("NEW_RULE " * 1_000, encoding="utf-8")
    candidate = CompactionResult(
        [{"role": "user", "content": "short history"}], 10_000, 10,
        compacted=True, applied_stages=["llm_summary"],
    )
    events = []

    # 完成事件到达时读取磁盘，确认通知没有早于提交。
    async def collect(event) -> None:
        events.append(event)
        if event.type == "context.compacted":
            assert "NEW_AUTO" in store.checkpoints.load(session.id).run.memory_snapshot.text

    manager._bus.subscribe(collect)
    with patch("kama_claude.core.compact.manager.Compactor.prepare_context", AsyncMock(return_value=candidate)):
        if mode == "save_failure":
            with patch.object(store, "save_checkpoint", side_effect=CheckpointError("disk full")):
                with pytest.raises(CheckpointError):
                    await manager.compact(session.id)
        else:
            result = await manager.compact(session.id)
            assert result.compacted == (mode == "success")
            if mode == "hard_budget":
                assert result.reason == "context_budget_exceeded"
    saved = store.checkpoints.load(session.id)
    if mode != "success":
        assert saved.messages == original.messages and saved.run.system_prompt == original.run.system_prompt
    else:
        assert "OLD_RULE" in saved.run.system_prompt
        assert "NEW_AUTO" in saved.run.memory_snapshot.text
    if mode != "success":
        assert not any(event.type == "context.compacted" for event in events)
    assert store.read_history(session.id) == original_history and len(model.calls) == 1


# 功能：比例预算支持环境覆盖，拒绝旧字段和非法比例。
# 设计：直接调用解析入口，避免外部配置文件影响边界验证。
def test_memory_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = KamaConfig()
    apply_toml(config, {"memory": {"global_auto_ratio": 0.0005, "project_index_ratio": 0.001}})
    monkeypatch.setenv("KAMA_MEMORY_PROJECT_INDEX_RATIO", "0.002")
    apply_env(config)
    assert config.memory == MemoryConfig(global_auto_ratio=0.0005, project_index_ratio=0.002)
    for value in ({"global_tokens": 512}, {"project_index_ratio": 0}, {"global_auto_ratio": True}):
        with pytest.raises(SystemExit):
            apply_toml(config, {"memory": value})
