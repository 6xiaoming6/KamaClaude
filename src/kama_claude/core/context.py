from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.memory.context import compose_system
from kama_claude.core.memory.model import MemorySnapshot


@dataclass
class ExecutionContext:
    # 所有运行字段共用这一份 run，messages 只保存模型工作历史
    run: RunCheckpoint
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 子 Agent 角色可覆盖默认提示；Skill 正文独立放在工作历史中。
    system_prompt_override: str | None = None

    # 复制输入历史，新会话则以当前 Run 目标建立第一条消息
    def __post_init__(self) -> None:
        self.messages = [dict(message) for message in self.messages]
        if not self.messages:
            self.messages.append({"role": "user", "content": self.run.goal})

    # 返回当前 run 的 system prompt；有 override 时跳过 base，直接注入记忆层
    def system_prompt(self, base: str) -> str:
        if self.run.system_prompt:
            return self.run.system_prompt
        prompt = self.system_prompt_override if self.system_prompt_override else base
        return compose_system(prompt, self.run.memory_snapshot or MemorySnapshot())

    # 将 LLM 响应的 content blocks 追加为 assistant 消息
    def add_assistant_message(self, content: list[Any]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(
        self, tool_use_id: str, content: str | list[dict[str, object]], is_error: bool = False
    ) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        # 将一次回复的多个工具调用结果放到一个数组里面，他们在一条message里面
        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})
        return block

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.run.status != "running"

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.run.status = "success"
        self.run.phase = "finished"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        self.run.status = "failed"
        self.run.reason = reason
        self.run.phase = "finished"

    # 记录中断原因并保留原阶段，供恢复时从该位置继续
    def mark_interrupted(self, reason: str) -> None:
        self.run.status = "interrupted"
        self.run.reason = reason
