from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from kama_claude.core.config import KamaConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, evaluate
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin.memory import MemoryReadTool, MemoryWriteTool
from tests.unit.test_checkpoint_recovery import Crash, Provider, finish


# 构建完整执行链路，仅模型替换为可观察的固定响应
def setup_manager(tmp_path: Path, provider: Provider) -> tuple[SessionStore, SessionManager]:
    store = SessionStore(tmp_path / "sessions")
    bus = EventBus()
    manager = SessionManager(
        store, lambda: AgentRunner(KamaConfig(), provider=provider, bus=bus), bus,
    )
    return store, manager


# 隔离真实用户目录与项目目录，避免用例读取用户自己的记忆
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


# 功能：验证本轮写入不改冻结提示，下一轮读取新记忆，全文历史不包含自动注入
# 设计：真实 Manager/Runner/Loop 连续执行两轮并查看快照，固定模型只产生三次回复
async def test_memory_write_refreshes_only_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate(tmp_path, monkeypatch)
    model = Provider([
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("m", "memory_write", {
            "scope": "session", "key": "python", "summary": "Python", "content": "Python 3.12",
        })]),
        LlmResponse("end_turn", text="saved"),
        LlmResponse("end_turn", text="remembered"),
    ])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    await manager.send_message(session.id, "remember the version")
    assert model.calls[0]["system"] == model.calls[1]["system"]
    assert "Python 3.12" not in model.calls[0]["system"]
    await manager.send_message(session.id, "what is the version")
    assert "Python 3.12" in model.calls[2]["system"]
    assert len(model.calls) == 3
    inspection = manager.inspect_memory(session.id)
    assert inspection["snapshot"]["layers"][2]["included"] == ["python"]
    assert "Python 3.12" in inspection["snapshot"]["text"]
    cp = store.checkpoints.load(session.id)
    assert cp.run.memory_snapshot.text in cp.run.system_prompt
    history = store.read_history(session.id)
    assert len(history) == 6
    assert not any("## Memory policy" in str(message["content"]) for message in history)
    assert not (store.session_dir(session.id) / "notes.md").exists()


# 功能：验证恢复跳过记忆文件读取，当前文件变化也不替换旧 Run 的注入快照
# 设计：先在模型请求处中断，再更新并损坏记忆文件，同时令构建函数调用必定失败
async def test_resume_reuses_snapshot_without_reading_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate(tmp_path, monkeypatch)
    memory = MemoryStore(tmp_path)
    memory.upsert("project", "version", summary="Version", content="original version", source="s / r")
    model = Provider([Crash(), LlmResponse("end_turn", text="done")])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    with pytest.raises(Crash):
        await manager.send_message(session.id, "goal")
    original = store.checkpoints.load(session.id).run.memory_snapshot
    (memory.directory("project") / "version.md").write_text("broken new file", encoding="utf-8")
    monkeypatch.setattr(
        "kama_claude.core.runner.build_memory_context",
        Mock(side_effect=AssertionError("resume must not load memory")),
    )
    _, restored = setup_manager(tmp_path, model)
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    assert model.calls[0]["system"] == model.calls[1]["system"]
    assert "original version" in model.calls[1]["system"]
    inspection = restored.inspect_memory(session.id)
    assert inspection["snapshot"] == original.model_dump(mode="json")
    assert inspection["layers"][1]["warnings"]


# 功能：验证只读检查不创建 Run、不读模型、不提交快照或修改文件
# 设计：记录全部磁盘字节和修订号，重复查看及分页后要求完全不变
async def test_inspect_is_read_only_and_old_files_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate(tmp_path, monkeypatch)
    model = Provider([LlmResponse("end_turn", text="done")])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    old = tmp_path / ".kama" / "context.md"
    old.parent.mkdir()
    old.write_text("DO NOT IMPORT OLD CONTEXT", encoding="utf-8")
    notes = store.session_dir(session.id) / "notes.md"
    notes.write_text("DO NOT IMPORT OLD NOTES", encoding="utf-8")
    await manager.send_message(session.id, "goal")
    assert "DO NOT IMPORT" not in model.calls[0]["system"]
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    for _ in range(2):
        assert manager.inspect_memory(session.id)["snapshot"]["text"] == ""
    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after and len(model.calls) == 1


# 功能：验证旧工具缺失时明确拒绝续跑，仍允许终止后开始新任务
# 设计：仅把旧名字加入冻结工具清单，不创建兼容工具，检查模型没有被再次调用
async def test_removed_tool_blocks_old_run_but_new_run_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate(tmp_path, monkeypatch)
    model = Provider([Crash(), LlmResponse("end_turn", text="new run")])
    store, manager = setup_manager(tmp_path, model)
    session = await manager.create("chat")
    with pytest.raises(Crash):
        await manager.send_message(session.id, "old goal")
    cp = store.checkpoints.load(session.id)
    cp.run.tool_schemas.append({
        "name": "note_save", "description": "removed", "input_schema": {"type": "object"},
    })
    store.save_checkpoint(cp)
    _, restored = setup_manager(tmp_path, model)
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    cp = store.checkpoints.load(session.id)
    assert cp.run.status == "interrupted" and "missing: note_save" in cp.run.reason
    assert len(model.calls) == 1
    await restored.resume(session.id, cp.revision, "abort")
    await restored.send_message(session.id, "new goal")
    assert store.checkpoints.load(session.id).run.status == "success"


# 功能：验证全局写入不被项目层 always_allow 缓存放行，默认项目和会话仍允许
# 设计：项目/会话正常放行，全局写入则由真实权限管理器挂起并等待回答。
async def test_global_write_requires_scoped_approval() -> None:
    permissions = PermissionManager(timeout_s=1)
    events = []

    # 在事件抵达时模拟用户只批准这一次全局写入
    async def approve(event: dict[str, Any]) -> None:
        events.append(event)
        permissions.respond(event["request_id"], "allow_once", session_id=event["session_id"], run_id=event["run_id"])

    assert evaluate("memory_write", {"scope": "global"}) == PermissionDecision.ASK
    assert evaluate("memory_write", {"scope": "project"}) == PermissionDecision.ALLOW
    for scope in ("project", "session", "global"):
        allowed, _ = await permissions.check_and_wait(
            scope, "memory_write", {"scope": scope, "key": "x"}, "s", approve,
        )
        assert allowed
    assert [event["params"]["scope"] for event in events] == ["global"]


# 功能：验证两个工具的新增、读取、删除闭环，不依赖旧文件或专用接口
# 设计：直接读取结构化工具响应，确认正文分页和幂等删除状态可供模型理解
async def test_memory_tools_read_write_delete(tmp_path: Path) -> None:
    import json

    memory = MemoryStore(tmp_path, tmp_path / "s", global_dir=tmp_path / "g")
    writer = MemoryWriteTool(memory, "s", "r")
    reader = MemoryReadTool(memory)
    params = {"scope": "session", "key": "decision", "summary": "Decision", "content": "Use TCP"}
    assert not (await writer.invoke(params)).is_error
    listed = json.loads((await reader.invoke({"scope": "session"})).content)
    assert listed["entries"][0]["key"] == "decision"
    result = json.loads((await reader.invoke({"scope": "session", "key": "decision"})).content)
    assert result["content"] == "Use TCP" and result["source"] == "s / r"
    assert not (await writer.invoke({"scope": "session", "key": "decision", "action": "delete"})).is_error
    assert (await reader.invoke({"scope": "session", "key": "decision"})).is_error


# 功能：验证配置接受 0，环境变量覆盖 TOML，并拒绝负数和未知配置
# 设计：对独立配置对象使用现有解析入口，避免依赖本机配置文件
def test_memory_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = KamaConfig()
    apply_toml(config, {"memory": {"project_tokens": 100, "session_tokens": 0}})
    monkeypatch.setenv("KAMA_MEMORY_PROJECT_TOKENS", "200")
    apply_env(config)
    assert config.memory.project_tokens == 200 and config.memory.session_tokens == 0
    with pytest.raises(SystemExit):
        apply_toml(config, {"memory": {"global_tokens": -1}})
    with pytest.raises(SystemExit):
        apply_toml(config, {"memory": {"unknown": 5}})
    monkeypatch.setenv("KAMA_MEMORY_GLOBAL_TOKENS", "-1")
    with pytest.raises(SystemExit):
        apply_env(config)


# 功能：验证全局记忆授权只在原会话复用，不写权限文件。
# 设计：重复请求命中范围授权；新管理器与另一会话必须重新审批。
async def test_explicit_global_always_permission_is_separate(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    permissions = PermissionManager(policy_file=path, timeout_s=1)
    events = []

    # 用户明确授权全局记忆，不借用之前项目层的许可
    async def approve(event: dict[str, Any]) -> None:
        events.append(event)
        permissions.respond(event["request_id"], "always_allow", session_id=event["session_id"], run_id=event["run_id"])

    assert (await permissions.check_and_wait(
        "first", "memory_write", {"scope": "global"}, "s", approve,
    ))[0]
    assert not path.exists()
    assert (await permissions.check_and_wait(
        "same", "memory_write", {"scope": "global"}, "s", approve,
    ))[0]
    permissions = PermissionManager(policy_file=path, timeout_s=1)
    assert (await permissions.check_and_wait(
        "second", "memory_write", {"scope": "global"}, "another", approve,
    ))[0]
    assert len(events) == 2  # 同会话复用；重启和换会话都重新审批。
