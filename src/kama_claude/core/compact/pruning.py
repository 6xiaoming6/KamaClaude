from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from kama_claude.core.compact.budget import (
    ARCHIVED_RESULT_MARKER,
    archive_tool_output,
    read_archived_output,
)
from kama_claude.core.compact.estimator import TokenCounter

_READ_ONLY_TOOLS = {"read_file", "list_dir", "task_get", "task_list"}


# 按消息顺序关联调用与结果，调用 ID 在不同批次重用时也不会串联
def _tool_results(
    messages: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    pending: dict[str, dict[str, Any]] = {}
    records = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if message["role"] == "assistant" and block.get("type") == "tool_use":
                pending[str(block["id"])] = block
            elif message["role"] == "user" and block.get("type") == "tool_result":
                call = pending.pop(str(block.get("tool_use_id", "")), None)
                if call is not None:
                    records.append((index, call, block))
    return records


# 短记录只在正文中表达，归档可独立读取，不依赖另一条结果还在上下文
def _short_record(
    call: dict[str, Any], path: Path, duplicate_of: str = "",
) -> str:
    record = {"tool": call["name"], "call": call["id"], "path": str(path)}
    if duplicate_of:
        record["duplicate_of"] = duplicate_of
    return ARCHIVED_RESULT_MARKER + json.dumps(record, ensure_ascii=False, separators=(",", ":"))


# 先确认短记录确实更短，再写入全文；传入的只是未提交候选
def _replace_result(
    call: dict[str, Any], block: dict[str, Any], original: str,
    archived: Path | None, outputs_dir: Path, duplicate_of: str = "",
    *, counter: TokenCounter | None = None,
) -> bool:
    text = block["content"]
    if text.startswith(ARCHIVED_RESULT_MARKER):
        return False
    digest = hashlib.sha256((str(call["id"]) + "\0" + original).encode("utf-8")).hexdigest()
    path = archived or (outputs_dir.resolve() / f"{digest}.txt")
    replacement = _short_record(call, path, duplicate_of)
    measure = counter or TokenCounter()
    if measure.count(replacement) >= measure.count(text):
        return False
    if archived is None:
        archive_tool_output(str(call["id"]), original, outputs_dir)
    block["content"] = replacement
    return True


# 只缩减旧只读调用的完全相同结果，不影响实际执行，也不修改受保护消息
def deduplicate_results(
    messages: list[dict[str, Any]], outputs_dir: Path, protected_start: int,
    system: str, tool_schemas: list[dict[str, Any]], target_tokens: float,
    *, counter: TokenCounter | None = None,
) -> int:
    seen: dict[tuple[str, str, str], tuple[int, str]] = {}
    changes = 0
    measure = counter or TokenCounter()
    for index, call, block in reversed(_tool_results(messages)):
        if measure.request(system, messages, tool_schemas) <= target_tokens:
            break
        text = block.get("content")
        if (
            call["name"] not in _READ_ONLY_TOOLS or block.get("is_error")
            or not isinstance(text, str)
        ):
            continue
        archived = read_archived_output(text, str(call["id"]))
        original = archived[0] if archived else text
        key = (
            str(call["name"]),
            json.dumps(call.get("input", {}), sort_keys=True, ensure_ascii=False),
            hashlib.sha256(original.encode("utf-8")).hexdigest(),
        )
        occurrence = (index, str(call["id"]))
        latest = seen.setdefault(key, occurrence)
        if index >= protected_start or latest == occurrence:
            continue
        changes += _replace_result(
            call, block, original, archived[1] if archived else None,
            outputs_dir, latest[1], counter=measure,
        )
    return changes


# 从最旧的成功结果开始归档遮蔽，调用参数、错误结果和近期内容都保留
def prune_results(
    messages: list[dict[str, Any]], outputs_dir: Path, protected_start: int,
    system: str, tool_schemas: list[dict[str, Any]], target_tokens: float,
    *, counter: TokenCounter | None = None,
) -> int:
    changes = 0
    measure = counter or TokenCounter()
    for index, call, block in _tool_results(messages):
        if measure.request(system, messages, tool_schemas) <= target_tokens:
            break
        text = block.get("content")
        if (
            index >= protected_start or block.get("is_error")
            or not isinstance(text, str) or text.startswith(ARCHIVED_RESULT_MARKER)
        ):
            continue
        archived = read_archived_output(text, str(call["id"]))
        changes += _replace_result(
            call, block, archived[0] if archived else text,
            archived[1] if archived else None, outputs_dir, counter=measure,
        )
    return changes
