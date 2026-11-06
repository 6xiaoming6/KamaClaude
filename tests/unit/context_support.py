from __future__ import annotations

from typing import Any


# 构造完整工具批次，保留用户约束、模型决定及 thinking 以检查保护范围
def tool_history(
    count: int = 8, *, repeated: bool = True, name: str = "read_file", size: int = 1_600,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Only inspect files. Do not modify or commit."},
    ]
    for index in range(count):
        messages.extend([
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": f"opaque-{index}", "signature": "signature"},
                {"type": "text", "text": f"decision {index} must remain"},
                {"type": "tool_use", "id": f"call-{index}", "name": name, "input": {"path": "same"}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": f"call-{index}",
                "content": ("report" if repeated else f"report-{index}") + "中" * size,
            }]},
        ])
    return messages


# 只提取模型可见的工具结果，便于检查 ID 与正文
def results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        block for message in messages if message["role"] == "user"
        and isinstance(message["content"], list)
        for block in message["content"] if block.get("type") == "tool_result"
    ]
