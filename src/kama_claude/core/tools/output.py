from __future__ import annotations

import json
from typing import Any

from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import MAX_OUTPUT_BYTES

MAX_RESULT_CHARS = 8_000
_NOTICE_RESERVE = 512


def fits_result(value: dict[str, Any]) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return (len(text) <= MAX_RESULT_CHARS - _NOTICE_RESERVE
            and len(text.encode("utf-8")) <= MAX_OUTPUT_BYTES)


# 先移除完整记录，再序列化；独立限长的 JSON 不能被通用头尾预览切断。
def json_result(value: dict[str, Any], records_key: str | None = None) -> ToolResult:
    if records_key is not None:
        records = value[records_key]
        while records and not fits_result(value):
            records.pop()
            value.update(truncated=True, reason="output_limit")
    if not fits_result(value):
        raise ToolError("result metadata exceeds output limit; narrow the request",
                        "resource_limit")
    return ToolResult(content=json.dumps(value, ensure_ascii=False), preserve_in_context=True)


# 无进展提醒仍需送给模型，但不能把一行文字直接拼在 JSON 后面。
def append_tool_warning(result: ToolResult, warning: str) -> None:
    if result.preserve_in_context:
        try:
            value = json.loads(result.content)
        except ValueError:
            value = None
        if isinstance(value, dict):
            value["notice"] = warning[:400]
            text = json.dumps(value, ensure_ascii=False)
            if len(text) <= MAX_RESULT_CHARS and len(text.encode("utf-8")) <= MAX_OUTPUT_BYTES:
                result.content = text
                return
    result.content += "\n" + warning
