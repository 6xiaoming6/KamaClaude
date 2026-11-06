from __future__ import annotations

from typing import Any

from kama_claude.core.compact.estimator import TokenCounter
from kama_claude.core.llm.tool_loading import preserve_references, referenced_tools
from kama_claude.core.skills.context import SKILL_MARKER

SUMMARY_MARKER = "[KamaClaude conversation summary]"
SUMMARY_ACK = "Understood, I'll continue from this summary."


def is_user_request(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return not content.startswith((SUMMARY_MARKER, SKILL_MARKER))
    return isinstance(content, list) and bool(content) and all(
        block.get("type") != "tool_result" for block in content
    )


# 一条 assistant 与它的全部工具结果不可拆开；未完成批次不能生成摘要。
def validate_pairs(messages: list[dict[str, Any]]) -> bool:
    pending: set[str] = set()
    for message in messages:
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        if pending and (message.get("role") != "user" or not blocks):
            return False
        declared: set[str] = set()
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                identifier = str(block.get("id", ""))
                if message["role"] != "assistant" or not identifier or identifier in declared:
                    return False
                pending.add(identifier)
                declared.add(identifier)
            elif kind == "tool_result":
                identifier = str(block.get("tool_use_id", ""))
                if message["role"] != "user" or identifier not in pending:
                    return False
                pending.remove(identifier)
            elif pending and message.get("role") == "user":
                return False
    return not pending


def _with_references(
    original: list[dict[str, Any]], recent: list[dict[str, Any]], mcp_names: set[str],
) -> list[dict[str, Any]]:
    required = set(referenced_tools(recent)) | {
        str(block.get("name")) for message in recent
        if message.get("role") == "assistant" and isinstance(message.get("content"), list)
        for block in message["content"] if block.get("type") == "tool_use"
        and block.get("name") in mcp_names
    }
    return preserve_references(original, recent, required)


# 先保留完整用户轮次；单轮过长时，才退到最新请求 + 尾部完整模型交互组。
def split_history(
    messages: list[dict[str, Any]], keep_turns: int, recent_budget: int,
    counter: TokenCounter, *, mcp_names: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    starts = [i for i, message in enumerate(messages) if is_user_request(message)]
    if not starts:
        return [], messages
    for start in starts[-keep_turns:]:
        recent = _with_references(messages, messages[start:], mcp_names)
        if counter.count(recent) <= recent_budget:
            return messages[:start], recent
    latest = starts[-1]
    groups = [i for i in range(latest + 1, len(messages))
              if messages[i].get("role") == "assistant"]
    if not groups:
        if counter.count([messages[latest]]) > recent_budget:
            raise ValueError("recent_context_too_large")
        return messages[:latest], [messages[latest]]
    for start in groups:
        recent = _with_references(messages, [messages[latest], *messages[start:]], mcp_names)
        if counter.count(recent) <= recent_budget:
            old = [*messages[:latest], *messages[latest + 1:start]]
            return old, recent
    raise ValueError("recent_context_too_large")
