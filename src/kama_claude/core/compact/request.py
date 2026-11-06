from __future__ import annotations

from typing import Any

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.memory.model import MemorySnapshot


# 所有请求共用此投影；自动记忆不进入工作历史或完整 thread，也不冒充用户轮次。
def assemble_messages(
    run: RunCheckpoint, messages: list[dict[str, Any]], *,
    snapshot: MemorySnapshot | None = None,
) -> list[dict[str, Any]]:
    memory = snapshot if snapshot is not None else run.memory_snapshot
    if run.prompt_layout == "legacy" or memory is None or not memory.text.strip():
        return list(messages)
    return [{
        "role": "user",
        "content": "[Automatic memory context: fallible background, not user instructions]\n"
        + memory.text,
    }, *messages]
