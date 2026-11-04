from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import ToolResult
from kama_claude.tui.app import KamaTuiApp, LLMStreamBlock, ToolCallBlock
from tests.unit.test_checkpoint_recovery import (
    Crash,
    ProbeTool,
    Provider,
    finish,
    manager,
    tool_response,
)
from tests.unit.test_run_persistence import _binding
from tests.unit.test_tui_recovery import idle_socket


# 功能：验证快照提交后、历史追加前崩溃，重启能够补写且不会重复
# 设计：只提交含固定消息 ID 的快照，重新加载三次，检查 thread 的原始行数量
def test_committed_pending_history_replays_once(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    cp = persistence.checkpoint
    cp.messages = persistence.context.messages
    persistence.store.checkpoints.save(cp)
    assert persistence.store.read_history("sess-one") == []
    restored = SessionStore(tmp_path)
    for _ in range(3):
        restored.ensure_checkpoint("sess-one")
    assert restored.read_history("sess-one") == [{"role": "user", "content": "goal"}]
    assert len((tmp_path / "sess-one" / "thread.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("partial", [b'{"id":', b'{"content":"\xe4\xb8'])
# 功能：验证崩溃留下的半行 JSON 或半个中文字符可从已提交快照补写
# 设计：在完整旧行后追加两种损坏末行，补写后旧内容不变且新原文只出现一次
def test_partial_last_line_is_repaired(tmp_path: Path, partial: bytes) -> None:
    persistence = _binding(tmp_path)
    persistence.save()
    original = (tmp_path / "sess-one" / "thread.jsonl").read_bytes()
    persistence.record_message("assistant", "完整回复")
    persistence.store.checkpoints.save(persistence.checkpoint)
    path = tmp_path / "sess-one" / "thread.jsonl"
    path.write_bytes(original + partial)
    restored = SessionStore(tmp_path)
    restored.ensure_checkpoint("sess-one")
    assert path.read_bytes().startswith(original)
    assert restored.read_history("sess-one")[-1] == {"role": "assistant", "content": "完整回复"}
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


# 功能：验证中间损坏记录不会被当成可修复的半行而静默丢弃
# 设计：构造带换行的非法旧记录，要求补写失败且文件字节保持不变
def test_corrupt_existing_history_stops_append(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    persistence.save()
    path = tmp_path / "sess-one" / "thread.jsonl"
    damaged = path.read_bytes() + b"broken\n"
    path.write_bytes(damaged)
    persistence.record_message("assistant", "new")
    with pytest.raises(CheckpointError, match="invalid history row"):
        persistence.save()
    assert path.read_bytes() == damaged


# 功能：验证相同文本的两次真实输入仍分别保留，不按内容误去重
# 设计：连续追加内容完全相同的消息，再反复加载，断言两个不同 ID 对应两行
def test_equal_real_messages_are_not_deduplicated(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    persistence.save()
    for _ in range(2):
        persistence.store.append_message("sess-one", "user", "again")
    for _ in range(2):
        persistence.store.ensure_checkpoint("sess-one")
    history = persistence.store.read_history("sess-one")
    assert [message["content"] for message in history] == ["goal", "again", "again"]
    rows = [json.loads(line) for line in (tmp_path / "sess-one" / "thread.jsonl").read_text().splitlines()]
    assert len({row["id"] for row in rows}) == 3


# 功能：验证完整历史遗失后拒绝拿压缩上下文假冒完整对话
# 设计：模拟已初始化会话被外部删除 thread，重新打开必须明确报错
def test_missing_initialized_history_is_not_recreated(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    persistence.save()
    path = tmp_path / "sess-one" / "thread.jsonl"
    path.unlink()
    with pytest.raises(CheckpointError, match="full history file is missing"):
        SessionStore(tmp_path).ensure_checkpoint("sess-one")
    assert not path.exists()


# 功能：验证记录原文时深拷贝，之后对工作消息的原地修改不会污染原始历史
# 设计：登记后主动改写嵌套工具结果，最终只允许检查点看到裁剪内容
def test_pending_history_does_not_share_working_blocks(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    content = [{"type": "tool_result", "tool_use_id": "t", "content": "original"}]
    persistence.record_message("user", content)
    content[0]["content"] = "preview"
    persistence.save()
    assert persistence.store.read_history("sess-one")[-1]["content"][0]["content"] == "original"


class LongTool(ProbeTool):
    # 返回足够长的原文，触发第一层整理；工具本身不裁剪
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult(content="HEAD" + "中" * 12_000 + "TAIL")


# 功能：验证长工具原文和 thinking 保留，重开显示完整历史而模型只读取预览
# 设计：真实 Manager、Runner、Loop 和存储执行两轮，不依赖模型网络或人工查看终端
async def test_full_history_survives_preview_and_next_run(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    tool = LongTool("long")
    response = tool_response("long")
    response.text = "准备读取"
    response.thinking_blocks = [{"type": "thinking", "thinking": "推理内容", "signature": "sig"}]
    provider = Provider([
        response,
        LlmResponse("end_turn", text="第一轮完整答复"),
        LlmResponse("end_turn", text="第二轮完整答复"),
    ])
    mgr = manager(store, provider, [tool])
    session = await mgr.create("chat")
    await mgr.send_message(session.id, "读取全文")
    first = await mgr.open(session.id)
    assert len(first["messages"]) == 4
    assert first["messages"][1]["content"][0] == response.thinking_blocks[0]
    full_result = first["messages"][2]["content"][0]["content"]
    assert full_result == "HEAD" + "中" * 12_000 + "TAIL"
    model_result = provider.calls[1]["messages"][2]["content"][0]["content"]
    assert "chars omitted" in model_result and len(model_result) < len(full_result)
    await mgr.send_message(session.id, "继续追问")
    reopened = await manager(SessionStore(tmp_path), provider, [tool]).open(session.id)
    assert reopened["messages"][:4] == first["messages"]
    assert reopened["messages"][-2] == {"role": "user", "content": "继续追问"}
    assert reopened["messages"][-1]["content"] == [{"type": "text", "text": "第二轮完整答复"}]
    assert await mgr.get_history(session.id) == reopened["messages"]
    assert tool.calls == 1


# 功能：验证结果已提交但 thread 追加失败时停止，重启补齐后不重跑已完成工具
# 设计：在两个工具之间阻断历史写入，恢复时检查执行次数和完整历史的配对结果
async def test_history_append_failure_preserves_completed_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    a, b = ProbeTool("a"), ProbeTool("b")
    provider = Provider([tool_response("a", "b"), LlmResponse("end_turn", text="done")])
    mgr = manager(store, provider, [a, b])
    session = await mgr.create("chat")
    original = store.flush_history
    events: list[str] = []

    # 记录事件类型，确保保存失败不会发布成功终态
    async def collect(event: Any) -> None:
        events.append(event.type)

    mgr._bus.subscribe(collect)

    # 精确阻断第一个工具的结果归档，检查点已经是 completed
    def fail_after_first_result(cp: Any) -> None:
        if cp.run and cp.run.phase == "tools" and cp.run.tool_index == 1:
            raise CheckpointError("history disk unavailable")
        original(cp)

    monkeypatch.setattr(store, "flush_history", fail_after_first_result)
    with pytest.raises(CheckpointError, match="history disk unavailable"):
        await mgr.send_message(session.id, "goal")
    assert (a.calls, b.calls) == (1, 0)
    assert "run.finished" not in events
    monkeypatch.setattr(store, "flush_history", original)
    restored = manager(SessionStore(tmp_path), provider, [a, b])
    opened = await restored.open(session.id)
    assert opened["messages"][-1]["content"][0]["content"] == "a result"
    await restored.resume(session.id, opened["revision"])
    await finish(restored, session.id)
    final = await restored.open(session.id)
    assert (a.calls, b.calls) == (1, 1)
    ids = [
        block["tool_use_id"]
        for message in final["messages"]
        if isinstance(message["content"], list)
        for block in message["content"] if block["type"] == "tool_result"
    ]
    assert ids == ["id-a", "id-b"]


# 功能：验证中断后终止的配对说明也进入完整历史，重开不会重复追加
# 设计：未知副作用只执行一次，用户明确终止后读取两次原文并检查错误标记
async def test_abort_is_recorded_in_full_history(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    tool = ProbeTool("a", crash=True)
    provider = Provider([tool_response("a")])
    mgr = manager(store, provider, [tool])
    session = await mgr.create("chat")
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "goal")
    restored = manager(SessionStore(tmp_path), provider, [tool])
    opened = await restored.open(session.id)
    await restored.resume(session.id, opened["revision"], "abort")
    final = await restored.open(session.id)
    assert len(final["messages"]) == 3
    result = final["messages"][-1]["content"][0]
    assert result["is_error"] is True and "UNKNOWN" in result["content"]
    assert (await restored.open(session.id))["messages"] == final["messages"]
    assert tool.calls == 1


# 功能：验证实际 TUI 打开压缩过的会话，显示的是原始用户、答复和完整工具结果
# 设计：假客户端直接委托真实 Manager.open，使用 Textual 生命周期检查渲染组件
async def test_tui_open_renders_full_history_after_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path)
    mgr = manager(store, Provider([]), [])
    session = await mgr.create("chat")
    original = [
        {"role": "user", "content": "最初的问题"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "压缩前的完整答复"},
            {"type": "tool_use", "id": "t", "name": "read_file", "input": {"path": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "完整结果" * 3_000},
        ]},
    ]
    for message in original:
        store.append_message(session.id, message["role"], message["content"])
    store.write_compacted(session.id, [{"role": "user", "content": "只给模型看的摘要"}])

    class Client:
        # 将打开请求交给真实后端，仅替代 TCP 传输
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "session.open"
            return await mgr.open(params["session_id"])

    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = Client()
        await app._open_session(session.id)
        await pilot.pause()
        assert any(widget.content == "> 最初的问题" for widget in app.query(Static))
        assert [block._text for block in app.query(LLMStreamBlock)] == ["压缩前的完整答复"]
        assert app.query_one(ToolCallBlock)._output == "完整结果" * 3_000
        assert store.read_messages(session.id) == [{"role": "user", "content": "只给模型看的摘要"}]
