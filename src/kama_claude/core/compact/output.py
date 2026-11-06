from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from kama_claude.core.compact.budget import archive_tool_output
from kama_claude.core.config import CompactionConfig
from kama_claude.core.tools.base import ToolResult


class ToolOutputPreparer:
    # 仅在工具结果入口处理一次；不扫描或重新裁剪已进入工作历史的旧结果。
    def __init__(self, config: CompactionConfig, run_path: Path | None) -> None:
        self._config = config
        self._outputs = run_path / "outputs" if run_path is not None else None

    def prepare(self, tool_id: str, result: ToolResult) -> str | list[dict[str, object]]:
        content = result.message_content()
        if result.preserve_in_context:
            return content
        if isinstance(content, str):
            return self._preview(tool_id, content)
        # 结构化引用不裁剪；这里只缩短文本正文。
        projected = deepcopy(content)
        text_blocks = [block for block in projected if block.get("type") == "text"]
        length = sum(len(str(block.get("text", ""))) for block in text_blocks)
        if length <= self._config.tool_result_limit:
            return projected
        text = "\n".join(str(block.get("text", "")) for block in text_blocks)
        preview = self._preview(tool_id, text)
        return [{"type": "text", "text": preview}, *[
            block for block in projected if block.get("type") != "text"
        ]]

    def _preview(self, tool_id: str, text: str) -> str:
        if len(text) <= self._config.tool_result_limit:
            return text
        if self._outputs is None:
            raise OSError("tool output archive directory unavailable")
        path = archive_tool_output(tool_id, text, self._outputs)
        keep = self._config.tool_result_keep
        head, tail = (keep + 1) // 2, keep // 2
        marker = (f"\n[kama: tool output truncated; full output: {path}; "
                  f"{len(text) - keep} chars omitted]\n")
        return text[:head] + marker + (text[-tail:] if tail else "")
