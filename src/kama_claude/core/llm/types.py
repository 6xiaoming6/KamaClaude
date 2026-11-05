from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UsageStats:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    context_pct: float = 0.0
    # Provider 显式归一化；测试替身默认把 input_tokens 当作总输入
    total_input_tokens: int | None = None

    @property
    # 不重复累加已经包含在总输入中的缓存命中
    def total_input(self) -> int:
        return self.input_tokens if self.total_input_tokens is None else self.total_input_tokens


@dataclass
class ToolCallBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass
class LlmResponse:
    stop_reason: str  # "end_turn" | "tool_use"
    tool_calls: list[ToolCallBlock] = field(default_factory=list)
    text: str = ""
    usage: UsageStats | None = None
    # thinking blocks from extended thinking — must be preserved verbatim in conversation history
    thinking_blocks: list[dict[str, object]] = field(default_factory=list)

    # 锚点和 Loop 共用相同消息格式，避免上次输出被重复估算或误判为历史改写。
    def content_blocks(self) -> list[dict[str, object]]:
        blocks = list(self.thinking_blocks)
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for call in self.tool_calls:
            blocks.append({
                "type": "tool_use", "id": call.id, "name": call.name, "input": call.input,
            })
        return blocks
