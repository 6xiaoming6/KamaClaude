from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact import budget
from kama_claude.core.compact.compactor import SUMMARY_MARKER, Compactor
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.loop import AgentLoop
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.registry import ToolRegistry
from tests.unit.test_compactor import history, provider


# 构造请求前阶段的 Loop，直接观察模型入参、事件和检查点提交
def make_loop(
    tmp_path: Path, messages: list[dict[str, Any]], model: Any,
    config: CompactionConfig | None = None, persistence: Any = None,
) -> tuple[AgentLoop, ExecutionContext, list[Any]]:
    config = config or CompactionConfig(
        context_window_tokens=8_192, output_reserve_tokens=512,
        summary_max_tokens=256, auto_threshold=0.1, prune_target_ratio=0.05,
    )
    context = ExecutionContext(
        run=RunCheckpoint(
            run_id="run-s9", goal="continue", model="fake", workspace=str(tmp_path),
            max_steps=10, phase="llm", step=3,
        ), messages=messages,
    )
    bus = EventBus()
    events: list[Any] = []

    # 收集总线事件并保留顺序，断言保存与通知边界
    async def collect(event: Any) -> None:
        events.append(event)

    bus.subscribe(collect)
    loop = AgentLoop(
        model, ToolRegistry(), bus, context=context, compactor=Compactor(config),
        compaction=config, run_path=tmp_path, persistence=persistence, session_id="sess-s9",
    )
    return loop, context, events


# 功能：验证恢复模型调用沿用 Step，并在调用前按实际历史而非旧占比压缩
# 设计：context_pct 为零仍触发压缩，捕获摘要调用和正式请求的不同参数
async def test_request_preparation_compacts_before_model_and_keeps_step(tmp_path: Path) -> None:
    model = provider()
    model.chat.side_effect = [LlmResponse("end_turn", text="summary"), LlmResponse("end_turn", text="done")]
    persistence = Mock()
    loop, context, events = make_loop(tmp_path, history(), model, persistence=persistence)
    await loop.run()
    assert context.run.status == "success" and context.run.step == 3
    assert model.chat.call_count == 2
    assert model.chat.call_args_list[0].kwargs["run_id"] == "compact"
    request = model.chat.call_args_list[1].kwargs
    assert request["messages"][0]["content"].startswith(SUMMARY_MARKER)
    assert request["max_output_tokens"] == 512
    assert [e.type for e in events] == [
        "step.started", "context.compaction_started", "context.compacted", "step.finished",
    ]
    assert persistence.save.called
    assert list(tmp_path.glob("summary_*.md"))


@pytest.mark.parametrize("large_part", ["messages", "system", "tools"])
# 功能：验证关闭自动压缩后仍阻止超过硬预算的请求
# 设计：分别由消息、系统提示和工具 Schema 撑满预算，不能只检查历史长度
async def test_hard_budget_applies_with_auto_disabled(tmp_path: Path, large_part: str) -> None:
    model = provider()
    config = CompactionConfig(
        auto_threshold=0, context_window_tokens=2_048, output_reserve_tokens=512,
        summary_max_tokens=256,
    )
    messages = [{"role": "user", "content": "hello"}]
    if large_part == "messages":
        messages[0]["content"] = "中" * 1_000
    loop, context, _ = make_loop(tmp_path, messages, model, config)
    if large_part == "system":
        context.run.system_prompt = "中" * 1_000
    if large_part == "tools":
        loop._registry = Mock(tool_schemas=Mock(return_value=[{"description": "中" * 1_000}]))
    await loop.run()
    assert context.run.status == "interrupted"
    assert context.run.phase == "llm" and context.run.step == 3
    assert context.run.reason == "context_budget_exceeded"
    model.chat.assert_not_called()


@pytest.mark.parametrize("hard_limit", [False, True])
# 功能：验证摘要失败在软预算内可继续，超过硬预算则保留现场并中断
# 设计：同一份历史改变窗口，检查正式请求次数而非只检查压缩器返回值
async def test_failed_auto_compaction_obeys_hard_budget(tmp_path: Path, hard_limit: bool) -> None:
    model = provider()
    model.chat.side_effect = [RuntimeError("offline"), LlmResponse("end_turn", text="done")]
    config = CompactionConfig(
        context_window_tokens=4_096 if hard_limit else 8_192,
        output_reserve_tokens=1_024, summary_max_tokens=256, auto_threshold=0.1, prune_target_ratio=0.05,
    )
    original = history()
    loop, context, events = make_loop(tmp_path, copy.deepcopy(original), model, config)
    await loop.run()
    assert model.chat.call_count == (1 if hard_limit else 2)
    assert context.run.status == ("interrupted" if hard_limit else "success")
    assert context.messages[:len(original)] == original
    assert sum(e.type == "context.compaction_failed" for e in events) == 1


# 功能：验证候选摘要保存失败后不请求正式模型，也不发布压缩成功
# 设计：只在新摘要首次提交时注入磁盘错误，区别于摘要服务异常
async def test_summary_save_failure_stops_before_model(tmp_path: Path) -> None:
    model = provider()
    persistence = Mock()
    loop, _, events = make_loop(tmp_path, history(), model, persistence=persistence)
    persistence.save.side_effect = CheckpointError("disk full")
    with pytest.raises(CheckpointError):
        await loop.run()
    assert model.chat.call_count == 1
    assert not any(e.type == "context.compacted" for e in events)


# 功能：验证完成通知失败不会回滚已保存摘要或再次发起摘要请求
# 设计：订阅者仅在完成事件抛错，正式模型仍收到已提交的候选消息
async def test_notification_failure_does_not_repeat_compaction(tmp_path: Path) -> None:
    model = provider()
    model.chat.side_effect = [LlmResponse("end_turn", text="summary"), LlmResponse("end_turn", text="done")]
    persistence = Mock()
    loop, context, _ = make_loop(tmp_path, history(), model, persistence=persistence)

    # 模拟前端或观察者的完成通知失败
    async def broken_notification(event: Any) -> None:
        if event.type == "context.compacted":
            assert persistence.save.called
            raise RuntimeError("observer lost")

    loop._bus.subscribe(broken_notification)
    await loop.run()
    assert context.run.status == "success" and model.chat.call_count == 2
    assert context.messages[0]["content"].startswith(SUMMARY_MARKER)


class LargeTool(BaseTool):
    name = "large"
    description = "controlled output"
    input_schema = {"type": "object", "properties": {}}

    # 记录实际执行次数，排除归档恢复时重复调用
    def __init__(self) -> None:
        self.calls = 0

    # 返回超过裁剪阈值的确定结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult("HEAD" + "x" * 12_000 + "TAIL")


# 功能：验证工具已完成后归档失败，继续运行只整理输出而不重新执行工具
# 设计：在第二次请求前阻断归档，再用同一个游标恢复，核对真实调用次数与全文
async def test_archive_failure_preserves_completed_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = provider()
    model.chat.side_effect = [
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("large-id", "large", {})]),
        LlmResponse("end_turn", text="done"),
    ]
    config = CompactionConfig(auto_threshold=0)
    loop, context, _ = make_loop(tmp_path, [{"role": "user", "content": "goal"}], model, config)
    context.run.phase = "ready"
    context.run.step = 0
    tool = LargeTool()
    loop._registry.register(tool)
    original_archive = budget._archive_output
    monkeypatch.setattr(budget, "_archive_output", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        await loop.run()
    assert tool.calls == 1 and context.run.tools[0].status == "completed"
    assert context.run.phase == "llm" and context.run.step == 2
    monkeypatch.setattr(budget, "_archive_output", original_archive)
    await loop.run()
    assert tool.calls == 1 and context.run.step == 2 and context.run.status == "success"
    archived = list((tmp_path / "outputs").glob("*.txt"))
    assert len(archived) == 1 and archived[0].read_text(encoding="utf-8").endswith("TAIL")
    sent = model.chat.call_args.kwargs["messages"][2]["content"][0]["content"]
    assert sent.startswith("HEAD") and sent.endswith("TAIL") and "chars omitted" in sent


# 功能：验证本批多个新工具结果先归档再参与预算，触发不依赖旧 API 占比
# 设计：同一回复两个长结果，其中一个失败，检查配对、错误标志与两份全文都保留
async def test_new_multi_tool_results_are_archived_before_budget(tmp_path: Path) -> None:
    call = {"role": "assistant", "content": [
        {"type": "tool_use", "id": name, "name": "read", "input": {}} for name in ("a", "b")
    ]}
    results = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": name, "content": name * 12_000, "is_error": name == "b"}
        for name in ("a", "b")
    ]}
    model = provider()
    model.chat.side_effect = [LlmResponse("end_turn", text="summary"), LlmResponse("end_turn", text="done")]
    loop, context, _ = make_loop(tmp_path, [*history(), call, results], model)
    await loop.run()
    assert context.run.status == "success" and model.chat.call_count == 2
    assert len(list((tmp_path / "outputs").glob("*.txt"))) == 2
    sent = model.chat.call_args.kwargs["messages"]
    retained = next(m["content"] for m in sent if isinstance(m["content"], list) and m["role"] == "user")
    assert [block["tool_use_id"] for block in retained] == ["a", "b"]
    assert retained[1]["is_error"] is True
    assert all("chars omitted" in block["content"] for block in retained)


# 功能：验证手动和自动压缩使用相同候选规则，且通知发生在真实检查点提交之后
# 设计：向两条入口提供相同历史，比较最终消息并在完成订阅者中直接读取磁盘
async def test_manual_matches_auto_and_notifies_after_commit(tmp_path: Path) -> None:
    from kama_claude.core.session.manager import SessionManager
    from kama_claude.core.session.store import SessionStore

    config = CompactionConfig(
        context_window_tokens=8_192, output_reserve_tokens=512,
        summary_max_tokens=256, auto_threshold=0.1, prune_target_ratio=0.05,
    )
    store = SessionStore(tmp_path / "sessions")
    bus = EventBus()
    model = provider("summary")
    manager = SessionManager(store, Mock(), bus, provider=model, compaction=config)
    session = await manager.create("chat")
    for message in history():
        store.append_message(session.id, message["role"], message["content"])
    committed = []

    # 完成通知的订阅者必须已经读到新摘要
    async def verify_commit(event: Any) -> None:
        if event.type == "context.compacted":
            committed.append(store.checkpoints.load(session.id).messages)

    bus.subscribe(verify_commit)
    result = await manager.compact(session.id)
    assert result.compacted and result.saved_tokens > 0
    assert committed == [store.read_messages(session.id)]
    auto_model = provider()
    auto_model.chat.side_effect = [
        LlmResponse("end_turn", text="summary"), LlmResponse("end_turn", text="done"),
    ]
    loop, context, _ = make_loop(tmp_path / "auto", history(), auto_model, config)
    await loop.run()
    assert context.messages[:-1] == committed[0]


# 功能：验证手动压缩保存失败不会发送成功事件，也不会破坏磁盘历史
# 设计：候选生成后阻断唯一检查点写入口，检查真实存储与事件序列
async def test_manual_save_failure_preserves_disk_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kama_claude.core.session.manager import SessionManager
    from kama_claude.core.session.store import SessionStore

    store = SessionStore(tmp_path)
    manager = SessionManager(
        store, Mock(), EventBus(), provider=provider(),
        compaction=CompactionConfig(
            context_window_tokens=8_192, output_reserve_tokens=512,
            summary_max_tokens=256, auto_threshold=0.1, prune_target_ratio=0.05,
        ),
    )
    session = await manager.create("chat")
    for message in history():
        store.append_message(session.id, message["role"], message["content"])
    original = store.read_messages(session.id)
    events = []

    # 记录是否发布了误导用户的成功通知
    async def collect(event: Any) -> None:
        events.append(event.type)

    manager._bus.subscribe(collect)
    monkeypatch.setattr(store.checkpoints, "save", Mock(side_effect=CheckpointError("disk full")))
    with pytest.raises(CheckpointError):
        await manager.compact(session.id)
    assert store.read_messages(session.id) == original
    assert events == ["context.compaction_started"]


# 功能：验证 TUI 展示开始、完成和失败，并拒收其他会话的压缩事件
# 设计：通过真实前端事件分发入口捕获组件内容，不绕过会话过滤
def test_tui_compaction_feedback_is_session_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.tui.app import KamaTuiApp

    app = KamaTuiApp("127.0.0.1", 1)
    app._session_id = "sess-s9"
    content = []
    monkeypatch.setattr(app, "_append", lambda widget: content.append(str(widget.content)))
    base = {"session_id": "sess-s9", "run_id": "r"}
    app._handle_event({"type": "context.compaction_started", **base})
    app._handle_event({
        "type": "context.compacted", "original_tokens": 1000,
        "remaining_tokens": 400, "summary_tokens": 100, **base,
    })
    app._handle_event({
        "type": "context.compaction_failed", "reason": "empty_summary", **base,
    })
    assert len(content) == 3
    assert "正在整理上下文" in content[0]
    assert "1000 → 400" in content[1]
    assert "摘要为空" in content[2]
    app._handle_event({"type": "context.compaction_started", "session_id": "other"})
    assert len(content) == 3


# 功能：验证预算参数的 TOML、环境变量覆盖与非法配置处理
# 设计：在隔离目录加载配置，避免本机全局文件或项目密钥参与测试
def test_compaction_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from kama_claude.core.config import get_config

    monkeypatch.setattr("kama_claude.core.config.load_dotenv", lambda *args, **kwargs: None)
    for name in os.environ:
        if name.startswith("KAMA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[compaction]\ncontext_window_tokens=32768\noutput_reserve_tokens=1024\n"
        "keep_recent_groups=3\nsummary_max_tokens=512\n", encoding="utf-8",
    )
    monkeypatch.setenv("KAMA_CONFIG", str(config_path))
    monkeypatch.setenv("KAMA_COMPACT_WINDOW_TOKENS", "16384")
    config = get_config().compaction
    assert config.auto_threshold == 0.8
    assert config.context_window_tokens == 16384 and config.input_limit == 14336
    assert config.keep_recent_groups == 3 and config.summary_max_tokens == 512
    monkeypatch.setenv("KAMA_COMPACT_THRESHOLD", "0")
    assert get_config().compaction.auto_threshold == 0
    monkeypatch.setenv("KAMA_COMPACT_OUTPUT_TOKENS", "20000")
    with pytest.raises(SystemExit, match="context window"):
        get_config()


@pytest.mark.parametrize("kind", ["anthropic", "deepseek", "openai", "trace"])
# 功能：验证三家适配及 Trace 包装器真正透传指定输出上限
# 设计：替换 SDK 流与内部 Provider，检查最接近 API 的调用参数，避免只测上层接口
async def test_provider_output_limit_is_forwarded(kind: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kama_claude.core.llm.provider import AnthropicProvider, DeepseekProvider, OpenaiProvider
    from kama_claude.core.trace.provider import TracingProvider
    from tests.unit.test_llm_provider import FakeStream, _make_final
    from tests.unit.test_stage_provider_compat import _OpenaiStream

    if kind in ("anthropic", "deepseek"):
        client = Mock()
        client.messages.stream.return_value = FakeStream([], _make_final())
        constructor = AnthropicProvider if kind == "anthropic" else DeepseekProvider
        model = constructor("fake", client=client)
    elif kind == "openai":
        create = AsyncMock(return_value=_OpenaiStream())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        model = OpenaiProvider("fake", client=client)
    else:
        inner = Mock(chat=AsyncMock(return_value=LlmResponse("end_turn", text="ok")))
        trace = Mock()
        model = TracingProvider(inner, trace)
    await model.chat(
        messages=[], tool_schemas=[], bus=EventBus(), run_id="test", max_output_tokens=123,
    )
    if kind in ("anthropic", "deepseek"):
        assert client.messages.stream.call_args.kwargs["max_tokens"] == 123
    elif kind == "openai":
        assert create.call_args.kwargs["max_completion_tokens"] == 123
    else:
        assert inner.chat.call_args.kwargs["max_output_tokens"] == 123
        assert trace.emit.call_args_list[0].args[0].data["max_output_tokens"] == 123
