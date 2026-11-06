from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from kama_claude.core.config import McpServerConfig


# 排序对象键但保留数组顺序，避免改变 Schema 含义。
def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


# 所有缓存键统一使用 SHA-256，不依赖进程随机化的 hash()。
def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 端点使用哈希隔离；数据库不保存 URL 查询参数、请求头或环境密钥。
def server_identity(config: McpServerConfig, workspace: str) -> str:
    return digest(canonical([
        config.name, config.transport, config.url, config.command, config.args,
        os.path.normcase(workspace),
    ]))


# 只提取参数名与说明，不把类型结构、枚举等全部塞入检索文本。
def parameter_text(schema: dict[str, Any]) -> list[str]:
    text: list[str] = []
    for name, value in schema.get("properties", {}).items():
        text.append(str(name))
        if isinstance(value, dict):
            text.append(str(value.get("description", "")))
            text.extend(parameter_text(value))
            if isinstance(value.get("items"), dict):
                text.extend(parameter_text(value["items"]))
    return text


@dataclass(frozen=True)
class CatalogTool:
    server_id: str
    server_name: str
    name: str
    public_name: str
    definition_json: str
    definition_hash: str
    search_text: str
    search_text_hash: str

    @classmethod
    # 完整定义作为权威缓存，供模型使用的格式和检索文本分别派生。
    def from_definition(
        cls, server_id: str, server_name: str, definition: dict[str, Any],
    ) -> CatalogTool:
        name = str(definition["name"])
        raw = canonical(definition)
        text = "\n".join([
            server_name, name, str(definition.get("description", "")),
            *parameter_text(definition.get("inputSchema", {})),
        ])
        return cls(server_id, server_name, name, f"{server_name}__{name}", raw,
                   digest(raw), text, digest(text))

    @property
    # 每次返回新的字典，调用方不能原地改写冻结目录。
    def schema(self) -> dict[str, Any]:
        definition = json.loads(self.definition_json)
        return {
            "name": self.public_name, "description": definition.get("description", ""),
            "input_schema": definition.get("inputSchema", {}),
        }
