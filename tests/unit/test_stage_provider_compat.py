from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.bus.events import LlmUsageEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.provider import OpenaiProvider
from kama_claude.core.llm.types import LlmResponse, UsageStats
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore


# 功能：验证手动压缩按需创建后端、写回摘要，并复用已创建的后端。
# 设计：使用真实会话存储和假模型响应，确认创建会话不要求模型客户端且压缩结果确实落盘。
async def test_manual_compaction_creates_provider_on_demand(tmp_path: Path) -> None:
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=LlmResponse(
                stop_reason="end_turn",
                text="保留项目约定和剩余任务。",
                usage=UsageStats(input_tokens=100, output_tokens=10),
            )
        )
    )
    factory = Mock(return_value=provider)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, Mock(), EventBus(), provider_factory=factory)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "原始对话内容。" * 100)
    factory.assert_not_called()

    result = await manager.compact(session.id, focus="项目约定")

    factory.assert_called_once_with()
    assert result.summary_tokens == 10
    assert store.read_messages(session.id)[0]["content"] == "保留项目约定和剩余任务。"
    assert list(store.session_dir(session.id).glob("thread_*.jsonl.bak"))
    assert "项目约定" in provider.chat.call_args.kwargs["messages"][0]["content"]

    second_session = await manager.create("chat")
    store.append_message(second_session.id, "user", "另一段对话。" * 100)
    await manager.compact(second_session.id)
    factory.assert_called_once_with()


class _OpenaiStream:
    # 进入无需网络连接的模拟流。
    async def __aenter__(self) -> _OpenaiStream:
        return self

    # 退出模拟流。
    async def __aexit__(self, *args: object) -> None:
        return None

    # 提供包含文本、结束原因和用量的单个响应片段。
    def __aiter__(self):
        return self._chunks()

    # 生成可触发 S6 压缩阈值判断的上下文用量。
    async def _chunks(self):
        yield SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=180_000, completion_tokens=10),
            choices=[
                SimpleNamespace(
                    index=0,
                    finish_reason="stop",
                    delta=SimpleNamespace(content="done", tool_calls=None),
                )
            ],
        )


# 功能：验证 OpenAI 响应和用量事件都携带 S6 的上下文占用比例。
# 设计：注入模拟流，避免外部 SDK 和 API 依赖，同时核对动态 system 提示仍然被传递。
async def test_openai_reports_context_usage_and_preserves_system() -> None:
    create = AsyncMock(return_value=_OpenaiStream())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = OpenaiProvider("test-model", client=client)
    bus = EventBus()
    events: list[object] = []

    # 收集真实总线发布的事件以检查前端收到的用量字段。
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    response = await provider.chat(
        messages=[{"role": "user", "content": "hello"}],
        tool_schemas=[],
        bus=bus,
        run_id="run-compat",
        system="global / project / session context",
    )

    assert response.usage is not None
    assert response.usage.context_pct == pytest.approx(0.9)
    usage_event = next(event for event in events if isinstance(event, LlmUsageEvent))
    assert usage_event.context_pct == pytest.approx(0.9)
    assert create.call_args.kwargs["messages"][0] == {
        "role": "system",
        "content": "global / project / session context",
    }
