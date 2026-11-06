from __future__ import annotations

from copy import deepcopy
from typing import Any


# 只识别成功工具结果中的协议块，不把用户文字或工具参数中的同名字段当成授权。
def result_references(block: dict[str, Any]) -> list[str]:
    content = block.get("content")
    if block.get("type") != "tool_result" or block.get("is_error") or not isinstance(content, list):
        return []
    return list(dict.fromkeys(
        item["tool_name"] for item in content
        if isinstance(item, dict) and item.get("type") == "tool_reference"
        and isinstance(item.get("tool_name"), str)
    ))


def referenced_tools(messages: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            names.extend(result_references(block))
    return list(dict.fromkeys(names))


# 仅供预算估算：服务端会将引用展开为对应完整定义；实际请求仍发送引用。
def expand_references(value: Any, schemas: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [expand_references(item, schemas) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "tool_use":
            return value  # 参数是业务数据，不能将其内部字典解释成协议块。
        if value.get("type") == "tool_result" and isinstance(value.get("content"), list):
            content = []
            for item in value["content"]:
                schema = schemas.get(str(item.get("tool_name", "")))
                if item.get("type") == "tool_reference" and schema is not None:
                    content.append({key: data for key, data in schema.items()
                                    if key != "defer_loading"})
                else:
                    content.append(item)
            return {**value, "content": content}
        return {key: expand_references(item, schemas) for key, item in value.items()}
    return value


# 只补近期实际需要的真实发现记录；过时引用随旧历史退出，不重新执行搜索。
def preserve_references(
    original: list[dict[str, Any]], candidate: list[dict[str, Any]],
    required: set[str],
) -> list[dict[str, Any]]:
    missing = required - set(referenced_tools(candidate))
    if not missing:
        return candidate
    calls: dict[str, dict[str, Any]] = {}
    retained: list[dict[str, Any]] = []
    for message in original:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if message["role"] == "assistant" and block.get("type") == "tool_use":
                calls[str(block["id"])] = block
            elif message["role"] == "user" and block.get("type") == "tool_result":
                call = calls.pop(str(block.get("tool_use_id", "")), None)
                names = [name for name in result_references(block) if name in missing]
                if (
                    call is None or call.get("name") != "search_tool"
                    or not names or block.get("is_error")
                ):
                    continue
                retained.extend([
                    {"role": "assistant", "content": [deepcopy(call)]},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": call["id"],
                        "content": [{"type": "tool_reference", "tool_name": name}
                                    for name in names],
                    }]},
                ])
                missing.difference_update(names)
    if missing:
        raise ValueError("missing_tool_reference: " + ", ".join(sorted(missing)))
    # 先恢复必要发现记录，再跟近期调用；调用方负责在前面放摘要。
    return [*retained, *candidate]


# 新 Run 的 MCP 目录可能变化；旧引用不可用时明确转成说明，不向 API 发送悬空引用。
def project_references(
    messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = {str(schema["name"]) for schema in schemas}
    projected = deepcopy(messages)
    for message in projected:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            content = block.get("content")
            if block.get("type") != "tool_result" or not isinstance(content, list):
                continue
            block["content"] = [
                {"type": "text", "text": f"Tool no longer available: {item.get('tool_name', '')}"}
                if item.get("type") == "tool_reference" and item.get("tool_name") not in names
                else item for item in content
            ]
    return projected
