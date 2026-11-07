from __future__ import annotations

import asyncio
import copy
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.compact.compactor import SUMMARY_ACK, SUMMARY_MARKER, Compactor
from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.config import CompactionConfig
from kama_claude.core.llm.types import LlmResponse, UsageStats


# 构造足够长的旧历史，使摘要收益和近期原文都可以独立检查
def history(count: int = 5) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index in range(count):
        messages.extend([
            {"role": "user", "content": f"request {index}"},
            {"role": "assistant", "content": f"answer {index}: " + "x" * 2_400},
        ])
    return messages


# 注入不联网的摘要模型，保留调用参数供预算与输出上限断言
def provider(text: str = "Goal: continue. Completed: old work. TODO: finish.") -> Any:
    return Mock(chat=AsyncMock(return_value=LlmResponse(
        "end_turn", text=text, usage=UsageStats(100, 20),
    )))


# 功能：验证候选压缩保留近期原文、限制摘要输出且不修改输入历史
# 设计：用不同编号的长回复区分新旧内容，深拷贝检查纯候选生成的边界
async def test_candidate_preserves_recent_and_input() -> None:
    messages = history()
    original = copy.deepcopy(messages)
    model = provider()
    result = await Compactor().compact_messages(messages, model, focus="only edit tests")
    assert result.compacted
    assert result.messages[0]["content"].startswith(SUMMARY_MARKER)
    assert result.messages[-3:] == original[-3:]
    assert result.remaining_token_estimate < result.original_token_estimate * 0.9
    assert messages == original
    args = model.chat.call_args.kwargs
    assert args["tool_schemas"] == []
    config = CompactionConfig()
    assert args["max_output_tokens"] == config.summary_max_tokens
    assert args["messages"][:-1] == original
    assert "only edit tests" in args["messages"][-1]["content"]
    target = config.summary_target_for(estimate_bytes(original[:4]))
    assert f"Aim for about {target} tokens or fewer" in args["messages"][-1]["content"]
    assert "stop naturally" in args["messages"][-1]["content"]


# 功能：验证当前用户请求在很早的位置时仍保留原文，且不会重复插入
# 设计：一个用户目标后连续多个模型步骤，覆盖长任务中目标已离开近期窗口的情况
async def test_original_current_request_is_pinned_once() -> None:
    goal = {"role": "user", "content": "Only edit loop.py; do not commit."}
    messages = [goal, *[m for m in history() if m["role"] == "assistant"]]
    result = await Compactor(CompactionConfig(recent_history_ratio=(1_500) / (1_048_576))).compact_messages(messages, provider())
    assert result.compacted
    assert result.messages.count(goal) == 1
    assert result.messages[2] == goal
    assert result.messages[-2:] == messages[-2:]


# 功能：验证多工具调用、thinking 和配对结果在近期原文中完整保留
# 设计：同一回复两个工具结果分两条消息返回，避免只按消息数量切割而拆开批次
async def test_recent_multi_tool_batch_stays_whole() -> None:
    call = {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "opaque", "signature": "sig"},
        {"type": "tool_use", "id": "a", "name": "read_file", "input": {}},
        {"type": "tool_use", "id": "b", "name": "read_file", "input": {}},
    ]}
    results = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": identifier, "content": "done", "is_error": True},
    ]} for identifier in ("a", "b")]
    messages = [*history(), call, *results]
    result = await Compactor().compact_messages(messages, provider())
    assert result.compacted
    assert result.messages[-3:] == [call, *results]


# 功能：验证再次压缩更新同一份摘要，而不是堆叠摘要消息对
# 设计：第一次压缩后追加新旧交互，检查第二次请求包含前代摘要且结果只有一个摘要标记
async def test_repeated_compaction_replaces_previous_summary() -> None:
    first = await Compactor().compact_messages(history(), provider("first summary"))
    model = provider("updated summary")
    second = await Compactor().compact_messages([*first.messages, *history(4)], model)
    assert second.compacted
    assert sum(m.get("content") == SUMMARY_ACK for m in second.messages) == 1
    assert sum(str(m["content"]).startswith(SUMMARY_MARKER) for m in second.messages) == 1
    assert "first summary" in model.chat.call_args.kwargs["messages"][0]["content"]


@pytest.mark.parametrize(("response", "reason"), [
    (LlmResponse("end_turn", text=""), "empty_summary"),
    (LlmResponse("max_tokens", text="partial"), "summary_incomplete"),
    (LlmResponse("end_turn", text="中" * 3_000), "summary_too_large"),
    (LlmResponse("end_turn", text="x" * 12_000), "summary_too_large"),
])
# 功能：验证空摘要、未完成回复和超长摘要均不会替换原历史
# 设计：使用不同停止原因及中英文长度，排除仅看非空文本就接受摘要的问题
async def test_invalid_summary_preserves_history(response: LlmResponse, reason: str) -> None:
    messages = history()
    original = copy.deepcopy(messages)
    model = Mock(chat=AsyncMock(return_value=response))
    result = await Compactor(CompactionConfig(summary_max_ratio=(2_048) / (1_048_576), summary_target_ratio=(2_048) / (2 * (1_048_576)))).compact_messages(
        messages, model,
    )
    assert not result.compacted and result.reason == reason
    assert result.messages == original and messages == original


async def test_summary_can_exceed_soft_target_when_within_api_limit() -> None:
    model = provider()
    model.chat.return_value.usage = UsageStats(100, 2_000)
    result = await Compactor().compact_messages(history(), model)
    assert result.compacted and result.summary_tokens == 2_000


@pytest.mark.parametrize("limit", [512, 2_048, 8_192])
async def test_summary_target_leaves_room_for_completion(limit: int) -> None:
    model = provider()
    config = CompactionConfig(summary_max_ratio=limit / 1_048_576,
                              summary_target_ratio=limit / (2 * 1_048_576))
    await Compactor(config).compact_messages(history(), model)
    args = model.chat.call_args.kwargs
    assert args["max_output_tokens"] == limit
    target = config.summary_target_for(estimate_bytes(history()[:4]))
    assert target < limit
    assert f"Aim for about {target} tokens or fewer" in args["messages"][-1]["content"]


# 功能：验证没有节省至少百分之十时，不接受更长或收益很小的摘要
# 设计：放宽摘要输出上限，让验证确实进入整体缩减判断而不是先被长度限制挡住
async def test_insufficient_reduction_preserves_history() -> None:
    config = CompactionConfig(summary_max_ratio=(8_192) / (1_048_576), summary_target_ratio=(8_192) / (2 * (1_048_576)))
    result = await Compactor(config).compact_messages(history(), provider("x" * 10_000))
    assert not result.compacted and result.reason == "insufficient_reduction"


@pytest.mark.parametrize("failure", [RuntimeError("offline"), CheckpointError("disk")])
# 功能：验证普通摘要异常保留原历史，存储异常仍直接传播
# 设计：对相同入口注入两类异常，避免宽泛捕获掩盖必须停止的检查点错误
async def test_summary_failure_boundaries(failure: Exception) -> None:
    messages = history()
    model = Mock(chat=AsyncMock(side_effect=failure))
    if isinstance(failure, CheckpointError):
        with pytest.raises(CheckpointError):
            await Compactor().compact_messages(messages, model)
    else:
        result = await Compactor().compact_messages(messages, model)
        assert not result.compacted and result.reason == "summary_request_failed"
        assert result.messages == messages


# 功能：验证取消摘要会继续向上传播，不能当作普通压缩失败
# 设计：假模型抛出 CancelledError，检查当前任务的取消语义未被吞掉
async def test_compaction_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await Compactor().compact_messages(
            history(), Mock(chat=AsyncMock(side_effect=asyncio.CancelledError())),
        )


# 功能：验证未完成工具批次禁止压缩，而且不会发起摘要请求
# 设计：同一批两个工具只返回一个结果，覆盖部分配对窗口
async def test_unfinished_tool_batch_is_rejected() -> None:
    messages = [*history(), {"role": "assistant", "content": [
        {"type": "tool_use", "id": "a", "name": "read", "input": {}},
        {"type": "tool_use", "id": "b", "name": "read", "input": {}},
    ]}, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
    ]}]
    model = provider()
    result = await Compactor().compact_messages(messages, model)
    assert result.reason == "incomplete_tool_batch"
    model.chat.assert_not_called()


# 功能：验证最近两组过大时减少为一组，同时仍完整保留最新一组
# 设计：近期两组各含大量中文，让按字符除四的错误估算无法掩盖额度超限
async def test_recent_groups_shrink_without_splitting() -> None:
    messages = [*history(), {"role": "assistant", "content": "甲" * 6_000},
                {"role": "assistant", "content": "乙" * 6_000}]
    result = await Compactor(CompactionConfig(recent_history_ratio=(6_000) / (1_048_576))).compact_messages(messages, provider())
    assert result.compacted and result.messages[-1] == messages[-1]
    assert messages[-2] not in result.messages


# 功能：验证必留原文和摘要请求也有预算，不会无限调用模型尝试压缩
# 设计：分别让近期单组及旧历史超限，断言两个分支都不调用模型
async def test_uncompressible_inputs_do_not_call_model() -> None:
    model = provider()
    result = await Compactor(CompactionConfig(recent_history_ratio=16_384 / 1_048_576)).compact_messages(
        [*history(), {"role": "assistant", "content": "中" * 24_000}], model,
    )
    assert result.reason == "recent_context_too_large"
    config = CompactionConfig(context_window_tokens=4_096, output_reserve_ratio=(512) / (4_096))
    result = await Compactor(config).compact_messages(history(12), model)
    assert result.reason == "summary_input_too_large"
    model.chat.assert_not_called()


# 功能：验证系统提示和工具定义过大时，不提交仅消息历史缩小的假成功结果
# 设计：摘要本身可生成，但加入工具 Schema 后候选仍超过硬预算
async def test_candidate_budget_includes_system_and_tools() -> None:
    result = await Compactor(CompactionConfig(context_window_tokens=65_536)).compact_messages(
        history(), provider(), system="s" * 100,
        tool_schemas=[{"description": "中" * 100_000}],
    )
    assert not result.compacted and result.reason == "required_context_too_large"
    assert not result.llm_used
    assert result.original_token_estimate > estimate_bytes(history())
