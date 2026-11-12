from __future__ import annotations

from typing import Any

from kama_claude.core.skills.model import SkillEntry, SkillSnapshot

SKILL_MARKER = "[KamaClaude skill instructions: "


# 目录只列名称和简短说明；作为冻结提示的一部分，正文仍需按名称加载。
def render_catalog(entries: list[SkillEntry]) -> str:
    if not entries:
        return ""
    listing = "\n".join(
        f"- {entry.name}: {' '.join(entry.description.split())}" for entry in entries
    )
    return (
        "\n\n## Available skills\n"
        "When the task matches a skill below, first call load_skill with its name to read "
        "the full workflow before doing the relevant work. Load only when needed; "
        "one skill per Run. Skills do not grant tools or permissions.\n"
        + listing
    )


def render_skill(snapshot: SkillSnapshot) -> str:
    return (
        f"{SKILL_MARKER}{snapshot.name}; sha256={snapshot.digest}]\n"
        f"Resource directory: {snapshot.directory}\n"
        "Use this workflow for the current user task; it does not grant tool permissions.\n\n"
        + snapshot.body
    )


# 先找正文是否完整存在；工具结果或显式上下文都可复用，不只检查工具名称。
def retain_skill(
    messages: list[dict[str, Any]], snapshot: SkillSnapshot | None,
) -> list[dict[str, Any]]:
    if snapshot is None:
        return messages
    text = render_skill(snapshot)
    for message in messages:
        content = message.get("content")
        if content == text:
            return messages
        if message.get("role") == "user" and isinstance(content, list):
            if any(block.get("type") == "tool_result" and not block.get("is_error")
                   and block.get("content") == text for block in content):
                return messages
    # 摘要 + 确认之后、近期原文之前补回，绝不伪造工具调用或写入 thread。
    index = 2 if messages and str(messages[0].get("content", "")).startswith(
        "[KamaClaude conversation summary]"
    ) else 0
    return [*messages[:index], {"role": "user", "content": text}, *messages[index:]]
