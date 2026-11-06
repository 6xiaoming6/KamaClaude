from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

TOOL_RESULT_LIMIT = 8_000
TOOL_RESULT_KEEP = 4_000
_OUTPUT_MARKER = "[kama: tool output truncated; full output: "
ARCHIVED_RESULT_MARKER = "[kama: archived tool result] "


# 按 JSON 内容保守估算 token，中文等非 ASCII 字符不套用字符数除以四
def estimate_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ascii_count = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_count / 4) + len(text) - ascii_count


# 将系统提示、消息和工具定义一起计入本次请求预算
def estimate_request(
    system: str, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]],
) -> int:
    return estimate_tokens({"system": system, "messages": messages, "tools": tool_schemas}) + (
        16 + 8 * len(messages) + 8 * len(tool_schemas)
    )


# 全文落盘成功后才允许把短版本放进上下文，已有归档不覆盖
def _archive_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != text.encode("utf-8"):
            raise OSError(f"archived tool output changed: {path}")
        return
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="",
            dir=path.parent, prefix=".output-", delete=False,
        ) as file:
            temporary = file.name
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


# 根据调用 ID 与全文生成归档位置，重复整理复用相同全文。
def archive_tool_output(tool_id: str, text: str, outputs_dir: Path) -> Path:
    digest = hashlib.sha256((tool_id + "\0" + text).encode("utf-8")).hexdigest()
    path = outputs_dir.resolve() / f"{digest}.txt"
    _archive_output(path, text)
    return path


# 核对归档短记录或旧头尾预览，返回真实全文；原始正文返回 None
def read_archived_output(text: str, tool_id: str) -> tuple[str, Path] | None:
    if text.startswith(ARCHIVED_RESULT_MARKER):
        try:
            record = json.loads(text[len(ARCHIVED_RESULT_MARKER):])
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("call") != tool_id:
            return None
        if not isinstance(record.get("path"), str):
            return None
        path = Path(record["path"])
        if not re.fullmatch(r"[0-9a-f]{64}\.txt", path.name):
            return None
        original = path.read_bytes().decode("utf-8")
        digest = hashlib.sha256((tool_id + "\0" + original).encode("utf-8")).hexdigest()
        if digest != path.stem:
            raise OSError(f"archived tool output changed: {path}")
        return original, path
    pattern = re.escape(_OUTPUT_MARKER) + r"(.+); (\d+) chars omitted\]"
    for match in re.finditer(pattern, text):
        path = Path(match.group(1))
        if not re.fullmatch(r"[0-9a-f]{64}\.txt", path.name):
            continue
        # 引用的全文丢失就明确报错，不把预览再次归档成所谓全文
        original = path.read_bytes().decode("utf-8")
        digest = hashlib.sha256((tool_id + "\0" + original).encode("utf-8")).hexdigest()
        if digest != path.stem:
            continue
        head = text[:match.start()].removesuffix("\n")
        tail = text[match.end():].removeprefix("\n")
        kept = len(head) + len(tail)
        if (
            len(head) == (kept + 1) // 2 and len(tail) == kept // 2
            and head == original[:len(head)]
            and tail == (original[-len(tail):] if tail else "")
            and len(original) - kept == int(match.group(2))
        ):
            return original, path
    return None


# 保留工具输出的头尾；提供输出目录时先归档全文，不改原消息和工具执行状态
def truncate_tool_results(
    messages: list[dict[str, Any]],
    limit: int = TOOL_RESULT_LIMIT,
    keep: int = TOOL_RESULT_KEEP,
    *,
    outputs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    result = []
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            result.append(msg)
            continue
        blocks = []
        for block in content:
            text = block.get("content")
            if (
                block.get("type") == "tool_result" and isinstance(text, str)
                and len(text) > limit
                and read_archived_output(text, str(block.get("tool_use_id", ""))) is None
            ):
                location = "original session history"
                if outputs_dir is not None:
                    path = archive_tool_output(
                        str(block.get("tool_use_id", "")), text, outputs_dir,
                    )
                    location = str(path)
                head = (keep + 1) // 2
                tail = keep // 2
                marker = (
                    f"\n{_OUTPUT_MARKER}{location}; "
                    f"{len(text) - keep} chars omitted]\n"
                )
                block = {
                    **block, "content": text[:head] + marker + (text[-tail:] if tail else ""),
                }
            blocks.append(block)
        result.append({**msg, "content": blocks})
    return result
